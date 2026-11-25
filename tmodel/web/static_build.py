"""
`tmodel build-web`: a static site that runs the web app entirely in the browser.

Output (any static host; wrangler.jsonc serves it on Cloudflare Workers with
index.html for unknown paths):

  index.html             boot shell: registers the service worker, then reloads
  sw.js                  the host: Pyodide + viz.js + the app (assets/sw.js)
  _shell/folder.js       folder mode's page side, for the worker's own pages
  _headers               Cloudflare cache rules (sw.js and build.json never stale)
  _shell/build.json      build id (changes => open pages reload), versions
  _shell/tmodel-*.zip    this package: Python, templates, assets
  _shell/seed-*.zip      models copied into browser storage on first visit
  _shell/wheels/*.whl    pure-Python dependencies at their uv.lock versions

Dependencies: a package from uv.lock is vendored (exact locked wheel, hash
checked) when it and everything it needs are pure Python. Anything that needs
compiled code (pydantic-core, pyyaml, markupsafe) comes from Pyodide's own
distribution instead, together with its dependants (pydantic, jinja2), so the
browser's versions of those may differ from the lockfile; build.json records both.

Pure standard library, so it runs on any static host's build image.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import shutil
import subprocess
import threading
import time
import tomllib
import urllib.request
import zipfile
from functools import partial
from pathlib import Path

PYODIDE_VERSION = "314.0.7"
VIZ_VERSION = "3.31.0"
PYODIDE_URL = f"https://cdn.jsdelivr.net/pyodide/v{PYODIDE_VERSION}/full/"
VIZ_URL = f"https://cdn.jsdelivr.net/npm/@viz-js/viz@{VIZ_VERSION}/dist/viz.js"

PKG = Path(__file__).resolve().parent.parent  # the tmodel package
ASSETS = Path(__file__).resolve().parent / "assets"
SERVER_ONLY = {"uvicorn"}  # not needed in the browser, nor anything only it pulls in
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "tmodel-build-web"


# Dependencies -------------------------------------------------------------------


def plan_dependencies(lock_path: Path) -> dict:
    """Split the runtime closure of uv.lock into vendored wheels and Pyodide packages."""
    lock = tomllib.loads(lock_path.read_text())
    pkgs = {p["name"]: p for p in lock["package"]}
    deps = {n: [d["name"] for d in p.get("dependencies", [])] for n, p in pkgs.items()}

    closure: list[str] = []

    def walk(name: str) -> None:
        if name in closure or name in SERVER_ONLY:
            return
        closure.append(name)
        for d in deps.get(name, []):
            walk(d)

    for d in deps["tmodel"]:
        walk(d)

    def pure_wheel(name: str) -> dict | None:
        return next((w for w in pkgs[name].get("wheels", []) if w["url"].endswith("-none-any.whl")), None)

    def needs_compiled(name: str, seen: frozenset = frozenset()) -> bool:
        if name in seen:
            return False
        return pure_wheel(name) is None or any(needs_compiled(d, seen | {name}) for d in deps.get(name, []))

    from_pyodide = {n for n in closure if needs_compiled(n)}
    # A pure package used by a Pyodide-provided one comes from Pyodide too, so
    # Pyodide's resolver and ours never install two copies.
    for n in list(from_pyodide):
        stack = list(deps.get(n, []))
        while stack:
            d = stack.pop()
            if d not in from_pyodide:
                from_pyodide.add(d)
                stack += deps.get(d, [])
    vendored = [n for n in closure if n not in from_pyodide]
    return {
        "pyodide": sorted(from_pyodide & set(closure)),
        "wheels": [{"name": n, "version": pkgs[n]["version"], **pure_wheel(n)} for n in vendored],
        "lock_versions": {n: pkgs[n]["version"] for n in sorted(closure)},
    }


def fetch_wheel(w: dict) -> Path:
    """Download a locked wheel once (hash-checked) into the user cache."""
    name = w["url"].rsplit("/", 1)[1]
    algo, digest = w["hash"].split(":", 1)
    dest = CACHE / name
    if dest.exists() and hashlib.new(algo, dest.read_bytes()).hexdigest() == digest:
        return dest
    data = urllib.request.urlopen(w["url"], timeout=60).read()
    if hashlib.new(algo, data).hexdigest() != digest:
        raise RuntimeError(f"hash mismatch for {name}")
    CACHE.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest


# Build ---------------------------------------------------------------------------


def _zip(dest: Path, files: dict[str, Path]) -> None:
    """Deterministic zip (fixed timestamps) so the build id only changes with content."""
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for arc, src in sorted(files.items()):
            info = zipfile.ZipInfo(arc, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, src.read_bytes())


def _package_files() -> dict[str, Path]:
    return {
        str(p.relative_to(PKG.parent)): p
        for p in sorted(PKG.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts and p.suffix in {".py", ".html", ".js", ".css"}
    }


def _git_label(repo: Path) -> str:
    sha = os.environ.get("CF_PAGES_COMMIT_SHA", "")[:7]
    if sha:
        return sha
    try:
        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], capture_output=True, text=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain", "--", "tmodel"], capture_output=True, text=True
        ).stdout.strip()
        return (sha or "nogit") + ("+dirty" if dirty else "")
    except OSError:
        return "nogit"


def build(out: Path, seeds: list[Path], lock: Path, repo: Path | None = None) -> dict:
    out = Path(out)
    shell = out / "_shell"
    if out.exists():
        shutil.rmtree(out)
    (shell / "wheels").mkdir(parents=True)

    plan = plan_dependencies(lock)
    wheels = []
    for w in plan["wheels"]:
        src = fetch_wheel(w)
        shutil.copy(src, shell / "wheels" / src.name)
        wheels.append(src.name)

    app_zip, seed_zip = shell / "app.zip", shell / "seed.zip"
    _zip(app_zip, _package_files())
    models = {p.name: p for d in seeds if Path(d).is_dir() for p in sorted(Path(d).glob("*.yaml"))}
    _zip(seed_zip, models)

    h = hashlib.sha256()
    for p in [
        app_zip,
        seed_zip,
        *sorted((shell / "wheels").iterdir()),
        ASSETS / "sw.js",
        ASSETS / "folder.js",
        ASSETS / "index.html",
    ]:
        h.update(p.read_bytes())
    build_id = h.hexdigest()[:12]
    # Content-addressed names: a stale HTTP cache can never mix builds.
    app_name, seed_name = f"tmodel-{build_id}.zip", f"seed-{build_id}.zip"
    app_zip.rename(shell / app_name)
    seed_zip.rename(shell / seed_name)

    info = {
        "id": build_id,
        "git": _git_label(repo or PKG.parent),
        "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "pyodide": PYODIDE_VERSION,
        "viz": VIZ_VERSION,
        "app": app_name,
        "seed": seed_name,
        "seed_models": sorted(models),
        "wheels": wheels,
        "pyodide_packages": plan["pyodide"],
        "lock_versions": plan["lock_versions"],
    }
    sw = (ASSETS / "sw.js").read_text()
    sw = sw.replace("__PYODIDE__", PYODIDE_URL).replace("__VIZ__", VIZ_URL)
    sw = sw.replace("__BUILD__", json.dumps(info))
    (out / "sw.js").write_text(sw)
    shutil.copy(ASSETS / "index.html", out / "index.html")
    shutil.copy(ASSETS / "folder.js", shell / "folder.js")
    (shell / "build.json").write_text(json.dumps(info, indent=2) + "\n")
    (out / "_headers").write_text(
        "/sw.js\n  Cache-Control: no-cache\n/_shell/folder.js\n  Cache-Control: no-cache\n"
        "/_shell/build.json\n  Cache-Control: no-store\n"
        "/_shell/*.zip\n  Cache-Control: public, max-age=31536000, immutable\n"
    )
    return info


# Local serving and watching ------------------------------------------------------


class _Handler(http.server.SimpleHTTPRequestHandler):
    """Static files; unknown paths get index.html (as wrangler.jsonc does on Cloudflare)."""

    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".mjs": "text/javascript",
        ".js": "text/javascript",
        ".whl": "application/zip",
        ".json": "application/json",
    }

    def send_head(self):
        path = Path(self.translate_path(self.path))
        if not path.exists() and not self.path.startswith("/_shell/"):
            self.path = "/index.html"
        return super().send_head()

    def end_headers(self):
        if self.path in ("/sw.js", "/_shell/build.json", "/_shell/folder.js"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *a):
        pass


def serve(out: Path, host: str, port: int) -> http.server.ThreadingHTTPServer:
    srv = http.server.ThreadingHTTPServer((host, port), partial(_Handler, directory=str(out)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _snapshot(paths: list[Path]) -> dict[Path, float]:
    snap = {}
    for root in paths:
        for p in Path(root).rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts:
                snap[p] = p.stat().st_mtime
    return snap


def watch(rebuild, paths: list[Path], every: float = 0.5) -> None:
    """Rebuild whenever a file under `paths` changes. Open pages notice the new
    build.json and reload themselves."""
    last = _snapshot(paths)
    while True:
        time.sleep(every)
        now = _snapshot(paths)
        if now != last:
            last = now
            try:
                info = rebuild()
                print(f"tmodel: rebuilt {info['id']}", flush=True)
            except Exception as e:  # keep watching; the previous build stays up
                print(f"tmodel: build failed: {e}", flush=True)
