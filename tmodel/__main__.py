from __future__ import annotations

import argparse
import json
import sys

from pydantic import ValidationError

from . import io
from .diagram import render
from .lint import lint
from .schema import ThreatModel


def _load_or_exit(path: str) -> ThreatModel:
    try:
        return io.load(path)
    except ValidationError as e:
        print(f"INVALID: {path}\n{e}", file=sys.stderr)
        sys.exit(1)


def _write_out(text: str, path: str | None) -> None:
    if path:
        with open(path, "w") as f:
            f.write(text)
    else:
        sys.stdout.write(text)


def cmd_check(a: argparse.Namespace) -> int:
    m = _load_or_exit(a.model)
    print(
        f"OK: {m.system} rev {m.revision} (schema {m.schema_version}) — "
        f"{len(m.domains)} domains, {len(m.surfaces)} surfaces, {len(m.assets)} assets, "
        f"{len(m.damages)} damages, {len(m.threats)} threats, "
        f"{len(m.goals)} goals"
    )
    findings = lint(m)
    for f in findings:
        if f.level == "info" and not a.info:
            continue
        print(f"  {f}")
    n_warn = sum(f.level == "warn" for f in findings)
    return 1 if (a.strict and n_warn) else 0


def cmd_export(a: argparse.Namespace) -> int:
    m = _load_or_exit(a.model)
    if a.format == "yaml":
        text = io.dumps(m)
    else:
        text = json.dumps(m.model_dump(mode="json", exclude_none=True), indent=2) + "\n"
    _write_out(text, a.out)
    return 0


def cmd_schema(a: argparse.Namespace) -> int:
    _write_out(json.dumps(ThreatModel.model_json_schema(), indent=2) + "\n", a.out)
    return 0


def cmd_draw(a: argparse.Namespace) -> int:
    m = _load_or_exit(a.model)
    render(m, a.out, threats_on_edges=not a.no_threats)
    print(f"wrote {a.out}")
    return 0


def cmd_serve(a: argparse.Namespace) -> int:
    import os
    from pathlib import Path

    import uvicorn

    from .web.app import create_app

    git = True if a.git else (False if a.no_git else None)
    url = f"http://{a.host}:{a.port}/"
    if a.open:
        import webbrowser

        webbrowser.open(url)
    if not a.reload:
        app = create_app(a.dir, git=git)
        print(f"tmodel: models in {app.state.store.root}  git={'on' if app.state.store.git else 'off'}")
        print(f"tmodel: {url}")
        uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
        return 0
    # Default: uvicorn runs the app in a worker process and restarts it on any
    # change under tmodel/ (code or templates); open pages poll /__dev/boot and
    # reload themselves. The worker builds the app, so the arguments travel in
    # the environment (see web/app.py:dev_factory).
    os.environ.update(TMODEL_DIR=str(Path(a.dir).resolve()), TMODEL_GIT={True: "1", False: "0"}.get(git, ""))
    print(f"tmodel: models in {Path(a.dir).resolve()}  git={ {True: 'on', False: 'off'}.get(git, 'auto') }")
    print(f"tmodel: {url}  (live reload on changes in {Path(__file__).parent}; --no-reload to turn off)")
    try:
        import watchfiles  # noqa: F401
    except ImportError:
        print("tmodel: without watchfiles only .py edits reload (uv sync installs it with the dev group)")
    uvicorn.run(
        "tmodel.web.app:dev_factory",
        factory=True,
        host=a.host,
        port=a.port,
        reload=True,
        reload_dirs=[str(Path(__file__).parent)],
        reload_includes=["*.py", "*.html", "*.js", "*.css"],
        log_level="warning",
    )
    return 0


def cmd_build_web(a: argparse.Namespace) -> int:
    from pathlib import Path

    from .web import static_build

    repo = Path(__file__).resolve().parent.parent
    if not (repo / "uv.lock").exists():
        print("tmodel: build-web runs from a checkout of the repo (it pins dependencies from uv.lock)")
        return 2
    # Nothing is seeded unless asked: the build is published (Cloudflare) and
    # everything in it is downloadable, so models only go in on purpose.
    seeds = a.seed or []

    def rebuild() -> dict:
        return static_build.build(Path(a.out), seeds, repo / "uv.lock", repo)

    info = rebuild()
    seeded = ", ".join(info["seed_models"]) or "no models seeded"
    print(f"tmodel: built {info['id']} ({info['git']}) into {a.out}: {seeded}")
    if a.serve or a.watch:
        static_build.serve(Path(a.out), a.host, a.port)
        print(f"tmodel: http://{a.host}:{a.port}/  (the in-browser build; needs internet for Pyodide)")
        if a.watch:
            static_build.watch(rebuild, [repo / "tmodel"])
        else:
            import time

            while True:
                time.sleep(3600)
    return 0


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    return a.fn(a)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tmodel", description="Threat model tooling.")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="validate and lint a model")
    c.add_argument("model")
    c.add_argument("--info", action="store_true", help="also show info-level findings")
    c.add_argument("--strict", action="store_true", help="exit 1 on any warning")
    c.set_defaults(fn=cmd_check)

    e = sub.add_parser("export", help="normalise to YAML, or dump JSON for agents")
    e.add_argument("model")
    e.add_argument("-f", "--format", choices=["yaml", "json"], default="yaml")
    e.add_argument("-o", "--out")
    e.set_defaults(fn=cmd_export)

    s = sub.add_parser("schema", help="export JSON Schema for editors and agents")
    s.add_argument("-o", "--out")
    s.set_defaults(fn=cmd_schema)

    d = sub.add_parser("draw", help="render the domain/surface diagram")
    d.add_argument("model")
    d.add_argument("out", nargs="?", default="threat-model.svg", help=".svg/.png/.pdf/.dot")
    d.add_argument("--no-threats", action="store_true", help="hide threat IDs")
    d.set_defaults(fn=cmd_draw)

    v = sub.add_parser("serve", help="run the web editor on localhost")
    v.add_argument(
        "dir", nargs="?", default="models", help="directory of model .yaml files (default: ./models)"
    )
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--port", type=int, default=8765)
    g = v.add_mutually_exclusive_group()
    g.add_argument("--git", action="store_true", help="auto-commit every save (git init if needed)")
    g.add_argument("--no-git", action="store_true", help="never commit, even inside a repo")
    v.add_argument("--open", action="store_true", help="open the browser")
    v.add_argument(
        "--reload",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="restart on code/template changes and reload open pages (default: on)",
    )
    v.set_defaults(fn=cmd_serve)

    b = sub.add_parser("build-web", help="static site that runs the editor entirely in the browser")
    b.add_argument("-o", "--out", default="dist/web")
    b.add_argument(
        "--seed",
        action="append",
        type=str,
        help="model dir copied into a fresh browser; local testing only, never for a "
        "published build (default: none)",
    )
    b.add_argument("--serve", action="store_true", help="serve the build locally")
    b.add_argument("--watch", action="store_true", help="serve, and rebuild on changes (pages reload)")
    b.add_argument("--host", default="127.0.0.1")
    b.add_argument("--port", type=int, default=8766)
    b.set_defaults(fn=cmd_build_web)
    return p


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:  # e.g. `tmodel check x.yaml | head`
        sys.exit(0)
