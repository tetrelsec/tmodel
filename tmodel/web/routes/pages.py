"""Model pages other than the tables: YAML, settings (and copy), report,
diagram, and search / jump to an ID."""

from __future__ import annotations

import re

from pydantic import ValidationError
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from ... import io
from ...report import FEASIBILITIES, IMPACTS, damage_groups, goal_mappings, heatmap, register
from ...schema import ThreatModel
from ..context import ctx, errors, etag, href
from ..forms import COLLECTIONS, label_of, parse_form, root_fields, typed_values
from ..store import MODEL_ID
from .models import slug


async def yaml_page(request: Request):
    c = ctx(request)
    mid = request.path_params["mid"]
    if not c.store.exists(mid):
        raise HTTPException(404, f"no model {mid!r}")
    errs: list[str] = []
    snapshot = c.snapshot(mid)
    text = snapshot.text
    current = text
    if request.method == "POST":
        form = await request.form()
        text = form.get("text", "")
        try:
            c.check_stale(form, current, "The file")
            c.store.save_text(mid, text, f"{mid}: edit yaml", expected=snapshot.etag)
            return RedirectResponse(f"/m/{mid}/yaml?saved=1", status_code=303)
        except ValidationError as e:
            errs = errors(e)
        except Exception as e:  # yaml syntax, or changed meanwhile
            errs = [str(e)]
    try:
        model = io.loads(text)
        side = c.side(model)
    except Exception:
        model, side = None, {"svg": None, "findings": []}
    return c.page(
        "yaml.html",
        mid=mid,
        model=model,
        text=text,
        errors=errs,
        saved=request.query_params.get("saved"),
        etag=etag(current),
        **side,
    )


async def settings(request: Request):
    c = ctx(request)
    mid = request.path_params["mid"]
    snapshot = c.snapshot(mid)
    model = snapshot.model
    errs: list[str] = []
    opened = c.root_scalars(model)
    form = None
    if request.method == "POST":
        form = await request.form()
        full = model.model_dump(mode="json")
        try:
            names = [n for n in ThreatModel.model_fields if n not in COLLECTIONS and n != "schema_version"]
            data = parse_form(ThreatModel, form, only=names)
            # A scalar left empty goes back to its default.
            for name in names:
                if name in data:
                    full[name] = data[name]
                else:
                    full.pop(name, None)
            c.check_stale(form, opened, "Settings")
            model = ThreatModel.model_validate(full)
            c.store.save(mid, model, f"{mid}: settings (rev {model.revision})", expected=snapshot.etag)
            return RedirectResponse(f"/m/{mid}/settings?saved=1", status_code=303)
        except (ValidationError, ValueError) as e:
            errs = errors(e) if isinstance(e, ValidationError) else [str(e)]
            model = snapshot.model
    fields = root_fields(model)
    if form is not None:
        typed_values(fields, form)
    return c.page(
        "settings.html",
        mid=mid,
        model=model,
        errors=errs,
        fields=fields,
        history=c.store.history(mid),
        saved=request.query_params.get("saved"),
        etag=etag(opened),
        copy_error=request.query_params.get("copy_error"),
        **c.side(model),
    )


async def copy_model(request: Request):
    """Start a new model from this one, verbatim (comments kept)."""
    c = ctx(request)
    mid = request.path_params["mid"]
    c.load(mid)
    form = await request.form()
    new = slug(form.get("id") or "")
    try:
        if not MODEL_ID.match(new):
            raise ValueError(f"bad model id {new!r}: lower-case letters, digits, - and _")
        if c.store.exists(new):
            raise FileExistsError(f"{new} already exists")
        c.store.save_text(new, c.store.read_text(mid), f"{new}: copy of {mid}", expected="")
    except (ValueError, FileExistsError, ValidationError) as e:
        return RedirectResponse(f"/m/{mid}/settings?copy_error={e}", status_code=303)
    return RedirectResponse(f"/m/{new}/settings", status_code=303)


async def report(request: Request):
    c = ctx(request)
    mid = request.path_params["mid"]
    model = c.load(mid)
    q = request.query_params
    impact = next((i for i in IMPACTS if i.value == q.get("impact")), None)
    feasibility = next((f for f in FEASIBILITIES if f.value == q.get("feasibility")), None)
    return c.page(
        "report.html",
        mid=mid,
        model=model,
        grid=heatmap(model),
        feasibilities=FEASIBILITIES,
        entries=register(model, impact, feasibility),
        damage_groups=damage_groups(model),
        goal_mappings=goal_mappings(model),
        impact=impact,
        feasibility=feasibility,
        svg=c.svg(model),
    )


async def diagram_svg(request: Request):
    c = ctx(request)
    svg = c.svg(c.load(request.path_params["mid"]), request.query_params.get("highlight"))
    if svg is None:
        return PlainTextResponse("graphviz 'dot' not installed", 503)
    return Response(svg, media_type="image/svg+xml")


# Jump to an ID / search -----------------------------------------------------


def _strings(v) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict):
        return [s for x in v.values() for s in _strings(x)]
    if isinstance(v, list):
        return [s for x in v for s in _strings(x)]
    return []


async def go(request: Request):
    """Jump to an entity by ID (tolerant: `ts3` finds TS_03), else list
    every entity whose text contains the query."""
    c = ctx(request)
    mid = request.path_params["mid"]
    model = c.load(mid)
    raw = request.query_params.get("id", "").strip()
    if not raw:
        return RedirectResponse(f"/m/{mid}", status_code=303)
    key = re.sub(r"[\s-]+", "_", raw.upper())
    ids = [e.id for coll in COLLECTIONS for e in getattr(model, coll)]
    exact = [i for i in ids if i == key]
    m = re.fullmatch(r"([A-Z]+)_?0*(\d+)", key)
    loose = [i for i in ids if m and re.fullmatch(rf"{m[1]}_0*{m[2]}", i)]
    words = raw.casefold().split()
    hits = [
        (e.id, coll, label_of(model, e.id, 90))
        for coll in COLLECTIONS
        for e in getattr(model, coll)
        if all(w in " ".join(_strings(e.model_dump(mode="json"))).casefold() for w in words)
    ]
    for found in (exact, loose, [h[0] for h in hits]):
        if len(found) == 1:
            return RedirectResponse(href(mid, found[0]), status_code=303)
    return c.page("go.html", mid=mid, model=model, query=raw, hits=hits, coll="go", **c.side(model))
