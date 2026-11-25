"""The page script in a real browser: runs scripts/ui.mjs against a live server.

Marked `browser`, so the default run deselects it (it needs node, Playwright
and Chromium). Run it with `uv run pytest -m browser` or `scripts/check.sh --full`;
CI runs it on every pull request. It skips, with the reason, when node or
Playwright is missing.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import pytest
import uvicorn

from tmodel.web.app import create_app

REPO = Path(__file__).parent.parent
pytestmark = pytest.mark.browser


def _playwright_missing() -> str | None:
    if not shutil.which("node"):
        return "node is not installed"
    probe = subprocess.run(
        ["node", "-e", "require('playwright')"], cwd=REPO / "scripts", capture_output=True, text=True
    )
    return "Playwright is not installed for node" if probe.returncode else None


def test_worker_persistence_retry():
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    r = subprocess.run(
        ["node", str(REPO / "scripts" / "sw-test.mjs")], capture_output=True, text=True, timeout=20
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_page_script_in_a_real_browser(tmp_path):
    reason = _playwright_missing()
    if reason:
        pytest.skip(reason)
    for f in (REPO / "examples").glob("*.yaml"):
        shutil.copy(f, tmp_path / f.name)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(create_app(tmp_path, git=True), host="127.0.0.1", port=port, log_level="error")
    )
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/", timeout=1).close()
            break
        except Exception:
            time.sleep(0.1)
    try:
        r = subprocess.run(
            ["node", str(REPO / "scripts" / "ui.mjs"), base], capture_output=True, text=True, timeout=600
        )
    finally:
        server.should_exit = True
    assert r.returncode == 0, r.stdout + r.stderr
