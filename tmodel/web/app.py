"""
Starlette app. Every page is plain HTML that works without JavaScript; the
page script (assets/page.js) adds inline row editing by swapping <tr> partials.

This module is the entry point and the URL table. Handlers live in routes/
(one module per area), shared helpers in context.py (`Ctx`), and what tables
show in tables.py.

Routes                                                         handler module
  GET  /                              model list               models
  POST /models                        create model             models
  POST /models/import                 upload .yaml files or a .zip of them
  GET  /models.zip                    every model file, verbatim
  GET  /m/{id}                        redirect to first collection   entities
  GET  /m/{id}/{coll}                 table of entities + side panel; ?q= filters, ?sort=&dir= sorts
  GET  /m/{id}/{coll}/new             new-entity form; ?from=ID prefills from an existing entity
  POST /m/{id}/{coll}/new
  GET  /m/{id}/{coll}/{eid}           edit form
  POST /m/{id}/{coll}/{eid}
  POST /m/{id}/{coll}/{eid}/delete
  GET  /m/{id}/{coll}/{eid}/row       view-mode <tr> partial
  GET  /m/{id}/{coll}/{eid}/row/edit  edit-mode <tr> partial (table columns only)
  POST /m/{id}/{coll}/{eid}/row       save; X-Inline: 1 returns a <tr>, else redirects
  GET  /m/{id}/{coll}/new/row         blank edit row; POST there creates
  GET  /m/{id}/side                   diagram + lint partial
  GET  /m/{id}/report                 heat map + threat register; ?impact=&feasibility= picks a cell   pages
  GET  /m/{id}/go?id=                 jump to an entity by ID, else search every entity's text
  GET  /m/{id}/yaml   POST            raw YAML (comments preserved)
  GET  /m/{id}/settings  POST         root fields (system, revision, ...); git history
  POST /m/{id}/copy                   new model from this one's file
  GET  /m/{id}/diagram.svg            ?highlight=ID marks an entity (a threat: its whole path)
  GET  /m/{id}/export.{yaml|json}                              exports
  GET  /m/{id}/export/{coll}.csv
  GET  /m/{id}/report.csv             threat register as CSV
  GET  /m/{id}/history/{sha}          diff of one commit (git only)   history
  POST /m/{id}/history/{sha}/restore  write that version as a new commit
  GET  /api/models                    JSON, for agents         models
  GET  /api/models/{id}
  PUT  /api/models/{id}
  GET  /__dev/boot                    dev only: changes on every restart (page live reload)

The app runs on two hosts with identical routes and templates: uvicorn
(`tmodel serve`) and Pyodide in a service worker (`tmodel build-web`, see
browser.py). What differs is passed in as a `Host`.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from .context import Ctx, href
from .forms import COLLECTIONS
from .guards import RequestGuards
from .routes import api, entities, exports, history, models, pages
from .store import ConflictError, FileStore, HistoryError

HERE = Path(__file__).parent


def dot_svg(dot: str) -> str | None:
    """Graphviz `dot` binary, Cairo renderer (text measured and drawn with the real font)."""
    if not shutil.which("dot"):
        return None
    r = subprocess.run(["dot", "-Tsvg:cairo"], input=dot.encode(), capture_output=True, timeout=5)
    if r.returncode:
        logging.getLogger(__name__).warning("Graphviz failed: %s", r.stderr.decode(errors="replace")[:500])
        return None
    return r.stdout.decode()


@contextmanager
def server_lock(root: Path):
    """Serialize writers across uvicorn processes; the OS releases it on crashes."""
    import fcntl

    with (root / ".tmodel.lock").open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


@dataclass
class Host:
    """What differs between the server and the in-browser build. Everything else
    (routes, templates, store) is shared."""

    label: str = "server"
    guard: Callable[[Path], AbstractContextManager] = server_lock
    durable: bool = True
    render_svg: Callable[[str], str | None] = dot_svg  # DOT source -> SVG text, or None
    script: str = ""  # extra JS on every page (live reload, service-worker keep-alive)


def live_reload(url: str, every_ms: int) -> str:
    """Page script that reloads when the body of `url` changes (see assets/reload.js)."""
    js = (HERE / "assets" / "reload.js").read_text()
    return js.replace("__WATCH_URL__", url).replace("__WATCH_MS__", str(every_ms))


def create_app(root: Path, git: bool | None = None, host: Host | None = None, dev: bool = False) -> Starlette:
    host = host or Host()
    store = FileStore(root, git=git, guard=host.guard, durable=host.durable)
    boot_id = uuid.uuid4().hex
    script = host.script
    if dev:
        script += "\n" + live_reload("/__dev/boot", 1000)
    env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html"]))
    env.globals.update(
        collections=list(COLLECTIONS),
        git=store.git,
        host=host,
        host_script=script,
        href=href,
        # Inlined into every page (no extra requests: in the browser build each one goes through Python).
        page_css=(HERE / "assets" / "page.css").read_text(),
        page_js=(HERE / "assets" / "page.js").read_text(),
    )

    async def dev_boot(request: Request):
        return PlainTextResponse(boot_id, headers={"Cache-Control": "no-store"})

    # Order matters: specific paths before /m/{mid}/{coll}, which would match them.
    routes = [
        *([Route("/__dev/boot", dev_boot)] if dev else []),
        Route("/", models.index),
        Route("/models", models.create_model, methods=["POST"]),
        Route("/models/import", models.import_models, methods=["POST"]),
        Route("/models.zip", models.export_all),
        Route("/api/validate", api.validate, methods=["POST"]),
        Route("/api/models", api.models),
        Route("/api/models/{mid}", api.model_get),
        Route("/api/models/{mid}", api.model_put, methods=["PUT"]),
        Route("/api/models/{mid}/snapshot", api.snapshot_get),
        Route("/api/models/{mid}/{coll}", api.entity_write, methods=["POST"]),
        Route("/api/models/{mid}/{coll}/{eid}", api.entity_write, methods=["PUT", "DELETE"]),
        Route("/m/{mid}", entities.model_root),
        Route("/m/{mid}/yaml", pages.yaml_page, methods=["GET", "POST"]),
        Route("/m/{mid}/settings", pages.settings, methods=["GET", "POST"]),
        Route("/m/{mid}/copy", pages.copy_model, methods=["POST"]),
        Route("/m/{mid}/history/{sha}", history.history_view),
        Route("/m/{mid}/history/{sha}/restore", history.history_restore, methods=["POST"]),
        Route("/m/{mid}/go", pages.go),
        Route("/m/{mid}/report.csv", exports.report_csv),
        Route("/m/{mid}/export/{name}", exports.export_csv),
        Route("/m/{mid}/diagram.svg", pages.diagram_svg),
        Route("/m/{mid}/export.{fmt}", exports.export),
        Route("/m/{mid}/side", entities.side_panel),
        Route("/m/{mid}/report", pages.report),
        Route("/m/{mid}/{coll}", entities.table),
        Route("/m/{mid}/{coll}/{eid}/row", entities.row, methods=["GET", "POST"]),
        Route("/m/{mid}/{coll}/{eid}/row/edit", entities.row_edit_view),
        Route("/m/{mid}/{coll}/new", entities.entity_form, methods=["GET", "POST"]),
        Route("/m/{mid}/{coll}/{eid}", entities.entity_form, methods=["GET", "POST"]),
        Route("/m/{mid}/{coll}/{eid}/delete", entities.entity_delete, methods=["POST"]),
    ]

    async def history_error(request: Request, exc: HistoryError):
        logging.getLogger(__name__).error("Model saved without git history", exc_info=exc)
        return PlainTextResponse(str(exc), status_code=503)

    async def conflict_error(request: Request, exc: ConflictError):
        return PlainTextResponse(str(exc), status_code=409)

    app = Starlette(
        routes=routes,
        middleware=[Middleware(RequestGuards)],
        exception_handlers={HistoryError: history_error, ConflictError: conflict_error},
    )
    app.state.store = store
    app.state.ctx = Ctx(store=store, env=env, host=host)
    return app


def dev_factory() -> Starlette:
    """`tmodel serve` entry point (live reload, the default): uvicorn re-imports this in each
    worker process, so the CLI's arguments travel in the environment."""
    git = {"1": True, "0": False}.get(os.environ.get("TMODEL_GIT", ""))
    return create_app(Path(os.environ.get("TMODEL_DIR", "models")), git=git, dev=True)
