// Folder mode for the in-browser build: models saved as YAML files in a folder
// on this computer instead of the browser's (evictable) storage. Chromium only
// (File System Access API); elsewhere this does nothing and the build keeps
// using browser storage.
//
// Runs in every app page (browser.py inlines it into the host script) and in
// the service worker's "open the folder again" page (as /_shell/folder.js).
// The page owns the folder handle: it keeps it in IndexedDB and hands it to
// the service worker by postMessage, because Chromium crashes when a service
// worker reads a stored handle itself. sw.js does the reading and writing.
(function () {
  if (!("showDirectoryPicker" in window) || !navigator.serviceWorker) return;

  const kv = (mode, fn) => new Promise((ok, fail) => {
    const open = indexedDB.open("tmodel-folder", 1);
    open.onupgradeneeded = () => open.result.createObjectStore("kv");
    open.onerror = () => fail(open.error);
    open.onsuccess = () => {
      const tx = open.result.transaction("kv", mode);
      const req = fn(tx.objectStore("kv"));
      tx.oncomplete = () => { open.result.close(); ok(req.result); };
      tx.onerror = () => fail(tx.error);
    };
  });
  const get = (k) => kv("readonly", (s) => s.get(k));
  const put = (k, v) => kv("readwrite", (s) => s.put(v, k));
  const del = (k) => kv("readwrite", (s) => s.delete(k));

  // A message to the service worker, resolved with its answer.
  async function tell(msg) {
    const reg = await navigator.serviceWorker.ready;
    const sw = navigator.serviceWorker.controller || reg.active;
    return new Promise((ok, fail) => {
      const ch = new MessageChannel();
      ch.port1.onmessage = (e) => ok(e.data);
      setTimeout(() => fail(new Error("the service worker did not answer")), 15000);
      sw.postMessage(msg, [ch.port2]);
    });
  }

  async function connect(handle) {
    await put("handle", handle);
    await put("folder", { name: handle.name });  // the only key the service worker reads
    await tell({ type: "folder", handle });
    location.reload();
  }
  async function pick() {
    let handle;
    try {
      handle = await window.showDirectoryPicker({ id: "tmodel", mode: "readwrite" });
    } catch (e) {
      if (e.name === "AbortError") return;
      throw e;
    }
    await connect(handle);
  }
  async function stop() {
    await del("handle");
    await del("folder");
    await tell({ type: "folder-off" });
    location.reload();
  }
  async function copy() {
    await fetch("/_folder/copy", { method: "POST" });
    location.reload();
  }
  window.tmodelFolder = { connect, pick, stop, copy };

  // A restarted service worker asks open pages for the folder again.
  navigator.serviceWorker.addEventListener("message", async (e) => {
    if (!e.data || e.data.type !== "folder-wanted") return;
    const handle = await get("handle");
    if (handle && (await handle.queryPermission({ mode: "readwrite" })) === "granted") {
      e.source.postMessage({ type: "folder", handle });
    }
  });
  navigator.serviceWorker.startMessages();

  const act = (root) => root.addEventListener("click", (e) => {
    const a = e.target.closest("[data-folder]");
    if (a) { e.preventDefault(); window.tmodelFolder[a.dataset.folder](); }
  });

  // The service worker's page when it has no folder: give it the folder again
  // (permission permitting), or choose another, or go back to browser storage.
  const gate = document.querySelector("[data-folder-gate]");
  if (gate) {
    act(gate);
    (async () => {
      const handle = await get("handle");
      if (!handle) return stop();
      const resume = async () => { await tell({ type: "folder", handle }); location.reload(); };
      const failed = gate.dataset.folderGate;  // why the folder stopped working, if it did
      if (!failed && (await handle.queryPermission({ mode: "readwrite" })) === "granted") return resume();
      gate.querySelector("[data-folder-allow]").onclick = async () => {
        if ((await handle.requestPermission({ mode: "readwrite" })) === "granted") return resume();
        gate.querySelector(".muted").textContent = "The browser did not allow it.";
      };
      gate.hidden = false;
    })();
    return;
  }

  // App pages: where models are saved, in the footer; the choice on the model list.
  (async () => {
    let s;
    try {
      const r = await fetch("/_folder/status");
      if (!r.ok) return;
      s = await r.json();
    } catch (e) { return; }
    const foot = document.querySelector("footer.host");
    if (foot) {
      const span = document.createElement("span");
      span.className = "folder-status";
      span.textContent = s.name ? ` · folder ${s.name}` : " · this browser's storage";
      foot.append(span);
    }
    const side = location.pathname === "/" && document.querySelector(".side");
    if (!side) return;
    const panel = document.createElement("div");
    panel.className = "panel";
    panel.id = "folder-panel";
    if (s.name) {
      panel.innerHTML =
        '<h3>Folder</h3><p>Models are saved as YAML files in <b class="id"></b> on this computer. ' +
        "Edits to those files show up here.</p>" +
        (s.browser_only.length
          ? '<p class="help"><span></span> <a href="" data-folder="copy">Copy them to the folder</a></p>'
          : "") +
        '<div class="actions"><button class="btn ghost" type="button" data-folder="pick">Change folder…</button>' +
        '<button class="btn ghost" type="button" data-folder="stop">Use browser storage</button></div>';
      panel.querySelector("b").textContent = s.name;
      const only = panel.querySelector(".help span");
      if (only) {
        const n = s.browser_only.length;
        only.textContent = `${n} model${n === 1 ? " is" : "s are"} only in this browser's storage (${s.browser_only.join(", ")}).`;
      }
    } else {
      panel.innerHTML =
        "<h3>Folder</h3><p>Models are in this browser's storage, which the browser may clear. " +
        "Open a folder to save them as YAML files on this computer instead.</p>" +
        '<div class="actions"><button class="btn" type="button" data-folder="pick">Open folder…</button></div>';
    }
    act(panel);
    side.prepend(panel);
  })();
})();
