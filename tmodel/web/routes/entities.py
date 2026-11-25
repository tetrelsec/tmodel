"""A collection's table, the full entity form, delete, the inline-editing
<tr> partials, and the side panel (diagram + lint) the page script refreshes."""

from __future__ import annotations

from urllib.parse import urlencode

import yaml
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from ...editing import delete_entity
from .. import tables
from ..context import ctx, errors, errors_of
from ..forms import parse_form, used_by


async def model_root(request: Request):
    return RedirectResponse(f"/m/{request.path_params['mid']}/domains", status_code=303)


async def table(request: Request):
    c = ctx(request)
    mid, coll = request.path_params["mid"], request.path_params["coll"]
    c.entity_class(coll)
    try:
        model = c.load(mid)
    except (ValidationError, yaml.YAMLError):  # hand-edited invalid file: repair in the YAML tab
        return RedirectResponse(f"/m/{mid}/yaml", status_code=303)
    cols = tables.columns(coll)
    qp = request.query_params
    q = qp.get("q", "").strip()
    sort = qp.get("sort") if qp.get("sort") in cols else None
    desc = qp.get("dir") == "desc"
    group = qp.get("group", tables.GROUP.get(coll, ""))
    group = group if group in cols and tables.groupable(coll, group) else None
    rows = [(e.id, c.cells(model, coll, e)) for e in getattr(model, coll)]
    total = len(rows)
    if q:  # every word must appear somewhere in the row
        words = q.casefold().split()
        rows = [r for r in rows if all(w in " ".join(map(tables.text, r[1])).casefold() for w in words)]
    if sort:
        i, key = cols.index(sort), tables.sort_key(coll, sort)
        rows.sort(key=lambda r: key(tables.text(r[1][i])), reverse=desc)
    if group:  # sections in ID order (or reversed when sorting on them), sort order inside each
        i, key = cols.index(group), tables.sort_key(coll, group)
        rows.sort(key=lambda r: key(tables.text(r[1][i])), reverse=desc and sort == group)

    def url(**change) -> str:
        """This table's URL with the current q / sort / dir / group, some changed."""
        p = {"q": q, "sort": sort or "", "dir": "desc" if desc else "", "group": group or ""} | change
        p = {k: v for k, v in p.items() if v} | (
            {"group": ""} if not p["group"] and tables.GROUP.get(coll) else {}
        )
        return f"/m/{mid}/{coll}" + (f"?{urlencode(p)}" if p else "")

    saved = qp.get("saved")
    return c.page(
        "model.html",
        mid=mid,
        model=model,
        coll=coll,
        cols=cols,
        rows=rows,
        total=total,
        q=q,
        sort=sort,
        desc=desc,
        group=group,
        gi=cols.index(group) if group else None,
        groupable=[x for x in cols if tables.groupable(coll, x)],
        url=url,
        saved=saved,
        note=c.saved_note(model, coll, saved) if saved else None,
        **c.side(model),
    )


async def entity_form(request: Request):
    c = ctx(request)
    mid, coll = request.path_params["mid"], request.path_params["coll"]
    eid = request.path_params.get("eid")
    cls = c.entity_class(coll)
    snapshot = c.snapshot(mid)
    model = snapshot.model
    instance = c.find(model, coll, eid) if eid else None
    if request.method == "GET":
        src = request.query_params.get("from")
        copy_of = c.find(model, coll, src) if src and not eid else None
        return c.render_form(mid, model, coll, eid, instance, copy_of=copy_of)

    form = await request.form()
    try:
        if instance is not None:
            c.check_stale(form, instance.model_dump(mode="json"), eid)
        _, entity = c.save_entity(mid, model, coll, eid, parse_form(cls, form), expected=snapshot.etag)
    except (ValidationError, ValueError) as err:
        return c.render_form(mid, model, coll, eid, instance, errors_of(err), form)
    return RedirectResponse(f"/m/{mid}/{coll}?saved={entity.id}", status_code=303)


async def entity_delete(request: Request):
    c = ctx(request)
    mid, coll, eid = (request.path_params[k] for k in ("mid", "coll", "eid"))
    c.entity_class(coll)
    snapshot = c.snapshot(mid)
    model = snapshot.model
    instance = c.find(model, coll, eid)
    users = used_by(model, eid)
    if users:
        refs = ", ".join(f"{u} ({field})" for u, field in users)
        return c.render_form(
            mid,
            model,
            coll,
            eid,
            instance,
            [f"cannot delete {eid}: referenced by {refs}. Remove those first."],
        )
    try:
        new_model = delete_entity(model, coll, eid)
    except ValidationError as e:
        return c.render_form(mid, model, coll, eid, instance, ["cannot delete: " + "; ".join(errors(e))])
    c.store.save(mid, new_model, f"{mid}: delete {eid}", expected=snapshot.etag)
    return RedirectResponse(f"/m/{mid}/{coll}", status_code=303)


# Inline row editing (partials; the page script swaps <tr>s) -----------------


async def row(request: Request):
    """GET: view row (or a blank edit row when eid is 'new').
    POST: save the table-column fields; eid 'new' creates."""
    c = ctx(request)
    mid, coll, eid = (request.path_params[k] for k in ("mid", "coll", "eid"))
    cls = c.entity_class(coll)
    snapshot = c.snapshot(mid)
    model = snapshot.model
    new = eid == "new"
    e = None if new else c.find(model, coll, eid)
    if request.method == "GET":
        return HTMLResponse(c.row_edit(mid, model, coll) if new else c.row_view(mid, model, coll, e))

    form = await request.form()
    inline = request.headers.get("x-inline") == "1"
    editable = [col for col in tables.columns(coll) if col in cls.model_fields and (new or col != "id")]
    data = {} if new else e.model_dump(mode="json")
    try:
        parsed = parse_form(cls, form, only=editable)
        for name in editable:  # a cleared optional goes back to its default
            if name in parsed:
                data[name] = parsed[name]
            else:
                data.pop(name, None)
        if not new:
            c.check_stale(form, e.model_dump(mode="json"), eid)
        new_model, saved = c.save_entity(mid, model, coll, None if new else eid, data, expected=snapshot.etag)
    except (ValidationError, ValueError) as err:
        if inline:
            return HTMLResponse(c.row_edit(mid, model, coll, e, errors_of(err), form), status_code=422)
        return c.render_form(mid, model, coll, None if new else eid, e, errors_of(err), form)
    if inline:
        return HTMLResponse(c.row_view(mid, new_model, coll, saved, flash=True))
    return RedirectResponse(f"/m/{mid}/{coll}?saved={saved.id}", status_code=303)


async def row_edit_view(request: Request):
    c = ctx(request)
    mid, coll, eid = (request.path_params[k] for k in ("mid", "coll", "eid"))
    c.entity_class(coll)
    model = c.load(mid)
    return HTMLResponse(c.row_edit(mid, model, coll, None if eid == "new" else c.find(model, coll, eid)))


async def side_panel(request: Request):
    c = ctx(request)
    mid = request.path_params["mid"]
    model = c.load(mid)
    return HTMLResponse(c.env.get_template("_side.html").render(mid=mid, **c.side(model)))
