#!/usr/bin/env bash
# Everything a change must pass before it is pushed. CI runs the same steps.
#
#   scripts/check.sh           ruff (lint + format check) and pytest (~10 s)
#   scripts/check.sh --fix     ruff --fix and ruff format first, then the same
#   scripts/check.sh --full    also the browser UI test (scripts/ui.mjs) and
#                              scripts/parity.sh: needs node + Playwright and
#                              jsDelivr; run it after UI or app changes (~3 min)
set -euo pipefail
cd "$(dirname "$0")/.."
# uv renamed UV_NATIVE_TLS; keep the behaviour without the warning on every line.
if [ -n "${UV_NATIVE_TLS:-}" ]; then export UV_SYSTEM_CERTS=1; unset UV_NATIVE_TLS; fi

FIX=0 FULL=0
for a in "$@"; do
  case $a in
    --fix) FIX=1 ;;
    --full) FULL=1 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown option $a" >&2; exit 2 ;;
  esac
done

step() { printf '\n== %s\n' "$*"; }
if [ $FIX = 1 ]; then
  step "ruff fix + format"
  uv run ruff check --fix --quiet . || true
  uv run ruff format --quiet .
fi
step "ruff check";        uv run ruff check .
step "ruff format";       uv run ruff format --check .
step "mypy (mutation/storage/client boundaries)"; uv run mypy
step "pytest";            uv run pytest -q
if [ $FULL = 1 ]; then
  step "browser UI test"; uv run pytest -q -m browser
  step "parity";          scripts/parity.sh "${PARITY_BASE:-HEAD}"
fi
printf '\nall checks passed\n'
