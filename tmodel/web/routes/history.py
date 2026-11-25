"""Git history (git store only): one commit's diff of a model, and restoring it."""

from __future__ import annotations

import re

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import RedirectResponse

from ..context import Ctx, ctx, errors_of


def _commit(c: Ctx, request: Request) -> tuple[str, str]:
    mid, sha = request.path_params["mid"], request.path_params["sha"]
    if not c.store.git or not re.fullmatch(r"[0-9a-f]{4,40}", sha):
        raise HTTPException(404, "no such version")
    c.load(mid)
    return mid, sha


async def history_view(request: Request):
    c = ctx(request)
    mid, sha = _commit(c, request)
    diff = c.store.diff(mid, sha)
    if diff is None:
        raise HTTPException(404, f"no version {sha} of {mid}")
    model = c.load(mid)
    return c.page(
        "history.html",
        mid=mid,
        model=model,
        sha=sha,
        diff=diff,
        entry=next((x for x in c.store.history(mid, 200) if x.sha == sha), None),
        **c.side(model),
    )


async def history_restore(request: Request):
    """Write the file as it was after `sha`, as a new commit (history is never rewritten)."""
    c = ctx(request)
    mid, sha = _commit(c, request)
    snapshot = c.snapshot(mid)
    text = c.store.text_at(mid, sha)
    if text is None:
        raise HTTPException(404, f"no version {sha} of {mid}")
    try:
        c.store.save_text(mid, text, f"{mid}: restore {sha}", expected=snapshot.etag)
    except Exception as e:  # that version no longer validates (schema moved on)
        model = c.load(mid)
        return c.page(
            "history.html",
            mid=mid,
            model=model,
            sha=sha,
            diff=c.store.diff(mid, sha) or "",
            entry=None,
            errors=errors_of(e),
            **c.side(model),
        )
    return RedirectResponse(f"/m/{mid}/settings?saved=1", status_code=303)
