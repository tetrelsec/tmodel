"""
What a table shows: its columns, the text or links in each cell, and how
rows sort. Columns come from the Pydantic class (forms.table_columns) unless
overridden here.

TABLE_COLUMNS, DERIVED and GROUP are the only hand-written UI knowledge of
specific fields (see CLAUDE.md): optional overrides for how a table looks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..schema import ThreatModel
from .forms import COLLECTIONS, classify, label_of, ref_kind, table_columns

# Optional overrides; any collection not listed gets columns derived from its
# Pydantic class. Derived (computed) columns are declared in DERIVED.
TABLE_COLUMNS = {
    "threats": ["id", "description", "damage", "surface", "vector", "feasibility", "risk", "treatment"],
}
DERIVED = {
    "threats": {
        "risk": lambda m, e: str(m.risk_of(e)),
        "vector": lambda m, e: m.vector_of(e).value + ("" if e.vector else "\u00a0·"),
    },
}

# Column a table is sectioned by when the URL does not say (?group=col, or
# ?group= for none). Any single-reference column can be grouped on.
GROUP = {"threats": "damage"}


def columns(coll: str) -> list[str]:
    return TABLE_COLUMNS.get(coll) or table_columns(COLLECTIONS[coll])


def groupable(coll: str, col: str) -> bool:
    """A single reference (a damage's asset, a threat's damage) can section the
    table under one header per referenced entity."""
    k = ref_kind(COLLECTIONS[coll], col) if col not in DERIVED.get(coll, {}) else None
    return k is not None and not k.many


@dataclass
class Ref:
    """A reference shown in a table cell: linked ID, full label as a tooltip,
    and a short label beside the ID for single references."""

    id: str
    label: str
    short: str = ""


def cell(model: ThreatModel, coll: str, e, col: str) -> str | list[Ref]:
    fn = DERIVED.get(coll, {}).get(col)
    if fn:
        return fn(model, e)
    v = getattr(e, col, "")
    k = ref_kind(COLLECTIONS[coll], col)
    if k is not None:
        ids = [x for x in (v if k.many else [v]) if x]
        return [
            Ref(x, label_of(model, x), "" if k.many else label_of(model, x, 32))
            if x not in k.literals
            else Ref(x, "")
            for x in ids
        ]
    if hasattr(v, "value"):
        return v.value
    if isinstance(v, list):
        return ", ".join(str(getattr(x, "value", x)) for x in v)
    if hasattr(v, "model_dump") or isinstance(v, dict):
        return "…"
    return "" if v is None else str(v)


def text(v: str | list[Ref]) -> str:
    """Plain text of a cell (filtering, sorting, CSV)."""
    return v if isinstance(v, str) else ", ".join(r.id for r in v)


def natural(s: str) -> list:
    """Sort key that orders TS_2 before TS_10 and "4" after "10" never."""
    return [int(t) if t.isdigit() else t.casefold() for t in re.split(r"(\d+)", s)]


def sort_key(coll: str, col: str):
    """Key for sorting rows by a column: enum columns in their declared order
    (Negligible < Severe), everything else naturally."""
    fi = COLLECTIONS[coll].model_fields.get(col)
    k = classify(fi.annotation, fi.metadata) if fi else None
    order = [m.value for m in k.enum] if k and k.enum and not k.many else []

    def key(t: str):
        t = t.rstrip("\u00a0· ")
        return (0, order.index(t), []) if t in order else (1, 0, natural(t))

    return key
