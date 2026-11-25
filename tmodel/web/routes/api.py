"""Small, conditional agent API. All mutations share the UI's editing contracts."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict
from functools import wraps

from pydantic import ValidationError
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ...editing import delete_entity, put_entity
from ...lint import lint
from ...schema import ThreatModel
from ..context import ctx
from ..store import ConflictError, HistoryError

logger = logging.getLogger(__name__)


class APIError(Exception):
    def __init__(self, code: str, message: str, status: int):
        self.code, self.message, self.status = code, message, status


def problem(code: str, message: str, status: int, **extra) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}, **extra}, status)


def endpoint(fn):
    @wraps(fn)
    async def handle(request: Request):
        try:
            return await fn(request)
        except APIError as e:
            return problem(e.code, e.message, e.status)
        except json.JSONDecodeError:
            return problem("malformed_json", "Malformed JSON.", 400)
        except ValidationError as e:
            issues = [{"code": x["type"], "path": list(x["loc"]), "message": x["msg"]} for x in e.errors()]
            return problem("validation_failed", "The model is invalid.", 422, issues=issues)
        except ConflictError as e:
            return problem("precondition_failed", str(e), 412)
        except HistoryError as e:
            logger.exception("API file saved without git history")
            return problem("history_failed", str(e), 503, saved=True)
        except HTTPException as e:
            return problem(
                "not_found" if e.status_code == 404 else "bad_request", str(e.detail), e.status_code
            )
        except FileNotFoundError:
            return problem("not_found", "No such model.", 404)
        except ValueError as e:
            return problem("invalid_operation", str(e), 422)
        except OSError:
            logger.exception("API storage failure")
            return problem("storage_failed", "Storage failed; reload before retrying.", 503)

    return handle


def condition(request: Request, *, allow_create: bool = False) -> str:
    match, none = request.headers.get("if-match"), request.headers.get("if-none-match")
    if match and none:
        raise APIError("bad_precondition", "Send only one precondition.", 400)
    if none is not None:
        if allow_create and none == "*":
            return ""
        raise APIError("bad_precondition", "If-None-Match: * is only for creating a model.", 400)
    if not match:
        raise APIError("precondition_required", "Send the exact If-Match from GET.", 428)
    if not re.fullmatch(r'"[a-f0-9]{64}"', match):
        raise APIError("bad_precondition", "If-Match must be the exact quoted ETag from GET.", 400)
    return match[1:-1]


def message(request: Request, action: str) -> str:
    """Self-reported provenance, not an authenticated identity or authorization."""
    parts = [f"{request.path_params['mid']}: api {action}"]
    for name in ("actor", "run", "reason"):
        value = request.headers.get(f"x-tmodel-{name}", "").strip()
        if len(value) > 200 or any(not c.isprintable() for c in value):
            raise APIError("bad_provenance", f"{name} must be printable and at most 200 characters.", 400)
        if value:
            parts.append(f"{name}={value}")
    return " | ".join(parts)


def saved(request: Request, model: ThreatModel, expected: str, action: str):
    dry = request.query_params.get("dry_run", "0")
    if dry not in ("0", "1"):
        raise APIError("bad_query", "dry_run must be 0 or 1.", 400)
    tag = ctx(request).store.save(
        request.path_params["mid"],
        model,
        message(request, action),
        expected=expected,
        dry_run=dry == "1",
    )
    result = {"ok": True, "revision": model.revision, "findings": [asdict(f) for f in lint(model)]}
    if dry == "1":
        return JSONResponse({**result, "dry_run": True, "candidate_sha256": tag})
    return JSONResponse(result, headers={"ETag": f'"{tag}"'})


@endpoint
async def models(request: Request):
    return JSONResponse([asdict(i) for i in ctx(request).store.list_models()])


@endpoint
async def model_get(request: Request):
    snapshot = ctx(request).snapshot(request.path_params["mid"])
    return JSONResponse(
        snapshot.model.model_dump(mode="json", exclude_none=True), headers={"ETag": f'"{snapshot.etag}"'}
    )


@endpoint
async def snapshot_get(request: Request):
    snapshot = ctx(request).snapshot(request.path_params["mid"])
    return Response(
        snapshot.text,
        media_type="application/yaml",
        headers={
            "ETag": f'"{snapshot.etag}"',
            "X-Snapshot-SHA256": snapshot.etag,
            "Cache-Control": "no-store",
        },
    )


@endpoint
async def model_put(request: Request):
    expected = condition(request, allow_create=True)
    model = ThreatModel.model_validate(await request.json())
    return saved(request, model, expected, f"put (rev {model.revision})")


@endpoint
async def validate(request: Request):
    model = ThreatModel.model_validate(await request.json())
    return JSONResponse({"valid": True, "findings": [asdict(f) for f in lint(model)]})


@endpoint
async def entity_write(request: Request):
    expected = condition(request)
    c = ctx(request)
    mid, coll = request.path_params["mid"], request.path_params["coll"]
    c.entity_class(coll)
    snapshot = c.snapshot(mid)
    if snapshot.etag != expected:
        raise ConflictError("The model changed. GET and review before retrying.")
    model = snapshot.model
    eid = request.path_params.get("eid")
    if request.method == "DELETE":
        c.find(model, coll, eid)
        updated = delete_entity(model, coll, eid)
        action = f"delete {eid}"
    else:
        if eid is not None:
            c.find(model, coll, eid)
        updated, entity = put_entity(model, coll, eid, await request.json())
        action = f"{'update' if eid else 'add'} {entity.id}"
    return saved(request, updated, expected, action)
