# tmodel

Structured threat models for embedded, operational technology, and transport
systems. Built by Tetrel Security.

## Ground rules

- **`tmodel/schema.py` is the source of truth.** The web UI, tables, forms,
  settings page, JSON Schema and YAML dumps are all derived from the Pydantic
  models. Never hand-code knowledge of a specific field into `tmodel/web/`.
  The only sanctioned overrides are `TABLE_COLUMNS` / `DERIVED` / `GROUP` in
  `web/tables.py`, and the report page, which renders what `tmodel/report.py`
  computes (domain knowledge lives there, like `diagram.py`).
  `tests/test_forms.py::test_ui_follows_schema_changes` enforces this; keep
  it passing.
- **Schema is still iterating.** `schema_version` stays `"0.1"` and there is
  no backwards compatibility until the shape settles. Change the models freely,
  update `examples/`, and regenerate `threat-model.schema.json`
  (`uv run tmodel schema -o threat-model.schema.json`); a test fails if it is stale.
- **IDs are stable and never reused.** `PREFIX_` + `[A-Z0-9_]+`; numeric and
  named both allowed. Deleted IDs remain in root `retired_ids`; every replacement
  must retain retirement history. Threat and goal IDs are the public API that
  findings will reference.
- **Derived values are not stored.** Risk comes from impact × feasibility
  (matrix in `schema.py`, overridable per model) and is never stored; the
  table shows it read-only via `DERIVED`. Vector comes from the surface; a
  threat may set it explicitly and the linter checks it against the surface.
- **`surface: none`** means a design-level threat with no entry point. An
  attacker already on the device still enters through a real surface (IPC,
  syscalls): model it.
- Lint rules are `warn` (probably wrong) or `info` (deliberate, worth a look).
  Every validator and lint rule has a test in `tests/test_schema.py`.
- The UI is server-rendered partials. Inline editing is the page script,
  `web/assets/page.js` (inlined into every page with `page.css`), which swaps
  `<tr>` elements returned by `.../{eid}/row[/edit]` (`eid` = `new` for a
  blank row that creates on POST); `X-Inline: 1` asks
  for a partial, otherwise the same POST redirects. Nothing is written until
  the whole model validates. Keep every interaction working without
  JavaScript (the ↗ full form always does).
- Models live as YAML files; `tmodel serve --git` commits each save. The
  file store (`web/store.py`) is the only thing to replace for a database.
- **Two hosts, one app.** `tmodel serve` (uvicorn) and `tmodel build-web` (a
  static site: Pyodide in a service worker, `web/browser.py` + `web/assets/sw.js`)
  run the same `create_app`. Everything host-specific goes through `Host` in
  `web/app.py` (label, diagram renderer, page script); the store is the same
  `FileStore` on IndexedDB with git off. Never branch on the host in routes or
  templates. Code that runs in the app must work in Pyodide: no subprocess or
  threads outside `Host` / the git paths of the store, no new compiled
  dependency unless Pyodide ships it (`static_build.plan_dependencies` decides
  what is vendored). Run `scripts/parity.sh` after UI or app changes: server
  vs browser must stay identical, and review its before/after diff.
- Phones: below 700px table rows become cards labelled by `data-label` (the
  column name). Keep new row cells labelled the same way.

## Workflow

```
uv sync
scripts/check.sh          # before every push: ruff + pytest (~10 s); --fix formats first
scripts/check.sh --full   # after UI/app changes: + browser UI test + parity.sh (~3 min)
scripts/dev.sh            # serve :8765 + build-web --watch :8766 on .dev/models; stop, logs
```

CI (`.github/workflows/ci.yml`) runs `check.sh`, the UI test and `parity.sh`
on every pull request; a red check is a bug to fix like a failing test.
`.claude/settings.json` lets agents run these commands (and read-only git)
without prompts, and runs ruff (import sort + format) on every edited `.py`.

Pieces, when you need one: `uv run pytest -m browser` (the page script in
Chromium: `scripts/ui.mjs` against a live server; deselected by default),
`scripts/parity.sh [BASE]` (server vs browser build on every example, before/
after against BASE, and `ui.mjs` on the browser build), `node scripts/shots.mjs
URL OUT` (phone/desktop screenshots), `scripts/browser-proxy.mjs` (the browser
build as an HTTP origin). Those need node + Playwright and jsDelivr access.

Tests: web tests start a real uvicorn server on a free port (no httpx).
`sample` is read-only; a test that writes takes the `mid` fixture (its own
fresh copy), so any order or subset passes. When the page script changes,
add a check to `scripts/ui.mjs`. Graphviz `dot` is optional at runtime and in
tests.

**Done means:** `scripts/check.sh` green (`--full` for UI/app changes, and its
parity before/after diff reviewed); docs updated (README features, this file's
Layout). When opening a PR, check CI and share the preview URL if the repository
has Cloudflare previews configured.

## Layout

```
tmodel/
  schema.py          Pydantic models, enums, risk matrix, derived accessors
  lint.py            Finding(entity, rule, level, message); lint(model)
  io.py              YAML load/dump (dump is minimal and order-stable)
  editing.py         pure validated entity mutations and ID retirement contracts
  client.py          stdlib conditional agent API client (no automatic write retries)
  diagram.py         Graphviz: nested domain boxes with assets, surfaces between; highlight
  report.py          threat register, heat map, damage review groups, goal mappings, register_table
  web/app.py         entry point: create_app, Host, and the one ordered URL table
  web/routes/        handlers by area: models (list, import, zip), api (conditional
                     model/entity writes, validate, hashed snapshots), entities
                     (tables, forms, <tr> partials), pages (yaml, settings, report,
                     diagram, search), exports, history
  web/context.py     Ctx: what every route shares (store, templates, rendering, save)
  web/tables.py      TABLE_COLUMNS / DERIVED / GROUP (default sections) overrides,
                     cell text and links, sorting
  web/forms.py       FieldSpec from Pydantic annotations; parse_form; refs_in / used_by
  web/store.py       FileStore: atomic YAML replacement, snapshot CAS, optional git
  web/guards.py      host-independent origin checks and bounded mutation bodies
  web/browser.py     in-browser host: ASGI bridge called by the service worker
  web/static_build.py  `build-web`: static site, vendored wheels, local serve/watch
  web/templates/     base.html (page shell), _row / _row_edit / _cell (inline
                     editing), _group (section header), one template per page
  web/assets/        page.css + page.js (inlined into every page), sw.js (Pyodide
                     host), folder.js (browser build: models in a folder on disk,
                     Chromium), index.html (boot shell), reload.js (live reload)
tests/               test_schema (every validator + lint rule), test_forms,
                     test_report, test_diagram, test_web (live server),
                     test_browser (bridge + build), test_ui (browser, opt-in)
scripts/             check.sh (Ruff + scoped mypy + pytest), dev.sh, ui.mjs
                     (page-script test), sw-test.mjs (persistence retry), parity.sh /
                     compare.py (cross-host diff), shots.mjs, browser-proxy.mjs
.github/workflows/   ci.yml: check.sh; UI test + parity.sh; dependency-audit.yml:
                     scheduled/PR locked-runtime vulnerability audit
.claude/settings.json  agent allowlist for the commands above; ruff on edited .py
docs/agent-api.md    HTTP/client contracts, provenance and snapshot limitations
examples/            fictional greenhouse, nested-domain handset, and minimal model
```

## Known simplifications (deliberate, revisit when they hurt)

- One threat → one damage (`Threat.damage` is scalar). 21434 allows many.
- Form saves rewrite the YAML canonically (comments lost); the YAML tab
  preserves them. No ruamel dependency yet.
- No Excel/Word rendering yet; clients sign off on those, so it is coming.
  Meanwhile: CSV of any table and of the register (`report.register_table`).
- No findings / evidence / agent-run entities yet. They will reference
  `(model id, revision, threat or goal id)`.
- Stale-edit protection is per form: an entity form or row carries a
  fingerprint of that entity (`_etag`), settings of the root fields, the
  YAML tab of the whole file. A mismatch refuses once; saving again
  overwrites. All app writes additionally compare source snapshots at commit time;
  the JSON API requires exact If-Match (or create-only If-None-Match: *). Git
  history is the recovery net, not concurrency control.
- Browser-driven tests (Playwright) are opt-in locally (`pytest -m browser`,
  `check.sh --full`) because they need node, Chromium and the CDN; CI always
  runs them. `tests/test_web.py` covers the endpoints the script calls,
  `tests/test_browser.py` the bridge and build.
- The diagram's asset grid relies on Graphviz edge-order behaviour that
  differs between versions (2.43 vs 16 reverse the order inside a domain).
- The browser build's storage is evictable and per device; it is a test bed.
  Folder mode (Chromium only: File System Access API) keeps models in a folder
  on disk instead. The page owns the handle (IndexedDB) and posts it to the
  service worker, which mirrors the folder into an in-memory root served by a
  second app instance; elsewhere the build stays on browser storage. A
  restarted worker gets the handle back from an open page or its own gate page.
  Chrome for Testing in cloud containers crashes restoring any stored handle,
  so `ui.mjs` skips that one check there.
- The deployed browser build seeds no models (`build-web` defaults to none):
  it is public to anyone with the URL. `--seed` is for local tests only.
- Cloudflare Access on the Worker redirects the module service worker's
  cookie-less script fetch and prevents the browser build from starting.
