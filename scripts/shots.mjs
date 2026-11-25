// Screenshots of key pages, phone and desktop. Dev-only.
//   node scripts/shots.mjs BASE OUTDIR [model ...]
// BASE can be `tmodel serve` or a static build of the browser version (it waits
// for the service worker to boot, so the same script measures both).
import { mkdirSync } from "node:fs";
import { createRequire } from "node:module";
const { chromium, devices } = createRequire(import.meta.url)("playwright"); // global install

const [base, out, ...ids] = process.argv.slice(2);
const models = ids.length ? ids : ["handset", "greenhouse", "minimal"];
mkdirSync(out, { recursive: true });

const browser = await chromium.launch();
for (const [label, ctxOpts] of [
  ["phone", devices["iPhone 13"]],
  ["desktop", { viewport: { width: 1400, height: 900 } }],
]) {
  const ctx = await browser.newContext(ctxOpts);
  const page = await ctx.newPage();
  page.on("pageerror", (e) => console.error(`[${label}] pageerror: ${e}`));
  await page.goto(base + "/", { waitUntil: "networkidle" });
  await page.waitForSelector("header .brand", { timeout: 120000 });
  for (const mid of models) {
    const shot = async (name) => {
      await page.screenshot({ path: `${out}/${label}-${mid}-${name}.png`, fullPage: true });
      await page.screenshot({ path: `${out}/${label}-${mid}-${name}-top.png` });
    };
    await page.goto(`${base}/m/${mid}/threats`, { waitUntil: "networkidle" });
    await shot("threats");
    const row = page.locator("table.rows tr[data-row]").first();
    if (await row.count()) {
      await row.locator("td").nth(1).click();
      await page.waitForSelector("tr.editing", { timeout: 15000 });
      await page.locator("tr.editing").scrollIntoViewIfNeeded();
      await shot("threats-edit");
    }
    await page.goto(`${base}/m/${mid}/report`, { waitUntil: "networkidle" });
    await shot("report");
  }
  await ctx.close();
}
await browser.close();
console.log(`screenshots in ${out}`);
