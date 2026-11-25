"""Downloads: a model as YAML or JSON, one table as CSV, the threat register as CSV."""

from __future__ import annotations

import csv
from io import StringIO

import yaml
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ... import io
from ...report import register_table
from .. import tables
from ..context import ctx
from ..forms import classify, steps_to_text


def yaml_dump(data) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True).strip()


def csv_value(v) -> str:
    if v is None:
        return ""
    if hasattr(v, "value"):
        return v.value
    if isinstance(v, list):
        if v and hasattr(v[0], "model_dump"):
            return yaml_dump([x.model_dump(mode="json", exclude_none=True) for x in v])
        return "; ".join(str(getattr(x, "value", x)) for x in v)
    if hasattr(v, "model_dump") or isinstance(v, dict):
        return yaml_dump(v.model_dump(mode="json") if hasattr(v, "model_dump") else v)
    return str(v)


def spreadsheet_safe(value: str) -> str:
    """Quoting CSV doesn't stop Excel/LibreOffice interpreting formulas."""
    if value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


def csv_response(header: list[str], rows: list[list[str]], filename: str) -> Response:
    buf = StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows([[spreadsheet_safe(v) for v in row] for row in rows])
    # BOM: Excel opens UTF-8 CSV correctly only with it.
    return Response(
        "\ufeff" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def export(request: Request):
    c = ctx(request)
    mid, fmt = request.path_params["mid"], request.path_params["fmt"]
    model = c.load(mid)
    if fmt == "yaml":
        return Response(
            io.dumps(model),
            media_type="application/yaml",
            headers={"Content-Disposition": f'attachment; filename="{mid}.yaml"'},
        )
    if fmt == "json":
        return JSONResponse(model.model_dump(mode="json", exclude_none=True))
    raise HTTPException(404, "yaml or json")


async def export_csv(request: Request):
    """One collection as CSV: every field, plus derived columns (risk)."""
    c = ctx(request)
    mid, name = request.path_params["mid"], request.path_params["name"]
    coll = name.removesuffix(".csv")
    if not name.endswith(".csv"):
        raise HTTPException(404, "collection.csv")
    cls = c.entity_class(coll)
    model = c.load(mid)
    fields = list(cls.model_fields)
    extra = [col for col in tables.DERIVED.get(coll, {}) if col not in cls.model_fields]
    kinds = {n: classify(fi.annotation, fi.metadata) for n, fi in cls.model_fields.items()}
    rows = []
    for e in getattr(model, coll):
        row = []
        for n in fields:
            v = getattr(e, n)
            row.append(steps_to_text(v) if kinds[n].base == "steps" else csv_value(v))
        rows.append(row + [tables.text(tables.cell(model, coll, e, col)) for col in extra])
    return csv_response(fields + extra, rows, f"{mid}-{coll}.csv")


async def report_csv(request: Request):
    c = ctx(request)
    mid = request.path_params["mid"]
    header, rows = register_table(c.load(mid))
    return csv_response(header, rows, f"{mid}-threat-register.csv")
