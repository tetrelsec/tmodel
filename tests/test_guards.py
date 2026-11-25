"""Host-independent request and export safety checks."""

import asyncio
import zipfile
from io import BytesIO

import pytest

from tmodel.web.app import Host, create_app
from tmodel.web.browser import call_asgi
from tmodel.web.guards import RequestGuards
from tmodel.web.routes.exports import spreadsheet_safe
from tmodel.web.routes.models import zip_models


def test_browser_origin_checks_include_local_browser_build(tmp_path):
    app = create_app(tmp_path, git=False, host=Host(render_svg=lambda _: None))

    def post(origin, *, site=None):
        headers = [("Origin", origin), ("Content-Type", "application/x-www-form-urlencoded")]
        if site:
            headers.append(("Sec-Fetch-Site", site))
        return asyncio.run(
            call_asgi(app, "POST", "http://localhost:8766/models", headers, b"id=created&system=New")
        )

    assert post("https://evil.example")["status"] == 403
    assert post("null")["status"] == 403
    assert post("http://localhost:8766", site="cross-site")["status"] == 403
    assert post("http://localhost:8766")["status"] == 303
    assert (tmp_path / "created.yaml").exists()


def test_tls_proxy_scheme_does_not_allow_a_forwarded_authority(tmp_path):
    app = create_app(tmp_path, git=False, host=Host(render_svg=lambda _: None))
    headers = [
        ("Host", "private.example"),
        ("X-Forwarded-Proto", "https"),
        ("Origin", "https://private.example"),
        ("Content-Type", "application/json"),
    ]
    result = asyncio.run(call_asgi(app, "POST", "http://backend:8000/api/validate", headers, b"{}"))
    assert result["status"] == 422  # reached model validation, not blocked as HTTP
    headers = [(k, "https://evil.example" if k == "Origin" else v) for k, v in headers]
    headers.append(("X-Forwarded-Host", "evil.example"))
    assert (
        asyncio.run(call_asgi(app, "POST", "http://backend:8000/api/validate", headers, b"{}"))["status"]
        == 403
    )


def test_request_limits_check_actual_bytes_and_declared_size():
    called = []

    async def app(scope, receive, send):
        called.append(True)

    guard = RequestGuards(app, max_body=4)
    r = asyncio.run(call_asgi(guard, "POST", "/", [], b"12345"))
    assert r["status"] == 413 and not called
    r = asyncio.run(call_asgi(guard, "POST", "/", [("Content-Length", "5")], b"1"))
    assert r["status"] == 413 and not called
    r = asyncio.run(call_asgi(guard, "POST", "/", [("Content-Length", "invalid")], b"1"))
    assert r["status"] == 400 and not called


def test_zip_expansion_preflight_happens_before_read(monkeypatch):
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n in range(101):
            z.writestr(f"{n}.yaml", b"small")

    def unexpected_read(*args):
        raise AssertionError("decompressed before checking limits")

    monkeypatch.setattr(zipfile.ZipFile, "read", unexpected_read)
    with pytest.raises(ValueError, match="100 models"):
        zip_models(buf.getvalue())


@pytest.mark.parametrize("value", ["=1+1", "+CMD()", "-CMD()", "@SUM(A1)", "  =1", "\t=1", "\r=1"])
def test_csv_neutralizes_formulas(value):
    assert spreadsheet_safe(value) == "'" + value
    assert spreadsheet_safe("ordinary text") == "ordinary text"
