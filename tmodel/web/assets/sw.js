// tmodel in-browser host (written into the build by `tmodel build-web`).
//
// This service worker runs the same Python app as `tmodel serve`, inside
// Pyodide, and answers every request the page makes with it: links, form posts,
// redirects and the inline-editing fetches all behave exactly as against the
// server. Models live in IndexedDB (an IDBFS mount at /models), or, in folder
// mode, in a folder on this computer (see "Folder mode" below).
//
// It is a module worker so that Pyodide's ES-module core can be imported
// statically: service workers do not allow dynamic import().
import { loadPyodide } from "__PYODIDE__pyodide.mjs";
import createPyodideModule from "__PYODIDE__pyodide.asm.mjs";
import { instance as vizInstance } from "__VIZ__";

const BUILD = __BUILD__;
const ROOT = "/models";
const FOLDER = "/folder";

// Shell files are served by the static host, not the app.
const passThrough = (url) =>
  url.origin !== location.origin || url.pathname === "/sw.js" || url.pathname.startsWith("/_shell/");

self.addEventListener("install", (e) => {
  self.skipWaiting();
  e.waitUntil(boot()); // the page reloads into a ready app
});
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (passThrough(url)) return;
  e.respondWith(serve(e.request));
});
self.addEventListener("message", (e) => onMessage(e));

let booting = null;
function boot() {
  booting ??= start().catch((err) => {
    booting = null;
    throw err;
  });
  return booting;
}

async function bytes(path) {
  const r = await fetch(path, { cache: "no-cache" });
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return new Uint8Array(await r.arrayBuffer());
}

async function start() {
  const t0 = Date.now();
  const [pyodide, viz] = await Promise.all([
    loadPyodide({ indexURL: "__PYODIDE__", createPyodideModule }),
    vizInstance(),
  ]);
  await pyodide.loadPackage(BUILD.pyodide_packages, { messageCallback: () => {} });
  const site = pyodide.runPython("import sysconfig; sysconfig.get_paths()['purelib']");
  for (const w of BUILD.wheels) pyodide.unpackArchive(await bytes(`/_shell/wheels/${w}`), "wheel", { extractDir: site });
  pyodide.unpackArchive(await bytes(`/_shell/${BUILD.app}`), "zip", { extractDir: "/app" });

  const FS = pyodide.FS;
  FS.mkdirTree(ROOT);
  FS.mount(FS.filesystems.IDBFS, {}, ROOT);
  await syncfs(FS, true);
  if (!FS.analyzePath(`${ROOT}/.seeded`).exists) {
    pyodide.unpackArchive(await bytes(`/_shell/${BUILD.seed}`), "zip", { extractDir: ROOT });
    FS.writeFile(`${ROOT}/.seeded`, BUILD.id);
    await syncfs(FS, false);
  }
  FS.mkdirTree(FOLDER); // in memory: a mirror of the folder, in folder mode

  pyodide.runPython("import sys; sys.path.insert(0, '/app')");
  const render = (dot) => viz.renderString(dot, { format: "svg", engine: "dot" });
  const label = `browser · ${BUILD.git}`;
  const create = pyodide.pyimport("tmodel.web.browser").create;
  let folderApp = null;
  const py = {
    FS,
    app: create(ROOT, render, label),
    folderApp: () => (folderApp ??= create(FOLDER, render, label)),
  };
  console.log(`tmodel: booted in ${Date.now() - t0} ms`, BUILD);
  return py;
}

const syncfs = (FS, populate) =>
  new Promise((ok, fail) => FS.syncfs(populate, (err) => (err ? fail(err) : ok())));

let writes = Promise.resolve(); // persist one at a time, in order

async function serve(request) {
  const url = new URL(request.url);
  let py;
  try {
    py = await boot();
  } catch (err) {
    return new Response(`tmodel failed to start in this browser:\n\n${err.stack || err}`, {
      status: 500,
      headers: { "Content-Type": "text/plain; charset=utf-8" },
    });
  }
  if (url.pathname.startsWith("/_folder/")) return folderApi(py, request, url);
  const write = !["GET", "HEAD"].includes(request.method);
  const body = write ? new Uint8Array(await request.arrayBuffer()) : null;
  if (await folderMode()) return serveFromFolder(py, request, url, body);
  const res = await call(py.app, request, url, body);
  if (write) {
    const persist = writes.then(() => syncfs(py.FS, false));
    writes = persist.catch(() => {}); // one failed sync must not poison every later save
    try {
      await persist;
    } catch (err) {
      return new Response(`The change is in memory, but browser storage could not be saved: ${err}. Export a backup before closing.`, {
        status: 503, headers: { "Content-Type": "text/plain; charset=utf-8" },
      });
    }
  }
  return res;
}

async function call(app, request, url, body) {
  const result = await app.handle(request.method, url.href, [...request.headers], body);
  const res = result.toJs({ dict_converter: Object.fromEntries });
  result.destroy();
  const empty = res.body.length === 0 || [204, 304].includes(res.status) || request.method === "HEAD";
  return new Response(empty ? null : res.body, { status: res.status, headers: res.headers });
}

// Folder mode ---------------------------------------------------------------------
//
// The page (assets/folder.js) picks a folder and hands its handle over by
// postMessage; it also stores it, with a plain {name} record this worker reads
// to know folder mode is on. The handle itself is never read from IndexedDB
// here: that crashes Chromium. A restarted worker asks open pages for it, or
// answers navigations with a page that does it (needing a click when the
// browser asks for permission again).
//
// The folder is mirrored into the in-memory FOLDER, which a second instance of
// the app serves: before each request, files changed on disk are read in (so
// edits made outside the browser show up); after a write, files the app changed
// are written back. Requests in folder mode run one at a time.

let folder = null; // { handle, name, known: Map(file -> {mtime, size, text}), pulled }
let folderFlag; // undefined: not read yet; null: off; {name}: on
let folderError = ""; // why the folder stopped working
let handleWaiters = [];
let folderQueue = Promise.resolve();

function kvGet(key) {
  return new Promise((ok, fail) => {
    const open = indexedDB.open("tmodel-folder", 1);
    open.onupgradeneeded = () => open.result.createObjectStore("kv");
    open.onerror = () => fail(open.error);
    open.onsuccess = () => {
      const tx = open.result.transaction("kv", "readonly");
      const req = tx.objectStore("kv").get(key);
      tx.oncomplete = () => { open.result.close(); ok(req.result); };
      tx.onerror = () => fail(tx.error);
    };
  });
}

async function folderMode() {
  if (folderFlag === undefined) folderFlag = (await kvGet("folder")) ?? null;
  return folderFlag !== null;
}

function onMessage(e) {
  const msg = e.data || {};
  if (msg.type === "folder" && msg.handle) {
    folder = { handle: msg.handle, name: msg.handle.name, known: new Map(), pulled: 0 };
    folderFlag = { name: folder.name };
    folderError = "";
    for (const ok of handleWaiters) ok(true);
    handleWaiters = [];
  } else if (msg.type === "folder-off") {
    folder = null;
    folderFlag = null;
    folderError = "";
  } else {
    return;
  }
  if (e.ports[0]) e.ports[0].postMessage({ ok: true });
}

// Ask open pages for the folder handle (after a restart of this worker).
async function askForHandle() {
  const pages = await self.clients.matchAll({ type: "window" });
  if (!pages.length) return false;
  const answer = new Promise((ok) => {
    handleWaiters.push(ok);
    setTimeout(() => ok(false), 1500);
  });
  for (const p of pages) p.postMessage({ type: "folder-wanted" });
  return answer;
}

function inFolderQueue(fn) {
  const run = folderQueue.then(fn);
  folderQueue = run.catch(() => {});
  return run;
}

async function serveFromFolder(py, request, url, body) {
  if (!folder && !folderError && !(await askForHandle())) return folderGate(request);
  if (!folder) return folderGate(request);
  const write = body !== null;
  try {
    return await inFolderQueue(async () => {
      await pull(py.FS, write || request.mode === "navigate");
      const res = await call(py.folderApp(), request, url, body);
      if (write) await push(py.FS);
      return res;
    });
  } catch (err) {
    if (!["NotAllowedError", "NotFoundError", "SecurityError", "InvalidStateError"].includes(err.name)) throw err;
    folder = null;
    folderError = `${err.name}: ${err.message}`;
    return folderGate(request);
  }
}

// Read in what changed in the folder: for every page and every write; for the
// requests a page makes (partials, diagram) at most twice a second.
async function pull(FS, force) {
  if (!force && Date.now() - folder.pulled < 500) return;
  const seen = new Set();
  for await (const [name, h] of folder.handle.entries()) {
    if (h.kind !== "file" || !name.endsWith(".yaml")) continue;
    seen.add(name);
    const file = await h.getFile();
    const k = folder.known.get(name);
    if (k && k.mtime === file.lastModified && k.size === file.size) continue;
    const text = await file.text();
    FS.writeFile(`${FOLDER}/${name}`, text);
    folder.known.set(name, { mtime: file.lastModified, size: file.size, text });
  }
  for (const name of FS.readdir(FOLDER)) {
    if (name.endsWith(".yaml") && !seen.has(name)) {
      FS.unlink(`${FOLDER}/${name}`);
      folder.known.delete(name);
    }
  }
  folder.pulled = Date.now();
}

// Write back the files the app changed.
async function push(FS) {
  for (const name of FS.readdir(FOLDER)) {
    if (!name.endsWith(".yaml")) continue;
    const text = FS.readFile(`${FOLDER}/${name}`, { encoding: "utf8" });
    const k = folder.known.get(name);
    if (k && k.text === text) continue;
    const fh = await folder.handle.getFileHandle(name, { create: true });
    const w = await fh.createWritable();
    await w.write(text);
    await w.close();
    const file = await fh.getFile();
    folder.known.set(name, { mtime: file.lastModified, size: file.size, text });
  }
}

const yamlFiles = (FS, dir) => FS.readdir(dir).filter((n) => n.endsWith(".yaml")).sort();

// /_folder/status (GET) and /_folder/copy (POST: browser-storage models the folder lacks).
async function folderApi(py, request, url) {
  const json = (data, status = 200) =>
    new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
  if (!(await folderMode())) return json({ name: null, browser_only: [] });
  if (!folder && !folderError) await askForHandle(); // the page asking is open: it answers
  if (!folder) return json({ error: folderError || "the folder is not open" }, 503);
  try {
    return await inFolderQueue(async () => {
      await pull(py.FS, true);
      const inFolder = new Set(yamlFiles(py.FS, FOLDER));
      const only = yamlFiles(py.FS, ROOT).filter((n) => !inFolder.has(n));
      if (url.pathname === "/_folder/copy" && request.method === "POST") {
        for (const n of only) py.FS.writeFile(`${FOLDER}/${n}`, py.FS.readFile(`${ROOT}/${n}`));
        await push(py.FS);
        return json({ name: folder.name, copied: only.map((n) => n.slice(0, -5)), browser_only: [] });
      }
      return json({ name: folder.name, browser_only: only.map((n) => n.slice(0, -5)) });
    });
  } catch (err) {
    return json({ error: String(err) }, 500);
  }
}

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

// No folder handle: navigations get a page that hands it over (folder.js), the rest a 503.
function folderGate(request) {
  const name = (folderFlag && folderFlag.name) || "the folder";
  if (request.mode !== "navigate") {
    return new Response(`The models folder (${name}) is not open; reload the page.`, {
      status: 503,
      headers: { "Content-Type": "text/plain; charset=utf-8" },
    });
  }
  const why = folderError ? `<p class="err">It stopped working: ${esc(folderError)}</p>` : "";
  const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>tmodel</title>
<style>
  body { margin: 0; font: 14px/1.5 system-ui, -apple-system, Segoe UI, Roboto, sans-serif; color: #1f2937; background: #f8fafc; }
  main { max-width: 520px; margin: 18vh auto 0; padding: 0 16px; }
  h1 { font-size: 18px; letter-spacing: .02em; }
  button { font: inherit; padding: 6px 12px; margin: 4px 6px 4px 0; border-radius: 6px; border: 1px solid #2563eb; background: #2563eb; color: #fff; cursor: pointer; }
  button.ghost { background: transparent; color: #2563eb; }
  .muted { color: #6b7280; font-size: 13px; }
  .err { color: #b91c1c; }
</style>
</head>
<body>
<main>
  <h1>tmodel</h1>
  <div data-folder-gate="${esc(folderError)}" hidden>
    <p>Models are saved in the folder <b>${esc(name)}</b> on this computer. The browser needs your permission to open it again.</p>
    ${why}
    <p><button type="button" data-folder-allow>Open ${esc(name)}</button>
       <button type="button" class="ghost" data-folder="pick">Choose another folder…</button>
       <button type="button" class="ghost" data-folder="stop">Use browser storage</button></p>
    <p class="muted"></p>
  </div>
</main>
<script src="/_shell/folder.js"></script>
</body>
</html>
`;
  return new Response(html, { status: 200, headers: { "Content-Type": "text/html; charset=utf-8" } });
}
