# Working on tmodel

Read `CLAUDE.md` for the full architectural and testing rules. This file is
the entry point for coding agents; it supplements those conventions.

## Conventions

- `tmodel/schema.py` is the source of truth. Derive forms, tables, settings and
  serialization from its Pydantic models; do not hard-code fields in the web UI.
  `TABLE_COLUMNS` / `DERIVED` in `web/tables.py` are sanctioned overrides.
- Keep IDs stable and never reuse them. Do not store derived risk values.
- Schema changes require updated examples, tests and regenerated JSON Schema:
  `uv run tmodel schema -o threat-model.schema.json`.
- Server and Pyodide hosts share one app. Host-specific behavior belongs in
  `Host`, not route/template conditionals. Preserve no-JavaScript workflows.
- Add tests for validators/lint rules and browser assertions in `scripts/ui.mjs`
  when changing page behavior. Use the writable `mid` fixture rather than
  modifying shared sample models.
- Python targets 3.11+, Ruff line length 110. Use the locked uv environment.
- Never publish client models or seed a deployed browser build with private data.

## Setup, build and run

From the repository root:

```sh
uv sync --locked
# Node 22 matches CI; Graphviz supplies the optional dot renderer.
npm install --no-save --no-package-lock playwright@1.63.0
npx playwright install --with-deps chromium
uv run tmodel serve ~/tmodel-models --no-git
uv run tmodel build-web -o dist/web
```

Use a separate git repository for model data when running with `--git`; do not
let model saves commit into the source checkout. For local testing on both
hosts, use `scripts/dev.sh`.

## Test before pushing

```sh
scripts/check.sh                 # Ruff lint, format check, non-browser pytest
scripts/check.sh --fix           # apply formatting/fixes, then check
uv run pytest -q -m browser      # Chromium UI checks
scripts/check.sh --full          # all above + cross-host parity
```

Run `--full` after UI/app changes and review `.parity/before-after.txt`.
The browser build needs jsDelivr access. `uv run tmodel build-web -o dist/web`
builds the deployable static app; no frontend bundler is needed.

## Git and review

- Use a feature branch; keep changes focused and never force-push shared history.
- Use the remote configured for your checkout. No token belongs in files.
- For pull requests, check CI and share the preview URL if available.
- Update README for features and `CLAUDE.md`'s layout when architecture changes.
