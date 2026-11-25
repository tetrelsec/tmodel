"""The in-browser host (browser.py, static_build.py) and dev live reload, on CPython.

The service worker itself (assets/sw.js) only moves bytes between fetch() and
`BrowserApp.handle`; whole-app parity between the two hosts is checked by
scripts/compare.py (see CLAUDE.md), which needs a browser and the network.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

from tmodel.web import static_build
from tmodel.web.app import Host, create_app
from tmodel.web.browser import BrowserApp, call_asgi, create

REPO = Path(__file__).parent.parent
SAMPLE = REPO / "examples" / "greenhouse.yaml"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    shutil.copy(SAMPLE, tmp_path / SAMPLE.name)
    return tmp_path


def run(coro):
    return asyncio.run(coro)


def fake_svg(dot: str) -> str:
    return '<?xml?><svg width="10pt" height="5pt"><text font-family="DejaVu Sans">x</text></svg>'


# ASGI bridge -------------------------------------------------------------------


def test_bridge_get_post_redirect_and_partial(root):
    app = create_app(root, git=False, host=Host(label="browser · test", render_svg=fake_svg))
    r = run(call_asgi(app, "GET", "/m/greenhouse/threats", [], b""))
    assert r["status"] == 200 and b"TS_01" in r["body"]
    assert b"tmodel \xc2\xb7 browser \xc2\xb7 test" in r["body"]  # footer names the host
    # The renderer's text falls back to a sans font; the fixed size is dropped.
    assert b'font-family="DejaVu Sans, Verdana, sans-serif"' in r["body"] and b'width="10pt"' not in r["body"]

    form = "id=DOM_NEW&name=New+domain&scope=in"
    hdrs = [("Content-Type", "application/x-www-form-urlencoded")]
    r = run(call_asgi(app, "POST", "/m/greenhouse/domains/new", hdrs, form.encode()))
    assert r["status"] == 303 and ["location", "/m/greenhouse/domains?saved=DOM_NEW"] in r["headers"]
    assert "DOM_NEW" in (root / SAMPLE.name).read_text()

    r = run(call_asgi(app, "GET", "/m/greenhouse/domains/DOM_NEW/row", [("X-Inline", "1")], b""))
    assert r["status"] == 200 and r["body"].lstrip().startswith(b"<tr")

    r = run(call_asgi(app, "GET", "/m/greenhouse/export.yaml", [], b""))
    assert ["content-disposition", f'attachment; filename="{SAMPLE.name}"'] in r["headers"]

    r = run(call_asgi(app, "GET", "/m/nope/threats?x=%20y", [], b""))
    assert r["status"] == 404


def test_body_arrival_cannot_overwrite_another_request(root, monkeypatch):
    from starlette.requests import Request

    app = create_app(root, git=False, host=Host(render_svg=lambda _: None))
    store = app.state.store
    original = Request.form

    async def delayed_form(self, *args, **kwargs):
        form = await original(self, *args, **kwargs)
        snapshot = store.snapshot(SAMPLE.stem)
        model = snapshot.model
        model.system = "another request committed while body arrived"
        store.save(SAMPLE.stem, model, "concurrent", expected=snapshot.etag)
        return form

    monkeypatch.setattr(Request, "form", delayed_form)
    r = run(
        call_asgi(
            app,
            "POST",
            "/m/greenhouse/goals/CG_01",
            [("Content-Type", "application/x-www-form-urlencoded")],
            b"id=CG_01&name=late",
        )
    )
    assert r["status"] == 200 and b"file changed before this save" in r["body"]
    assert store.load(SAMPLE.stem).system == "another request committed while body arrived"
    assert store.load(SAMPLE.stem).goals[0].name != "late"


def test_browser_app_handle_takes_plain_values(root):
    """On CPython there are no JsProxy objects; handle() accepts plain lists/bytes."""
    seen = []
    app = BrowserApp(str(root), lambda dot: seen.append(dot) or fake_svg(dot), "browser · t")
    r = run(app.handle("GET", "/m/greenhouse/side", [["Accept", "*/*"]], None))
    assert r["status"] == 200 and b"<svg" in r["body"] and seen and seen[0].startswith("digraph")
    assert not (root / ".git").exists()  # git is always off in the browser


def test_browser_page_script_watches_build_json(root):
    app = create(str(root), fake_svg, "browser · t", watch_ms=1234)
    r = run(app.handle("GET", "/", [], None))
    assert b'"/_shell/build.json", every = 1234' in r["body"]
    assert b"window.tmodelFolder" in r["body"]  # folder mode (assets/folder.js), browser host only
    plain = create_app(root, git=False, host=Host(render_svg=fake_svg))
    assert b"tmodelFolder" not in run(call_asgi(plain, "GET", "/", [], b""))["body"]


def test_renderer_failure_never_breaks_a_page(root):
    def boom(dot):
        raise RuntimeError("no wasm")

    app = create_app(root, git=False, host=Host(render_svg=boom))
    r = run(call_asgi(app, "GET", "/m/greenhouse/threats", [], b""))
    assert r["status"] == 200 and b"<svg" not in r["body"]


# Dev live reload ---------------------------------------------------------------


def test_dev_reload_route_and_script_only_in_dev(root):
    plain = create_app(root, git=False, host=Host(render_svg=fake_svg))
    assert run(call_asgi(plain, "GET", "/__dev/boot", [], b""))["status"] == 404
    assert b"Live reload" not in run(call_asgi(plain, "GET", "/", [], b""))["body"]

    dev = create_app(root, git=False, host=Host(render_svg=fake_svg), dev=True)
    a = run(call_asgi(dev, "GET", "/__dev/boot", [], b""))["body"]
    assert run(call_asgi(dev, "GET", "/__dev/boot", [], b""))["body"] == a  # stable per process
    assert run(call_asgi(create_app(root, git=False, dev=True), "GET", "/__dev/boot", [], b""))["body"] != a
    page = run(call_asgi(dev, "GET", "/m/greenhouse/threats", [], b""))["body"]
    assert b'"/__dev/boot", every = 1000' in page


def test_dev_factory_reads_environment(root, monkeypatch):
    from tmodel.web.app import dev_factory

    monkeypatch.setenv("TMODEL_DIR", str(root))
    monkeypatch.setenv("TMODEL_GIT", "0")
    app = dev_factory()
    assert app.state.store.root == root.resolve() and app.state.store.git is False


# Static build --------------------------------------------------------------------


def test_dependency_plan_vendors_pure_and_defers_compiled():
    plan = static_build.plan_dependencies(REPO / "uv.lock")
    vendored = {w["name"] for w in plan["wheels"]}
    assert {"starlette", "python-multipart", "anyio"} <= vendored
    # compiled, or tied to a compiled package: Pyodide provides them
    assert {"pydantic", "pydantic-core", "pyyaml", "jinja2", "markupsafe"} <= set(plan["pyodide"])
    assert not vendored & set(plan["pyodide"])
    assert "uvicorn" not in plan["lock_versions"]  # server only
    assert all(w["url"].endswith("-none-any.whl") and w["hash"].startswith("sha256:") for w in plan["wheels"])


@pytest.fixture
def offline_wheels(tmp_path, monkeypatch):
    """No network in tests: stand-in wheels."""

    def fake(w):
        p = tmp_path / "wheels" / w["url"].rsplit("/", 1)[1]
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(b"wheel:" + w["name"].encode())
        return p

    monkeypatch.setattr(static_build, "fetch_wheel", fake)


def test_build_web_output(tmp_path, offline_wheels):
    out = tmp_path / "web"
    info = static_build.build(out, [REPO / "examples"], REPO / "uv.lock", REPO)
    shell = out / "_shell"
    assert (out / "index.html").exists() and (out / "_headers").exists()
    assert json.loads((shell / "build.json").read_text())["id"] == info["id"]
    sw = (out / "sw.js").read_text()
    assert "__PYODIDE__" not in sw and "__BUILD__" not in sw and static_build.PYODIDE_URL in sw
    assert f'"id": "{info["id"]}"' in sw
    assert sorted(info["wheels"]) == sorted(p.name for p in (shell / "wheels").iterdir())
    assert info["seed_models"] == sorted(p.name for p in (REPO / "examples").glob("*.yaml"))
    # The worker's own "open the folder again" page loads folder mode from the shell.
    assert '<script src="/_shell/folder.js">' in sw
    assert (shell / "folder.js").read_bytes() == (REPO / "tmodel/web/assets/folder.js").read_bytes()
    assert "/_shell/folder.js\n  Cache-Control: no-cache" in (out / "_headers").read_text()

    names = zipfile.ZipFile(shell / info["app"]).namelist()
    assert {
        "tmodel/web/browser.py",
        "tmodel/web/templates/base.html",
        "tmodel/web/assets/reload.js",
        "tmodel/web/assets/page.css",
        "tmodel/web/assets/page.js",
        "tmodel/web/routes/entities.py",
    } <= set(names)
    assert not any("__pycache__" in n for n in names)

    # The zipped package imports and serves on its own (as Pyodide will load it).
    extracted = tmp_path / "app"
    zipfile.ZipFile(shell / info["app"]).extractall(extracted)
    probe = (
        "import sys, asyncio; sys.path.insert(0, {!r})\n"
        "import tmodel.web.browser as b; assert b.__file__.startswith({!r}), b.__file__\n"
        "app = b.create({!r}, lambda d: None, 'browser')\n"
        "r = asyncio.run(app.handle('GET', '/', [], None)); print(r['status'])\n"
    ).format(str(extracted), str(extracted), str(REPO / "examples"))
    res = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, text=True)
    assert res.stdout.strip() == "200", res.stderr

    # Same content, same id: rebuilding without changes never reloads open pages.
    again = static_build.build(tmp_path / "web2", [REPO / "examples"], REPO / "uv.lock", REPO)
    assert again["id"] == info["id"]


def test_static_server_falls_back_to_the_shell(tmp_path, offline_wheels):
    out = tmp_path / "web"
    static_build.build(out, [], REPO / "uv.lock", REPO)
    srv = static_build.serve(out, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/m/greenhouse/threats") as r:
            deep = r.read()
        assert deep == (out / "index.html").read_bytes()  # first visit to a deep link boots the app
        with urllib.request.urlopen(base + "/sw.js") as sw:
            assert sw.headers["Content-Type"].startswith("text/javascript")
            assert sw.headers["Cache-Control"] == "no-store"
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/_shell/missing.zip")
        e.value.close()
        assert e.value.code == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_serve_live_reload_is_the_default():
    from tmodel.__main__ import parser

    assert parser().parse_args(["serve"]).reload is True
    assert parser().parse_args(["serve", "--no-reload"]).reload is False


def test_build_web_seeds_no_models_by_default(tmp_path, offline_wheels):
    """The published build must never carry models (they would be public)."""
    from tmodel.__main__ import main

    out = tmp_path / "web"
    assert main(["build-web", "-o", str(out)]) == 0
    info = json.loads((out / "_shell" / "build.json").read_text())
    assert info["seed_models"] == []
    assert zipfile.ZipFile(out / "_shell" / info["seed"]).namelist() == []
