"""
In-browser host. The service worker (assets/sw.js) runs this module in Pyodide
and hands it every request the page makes; it answers with the very same
Starlette app `tmodel serve` runs. Nothing here knows about routes or fields.

Differences from the server are only those a `Host` carries, plus the store
being a Pyodide IDBFS (IndexedDB) mount with git off.

`handle` is plain ASGI and is tested on CPython (tests/test_browser.py).
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path

from .app import Host, create_app, live_reload


async def call_asgi(app, method: str, target: str, headers: list[tuple[str, str]], body: bytes) -> dict:
    """One HTTP request through an ASGI app. Returns {status, headers, body}."""
    url = urllib.parse.urlsplit(target)
    raw_path = url.path.encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": url.scheme or "https",
        "path": urllib.parse.unquote(url.path),
        "raw_path": raw_path,
        "query_string": url.query.encode(),
        "root_path": "",
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers],
        "client": ("browser", 0),
        "server": (url.hostname or "browser", url.port or (80 if url.scheme == "http" else 443)),
    }
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    out: dict = {"status": 500, "headers": [], "body": b""}
    chunks: list[bytes] = []

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]
            out["headers"] = [[k.decode("latin-1"), v.decode("latin-1")] for k, v in message["headers"]]
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await app(scope, receive, send)
    out["body"] = b"".join(chunks)
    return out


class BrowserApp:
    def __init__(self, root: str, render_svg: Callable[[str], str | None], label: str, page_script: str = ""):
        host = Host(
            label=label,
            render_svg=render_svg,
            script=page_script,
            guard=lambda _: nullcontext(),
            durable=False,
        )
        self.app = create_app(Path(root), git=False, host=host)

    async def handle(self, method: str, target: str, headers, body) -> dict:
        """Called from JS: `headers` is a JS array of [name, value], `body` a
        Uint8Array or null. Pyodide converts the returned dict for JS."""
        if hasattr(headers, "to_py"):
            headers = headers.to_py()
        if hasattr(body, "to_bytes"):
            body = body.to_bytes()
        return await call_asgi(self.app, method, target, [tuple(h) for h in headers], body or b"")


def create(root: str, render_svg, label: str, watch_ms: int = 5000) -> BrowserApp:
    """Entry point for sw.js. The page script reloads open pages when a new
    build is deployed (build.json changes). Its polling also keeps the service
    worker warm: browsers stop an idle one after ~30 s, and the next request
    would then wait for Python to boot again. It also carries folder mode's page
    side (assets/folder.js: saving models to a folder on disk, in Chromium).
    sw.js calls this once per models root: /models (browser storage) and the
    in-memory mirror of the folder."""
    script = SW_UPDATE + FOLDER_JS + "\n" + live_reload("/_shell/build.json", watch_ms)
    return BrowserApp(root, render_svg, label, script)


FOLDER_JS = (Path(__file__).parent / "assets" / "folder.js").read_text()


# Before reloading for a new build, install the new service worker: otherwise
# the old one (old app) would answer the reload.
SW_UPDATE = """
window.tmodelBeforeReload = async function () {
  const reg = await navigator.serviceWorker.getRegistration();
  if (!reg) return;
  await reg.update();
  const w = reg.installing || reg.waiting;
  if (!w) return;
  await new Promise((done) => {
    const check = () => (w.state === "activated" || w.state === "redundant") && done();
    w.addEventListener("statechange", check);
    check();
  });
};
"""
