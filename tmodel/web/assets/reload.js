// Live reload: poll a URL whose body changes when the app changes (a server
// boot id under `tmodel serve` (live reload is its default), build.json for the in-browser build)
// and reload the page. Never while a row or field is being edited: then a
// banner offers the reload instead, so nothing typed is lost.
(function () {
  const url = "__WATCH_URL__", every = __WATCH_MS__;
  let seen = null, banner = null;
  const editing = () =>
    document.querySelector("tr.editing, [data-dirty]") ||
    (document.activeElement && document.activeElement.matches("input, textarea, select"));
  async function check() {
    if (document.hidden) return;
    let token;
    try {
      const r = await fetch(url, { cache: "no-store" });
      if (!r.ok) return;
      token = await r.text();
    } catch (e) { return; }  // restarting: try again next tick
    if (seen === null) { seen = token; return; }
    if (token === seen) return;
    if (window.tmodelBeforeReload) await window.tmodelBeforeReload();
    if (!editing()) return location.reload();
    if (banner) return;
    banner = document.createElement("div");
    banner.className = "msg ok reload-banner";
    banner.innerHTML = 'New version available. <a href="">Reload</a> when you are done editing.';
    document.body.prepend(banner);
  }
  setInterval(check, every);
  check();
})();
