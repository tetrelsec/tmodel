"""
Before/after and server/browser comparison on real models. Dev-only.

  compare.py crawl  BASE OUT     GET every page reachable from / (links, row
                                 partials, side panels, exports) and save bodies
  compare.py writes BASE OUT     the same edits on every model, saving the YAML
                                 after each step
  compare.py diff   A B [--mask-svg] [--ignore REGEX ...]
                                 compare two crawl/writes dirs; exit 1 on difference
  compare.py models DIR          fresh copy of examples/ models into DIR

BASE is any HTTP origin: `tmodel serve`, or `scripts/browser-proxy.mjs`, which
answers through the in-browser build's service worker. The crawler only speaks
HTTP, so both hosts are measured by the same code. Redirects are followed;
the final URL is recorded alongside the body.
"""

from __future__ import annotations

import argparse
import difflib
import html
import json
import re
import shutil
import sys
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MODEL_SOURCES = sorted((REPO / "examples").glob("*.yaml"))


# HTTP ------------------------------------------------------------------------


def fetch(
    base: str,
    path: str,
    data: dict | list | None = None,
    headers: dict | None = None,
    *,
    method: str | None = None,
    json_body=None,
):
    """Returns (status, final_path, content_type, body_text)."""
    body = None
    h = dict(headers or {})
    if data is not None:
        body = urllib.parse.urlencode(data, doseq=True).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    if json_body is not None:
        body = json.dumps(json_body).encode()
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(
        base + path, data=body, headers=h, method=method or ("POST" if body is not None else "GET")
    )
    try:
        resp = urllib.request.urlopen(r, timeout=60)
    except urllib.error.HTTPError as e:
        resp = e
    final = resp.headers.get("X-Final-Url") or resp.geturl()
    final = urllib.parse.urlsplit(final)
    final_path = final.path + (f"?{final.query}" if final.query else "")
    return (
        resp.status,
        final_path,
        resp.headers.get("Content-Type", ""),
        resp.read().decode("utf-8", "replace"),
    )


# HTML scraping ---------------------------------------------------------------


class Links(HTMLParser):
    ATTRS = {"href", "data-row", "data-side", "data-add"}

    def __init__(self):
        super().__init__()
        self.found: list[str] = []

    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            if k in self.ATTRS and v and v.startswith("/"):
                self.found.append(v)
                if k == "data-row":
                    self.found.append(v + "/edit")


class Form(HTMLParser):
    """Successful controls of the form with `action` (or id `form_id`): fields inside
    it plus any with form="<id>", as a browser would submit them (minus buttons)."""

    def __init__(self, action: str | None = None, form_id: str | None = None):
        super().__init__(convert_charrefs=True)
        self.action, self.form_id = action, form_id
        self.inside = False
        self.fields: list[tuple[str, str]] = []
        self._ta: tuple[str, list[str]] | None = None
        self._select: str | None = None
        self._first_option: str | None = None
        self._selected = False

    def _mine(self, a: dict) -> bool:
        return self.inside or (self.form_id is not None and a.get("form") == self.form_id)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.inside = (self.action and a.get("action") == self.action) or (
                self.form_id and a.get("id") == self.form_id
            )
            if self.inside and not self.form_id:
                self.form_id = a.get("id")
            return
        if tag == "input" and self._mine(a) and a.get("name"):
            t = a.get("type", "text")
            if t in ("checkbox", "radio"):
                if "checked" in a:
                    self.fields.append((a["name"], a.get("value", "on")))
            elif t not in ("submit", "button"):
                self.fields.append((a["name"], a.get("value", "")))
        elif tag == "textarea" and self._mine(a) and a.get("name"):
            self._ta = (a["name"], [])
        elif tag == "select" and self._mine(a) and a.get("name"):
            self._select, self._first_option, self._selected = a["name"], None, False
        elif tag == "option" and self._select:
            v = a.get("value", "")
            if self._first_option is None:
                self._first_option = v
            if "selected" in a and not self._selected:
                self.fields.append((self._select, v))
                self._selected = True

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False
        elif tag == "textarea" and self._ta:
            self.fields.append((self._ta[0], "".join(self._ta[1])))
            self._ta = None
        elif tag == "select" and self._select:
            if not self._selected and self._first_option is not None:
                self.fields.append((self._select, self._first_option))
            self._select = None

    def handle_data(self, data):
        if self._ta:
            self._ta[1].append(data)


def form_fields(page: str, action: str | None = None, form_id: str | None = None) -> list[tuple[str, str]]:
    p = Form(action, form_id)
    p.feed(page)
    # A leading newline in a textarea is dropped by browsers.
    return [(k, v[1:] if v.startswith("\n") else v) for k, v in p.fields]


def setf(fields: list[tuple[str, str]], name: str, value: str) -> list[tuple[str, str]]:
    out = [(k, v) for k, v in fields if k != name]
    return [*out, (name, value)]


def api_model(base: str, mid: str) -> tuple[dict, str]:
    with urllib.request.urlopen(base + f"/api/models/{mid}", timeout=60) as response:
        return json.loads(response.read()), response.headers.get("ETag", "")


def model_ids(base: str) -> list[str]:
    _, _, _, body = fetch(base, "/api/models")
    return [m["id"] for m in json.loads(body)]


# Commands --------------------------------------------------------------------


def save(out: Path, key: str, status: int, final: str, ctype: str, body: str) -> None:
    name = urllib.parse.quote(key, safe="") or "_root"
    (out / f"{name}.txt").write_text(f"{status} {final} {ctype.split(';')[0]}\n{body}")


def cmd_crawl(a) -> int:
    out = Path(a.out)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    seen: set[str] = set()
    queue = ["/", "/api/models"]
    while queue:
        path = html.unescape(queue.pop(0))
        if path in seen or path.startswith("/__dev"):
            continue
        seen.add(path)
        status, final, ctype, body = fetch(a.base, path)
        save(out, path, status, final, ctype, body)
        if "html" in ctype:
            p = Links()
            p.feed(body)
            queue += [x for x in p.found if x not in seen]
    print(f"crawled {len(seen)} paths into {out}")
    return 0


def cmd_writes(a) -> int:
    """Same edits on every model; after each step save export.yaml and the raw YAML tab."""
    out = Path(a.out)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    base = a.base

    def snap(mid: str, step: str, resp=None):
        if resp:
            save(out, f"{mid}/{step}/response", *resp)
        save(out, f"{mid}/{step}/export.yaml", *fetch(base, f"/m/{mid}/export.yaml"))
        _, _, _, page = fetch(base, f"/m/{mid}/yaml")
        text = dict(form_fields(page, action=f"/m/{mid}/yaml")).get("text", "")
        save(out, f"{mid}/{step}/raw.yaml", 200, "", "text/yaml", text)

    for mid in model_ids(base):
        # 1. inline edit of the first threat (or domain): X-Inline partial back
        coll = "threats"
        _, _, _, api = fetch(base, f"/api/models/{mid}")
        model = json.loads(api)
        if not model.get("threats"):
            coll = "domains"
        if model.get(coll):
            eid = model[coll][0]["id"]
            _, _, _, row = fetch(base, f"/m/{mid}/{coll}/{eid}/row/edit")
            fields = form_fields(row, form_id=f"row-{eid}")
            col = "description" if coll == "threats" else "name"
            old = dict(fields).get(col, "")
            fields = setf(fields, col, old + " (edited)")
            r = fetch(base, f"/m/{mid}/{coll}/{eid}/row", fields, {"X-Inline": "1"})
            snap(mid, "1-inline-edit", r)

        # 2. add a domain through the full form, then 3. delete it
        _, _, _, page = fetch(base, f"/m/{mid}/domains/new")
        fields = form_fields(page, action=f"/m/{mid}/domains/new")
        fields = setf(setf(fields, "id", "DOM_ZZ_COMPARE"), "name", "Compare domain")
        r = fetch(base, f"/m/{mid}/domains/new", fields)
        snap(mid, "2-add-domain", r)
        r = fetch(base, f"/m/{mid}/domains/DOM_ZZ_COMPARE/delete", [])
        snap(mid, "3-delete-domain", r)

        # 4. settings: bump the revision
        _, _, _, page = fetch(base, f"/m/{mid}/settings")
        fields = form_fields(page, action=f"/m/{mid}/settings")
        rev = int(dict(fields).get("revision") or 1)
        r = fetch(base, f"/m/{mid}/settings", setf(fields, "revision", str(rev + 1)))
        snap(mid, "4-settings", r)

        # 5. YAML tab with a comment (kept verbatim)
        _, _, _, page = fetch(base, f"/m/{mid}/yaml")
        text = dict(form_fields(page, action=f"/m/{mid}/yaml"))["text"]
        r = fetch(base, f"/m/{mid}/yaml", [("text", "# compared\n" + text)])
        snap(mid, "5-yaml-comment", r)

        # 6. an invalid save is refused and nothing changes
        r = fetch(base, f"/m/{mid}/yaml", [("text", text.replace("schema_version", "schema_versio", 1))])
        snap(mid, "6-yaml-invalid", r)

        # Conditional agent operations, on both real Python and Pyodide.
        # Older baseline apps have no validation route; record that difference.
        current, tag = api_model(base, mid)
        r = fetch(base, "/api/validate", method="POST", json_body=current)
        snap(mid, "7-api-validate", r)
        if r[0] == 404:
            continue
        assert r[0] == 200, r
        entity = {"id": "CG_ZZ_COMPARE", "name": "Agent comparison goal"}
        endpoint = f"/api/models/{mid}/goals"
        r = fetch(base, endpoint + "?dry_run=1", headers={"If-Match": tag}, method="POST", json_body=entity)
        assert r[0] == 200 and api_model(base, mid) == (current, tag), r
        snap(mid, "8-api-preview", r)
        r = fetch(base, endpoint, headers={"If-Match": tag}, method="POST", json_body=entity)
        assert r[0] == 200, r
        snap(mid, "9-api-add", r)
        r = fetch(base, endpoint, headers={"If-Match": tag}, method="POST", json_body=entity)
        assert r[0] == 412, r
        snap(mid, "10-api-conflict", r)
        _, tag = api_model(base, mid)
        r = fetch(base, endpoint + "/CG_ZZ_COMPARE", headers={"If-Match": tag}, method="DELETE")
        assert r[0] == 200, r
        snap(mid, "11-api-delete", r)
        with urllib.request.urlopen(base + f"/api/models/{mid}/snapshot", timeout=60) as response:
            import hashlib

            raw = response.read()
            assert hashlib.sha256(raw).hexdigest() == response.headers["X-Snapshot-SHA256"]
            save(out, f"{mid}/12-api-snapshot", response.status, "", "application/yaml", raw.decode())
    print(f"writes done into {out}")
    return 0


SVG = re.compile(r"<svg.*?</svg>", re.S)


def normalise(text: str, mask_svg: bool, ignore: list[re.Pattern]) -> list[str]:
    if mask_svg:
        text = SVG.sub("<svg>…</svg>", text)
    for rx in ignore:
        text = rx.sub("", text)
    text = re.sub(r">\s+<", ">\n<", text)
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def cmd_diff(a) -> int:
    A, B = Path(a.a), Path(a.b)
    ignore = [re.compile(x, re.S) for x in a.ignore]
    fa = {p.relative_to(A) for p in A.rglob("*.txt")}
    fb = {p.relative_to(B) for p in B.rglob("*.txt")}
    bad = 0
    for p in sorted(fa ^ fb):
        print(f"only in {'A' if p in fa else 'B'}: {urllib.parse.unquote(str(p))}")
        bad += 1
    for p in sorted(fa & fb):
        la = normalise((A / p).read_text(), a.mask_svg, ignore)
        lb = normalise((B / p).read_text(), a.mask_svg, ignore)
        if la != lb:
            bad += 1
            print(f"--- {urllib.parse.unquote(str(p))}")
            d = list(difflib.unified_diff(la, lb, "A", "B", n=1, lineterm=""))
            print("\n".join(d[2 : 2 + a.lines]) + ("\n  …" if len(d) > a.lines + 2 else ""))
    print(f"{len(fa & fb)} common files, {bad} differ or missing")
    return 1 if bad else 0


def cmd_models(a) -> int:
    d = Path(a.dir)
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    for src in MODEL_SOURCES:
        shutil.copy(src, d / src.name)
    print("\n".join(s.name for s in MODEL_SOURCES))
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(required=True)
    c = sub.add_parser("crawl")
    c.add_argument("base")
    c.add_argument("out")
    c.set_defaults(fn=cmd_crawl)
    w = sub.add_parser("writes")
    w.add_argument("base")
    w.add_argument("out")
    w.set_defaults(fn=cmd_writes)
    d = sub.add_parser("diff")
    d.add_argument("a")
    d.add_argument("b")
    d.add_argument("--mask-svg", action="store_true")
    d.add_argument("--ignore", action="append", default=[], help="regex removed from both sides")
    d.add_argument("--lines", type=int, default=12)
    d.set_defaults(fn=cmd_diff)
    m = sub.add_parser("models")
    m.add_argument("dir")
    m.set_defaults(fn=cmd_models)
    a = p.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
