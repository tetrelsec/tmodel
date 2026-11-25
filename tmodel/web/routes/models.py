"""The model list: create, import, and download all."""

from __future__ import annotations

import re
import zipfile
from io import BytesIO
from pathlib import Path

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from ..context import ctx, errors_of
from ..guards import MAX_BODY
from ..store import MODEL_ID


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower()).strip("-_")


async def index(request: Request):
    c = ctx(request)
    q = request.query_params
    return c.index_page(q.get("error"), [x for x in q.get("imported", "").split(",") if x])


async def create_model(request: Request):
    c = ctx(request)
    form = await request.form()
    mid = (form.get("id") or "").strip().lower().replace(" ", "-")
    system = (form.get("system") or "").strip() or mid
    try:
        c.store.create(mid, system)
    except (ValueError, FileExistsError) as e:
        return RedirectResponse(f"/?error={type(e).__name__}: {e}", status_code=303)
    return RedirectResponse(f"/m/{mid}/domains", status_code=303)


def zip_models(data: bytes, *, max_models: int = 100, max_bytes: int = MAX_BODY) -> list[tuple[str, bytes]]:
    """Preflight expansion bounds before decompressing anything."""
    with zipfile.ZipFile(BytesIO(data)) as zf:
        entries = zf.infolist()
        models = [
            e
            for e in entries
            if e.filename.lower().endswith((".yaml", ".yml")) and "__MACOSX" not in e.filename
        ]
        if len(entries) > 1000 or len(models) > max_models or sum(e.file_size for e in models) > max_bytes:
            raise ValueError("zip exceeds 100 models, 1000 entries, or 8 MiB expanded YAML")
        return [(e.filename, zf.read(e)) for e in models]


async def import_models(request: Request):
    """Upload .yaml files, or a .zip of them (as /models.zip makes). Each
    becomes a model named after its file; existing models are never overwritten."""
    c = ctx(request)
    uploads: list[tuple[str, bytes]] = []
    async with request.form() as form:  # closes the uploads' spool files
        for f in form.getlist("file"):
            if getattr(f, "filename", ""):
                uploads.append((f.filename, await f.read()))
    done: list[str] = []
    errs: list[str] = []
    remaining_models, remaining_bytes = 100, MAX_BODY
    for filename, data in uploads:
        if filename.lower().endswith(".zip"):
            try:
                entries = zip_models(data, max_models=remaining_models, max_bytes=remaining_bytes)
            except (zipfile.BadZipFile, ValueError, RuntimeError, NotImplementedError) as e:
                errs.append(f"{filename}: invalid zip: {e}")
                continue
        else:
            if remaining_models < 1 or len(data) > remaining_bytes:
                errs.append(f"{filename}: import exceeds 100 models or 8 MiB expanded YAML")
                continue
            entries = [(filename, data)]
        remaining_models -= len(entries)
        remaining_bytes -= sum(len(raw) for _, raw in entries)
        for name, raw in entries:
            mid = slug(Path(name).stem)
            try:
                if not MODEL_ID.match(mid):
                    raise ValueError(f"bad model id {mid!r}")
                if c.store.exists(mid):
                    raise FileExistsError(f"model {mid} already exists; rename the file to import a copy")
                c.store.save_text(mid, raw.decode("utf-8-sig"), f"{mid}: import", expected="")
                done.append(mid)
            except Exception as e:
                errs.append(f"{name}: " + "; ".join(errors_of(e))[:500])
    if not errs:
        return RedirectResponse(f"/?imported={','.join(done)}", status_code=303)
    return c.index_page(" · ".join(errs), done, status=422)


async def export_all(request: Request):
    """Every model's file, verbatim, in one zip (a backup; import takes it back)."""
    c = ctx(request)
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for m in c.store.list_models():
            # Fixed timestamp: the same models always make the same zip.
            info = zipfile.ZipInfo(f"{m.id}.yaml", date_time=(1980, 1, 1, 0, 0, 0))
            zf.writestr(info, c.store.read_text(m.id), zipfile.ZIP_DEFLATED)
    return Response(
        buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="tmodel-models.zip"'},
    )
