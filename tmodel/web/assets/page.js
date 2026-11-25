// Inline row editing. Rows carry data-row="<url>"; the server returns <tr> partials.
// Without JS the ↗ link still opens the full form, so nothing here is required.
(function () {
  const H = { "X-Inline": "1" };
  const trOf = (html) => { const t = document.createElement("table"); t.innerHTML = html.trim(); return t.querySelector("tr"); };
  function toast(text) {
    if (!text) return;
    const old = document.querySelector(".toast");
    if (old) old.remove();
    const t = document.createElement("div");
    t.className = "toast"; t.setAttribute("role", "status"); t.textContent = text;
    document.body.appendChild(t);
    setTimeout(() => { t.style.opacity = "0"; setTimeout(() => t.remove(), 400); }, 4500);
  }
  async function swap(tr, url, opts) {
    let r;
    try {
      r = await fetch(url, Object.assign({ headers: H }, opts || {}));
    } catch (err) {
      alert("Request failed (is tmodel still running?): " + err);
      return { ok: false, tr };
    }
    if (r.status >= 500) {
      alert("Server error " + r.status + ". " + await r.text() + "\nReload to check whether the change was saved before retrying.");
      return { ok: false, tr };
    }
    const next = trOf(await r.text());
    tr.replaceWith(next);
    toast(next.dataset.toast);
    return { ok: r.ok, tr: next };
  }
  async function refreshSide() {
    const side = document.getElementById("side");
    if (!side) return;
    const r = await fetch(side.dataset.side);
    const box = document.createElement("div"); box.innerHTML = await r.text();
    side.replaceWith(box.firstElementChild);
  }
  function bumpTab(delta) {
    const n = document.querySelector("nav.tabs a.on .n");
    if (n) n.textContent = String((parseInt(n.textContent, 10) || 0) + delta);
  }
  async function addRow(url) {
    if (document.querySelector("tr.editing")) return;
    const r = await fetch(url, { headers: H });
    const tr = trOf(await r.text());
    document.querySelector("table.rows").appendChild(tr);
    const empty = document.querySelector("[data-empty]");
    if (empty) empty.remove();
    tr.scrollIntoView({ block: "nearest" });
    const first = tr.querySelector("input:not([readonly]):not([type=hidden]), select, textarea");
    if (first) first.focus();
  }
  document.addEventListener("click", async (e) => {
    const chip = e.target.closest("button[data-via]");
    if (chip) {  // append "  @SURF_X" to the step line the cursor is on (replacing a previous @)
      const ta = document.getElementById(chip.parentElement.dataset.for);
      const v = ta.value, at = ta.selectionStart ?? v.length;
      const start = v.lastIndexOf("\n", at - 1) + 1, end = v.indexOf("\n", at) < 0 ? v.length : v.indexOf("\n", at);
      const line = v.slice(start, end).replace(/\s+@SURF_[A-Z0-9_]+\s*$/, "") + "  @" + chip.dataset.via;
      ta.value = v.slice(0, start) + line + v.slice(end);
      ta.focus(); ta.selectionStart = ta.selectionEnd = start + line.length;
      ta.dispatchEvent(new Event("input", { bubbles: true }));
      return;
    }
    const add = e.target.closest("a[data-add]");
    if (add) {
      e.preventDefault();
      return addRow(add.dataset.add);
    }
    if (e.target.closest("[data-cancel]")) {
      const tr = e.target.closest("tr[data-row]");
      if (tr.hasAttribute("data-new")) return tr.remove();
      return swap(tr, tr.dataset.row);
    }
    const tr = e.target.closest("tr[data-row]");
    if (!tr || tr.classList.contains("editing")) return;
    if (e.target.closest("a, button, input, select, textarea, label")) return;
    if (String(window.getSelection())) return;          // selecting text, not clicking
    if (document.querySelector("tr.editing")) return;   // one at a time
    e.preventDefault();
    const { tr: edit } = await swap(tr, tr.dataset.row + "/edit");
    const first = edit.querySelector("input:not([readonly]):not([type=hidden]), select, textarea");
    if (first) first.focus();
  });
  document.addEventListener("submit", async (e) => {
    const form = e.target;
    if (!form.hasAttribute("data-inline")) return;
    e.preventDefault();
    const tr = form.closest("tr[data-row]");
    const wasNew = tr.hasAttribute("data-new");
    const next = e.submitter && e.submitter.name === "next";
    const body = new FormData(form);
    form.querySelectorAll("button").forEach((b) => (b.disabled = true));
    const { ok } = await swap(tr, form.action, { method: "POST", body });
    form.querySelectorAll("button").forEach((b) => (b.disabled = false));
    if (!ok) return;
    refreshSide();
    if (wasNew) bumpTab(1);
    if (wasNew && next) addRow(form.action);
  });
  document.addEventListener("keydown", (e) => {
    const typing = e.target.matches && e.target.matches("input, textarea, select");
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter" && e.target.form) {  // save
      e.preventDefault();
      return e.target.form.requestSubmit();
    }
    if (e.key === "Escape") {
      const tr = document.querySelector("tr.editing");
      if (!tr) return;
      if (tr.hasAttribute("data-new")) tr.remove(); else swap(tr, tr.dataset.row);
      return;
    }
    if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "n") {
      const add = document.querySelector("a[data-add]");
      if (add) { e.preventDefault(); addRow(add.dataset.add); }
    } else if (e.key === "/") {
      const box = document.querySelector("[data-filter]") || document.querySelector("[data-jump]");
      if (box) { e.preventDefault(); box.focus(); box.select(); }
    }
  });
  // Filter as you type (rows hidden in place; Enter asks the server, which filters the same way).
  const filter = document.querySelector("[data-filter]");
  if (filter) {
    filter.addEventListener("input", () => {
      const words = filter.value.toLowerCase().split(/\s+/).filter(Boolean);
      document.querySelectorAll("table.rows tr[data-row]:not(.editing)").forEach((tr) => {
        const text = tr.textContent.toLowerCase();
        tr.hidden = !words.every((w) => text.includes(w));
      });
      document.querySelectorAll("table.rows tr[data-group]").forEach((g) => {  // hide a section with no rows left
        let n = g.nextElementSibling;
        while (n && n.hasAttribute("data-row") && n.hidden) n = n.nextElementSibling;
        g.hidden = !(n && n.hasAttribute("data-row"));
      });
      const u = new URL(location.href);
      if (filter.value) u.searchParams.set("q", filter.value); else u.searchParams.delete("q");
      u.searchParams.delete("saved");
      history.replaceState(null, "", u);
    });
  }
  // Unsaved changes: mark a form (or inline row) dirty on input; ask before leaving the page.
  document.addEventListener("input", (e) => {
    if (e.target.matches("[data-filter], [data-jump]")) return;
    const form = e.target.form || e.target.closest("form");
    const box = e.target.closest("tr.editing") || (form && form.method === "post" ? form : null);
    if (box) box.setAttribute("data-dirty", "");
  });
  document.addEventListener("submit", (e) => {
    e.target.removeAttribute("data-dirty");
  }, true);
  window.addEventListener("beforeunload", (e) => {
    if (document.querySelector("[data-dirty]")) { e.preventDefault(); e.returnValue = ""; }
  });
})();
