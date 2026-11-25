"""
What every route shares: the store, the template environment, the host, and
the helpers that render pages, side panels, forms and table rows, load and
save models, and guard against stale edits.

One `Ctx` per app (create_app makes it; routes get it with `ctx(request)`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jinja2 import Environment
from pydantic import ValidationError
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse

from ..diagram import to_dot
from ..editing import allocated_ids, put_entity
from ..lint import lint
from ..schema import ThreatModel
from . import tables
from .forms import COLLECTIONS, collection_of, form_fields, next_id, typed_values, used_by
from .store import FileStore, Snapshot

if TYPE_CHECKING:  # app imports this module
    from .app import Host


def errors(e: ValidationError) -> list[str]:
    out = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err["loc"] if x != "__root__")
        out.append(f"{loc}: {err['msg']}" if loc else err["msg"])
    return out


def errors_of(err: Exception) -> list[str]:
    return errors(err) if isinstance(err, ValidationError) else [str(err)]


def etag(data) -> str:
    """Fingerprint of what a form was opened on, to refuse saving over a change
    made meanwhile (another tab, the YAML tab, an edit on disk)."""
    raw = data if isinstance(data, str) else json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


STALE = (
    "{what} changed since you opened this page (another tab, or an edit on disk). "
    "Your input is kept below. Reload to see the other change, or save again to overwrite it."
)


def fit_svg(svg: str | None) -> str | None:
    """Drop the fixed size so the diagram scales to its panel."""
    if not svg or "<svg" not in svg:
        return None
    svg = svg[svg.index("<svg") :]
    svg = re.sub(r'\s(width|height)="[^"]+"', "", svg, count=2)
    # Text left as <text> (not Cairo outlines) is drawn by the viewer's fonts: fall
    # back to Verdana (metrics close to DejaVu Sans, which phones lack), never serif.
    svg = re.sub(r'font-family="([^"]+)"', r'font-family="\1, Verdana, sans-serif"', svg)
    return svg.replace("<svg", '<svg style="max-width:100%;height:auto"', 1)


def ctx(request: Request) -> Ctx:
    return request.app.state.ctx


def href(mid: str, eid: str) -> str | None:
    """Full-form URL of any entity, from its ID prefix."""
    coll = collection_of(eid)
    return f"/m/{mid}/{coll}/{eid}" if coll else None


@dataclass
class Ctx:
    store: FileStore
    env: Environment
    host: Host

    # Rendering --------------------------------------------------------------

    def svg(self, model: ThreatModel, highlight: str | None = None) -> str | None:
        try:
            return fit_svg(self.host.render_svg(to_dot(model, highlight=highlight)))
        except Exception:  # a renderer failure must never break a page
            logging.getLogger(__name__).exception("Diagram rendering failed")
            return None

    def page(self, name: str, **ctx) -> HTMLResponse:
        return HTMLResponse(self.env.get_template(name).render(**ctx))

    def side(self, model: ThreatModel, highlight: str | None = None) -> dict:
        return {"svg": self.svg(model, highlight), "findings": lint(model), "highlight": highlight}

    def saved_note(self, model: ThreatModel, coll: str, eid: str) -> dict | None:
        """What to say after a save: derived values (risk) and the lint
        findings on the saved entity, while it is fresh in mind."""
        e = next((x for x in getattr(model, coll) if x.id == eid), None)
        if e is None:
            return None
        cls = COLLECTIONS[coll]
        derived = [
            f"{c} {tables.text(tables.cell(model, coll, e, c))}"
            for c in tables.DERIVED.get(coll, {})
            if c not in cls.model_fields
        ]
        findings = [f for f in lint(model) if f.entity == eid]
        text = " · ".join([f"Saved {eid}", *derived])
        toast = "\n".join([text, *(f"{f.level}: {f.message}" for f in findings[:3])])
        return {"text": text, "findings": findings, "toast": toast}

    # Loading ----------------------------------------------------------------

    def snapshot(self, mid: str) -> Snapshot:
        try:
            return self.store.snapshot(mid)
        except FileNotFoundError as e:
            raise HTTPException(404, f"no model {mid!r}") from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    def load(self, mid: str) -> ThreatModel:
        return self.snapshot(mid).model

    def entity_class(self, coll: str):
        if coll not in COLLECTIONS:
            raise HTTPException(404, f"no collection {coll!r}")
        return COLLECTIONS[coll]

    def find(self, model: ThreatModel, coll: str, eid: str):
        e = next((e for e in getattr(model, coll) if e.id == eid), None)
        if e is None:
            raise HTTPException(404, f"no {coll[:-1]} {eid!r}")
        return e

    def cells(self, model: ThreatModel, coll: str, e) -> list[str | list[tables.Ref]]:
        return [tables.cell(model, coll, e, c) for c in tables.columns(coll)]

    # Forms and saving -------------------------------------------------------

    def render_form(
        self, mid, model, coll, eid, instance, errors=(), form=None, copy_of=None
    ) -> HTMLResponse:
        """The full-page entity form, optionally re-filled with submitted values.
        `copy_of`: prefill a new entity from an existing one (Duplicate)."""
        cls = COLLECTIONS[coll]
        fields = form_fields(cls, model, instance or copy_of)
        if instance is None:  # new: suggest the next free ID, editable
            for f in fields:
                if f.name == "id":
                    f.value, f.readonly = next_id(cls, allocated_ids(model)), False
        if form is not None:
            typed_values(fields, form)
        return self.page(
            "form.html",
            mid=mid,
            model=model,
            coll=coll,
            eid=eid,
            fields=fields,
            errors=list(errors),
            singular=coll[:-1],
            copy_of=copy_of.id if copy_of else None,
            used_by=used_by(model, eid) if eid else [],
            etag=etag(instance.model_dump(mode="json")) if instance else "",
            **self.side(model, highlight=eid),
        )

    def check_stale(self, form, current, what: str) -> None:
        """Raise if the form was opened on a different version of `current`.
        A form without `_etag` (API clients, old pages) is not checked."""
        tag = form.get("_etag")
        if tag and tag != etag(current):
            raise ValueError(STALE.format(what=what))

    def save_entity(
        self, mid: str, model: ThreatModel, coll: str, eid: str | None, data: dict, *, expected: str
    ):
        """Validate `data` as a new/updated entity, re-validate the whole
        model (cross references), save. Raises ValidationError or ValueError."""
        new_model, entity = put_entity(model, coll, eid, data)
        self.store.save(mid, new_model, f"{mid}: {'update' if eid else 'add'} {entity.id}", expected=expected)
        return new_model, entity

    def root_scalars(self, model: ThreatModel) -> dict:
        return {k: v for k, v in model.model_dump(mode="json").items() if k not in COLLECTIONS}

    # Inline row editing (partials; the page script swaps <tr>s) -------------

    def row_view(self, mid: str, model: ThreatModel, coll: str, e, flash: bool = False) -> str:
        note = self.saved_note(model, coll, e.id) if flash else None
        return self.env.get_template("_row.html").render(
            mid=mid,
            coll=coll,
            eid=e.id,
            cells=self.cells(model, coll, e),
            cols=tables.columns(coll),
            flash=flash,
            toast=note["toast"] if note else "",
        )

    def row_edit(self, mid: str, model: ThreatModel, coll: str, e=None, errors=(), form=None) -> str:
        """Edit-mode <tr>. `e` None means a blank row for a new entity."""
        cls = COLLECTIONS[coll]
        cols = tables.columns(coll)
        specs = {f.name: f for f in form_fields(cls, model, e)}
        if e is None:
            specs["id"].value = next_id(cls, allocated_ids(model))
        if form is not None:
            typed_values(list(specs.values()), form)
        fields = [specs.get(c) for c in cols]  # None for derived-only columns
        derived = [tables.cell(model, coll, e, c) if e and c not in specs else "" for c in cols]
        return self.env.get_template("_row_edit.html").render(
            mid=mid,
            coll=coll,
            eid=e.id if e else "new",
            new=e is None,
            cols=cols,
            fields=fields,
            derived=derived,
            errors=list(errors),
            etag=etag(e.model_dump(mode="json")) if e else "",
        )

    # Model list -------------------------------------------------------------

    def index_page(self, error=None, imported=(), status=200) -> HTMLResponse:
        models = self.store.list_models()
        warns = {}
        for m in models:
            if not m.error:
                warns[m.id] = sum(f.level == "warn" for f in lint(self.store.load(m.id)))
        r = self.page("index.html", models=models, warns=warns, error=error, imported=list(imported))
        r.status_code = status
        return r
