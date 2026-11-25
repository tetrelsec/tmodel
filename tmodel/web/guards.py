"""Small host-independent request guards; deployment still supplies authentication."""

from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse

MAX_BODY = 8 * 1024 * 1024


def origin_key(url: str) -> tuple:
    parsed = urlsplit(url)
    return parsed.scheme, parsed.hostname, parsed.port or {"http": 80, "https": 443}.get(parsed.scheme)


class RequestGuards:
    """Bound mutation bodies before parsing and reject browser cross-origin writes.

    Buffering costs at most MAX_BODY per in-flight write, with no tasks/threads.
    Non-browser clients without Origin/Fetch-Metadata are allowed (not auth).
    """

    def __init__(self, app, max_body: int = MAX_BODY):
        self.app, self.max_body = app, max_body

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            return await self.app(scope, receive, send)
        request = Request(scope)
        origin = request.headers.get("origin")
        # TLS-terminating proxies (including exe.dev) may reach uvicorn over HTTP.
        # Use the forwarded scheme, never a forwarded authority: Origin must still
        # match the actual Host. This header isn't an authentication mechanism.
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        if scheme not in ("http", "https"):
            scheme = request.url.scheme
        target = str(request.url.replace(scheme=scheme))
        try:
            cross_origin = origin is not None and origin_key(origin) != origin_key(target)
        except ValueError:
            cross_origin = True
        if cross_origin or request.headers.get("sec-fetch-site") == "cross-site":
            return await self.reject(
                scope, receive, send, "cross_origin_write", "Cross-origin writes are refused.", 403
            )
        length = request.headers.get("content-length")
        if length is not None:
            try:
                oversized = int(length) > self.max_body or int(length) < 0
            except ValueError:
                return await self.reject(scope, receive, send, "bad_length", "Invalid Content-Length.", 400)
            if oversized:
                return await self.reject(
                    scope, receive, send, "payload_too_large", "Request body is too large.", 413
                )
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_body:
                return await self.reject(
                    scope, receive, send, "payload_too_large", "Request body is too large.", 413
                )
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        sent = False

        async def buffered_receive():
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        return await self.app(scope, buffered_receive, send)

    @staticmethod
    async def reject(scope, receive, send, code, message, status):
        response = JSONResponse({"error": {"code": code, "message": message}}, status)
        await response(scope, receive, send)
