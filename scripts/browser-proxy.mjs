// Expose the in-browser build as a plain HTTP server, for scripts/compare.py
// (and curl). Dev-only.
//   node scripts/browser-proxy.mjs BUILD_URL PORT
// Opens BUILD_URL in headless Chromium, waits for the service worker to boot,
// then answers each request on PORT by running fetch() inside that page, so
// the service worker (Pyodide + the app) produces the response. Redirects are
// followed; the final URL comes back in X-Final-Url.
import http from "node:http";
import { createRequire } from "node:module";
const { chromium } = createRequire(import.meta.url)("playwright"); // global install

const [build, port] = process.argv.slice(2);
const browser = await chromium.launch();
for (const signal of ["SIGTERM", "SIGINT"]) {
  process.once(signal, async () => { await browser.close(); process.exit(0); });
}
const page = await browser.newPage();
page.on("pageerror", (e) => console.error("pageerror:", e));
await page.goto(build + "/");
await page.waitForSelector("header .brand", { timeout: 180000 });

http
  .createServer(async (req, res) => {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    const body = chunks.length ? Buffer.concat(chunks).toString("latin1") : null;
    const headers = {};
    for (const [k, v] of Object.entries(req.headers)) if (!["host", "connection", "content-length"].includes(k)) headers[k] = v;
    try {
      const r = await page.evaluate(
        async ({ method, path, headers, body }) => {
          const bytes = body === null ? undefined : Uint8Array.from(body, (c) => c.charCodeAt(0));
          const r = await fetch(path, { method, headers, body: bytes });
          const buf = new Uint8Array(await r.arrayBuffer());
          let s = "";
          for (let i = 0; i < buf.length; i += 0x8000) s += String.fromCharCode(...buf.subarray(i, i + 0x8000));
          return { status: r.status, url: r.url, headers: [...r.headers], body: s };
        },
        { method: req.method, path: req.url, headers, body },
      );
      const h = Object.fromEntries(r.headers.filter(([k]) => !["content-length", "content-encoding"].includes(k)));
      h["x-final-url"] = r.url;
      res.writeHead(r.status, h);
      res.end(Buffer.from(r.body, "latin1"));
    } catch (e) {
      res.writeHead(502, { "content-type": "text/plain" });
      res.end(String(e));
    }
  })
  .listen(Number(port), "127.0.0.1", () => console.log(`browser build ${build} on http://127.0.0.1:${port}`));
