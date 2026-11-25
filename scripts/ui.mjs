// Browser test of the page script (inline editing, keys, filter, guards, import):
// what pytest cannot see. Dev/CI only.
//
//   node scripts/ui.mjs BASE
//
// BASE is `tmodel serve` on a fresh copy of examples/ (tests/test_ui.py starts
// one), or a static browser build seeded with examples (it waits for the
// service worker to boot). It edits the fictional greenhouse model. Each check
// prints ok/FAIL; the exit status is 1 if any failed or the page threw.
import { mkdtempSync, writeFileSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";
const { chromium, devices } = createRequire(import.meta.url)("playwright");

const BASE = process.argv[2];
if (!BASE) { console.error("usage: node scripts/ui.mjs BASE"); process.exit(2); }
const failures = [];
const pageErrors = [];

async function check(name, fn) {
  try { await fn(); console.log(`ok    ${name}`); }
  catch (e) { failures.push(name); console.log(`FAIL  ${name}\n      ${String(e.message || e).split("\n")[0]}`); }
}
function assert(cond, msg) { if (!cond) throw new Error(msg); }

const browser = await chromium.launch();
async function open(ctxOpts = { viewport: { width: 1400, height: 900 } }) {
  const ctx = await browser.newContext(ctxOpts);
  const page = await ctx.newPage();
  page.on("pageerror", (e) => pageErrors.push(String(e)));
  page.dialogs = [];
  page.on("dialog", (d) => { page.dialogs.push(d.type()); d.type() === "beforeunload" ? d.accept() : d.dismiss(); });
  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  await page.waitForSelector("header .brand", { timeout: 180000 }); // browser build: Pyodide boot
  return page;
}
const go = (page, path) => page.goto(BASE + path, { waitUntil: "networkidle" });
// What has focus, as "tag[name]" (plus button text), and whether it is inside the editing row.
const focused = (page) => page.evaluate(() => {
  const el = document.activeElement;
  return {
    tag: el.tagName.toLowerCase(), type: el.type || "", name: el.name || "",
    text: (el.textContent || "").trim(), inRow: !!el.closest("tr.editing"), hidden: el.type === "hidden",
  };
});
async function editFirstRow(page, path) {
  await go(page, path);
  await page.locator("table.rows tr[data-row]").first().locator("td").nth(1).click();
  await page.waitForSelector("tr.editing");
}

let page = await open();

await check("report review agenda links damage, goals, and threat detail without editing rows", async () => {
  await go(page, "/m/greenhouse/report");
  const rows = page.locator(".review-table tbody tr");
  assert(await rows.count() > 0, "damage tables contain threats");
  await rows.first().locator("td").nth(1).click();
  assert(await page.locator("tr.editing").count() === 0, "report cells are read-only");
  const threat = rows.first().locator("a.id");
  const target = (await threat.getAttribute("href")).split("#")[1];
  await threat.click();
  assert(page.url().endsWith("#" + target), "threat link has stable anchor");
  assert(await page.locator("article:target").count() === 1, "linked detail exists");
  await page.locator('.review-nav a[href="#goal-review"]').click();
  assert(await page.locator("#goal-review:target").count() === 1, "agenda jumps to goals");
  const mappingLink = page.locator('.review-table a[href^="#goal-"]').first();
  const goalTarget = await mappingLink.getAttribute("href");
  await mappingLink.click();
  assert(await page.locator(goalTarget + ":target").count() === 1, "goal mapping exists");
  await go(page, "/m/greenhouse/report?impact=Moderate&feasibility=Medium");
  await page.locator('.review-table a.id').first().click();
  assert(!page.url().includes("impact="), "cross-links clear the detail filter");
  assert(await page.locator("article:target").count() === 1, "filtered review still reaches detail");
});

await check("domain parent selector saves nesting and rejects cycles inline", async () => {
  await go(page, "/m/greenhouse/domains");
  const ids = await page.locator("table.rows tr[data-row] td.id").allTextContents();
  const child = ids[0], parent = ids[1];
  assert(child && parent, "two domains available");
  async function edit(id) {
    await go(page, "/m/greenhouse/domains");
    await page.locator(`tr[data-row$="/${id}/row"] td`).nth(1).click();
    await page.waitForSelector("tr.editing select[name=parent]");
  }
  await edit(child);
  await page.locator("tr.editing select[name=parent]").selectOption(parent);
  await page.locator("tr.editing button:has-text('Save')").click();
  await page.waitForSelector("tr.editing", { state: "detached" });
  assert(await page.locator(`tr[data-row$="/${child}/row"] a[href$="/domains/${parent}"]`).count() === 1,
    "parent appears as linked table cell");
  await edit(parent);
  await page.locator("tr.editing select[name=parent]").selectOption(child);
  await page.locator("tr.editing button:has-text('Save')").click();
  await page.waitForSelector("tr.editing .row-errors");
  assert((await page.locator(".row-errors").textContent()).includes("parent cycle"), "cycle error");
  await page.keyboard.press("Escape");
  await edit(child);
  await page.locator("tr.editing select[name=parent]").selectOption("");
  await page.locator("tr.editing button:has-text('Save')").click();
  await page.waitForSelector("tr.editing", { state: "detached" });
});

await check("filter as you type hides rows and keeps the query in the URL", async () => {
  await go(page, "/m/greenhouse/threats");
  const total = await page.locator("table.rows tr[data-row]").count();
  await page.fill("[data-filter]", "panel");
  const shown = await page.locator("table.rows tr[data-row]:visible").count();
  assert(shown > 0 && shown < total, `${shown} of ${total} rows visible`);
  assert(page.url().includes("q=panel"), `url ${page.url()}`);
  await page.fill("[data-filter]", "");
  assert((await page.locator("table.rows tr[data-row]:visible").count()) === total, "clearing shows all rows");
});

await check("filtering hides section headers with no rows left", async () => {
  await go(page, "/m/greenhouse/threats");  // grouped by damage by default
  const groups = await page.locator("table.rows tr[data-group]").count();
  const id = await page.locator("table.rows tr[data-row] td.id").first().textContent();
  await page.fill("[data-filter]", id.trim());
  const shown = await page.locator("table.rows tr[data-group]:visible").count();
  assert(groups > 1 && shown === 1, `${shown} of ${groups} sections visible`);
  await page.fill("[data-filter]", "");
  assert((await page.locator("table.rows tr[data-group]:visible").count()) === groups, "clearing shows all sections");
});

await check("editing a row focuses its first field (never a hidden input)", async () => {
  await editFirstRow(page, "/m/greenhouse/threats");
  const f = await focused(page);
  assert(f.inRow && !f.hidden && ["input", "select", "textarea"].includes(f.tag), JSON.stringify(f));
});

await check("Tab walks every field of the row, then Save", async () => {
  const fields = await page.locator("tr.editing").evaluate((tr) =>
    [...tr.querySelectorAll("input, select, textarea")].filter((e) => e.type !== "hidden" && !e.readOnly).length);
  let n = 1, f = await focused(page);
  for (;;) {
    await page.keyboard.press("Tab");
    f = await focused(page);
    if (!f.inRow || f.tag === "button" || f.tag === "a") break;
    n++;
  }
  assert(f.inRow && f.tag === "button" && f.text === "Save",
    `after ${n} field(s) Tab reached ${f.tag} "${f.text}"${f.inRow ? "" : " outside the row"}, not Save`);
  assert(n === fields, `${n} fields before Save, row has ${fields}`);
  await page.keyboard.press("Escape");
});

await check("Ctrl+Enter saves the row and a toast reports it", async () => {
  await editFirstRow(page, "/m/greenhouse/threats");
  await page.locator("tr.editing select[name=treatment]").selectOption("Retain");
  await page.locator("tr.editing select[name=treatment]").press("Control+Enter");
  await page.waitForSelector(".toast", { timeout: 20000 });
  const text = await page.locator(".toast").textContent();
  assert(/^Saved TS_\w+ · risk \d/.test(text), `toast "${text}"`);
  assert((await page.locator("tr.editing").count()) === 0, "row left edit mode");
});

await check("Escape cancels an edit without saving", async () => {
  await editFirstRow(page, "/m/greenhouse/goals");
  await page.locator("tr.editing input[name=name]").fill("SHOULD NOT SAVE");
  await page.keyboard.press("Escape");
  await page.waitForSelector("tr.editing", { state: "detached" });
  await go(page, "/m/greenhouse/goals");
  assert(!(await page.content()).includes("SHOULD NOT SAVE"), "cancelled text was saved");
});

await check("n opens a new row with the next ID; Save & next chains another", async () => {
  await go(page, "/m/greenhouse/goals");
  const before = await page.locator("table.rows tr[data-row]:not([data-new])").count();
  await page.locator("body").click({ position: { x: 2, y: 2 } });
  await page.keyboard.press("n");
  await page.waitForSelector("tr[data-new]");
  const id = await page.locator("tr[data-new] input[name=id]").inputValue();
  assert(/^CG_\d+$/.test(id), `suggested id "${id}"`);
  const f = await focused(page);
  assert(f.name === "id", `focus on ${JSON.stringify(f)}`);
  await page.locator("tr[data-new] input[name=name]").fill("Added by ui.mjs");
  await page.click("tr[data-new] button[name=next]");
  await page.waitForFunction((n) => document.querySelectorAll("table.rows tr[data-row]:not([data-new])").length === n + 1, before);
  await page.waitForSelector("tr[data-new]");
  const id2 = await page.locator("tr[data-new] input[name=id]").inputValue();
  assert(id2 !== id, `second new row reuses ${id}`);
  await page.keyboard.press("Escape");
  assert((await page.locator("tr[data-new]").count()) === 0, "Escape removes the blank row");
});

await check("a failed inline save keeps the row open with the error", async () => {
  await go(page, "/m/greenhouse/goals");
  await page.click("a[data-add]");
  await page.waitForSelector("tr[data-new]");
  await page.locator("tr[data-new] input[name=id]").fill("CG_01"); // exists already
  await page.locator("tr[data-new] input[name=name]").fill("dup");
  await page.click("tr[data-new] button:has-text('Save')");
  await page.waitForSelector("tr.editing .row-errors");
  assert((await page.locator(".row-errors").textContent()).includes("already exists"), "error text");
  await page.keyboard.press("Escape");
});

await check("server errors keep the edited row and warn that saving may have happened", async () => {
  await editFirstRow(page, "/m/greenhouse/goals");
  // Override fetch rather than routing: service-worker requests aren't interceptable by page.route.
  await page.evaluate(() => {
    window.originalFetch = window.fetch;
    window.fetch = async () => new Response("The model was saved, but its git commit failed.", { status: 503 });
    window.saveWarning = "";
    window.originalAlert = window.alert;
    window.alert = (text) => { window.saveWarning = text; };
  });
  try {
    await page.locator("tr.editing input[name=name]").fill("unsure whether saved");
    await page.click("tr.editing button:has-text('Save')");
    await page.waitForFunction(() => window.saveWarning.includes("git commit failed"));
    assert((await page.locator("tr.editing").count()) === 1, "edit lost on server failure");
    assert((await page.evaluate(() => window.saveWarning)).includes("Reload to check"), "retry guidance missing");
  } finally {
    await page.evaluate(() => { window.fetch = window.originalFetch; window.alert = window.originalAlert; });
  }
  await page.keyboard.press("Escape");
});

await check("leaving a form with unsaved changes asks first", async () => {
  await go(page, "/m/greenhouse/threats/TS_02");
  page.dialogs.length = 0;
  await page.locator("#f-description").fill("changed, not saved");
  await go(page, "/m/greenhouse/threats");
  assert(page.dialogs.includes("beforeunload"), `dialogs: ${page.dialogs}`);
  page.dialogs.length = 0;
  await go(page, "/m/greenhouse/threats/TS_02");
  await go(page, "/m/greenhouse/threats"); // untouched: no prompt
  assert(page.dialogs.length === 0, `unexpected dialogs: ${page.dialogs}`);
});

await check("surface chips append @SURF to the step line; they are not Tab stops", async () => {
  await go(page, "/m/greenhouse/threats/TS_02");
  const ta = page.locator("#f-attack_path");
  await ta.click();
  await ta.press("Control+Home");
  const chip = page.locator("button[data-via]").first();
  const via = await chip.getAttribute("data-via");
  await chip.click();
  const first = (await ta.inputValue()).split("\n")[0];
  assert(first.endsWith(`  @${via}`) && first.split("@").length === 2, `first line "${first}"`);
  assert((await chip.getAttribute("tabindex")) === "-1", "chip is a Tab stop");
  page.dialogs.length = 0;
  await go(page, "/m/greenhouse/threats"); // the form is dirty now: accept the prompt
});

await check("header box jumps to an ID", async () => {
  await go(page, "/m/greenhouse/threats");
  await page.fill("[data-jump]", "ts3");
  await page.press("[data-jump]", "Enter");
  await page.waitForURL(/\/m\/greenhouse\/threats\/TS_03$/);
});

await check("import a YAML file, then download every model as a zip", async () => {
  const dir = mkdtempSync(join(tmpdir(), "tmodel-ui-"));
  const name = `ui-import-${Date.now()}`;
  const file = join(dir, `${name}.yaml`);
  writeFileSync(file, readFileSync(new URL("../examples/minimal.yaml", import.meta.url)));
  await go(page, "/");
  await page.setInputFiles("input[type=file][name=file]", file);
  await page.click("button:has-text('Import')");
  await page.waitForURL(/imported=/);
  assert((await page.locator(".msg.ok").textContent()).includes(name), "import message");
  const dl = page.waitForEvent("download");
  await page.click("a[href='/models.zip']");
  assert((await dl).suggestedFilename() === "tmodel-models.zip", "zip name");
});

await check("phone: no sideways scroll; an edited card shows Save under its fields", async () => {
  const phone = await open(devices["iPhone 13"]);
  for (const path of ["/", "/m/greenhouse/threats", "/m/greenhouse/report", "/m/handset/surfaces"]) {
    await go(phone, path);
    const [sw, vw] = await phone.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
    assert(sw <= vw, `${path}: page ${sw}px wide in a ${vw}px viewport`);
    if (path.endsWith("/report")) {
      const caption = await phone.locator(".review-table caption").first().boundingBox();
      assert(caption.width > 200 && caption.height < 50, "report caption stays a readable line, not a narrow column");
    }
  }
  await editFirstRow(phone, "/m/greenhouse/threats");
  const card = await phone.locator("tr.editing").boundingBox();
  const save = await phone.locator("tr.editing button:has-text('Save')").boundingBox();
  const last = await phone.locator("tr.editing select[name=treatment]").boundingBox();
  assert(save.y > last.y && save.y + save.height <= card.y + card.height + 1, "Save is below the fields, inside the card");
  await phone.context().close();
});

// Folder mode (assets/folder.js + sw.js): browser build only. The origin-private
// file system stands in for a folder from the picker: the same handle type, so
// the same code path, minus the permission prompt.
if (await page.evaluate(() => !!navigator.serviceWorker.controller)) {
  const opfs = (fn, arg) => page.evaluate(fn, arg);
  const folderText = (name) => opfs(async (name) => {
    const d = await (await navigator.storage.getDirectory()).getDirectoryHandle("ui-folder");
    try { return await (await (await d.getFileHandle(name)).getFile()).text(); } catch (e) { return null; }
  }, name);
  const writeInFolder = (name, text) => opfs(async ([name, text]) => {
    const d = await (await navigator.storage.getDirectory()).getDirectoryHandle("ui-folder", { create: true });
    const w = await (await d.getFileHandle(name, { create: true })).createWritable();
    await w.write(text);
    await w.close();
  }, [name, text]);
  const listed = (p) => p.locator("table td.id a").allTextContents();
  const minimal = readFileSync(new URL("../examples/minimal.yaml", import.meta.url), "utf8");

  await check("folder: the model list offers to open a folder", async () => {
    await go(page, "/");
    assert(await page.locator("#folder-panel [data-folder=pick]").isVisible(), "Open folder button");
    await page.waitForFunction(() => document.querySelector("footer.host").textContent.includes("browser's storage"));
  });

  await check("folder: once open, the list shows the folder's models", async () => {
    await opfs(() => navigator.storage.getDirectory().then((r) => r.removeEntry("ui-folder", { recursive: true }).catch(() => {})));
    await writeInFolder("ondisk.yaml", minimal);
    await Promise.all([
      page.waitForNavigation(),
      opfs(async () => {
        const d = await (await navigator.storage.getDirectory()).getDirectoryHandle("ui-folder");
        await window.tmodelFolder.connect(d);
      }).catch(() => {}),  // the page reloads under it
    ]);
    await page.waitForSelector("#folder-panel");
    const ids = await listed(page);
    assert(ids.includes("ondisk") && !ids.includes("greenhouse"), `listed: ${ids}`);
    await page.waitForFunction(() => document.querySelector("footer.host").textContent.includes("folder ui-folder"));
    assert((await page.locator("#folder-panel").textContent()).includes("only in this browser's storage"), "offers the copy");
  });

  await check("folder: a new model is written to the folder", async () => {
    await page.fill("form[action='/models'] input[name=id]", "fresh");
    await page.fill("form[action='/models'] input[name=system]", "Made in the folder");
    await page.click("form[action='/models'] button[type=submit]");
    await page.waitForURL(/\/m\/fresh\//);
    const text = await folderText("fresh.yaml");
    assert(text && text.includes("Made in the folder"), `fresh.yaml: ${text}`);
  });

  await check("folder: an inline edit changes the file", async () => {
    await writeInFolder("greenhouse.yaml", readFileSync(new URL("../examples/greenhouse.yaml", import.meta.url), "utf8"));
    await editFirstRow(page, "/m/greenhouse/goals");
    await page.locator("tr.editing input[name=name]").fill("Renamed in the folder");
    await page.locator("tr.editing input[name=name]").press("Control+Enter");
    await page.waitForSelector(".toast", { timeout: 20000 });
    assert((await folderText("greenhouse.yaml")).includes("Renamed in the folder"), "greenhouse.yaml not updated");
  });

  await check("folder: a file changed on disk shows up", async () => {
    await writeInFolder("ondisk.yaml", minimal.replace(/^system: .*$/m, "system: Edited on disk"));
    await page.waitForTimeout(600);
    await go(page, "/");
    assert((await page.locator("table").first().textContent()).includes("Edited on disk"), "new system name not shown");
  });

  await check("folder: copy the browser's models into the folder", async () => {
    await go(page, "/");
    await Promise.all([page.waitForNavigation(), page.click("#folder-panel [data-folder=copy]")]);
    await page.waitForSelector("#folder-panel");
    assert((await folderText("handset.yaml")) !== null, "handset.yaml not copied");
    assert((await folderText("greenhouse.yaml")).includes("Renamed in the folder"), "copy overwrote greenhouse.yaml");
    assert(!(await page.locator("#folder-panel").textContent()).includes("only in this browser"), "still offers the copy");
  });

  // Reopening needs a handle stored in IndexedDB, and some Chromium builds
  // (Chrome for Testing 153 in a cloud container) crash restoring any file
  // handle from IndexedDB, with or without a service worker. Probe that in a
  // throwaway browser; skip, saying so, where it crashes.
  const restoreWorks = await (async () => {
    const probe = await chromium.launch();
    try {
      const p = await probe.newPage();
      await p.goto(BASE + "/_shell/build.json");
      return await Promise.race([
        p.evaluate(async () => {
          const h = await (await navigator.storage.getDirectory()).getDirectoryHandle("probe", { create: true });
          const db = await new Promise((ok) => {
            const r = indexedDB.open("probe", 1);
            r.onupgradeneeded = () => r.result.createObjectStore("kv");
            r.onsuccess = () => ok(r.result);
          });
          const tx = (mode, fn) => new Promise((ok) => {
            const t = db.transaction("kv", mode), q = fn(t.objectStore("kv"));
            t.oncomplete = () => ok(q.result);
          });
          await tx("readwrite", (s) => s.put(h, "h"));
          return (await tx("readonly", (s) => s.get("h"))).kind === "directory";
        }),
        new Promise((ok) => probe.on("disconnected", () => ok(false))),
      ]);
    } catch (e) {
      return false;
    } finally {
      await probe.close().catch(() => {});
    }
  })();
  if (!restoreWorks) console.log("skip  folder: a restarted service worker opens the folder again (this Chromium crashes restoring a stored handle)");
  if (restoreWorks) await check("folder: a restarted service worker opens the folder again", async () => {
    const fresh = await page.context().newPage();
    fresh.on("pageerror", (e) => pageErrors.push(String(e)));
    const cdp = await page.context().newCDPSession(fresh);
    await page.close();  // no page left to hand the folder over: the worker's gate page does it
    await cdp.send("ServiceWorker.enable");
    await cdp.send("ServiceWorker.stopAllWorkers");
    let gated = false;
    fresh.on("response", async (r) => {
      if (r.request().isNavigationRequest()) gated ||= (await r.text().catch(() => "")).includes("data-folder-gate");
    });
    await fresh.goto(BASE + "/");
    await fresh.waitForSelector("#folder-panel", { timeout: 180000 });
    assert(gated, "the gate page was not used");
    assert((await listed(fresh)).includes("fresh"), "folder models not listed");
    page = fresh;
  });

  await check("folder: back to browser storage", async () => {
    await go(page, "/");
    await Promise.all([page.waitForNavigation(), page.click("#folder-panel [data-folder=stop]")]);
    await page.waitForSelector("#folder-panel [data-folder=pick]");
    const ids = await listed(page);
    assert(ids.includes("greenhouse") && !ids.includes("fresh") && !ids.includes("ondisk"), `listed: ${ids}`);
  });
}

await check("no script errors on any page", async () => {
  assert(pageErrors.length === 0, pageErrors.join(" | "));
});

await browser.close();
console.log(failures.length ? `\n${failures.length} failed` : "\nall UI checks passed");
process.exit(failures.length ? 1 : 0);
