// Exercise worker persistence failure/retry without downloading Pyodide.
// Real host parity and folder behavior are tested separately in Chromium.
import { readFileSync } from "node:fs";
import vm from "node:vm";
import assert from "node:assert/strict";

const source = readFileSync(new URL("../tmodel/web/assets/sw.js", import.meta.url), "utf8")
  .replace(/^import .*;$/gm, "")
  .replace("__BUILD__", "{}");
let syncs = 0;
const context = {
  self: { addEventListener() {} },
  Response, URL, console, setTimeout,
  py: { FS: { syncfs(populate, done) { done(++syncs === 1 ? new Error("quota exceeded") : null); } } },
};
await vm.runInNewContext(source + `
boot = async () => py;
folderMode = async () => false;
call = async () => new Response("saved", { status: 200 });
(async () => {
  const request = { url: "https://example.test/models", method: "POST", arrayBuffer: async () => new ArrayBuffer(0) };
  const failed = await serve(request);
  if (failed.status !== 503 || !(await failed.text()).includes("in memory")) throw new Error("sync failure hidden");
  const retried = await serve(request);
  if (retried.status !== 200) throw new Error("failed sync poisoned later writes");
})();`, context);
assert.equal(syncs, 2);
console.log("ok    service-worker sync failure is explicit and retryable");
