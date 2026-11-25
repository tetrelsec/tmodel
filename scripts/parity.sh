#!/usr/bin/env bash
# Compare the app across versions and hosts on every model in examples/.
#
#   scripts/parity.sh [BASE_REF]      (default: HEAD)
#
# 1. before/after   server at BASE_REF vs the working tree. Differences are
#                   printed for review: they should be exactly what you meant to change.
# 2. server/browser the working tree on uvicorn vs the in-browser build (Pyodide
#                   in a service worker). Must be identical, except the diagram
#                   SVG (dot+cairo vs viz.js), the footer naming the host and
#                   the browser's page script. Exit status 1 if not.
#
# Each side is crawled (every page reachable from /, row partials, exports) and
# put through the same edits (inline edit, add/delete, settings, YAML with a
# comment, an invalid save). Then scripts/ui.mjs drives the browser build's page
# script (the server side is tests/test_ui.py). Needs: uv, node + Playwright,
# internet (Pyodide CDN).
# Output and logs: ${PARITY_OUT:-.parity}/
set -euo pipefail
cd "$(dirname "$0")/.."
REF=${1:-HEAD}
OUT=$(realpath -m "${PARITY_OUT:-.parity}")
rm -rf "$OUT"; mkdir -p "$OUT"
PY="$PWD/.venv/bin/python"
PIDS=()
cleanup() {
  for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null || true; done
  for p in "${PIDS[@]}"; do wait "$p" 2>/dev/null || true; done
  git worktree remove --force "$OUT/base" 2>/dev/null || true
}
trap cleanup EXIT

port() { "$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])'; }
wait_http() { for _ in $(seq 1 120); do curl -sf -o /dev/null "$1" && return 0; sleep 0.5; done; echo "no answer from $1" >&2; return 1; }
serve() {  # serve NAME [cwd]: start a fresh server; set URL in this shell (not a subshell)
  local name=$1 cwd=${2:-$PWD} p; p=$(port)
  # The baseline needs its own examples: new schema fields may not be readable
  # by the old app. Each host comparison still uses identical current models.
  "$PY" "$cwd/scripts/compare.py" models "$OUT/models-$name" >/dev/null
  (cd "$cwd" && exec "$PY" -m tmodel serve "$OUT/models-$name" --no-git --no-reload --port "$p" >"$OUT/$name.log" 2>&1) &
  PIDS+=($!); URL="http://127.0.0.1:$p"; wait_http "$URL/"
}
browser() {  # browser NAME BUILD_URL: proxy a fresh browser profile; echo its URL
  local p; p=$(port)
  node scripts/browser-proxy.mjs "$2" "$p" >"$OUT/$1.log" 2>&1 &
  PIDS+=($!); URL="http://127.0.0.1:$p"; wait_http "$URL/api/models"
}
run() { "$PY" scripts/compare.py "$@"; }

echo "== building the in-browser version"
uv run --quiet tmodel build-web -o "$OUT/web" --seed examples
WP=$(port)
(cd "$OUT/web" && exec "$PY" -m http.server "$WP" --bind 127.0.0.1 >"$OUT/web.log" 2>&1) &
PIDS+=($!)
wait_http "http://127.0.0.1:$WP/index.html"

echo "== before/after: server at $REF vs working tree"
git worktree add --force --detach "$OUT/base" "$REF" -q
serve base-crawl "$OUT/base"
run crawl "$URL" "$OUT/base-crawl" >/dev/null
serve base-writes "$OUT/base"
run writes "$URL" "$OUT/base-writes" >/dev/null
serve after-crawl
run crawl "$URL" "$OUT/after-crawl" >/dev/null
serve after-writes
run writes "$URL" "$OUT/after-writes" >/dev/null
run diff "$OUT/base-crawl" "$OUT/after-crawl" --lines 6 >"$OUT/before-after.txt" || true
run diff "$OUT/base-writes" "$OUT/after-writes" --lines 6 >>"$OUT/before-after.txt" || true
grep -E "common files" "$OUT/before-after.txt" | sed 's/^/   /'
echo "   details: $OUT/before-after.txt"

echo "== server vs browser"
browser browser-crawl "http://127.0.0.1:$WP"
run crawl "$URL" "$OUT/browser-crawl" >/dev/null
browser browser-writes "http://127.0.0.1:$WP"
run writes "$URL" "$OUT/browser-writes" >/dev/null
HOST=(--mask-svg --ignore '<footer class="host noprint">[^<]*</footer>'
      --ignore '<script>\s*window\.tmodelBeforeReload.*?</script>')
status=0
run diff "$OUT/after-crawl" "$OUT/browser-crawl" "${HOST[@]}" >"$OUT/server-browser.txt" || status=1
run diff "$OUT/after-writes" "$OUT/browser-writes" "${HOST[@]}" >>"$OUT/server-browser.txt" || status=1
grep -E "common files" "$OUT/server-browser.txt" | sed 's/^/   /'
[ $status = 0 ] && echo "   PARITY OK" || { echo "   PARITY BROKEN: $OUT/server-browser.txt"; head -40 "$OUT/server-browser.txt"; }

echo "== page script in the browser build (scripts/ui.mjs)"
if node scripts/ui.mjs "http://127.0.0.1:$WP" >"$OUT/ui-browser.txt" 2>&1; then
  echo "   UI OK"
else
  status=1; echo "   UI BROKEN: $OUT/ui-browser.txt"; grep -A1 FAIL "$OUT/ui-browser.txt" | sed 's/^/   /'
fi
exit $status
