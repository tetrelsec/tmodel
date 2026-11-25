"""Smoke tests against a live server (no httpx dependency needed)."""

from __future__ import annotations

import io
import json
import re
import shutil
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import pytest
import uvicorn

from tmodel.web import tables
from tmodel.web.app import create_app

EXAMPLES = Path(__file__).parent.parent / "examples"
SAMPLE = EXAMPLES / "greenhouse.yaml"
_server: tuple[str, Path] | None = None


def server() -> tuple[str, Path]:
    global _server
    if _server:
        return _server
    root = Path(tempfile.mkdtemp())
    shutil.copy(SAMPLE, root / "sample.yaml")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(
        uvicorn.Config(create_app(root, git=True), host="127.0.0.1", port=port, log_level="error")
    )
    threading.Thread(target=srv.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/", timeout=1).close()
            break
        except Exception:
            time.sleep(0.1)
    _server = (base, root)
    return _server


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(NoRedirect)


def req(
    path: str, data: dict | None = None, method: str | None = None, json_body=None, inline=False, headers=None
):
    """Returns (status, headers, body). `inline=True` sends X-Inline: 1, asking
    for a <tr> partial instead of a redirect."""
    base, _ = server()
    body = None
    headers = dict(headers or {})
    if inline:
        headers["X-Inline"] = "1"
    if json_body is not None:
        body = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    elif data is not None:
        body = urllib.parse.urlencode(data, doseq=True).encode()
    r = urllib.request.Request(
        base + path, data=body, method=method or ("POST" if body is not None else "GET"), headers=headers
    )
    return _send(r)


def _send(r: urllib.request.Request):
    """(status, headers, body) of a request, without following redirects; the
    connection is always closed."""
    try:
        with _opener.open(r) as resp:
            return resp.status, resp.headers, resp.read().decode()
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.headers, e.read().decode()


def test_pages_render():
    for p in [
        "/",
        "/m/sample/threats",
        "/m/sample/threats/TS_01",
        "/m/sample/threats/new",
        "/m/sample/yaml",
        "/m/sample/settings",
        "/m/sample/export.json",
        "/m/sample/report",
    ]:
        status, _, _body = req(p)
        assert status == 200, p
    assert req("/m/nope/threats")[0] == 404


def test_report_cell_filter():
    _, _, body = req("/m/sample/report")
    assert body.count("<article") == 5 and 'class="heat"' in body
    _, _, body = req("/m/sample/report?impact=Moderate&feasibility=Medium")
    assert body.count("<article") == 1 and ">TS_03<" in body and "show all" in body
    _, _, body = req("/m/sample/report?impact=bogus")  # unknown values are ignored
    assert body.count("<article") == 5


def test_report_review_agenda_and_full_model_mapping():
    _, _, body = req("/m/sample/report")
    assert 'aria-label="Review agenda"' in body
    assert 'id="damage-review"' in body and 'id="goal-review"' in body
    assert body.count('<table class="review-table">') > 0
    assert 'id="threat-TS_03"' in body
    assert 'href="/m/sample/report#threat-TS_03"' in body
    assert 'data-label="Treatment / rationale"' in body
    assert "mappings show intent, not verification" in body
    # A heat-map filter narrows only the detail register, never the review agenda.
    _, _, filtered = req("/m/sample/report?impact=Moderate&feasibility=Medium")
    agenda = body.split('<section id="damage-review"')[1].split('<div class="toolbar" id="register">')[0]
    assert agenda in filtered


def test_add_entity_and_commit(mid):
    status, h, _ = req(f"/m/{mid}/goals/new", {"id": "CG_09", "name": "Test goal"})
    assert status == 303 and h["location"].endswith(f"/m/{mid}/goals?saved=CG_09")
    _, root = server()
    assert "CG_09" in (root / f"{mid}.yaml").read_text()
    log = (
        __import__("subprocess")
        .run(["git", "-C", str(root), "log", "--oneline"], capture_output=True, text=True)
        .stdout
    )
    assert "add CG_09" in log


def test_bad_reference_rejected(mid):
    status, _, body = req(f"/m/{mid}/assets/new", {"id": "A_09", "name": "x", "domain": "DOM_NOPE"})
    assert status == 200 and "unknown domain DOM_NOPE" in body
    _, root = server()
    assert "A_09" not in (root / f"{mid}.yaml").read_text()


def test_delete_with_dependents_blocked():
    status, _, body = req("/m/sample/domains/DOM_CONTROLLER/delete", {})
    assert status == 200 and "cannot delete" in body


def test_edit_preserves_order_and_lint_updates(mid):
    status, _, _ = req(
        f"/m/{mid}/threats/TS_02",
        {
            "id": "TS_02",
            "damage": "DS_01",
            "surface": "SURF_PANEL",
            "description": "x",
            "attack_path": "a",
            "vector": "Network",
            "feasibility": "Low",
            "treatment": "Reduce",
            "goals": "CG_03",
        },
    )
    assert status == 303
    _, _, body = req(f"/m/{mid}/threats")
    assert "vector-exceeds-surface" in body
    ids = re.findall(rf'data-row="/m/{mid}/threats/(TS_\d+)/row"', body)
    assert ids[:2] == ["TS_01", "TS_02"]


def test_yaml_roundtrip_and_syntax_error(mid):
    _, _, text = req(f"/m/{mid}/export.yaml")
    assert req(f"/m/{mid}/yaml", {"text": "system: [oops"})[0] == 200
    assert req(f"/m/{mid}/yaml", {"text": text})[0] == 303


def test_api_put_and_get(mid):
    _, h, body = req(f"/api/models/{mid}")
    d = json.loads(body)
    d["revision"] = 7
    status, h, body = req(f"/api/models/{mid}", method="PUT", json_body=d, headers={"If-Match": h["etag"]})
    assert status == 200 and json.loads(body)["revision"] == 7
    assert json.loads(req(f"/api/models/{mid}")[2])["revision"] == 7
    status, _, body = req(
        f"/api/models/{mid}", method="PUT", json_body={"system": "x"}, headers={"If-Match": h["etag"]}
    )
    assert status == 422


def test_api_requires_exact_preconditions(mid):
    path = f"/api/models/{mid}"
    _, h, body = req(path)
    model = json.loads(body)
    assert req(path, method="PUT", json_body=model)[0] == 428
    assert req(path, method="PUT", json_body=model, headers={"If-Match": "*"})[0] == 400
    first = h["etag"]
    model["system"] = "changed"
    status, h, _ = req(path, method="PUT", json_body=model, headers={"If-Match": first})
    assert status == 200 and h["etag"] != first
    assert req(path, method="PUT", json_body=model, headers={"If-Match": first})[0] == 412
    assert req(path, method="PUT", json_body=model, headers={"If-None-Match": "*"})[0] == 412
    new = path + "-created"
    assert req(new, method="PUT", json_body=model, headers={"If-None-Match": "*"})[0] == 200


def test_invalid_inputs_are_recoverable_and_preserved(mid):
    status, _, body = req(f"/m/{mid}/settings", {"system": "unsaved name", "revision": "oops"})
    assert status == 200 and "revision" in body and 'value="oops"' in body
    assert 'value="unsaved name"' in body
    status, _, body = req(
        f"/m/{mid}/settings", {"system": "unsaved", "revision": "2", "risk_matrix": "[oops"}
    )
    assert status == 200 and "invalid YAML" in body and "[oops" in body
    _, h, _ = req(f"/api/models/{mid}")
    base, root = server()
    r = urllib.request.Request(
        base + f"/api/models/{mid}",
        data=b"{",
        method="PUT",
        headers={"Content-Type": "application/json", "If-Match": h["etag"]},
    )
    assert _send(r)[0] == 400
    (root / f"{mid}.yaml").write_text("system: [oops")
    status, h, _ = req(f"/m/{mid}/threats")
    assert status == 303 and h["location"].endswith("/yaml")


def test_ids_cannot_be_renamed_or_reused(mid):
    assert req(f"/m/{mid}/goals/new", {"id": "CG_99", "name": "new"})[0] == 303
    status, _, body = req(f"/m/{mid}/goals/CG_99", {"id": "CG_OTHER", "name": "renamed"})
    assert status == 200 and "immutable" in body
    assert req(f"/m/{mid}/goals/CG_99/delete", {})[0] == 303
    _, _, body = req(f"/m/{mid}/goals/new")
    assert 'value="CG_100"' in body
    status, _, body = req(f"/m/{mid}/goals/new", {"id": "CG_99", "name": "reuse"})
    assert status == 200 and "retired" in body


def test_entity_api_dry_run_errors_retirement_and_provenance(mid):
    path = f"/api/models/{mid}"
    _, h, original = req(path)
    tag = h["etag"]
    entity = {"id": "CG_99", "name": "agent goal"}
    headers = {
        "If-Match": tag,
        "X-Tmodel-Actor": "test-agent",
        "X-Tmodel-Run": "run-1",
        "X-Tmodel-Reason": "verify boot",
    }
    status, h, body = req(path + "/goals?dry_run=1", method="POST", json_body=entity, headers=headers)
    assert status == 200 and json.loads(body)["dry_run"] and "etag" not in h
    assert req(path)[2] == original
    status, _, body = req(path + "/goals", method="POST", json_body={"id": "bad"}, headers=headers)
    result = json.loads(body)
    assert status == 422 and result["error"]["code"] == "validation_failed"
    assert any(i["path"] == ["id"] for i in result["issues"])
    status, h, _ = req(path + "/goals", method="POST", json_body=entity, headers=headers)
    assert status == 200
    from tmodel.web.store import FileStore

    store = FileStore(server()[1], git=True)
    assert "actor=test-agent | run=run-1 | reason=verify boot" in store.history(mid)[0].message
    headers = {"If-Match": h["etag"]}
    status, _, body = req(
        path + "/goals/CG_99", method="PUT", json_body={**entity, "id": "CG_98"}, headers=headers
    )
    assert status == 422 and json.loads(body)["error"]["code"] == "invalid_operation"
    status, _, _ = req(path + "/goals/CG_99?dry_run=1", method="DELETE", headers=headers)
    assert status == 200 and "CG_99" not in json.loads(req(path)[2])["retired_ids"]
    status, h, _ = req(path + "/goals/CG_99", method="DELETE", headers=headers)
    assert status == 200 and "CG_99" in json.loads(req(path)[2])["retired_ids"]
    assert req(path + "/goals/CG_99", method="DELETE", headers={"If-Match": h["etag"]})[0] == 404


def test_python_client_live_round_trip(mid):
    import hashlib

    from tmodel.client import Client, ClientError

    client = Client(server()[0], headers={"X-Tmodel-Actor": "client-test"})
    original = client.get(mid)
    assert client.validate(original.model)["valid"]
    raw, digest = client.snapshot(mid)
    assert hashlib.sha256(raw).hexdigest() == digest == original.etag.strip('"')
    model = original.model.model_copy(update={"system": "client change"})
    assert client.put(mid, model, etag=original.etag, dry_run=True)["dry_run"]
    assert client.get(mid).etag == original.etag
    assert client.put(mid, model, etag=original.etag)["ok"]
    with pytest.raises(ClientError) as err:
        client.put(mid, original.model, etag=original.etag)
    assert err.value.status == 412 and err.value.detail["error"]["code"] == "precondition_failed"
    latest = client.get(mid)
    entity = {"id": "CG_99", "name": "new"}
    assert client.put_entity(mid, "goals", entity, etag=latest.etag)["ok"]
    latest = client.get(mid)
    assert client.delete_entity(mid, "goals", "CG_99", etag=latest.etag)["ok"]
    assert client.create(mid + "-copy", client.get(mid).model)["ok"]


def test_create_model():
    status, h, _ = req("/models", {"id": "new-one", "system": "New"})
    assert status == 303 and "/m/new-one/" in h["location"]
    assert req("/m/new-one/threats/new")[0] == 200


# Inline row editing -----------------------------------------------------------


def test_row_partials():
    status, _, body = req("/m/sample/threats/TS_03/row")
    assert status == 200 and body.strip().startswith("<tr") and "data-open" in body
    status, _, body = req("/m/sample/threats/TS_03/row/edit")
    assert status == 200 and 'class="editing"' in body and 'name="feasibility"' in body
    assert 'name="risk"' not in body  # derived, shown read-only
    assert 'name="attack_path"' not in body  # not a table column: untouched by inline edits
    assert req("/m/sample/threats/TS_99/row")[0] == 404


def test_inline_save_returns_row_and_keeps_other_fields(mid):
    _, root = server()
    data = {
        "description": "Moisture reading suppresses watering",
        "damage": "DS_02",
        "surface": "SURF_SENSOR",
        "vector": "",
        "feasibility": "High",
        "treatment": "Reduce",
    }
    status, _, html = req(f"/m/{mid}/threats/TS_03/row", data, inline=True)
    assert status == 200 and html.strip().startswith("<tr") and "Moisture reading" in html
    assert '<td data-label="risk">3</td>' in html  # Moderate x High -> 3, derived
    after = (root / f"{mid}.yaml").read_text()
    assert "Moisture reading suppresses watering" in after
    assert "Present an incorrect moisture reading" in after  # attack_path survived


def test_inline_save_error_is_422_edit_row(mid):
    data = {
        "description": "x",
        "damage": "DS_99",
        "surface": "SURF_SENSOR",
        "feasibility": "Low",
        "treatment": "Reduce",
    }
    status, _, html = req(f"/m/{mid}/threats/TS_03/row", data, inline=True)
    assert status == 422 and 'class="editing"' in html and "unknown damage DS_99" in html


def test_inline_save_without_js_redirects(mid):
    status, h, _ = req(f"/m/{mid}/goals/CG_01/row", {"name": "Console authorization (renamed)"})
    assert status == 303 and h["location"].endswith(f"/m/{mid}/goals?saved=CG_01")


def test_side_partial():
    status, _, body = req("/m/sample/side")
    assert status == 200 and 'id="side"' in body and "Lint" in body


def test_new_row_blank_and_create(mid):
    status, _, body = req(f"/m/{mid}/surfaces/new/row")
    assert status == 200 and "data-new" in body and 'name="next"' in body
    assert 'value="SURF_"' in body  # named IDs in use: prefix stub suggested
    data = {"id": "SURF_USB", "name": "USB", "connects": "DOM_CONTROLLER", "vector": "Physical"}
    status, _, html = req(f"/m/{mid}/surfaces/new/row", data, inline=True)
    assert status == 200 and f'data-row="/m/{mid}/surfaces/SURF_USB/row"' in html
    _, root = server()
    assert "SURF_USB" in (root / f"{mid}.yaml").read_text()


def test_new_row_invalid_is_422_and_not_saved(mid):
    _, root = server()
    before = (root / f"{mid}.yaml").read_text()
    missing = {"id": "DS_X", "description": "d"}
    duplicate = {"id": "DS_01", "asset": "A_01", "description": "d", "impact": "Major"}
    for data in (missing, duplicate):
        status, _, html = req(f"/m/{mid}/damages/new/row", data, inline=True)
        assert status == 422 and "data-new" in html
    assert (root / f"{mid}.yaml").read_text() == before


# Navigation, search, exports, safety -----------------------------------------


def fresh(mid: str) -> Path:
    """(Re)set model `mid` to a copy of the sample model. Returns its file."""
    _, root = server()
    shutil.copy(SAMPLE, root / f"{mid}.yaml")
    return root / f"{mid}.yaml"


@pytest.fixture
def mid(request) -> str:
    """A model of the test's own, fresh on every run: tests that write use it, so
    `sample` stays read-only and the suite passes in any order or subset."""
    name = re.sub(r"[^a-z0-9]+", "-", request.node.name.lower()).strip("-")[:60]
    fresh(name)
    return name


def multipart(path: str, files: list[tuple[str, bytes]]):
    """POST files as multipart/form-data under the field name `file`."""
    base, _ = server()
    boundary = "tmodelboundary"
    body = b""
    for name, data in files:
        body += (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
            + data
            + b"\r\n"
        )
    body += f"--{boundary}--\r\n".encode()
    r = urllib.request.Request(
        base + path, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
    )
    return _send(r)


def test_table_cells_link_references():
    _, _, body = req("/m/sample/threats")
    assert '<a class="ref" href="/m/sample/damages/DS_01"' in body
    assert '<td class="id">TS_01</td>' in body  # the row's own ID is not a link
    _, _, body = req("/m/sample/surfaces")
    assert 'href="/m/sample/domains/DOM_CONTROLLER"' in body  # list references too


def test_lint_findings_link_to_their_entity():
    _, _, body = req("/m/sample/side")
    assert re.search(r'<a class="id" href="/m/sample/\w+/[A-Z]+_\w+">', body)


def test_filter_and_sort():
    _, _, body = req("/m/sample/threats?q=panel")
    ids = re.findall(r'data-row="/m/sample/threats/(TS_\d+)/row"', body)
    assert ids and len(ids) < 5 and "matching" in body
    _, _, body = req("/m/sample/threats?q=zzzz-nothing")
    assert "No rows match" in body
    _, _, body = req("/m/sample/threats?sort=risk&dir=desc&group=")
    risks = [int(r) for r in re.findall(r'<td data-label="risk">(\d)</td>', body)]
    assert risks == sorted(risks, reverse=True) and len(risks) == 5
    _, _, body = req("/m/sample/damages?sort=impact")  # enum order, not alphabetical
    order = ["Negligible", "Moderate", "Major", "Severe"]
    impacts = re.findall(r'<td data-label="impact">(\w+)</td>', body)
    assert [order.index(i) for i in impacts] == sorted(order.index(i) for i in impacts)
    assert req("/m/sample/threats?sort=bogus")[0] == 200  # unknown column ignored


def test_tables_group_by_a_single_reference():
    def heads(body):
        return re.findall(
            r'<tr class="group" data-group><th colspan="\d+"><a class="ref" href="/m/sample/\w+/(\w+)"', body
        )

    def refs(body, col):
        return re.findall(
            rf'<td data-label="{col}"><span class="refs"><a class="ref" href="[^"]+" title="[^"]*">(\w+)</a>',
            body,
        )

    # threats are grouped by damage unless the URL says otherwise
    _, _, body = req("/m/sample/threats")
    assert heads(body) and heads(body) == list(
        dict.fromkeys(refs(body, "damage"))
    )  # one header per run of rows
    assert heads(body) == sorted(heads(body), key=tables.natural)
    assert 'href="/m/sample/threats?group=" title="Stop grouping"' in body
    # sorting applies inside each section
    _, _, body = req("/m/sample/threats?sort=risk&dir=desc")
    assert heads(body) == sorted(heads(body), key=tables.natural)
    risks = re.findall(r'<td data-label="risk">(\d)</td>', body)
    damages = refs(body, "damage")
    for d in set(damages):
        mine = [int(r) for r, x in zip(risks, damages, strict=True) if x == d]
        assert mine == sorted(mine, reverse=True)
    _, _, body = req("/m/sample/threats?sort=damage&dir=desc")  # sorting on the group reverses sections
    assert heads(body) == sorted(heads(body), key=tables.natural, reverse=True)
    assert '<tr class="group"' not in req("/m/sample/threats?group=")[2]  # turned off
    # damages are not grouped until asked; the toggle sits beside the asset header
    _, _, body = req("/m/sample/damages")
    assert '<tr class="group"' not in body and 'href="/m/sample/damages?group=asset"' in body
    _, _, body = req("/m/sample/damages?group=asset&q=a")
    assert heads(body) == list(dict.fromkeys(refs(body, "asset")))
    assert 'name="group" value="asset"' in body  # the filter form keeps it
    # only single references group: not a list (surfaces.connects), not a plain column
    assert 'class="grp' not in req("/m/sample/surfaces")[2]
    assert '<tr class="group"' not in req("/m/sample/damages?group=impact")[2]


def test_go_jumps_to_an_id_or_searches():
    for q in ("TS_03", "ts3", "ts 03"):
        status, h, _ = req(f"/m/sample/go?id={urllib.parse.quote(q)}")
        assert status == 303 and h["location"].endswith("/m/sample/threats/TS_03"), q
    status, _, body = req("/m/sample/go?id=console")
    assert status == 200 and body.count('<td class="id">') > 1
    status, _, body = req("/m/sample/go?id=zzzz-nothing")
    assert status == 200 and "Nothing in this model matches" in body


def test_used_by_and_delete_guard(mid):
    _, _, body = req(f"/m/{mid}/damages/DS_01")
    assert "Referenced by" in body and f'href="/m/{mid}/threats/TS_01"' in body
    status, _, body = req(f"/m/{mid}/damages/DS_01/delete", {})
    assert status == 200 and "cannot delete DS_01: referenced by TS_01 (damage)" in body


def test_nested_domain_forms_and_delete_guard(mid):
    root = f"/m/{mid}/domains"
    assert req(root + "/new", {"id": "DOM_CLOUD", "name": "Cloud"})[0] == 303
    assert req(root + "/new", {"id": "DOM_VM", "name": "VM", "parent": "DOM_CLOUD"})[0] == 303
    _, _, body = req(root + "/DOM_CLOUD")
    assert f'href="{root}/DOM_VM"' in body and "parent" in body
    status, _, body = req(root + "/DOM_CLOUD/delete", {})
    assert status == 200 and "referenced by DOM_VM (parent)" in body
    status, _, body = req(root + "/DOM_CLOUD", {"id": "DOM_CLOUD", "name": "Cloud", "parent": "DOM_VM"})
    assert status == 200 and "parent cycle" in body
    data = json.loads(req(f"/api/models/{mid}")[2])
    assert next(d for d in data["domains"] if d["id"] == "DOM_CLOUD").get("parent") is None
    assert req(root + "/DOM_VM", {"id": "DOM_VM", "name": "VM", "parent": ""})[0] == 303
    assert req(root + "/DOM_CLOUD/delete", {})[0] == 303


def test_duplicate_prefills_a_new_entity():
    status, _, body = req("/m/sample/threats/new?from=TS_02")
    assert status == 200 and "Prefilled from" in body
    assert re.search(r'name="id" value="TS_\d+"\s*>', body)  # next free ID, editable
    desc = next(t for t in json.loads(req("/api/models/sample")[2])["threats"] if t["id"] == "TS_02")
    assert f">{desc['description']}</textarea>" in body  # TS_02's fields carried over
    assert req("/m/sample/threats/new?from=TS_99")[0] == 404


def test_saved_note_after_save(mid):
    _, _, body = req(f"/m/{mid}/threats?saved=TS_01")
    assert "Saved TS_01 · risk" in body
    data = {"description": "d", "damage": "DS_01", "surface": "SURF_CONSOLE", "feasibility": "Low"}
    data["treatment"] = "Retain"
    _, _, html = req(f"/m/{mid}/threats/TS_01/row", data, inline=True)
    assert 'data-toast="Saved TS_01 · risk' in html and "treatment is Retain" in html


def test_stale_edit_refused_then_allowed(mid):
    _, _, row = req(f"/m/{mid}/goals/CG_01/row/edit")
    tag = re.search(r'name="_etag" value="(\w+)"', row)[1]
    assert req(f"/m/{mid}/goals/CG_01/row", {"name": "first", "_etag": tag}, inline=True)[0] == 200
    status, _, html = req(f"/m/{mid}/goals/CG_01/row", {"name": "second", "_etag": tag}, inline=True)
    assert status == 422 and "changed since you opened" in html
    _, _, page = req(f"/m/{mid}/goals/CG_02")  # another entity is not affected
    tag2 = re.search(r'name="_etag" value="(\w+)"', page)[1]
    assert req(f"/m/{mid}/goals/CG_02", {"id": "CG_02", "name": "ok", "_etag": tag2})[0] == 303
    # YAML tab: whole-file fingerprint
    _, _, page = req(f"/m/{mid}/yaml")
    tag3 = re.search(r'name="_etag" value="(\w+)"', page)[1]
    text = (fresh(mid)).read_text()  # changed on disk meanwhile
    status, _, body = req(f"/m/{mid}/yaml", {"text": text, "_etag": tag3})
    assert status == 200 and "changed since you opened" in body
    assert req(f"/m/{mid}/yaml", {"text": text})[0] == 303  # no fingerprint: not checked


def test_csv_exports():
    status, h, body = req("/m/sample/export/threats.csv")
    assert status == 200 and "text/csv" in h["content-type"] and body.startswith("﻿id,")
    assert body.splitlines()[0].endswith(",risk")  # derived column appended
    status, _, body = req("/m/sample/report.csv")
    assert status == 200 and body.startswith("﻿Risk,ID,Threat")
    assert req("/m/sample/export/nope.csv")[0] == 404


def test_import_and_zip_round_trip(mid):
    text = SAMPLE.read_bytes()
    name = f"{mid}-imported"
    status, h, _ = multipart("/models/import", [(f"{name}.yaml", text)])
    assert status == 303 and h["location"].endswith(f"imported={name}")
    status, _, body = multipart("/models/import", [(f"{name}.yaml", text)])
    assert status == 422 and "already exists" in body  # never overwritten
    status, _, body = multipart("/models/import", [("bad.yaml", b"system: [oops")])
    assert status == 422 and "bad.yaml" in body
    # The zip holds every model verbatim; importing it back restores what is missing
    # and refuses (422) every model that still exists.
    base, root = server()
    with urllib.request.urlopen(base + "/models.zip") as resp:
        data = resp.read()
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert f"{name}.yaml" in names and f"{mid}.yaml" in names and "sample.yaml" in names
    (root / f"{name}.yaml").unlink()
    status, _, body = multipart("/models/import", [("backup.zip", data)])
    assert status == 422 and "sample.yaml: model sample already exists" in body
    assert (root / f"{name}.yaml").read_bytes() == text


def test_copy_model(mid):
    status, h, _ = req(f"/m/{mid}/copy", {"id": f"{mid}-copy"})
    assert status == 303 and h["location"] == f"/m/{mid}-copy/settings"
    status, h, _ = req(f"/m/{mid}/copy", {"id": f"{mid}-copy"})
    assert status == 303 and "copy_error" in h["location"]


def test_history_diff_and_restore(mid):
    path = server()[1] / f"{mid}.yaml"
    req(f"/m/{mid}/goals/CG_01", {"id": "CG_01", "name": "v1"})
    req(f"/m/{mid}/goals/CG_01", {"id": "CG_01", "name": "v2"})
    _, _, page = req(f"/m/{mid}/settings")
    shas = re.findall(rf'href="/m/{mid}/history/([0-9a-f]+)"', page)
    assert len(shas) >= 2
    status, _, body = req(f"/m/{mid}/history/{shas[1]}")  # the v1 commit
    assert status == 200 and re.search(r'<span class="add">\+\s+name: v1</span>', body)
    status, _, _ = req(f"/m/{mid}/history/{shas[1]}/restore", {})
    assert status == 303 and "name: v1" in path.read_text()
    assert "restore " + shas[1] in req(f"/m/{mid}/settings")[2]
    assert req(f"/m/{mid}/history/nothex")[0] == 404


def test_diagram_highlight():
    status, _, body = req("/m/sample/diagram.svg?highlight=TS_01")
    assert status in (200, 503)  # 503 without Graphviz
    _, _, body = req("/m/sample/report")
    assert 'href="/m/sample/diagram.svg?highlight=TS_01"' in body
