# tmodel

Structured threat models: a schema, a validator, a linter, and a diagram.
Close to an ISO/SAE 21434 TARA, plus **domains** (trust boundaries) and
**surfaces** (the interfaces that connect them).

## Run it

Install from a source checkout with [uv](https://docs.astral.sh/uv/). Run the
editor on a separate directory so model data stays out of the source repository.

```sh
uv tool install .
mkdir -p ~/tmodel-models
tmodel serve ~/tmodel-models --no-git
```

Open <http://127.0.0.1:8765> and start a new model, or import
[`examples/greenhouse.yaml`](examples/greenhouse.yaml). Add `--git` **instead of**
`--no-git` if you want a commit per save in a separate model repository.
To try the editor without installing it, use
`uvx --from . tmodel serve ~/tmodel-models --no-git`.

There is also an **in-browser build**: the same app running in Pyodide in a
service worker, with models kept in browser storage. In Chrome or Edge on a
computer, **Open folder…** on the model list saves them instead as YAML files in
a folder you pick (a git checkout, a synced folder): every save writes the file,
edits made on disk show up, and nothing is lost if the browser clears its
storage. Run `uv run tmodel build-web -o dist/web` from the source checkout
to generate a static site you can host separately.

## CLI

```
tmodel serve  [DIR] [--git|--no-git] [--port 8765] [--open] [--no-reload]
tmodel build-web [-o dist/web] [--serve|--watch] [--seed DIR]   static in-browser build
tmodel check  model.yaml [--info] [--strict]   validate + lint
tmodel export model.yaml [-f json] [-o out]     normalised YAML, or JSON for agents
tmodel schema [-o out.json]                     JSON Schema for editors
tmodel draw   model.yaml out.svg [--no-threats] diagram (.svg/.png/.pdf/.dot; needs graphviz)
```

Run as `python -m tmodel ...` without installing. Diagrams need the Graphviz
`dot` binary; everything else is pure Python.

## Deployment safety

The server editor has no built-in authentication or per-model permissions.
Keep it on loopback for local use. To share it with trusted users, put it
behind an authenticated proxy. Cross-origin
browser writes are refused, mutation requests are limited to 8 MiB, and ZIP
imports are bounded before expansion. CSV downloads neutralize spreadsheet
formulas. Use the server, not browser folder mode, for concurrent agents.
The server's write lock coordinates app processes on one host; external file
editors do not honor it. Atomic writes are not a substitute for backups.

Keep private models in a separate repository. Publish browser builds without
`--seed`; the default build contains no models.

## Shape

```
domains    DOM_*   trust boundaries; optional parent: DOM_*; scope: in | out
surfaces   SURF_*  connect: [domains]; vector: Physical|Local|Adjacent|Network
assets     A_*     domain
damages    DS_*    asset, impact, properties [C,I,A]
threats    TS_*    damage, surface (or none), attack_path, feasibility, treatment
goals      CG_*    owner: product  — properties the product must provide (verifiable)
           CC_*    owner: external — claims: responsibilities placed on someone else
```

Rules of thumb:

- **IDs** are `PREFIX_` plus upper-case letters, digits, underscores. Numeric
  (`TS_01`) and named (`TS_CAN_UDS_FLASH`) both work. Never reuse one. Entity
  updates cannot rename IDs; deletion permanently reserves them in `retired_ids`.
  Raw YAML/API edits must retain that list and add any IDs they remove.
- **Nested domains** use an optional `parent` ID: a handset can contain an
  operating system and a modem, and the operating system can contain apps. Roots omit
  `parent`; existing flat models need no migration. Parents must exist and
  cycles (including self-parenting) are rejected. Diagrams draw nested boxes.
  Assets belong to their specific domain; scope stays explicit and surfaces
  still define connectivity—containment alone grants no access. There is no
  schema depth cap, though very deep diagrams may become impractical.
- **surface: none** means a design-level threat with no entry point. An
  attacker already on the device still enters through a surface (IPC, syscalls).
- **vector** lives on the surface. A threat inherits it; set it on the threat
  only when the attacker is *less* remote than the surface allows.
- **risk** is derived from impact × feasibility (default 21434-style matrix,
  overridable per model). It is never stored; change impact, feasibility or
  the model's matrix instead.
- **goals** and claims are one list. Reduce/Avoid needs a `product` goal,
  Share/Retain an `external` one. A prefix that disagrees with the owner is a
  warning, not an error, so changing the owner never forces an ID change.
- **attack_path** steps are strings, or `{text, via: SURF_X}` where a step
  crosses a boundary. With `via`, the linter checks the whole path reaches
  the asset's domain.
- `schema_version` is owned by the tool. `revision` is owned by the author and
  is what findings and verdicts reference.

Lint findings are `warn` (probably wrong) or `info` (deliberate, worth a look).
`--strict` makes warnings fail the run, for CI.

## Web editor

Plain HTML forms generated from the Pydantic models, with the diagram and
lint findings beside every page so each save shows its effect.

The schema is the source of truth: add a field, a nested type, a root field
or a whole new collection in `schema.py` and the tabs, tables, forms, settings
page and YAML dumps follow without touching `web/`. Field types without a
dedicated widget are edited as YAML inline until they get one. The only
hand-written UI knowledge is `TABLE_COLUMNS` / `DERIVED` in `web/tables.py`,
which are optional overrides for how a table looks.

No frontend framework or CDN: the only JavaScript is the page script
(`web/assets/page.js`, inlined into every page), and everything still works with it disabled. (The
in-browser build necessarily loads Pyodide and viz.js from jsDelivr.)
On a phone, table rows become labelled cards and still edit in place.

- Tables edit in place: click a row, change the cells, Save. Fields that are
  not table columns (attack path, rationale, links) are left untouched; the
  ↗ on each row opens the full form. Escape cancels.
- **+ Add** opens a blank row with the next ID suggested. It exists only in
  the browser until Save passes validation; **Save & next** chains rows for
  bulk entry. Required dropdowns on a new row start empty and must be chosen.
- One YAML file per model. `--git` commits every save with a message naming the
  entity changed; the settings tab shows the file's history. Several people
  can work on one repo the usual git way.
- Form saves rewrite the file in canonical form (comments dropped). The YAML tab
  saves verbatim, so comments survive if you edit there.
- `/api/models/<id>` serves JSON (`GET`) with a content-based `ETag`. Replacing
  it (`PUT`) requires that exact `If-Match` header; creation requires
  `If-None-Match: *`. Stale writes return 412; missing preconditions return 428.
  YAML saves are atomic; server writes are serialized and fsynced. A failed git
  commit explicitly reports that the file was saved but history was not recorded.
  Agents can also create/update/delete individual entities, dry-run changes,
  validate/lint, capture content-hashed YAML snapshots, and record run metadata.
  See [the agent API and Python client](docs/agent-api.md).
- Attack path steps are one per line; end a line with `@SURF_X` to mark the
  surface that step crosses.
- Navigation: IDs in table cells, lint findings, the report and search results
  link to what they name; every entity's form lists what references it (so a
  goal shows the threats it covers), and Delete refuses while anything does.
  The header box jumps to an ID (`ts3` finds `TS_03`) or searches every field.
- Tables filter as you type (`/`) and sort by any column header (enums in
  scale order). **group** beside a single-reference header (a damage's
  asset, a threat's damage) sections the table under a header per
  referenced entity, sorted within each; threats are grouped by damage
  by default. Keys: `n` new row, `Ctrl`+`Enter` save, `Esc` cancel.
- After a save, a message shows derived values (risk) and the lint findings
  on what you just saved. **Duplicate** on a form starts a new entity from it.
- Edits are not lost silently: leaving a form with unsaved changes asks
  first, and a save is refused if that entity (or, for the YAML tab and
  settings, the file) changed since the page was opened (another tab, an
  edit on disk). Saving again then overwrites, knowingly.
- With git, each commit in the settings history opens as a diff with
  **Restore this version** (a new commit; history is never rewritten).
  **Copy model** on settings starts a new engagement from an existing file.
- The model list imports `.yaml` files or a `.zip` of them (never
  overwriting), and **Download all** zips every model: the backup path for
  the in-browser build, whose storage the browser may evict (or use a folder,
  in Chrome or Edge: see above).
- CSV downloads: any table (every field plus derived risk), and the threat
  register from the report tab (names resolved, worst first) for spreadsheets.
- The diagram highlights the entity a form is open on (a threat: its whole
  path); the report links each threat to that view.
- The **report** tab is the presentable view: a risk heat map (impact ×
  feasibility; click a cell to filter) and diagram, followed by an engineering
  review agenda for screen-sharing on a call: threats in tables grouped by
  damage scenario (highest impact first), then product goals and external claims
  mapped back to threats. Unassessed damage scenarios, unmapped goals, missing
  rationale and threats without goals are called out for discussion; a mapping
  is not verification. Stable ID links jump between these full-model views and
  each threat's detailed attack path, treatment and rationale (worst first).
  Heat-map filters apply only to that detail register. It is laid out for print, so the browser's
  "save as PDF" gives a usable deliverable.

## Layout

`tmodel/schema.py` is the source of truth; `lint.py`, `diagram.py` and
`report.py` read it; `tmodel/web/` is the editor. `examples/` holds sample
models (a fictional greenhouse, a nested-domain handset, and a minimal model).
The full map, and the rules for changing things, are in `CLAUDE.md`.

## Development

```
uv sync                   # venv + deps + dev group (pytest, ruff)
scripts/check.sh          # ruff + pytest, before every push (--fix formats first)
scripts/check.sh --full   # + browser UI test + parity.sh, after UI/app changes
scripts/dev.sh            # serve :8765 + in-browser build :8766, live reload; stop, logs
uv run tmodel schema -o threat-model.schema.json   # after changing schema.py
```

CI (`.github/workflows/ci.yml`) runs the same on every pull request. The
browser checks need node with Playwright (`npm i --no-save playwright` and
`npx playwright install chromium`) and access to jsDelivr.

## License

Copyright 2026 Tetrel Security. Licensed under the [Apache License 2.0](LICENSE).
