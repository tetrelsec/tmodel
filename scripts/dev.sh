#!/usr/bin/env bash
# The dev loop in one command: `tmodel serve` (live reload) and the in-browser
# build (`build-web --watch`), in the background, on a scratch copy of examples/.
#
#   scripts/dev.sh          start both (or report them if already up), print the URLs
#   scripts/dev.sh stop     stop both
#   scripts/dev.sh logs     tail both logs
#
# State lives in .dev/ (gitignored): models/, *.log, *.pid. Delete .dev/models to
# start again from examples/. Ports: TMODEL_PORT (8765), TMODEL_WEB_PORT (8766).
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -n "${UV_NATIVE_TLS:-}" ]; then export UV_SYSTEM_CERTS=1; unset UV_NATIVE_TLS; fi
DEV=.dev PORT=${TMODEL_PORT:-8765} WEB_PORT=${TMODEL_WEB_PORT:-8766}
mkdir -p "$DEV"

up() { curl -sf -o /dev/null "http://127.0.0.1:$1/$2"; }
stop() {  # each server runs in its own process group (setsid): stop the group, not just `uv`
  for n in serve web; do
    if [ -f "$DEV/$n.pid" ]; then kill -TERM -- "-$(cat "$DEV/$n.pid")" 2>/dev/null || true; rm -f "$DEV/$n.pid"; fi
  done
  for _ in $(seq 1 40); do up "$PORT" "" || up "$WEB_PORT" "index.html" || break; sleep 0.25; done
  echo "stopped"
}
case "${1:-start}" in
  stop) stop; exit 0 ;;
  logs) exec tail -n 30 -f "$DEV/serve.log" "$DEV/web.log" ;;
  start) ;;
  *) sed -n '2,10p' "$0"; exit 2 ;;
esac

if [ ! -d "$DEV/models" ]; then mkdir -p "$DEV/models" && cp examples/*.yaml "$DEV/models/"; fi
# Its own repository: .dev/ is inside this one (and ignored), so `serve --git`
# would otherwise try to commit model saves into the tmodel repo.
[ -d "$DEV/models/.git" ] || git -C "$DEV/models" init -q
if up "$PORT" ""; then echo "server already up"; else
  setsid uv run tmodel serve "$DEV/models" --git --host 0.0.0.0 --port "$PORT" >"$DEV/serve.log" 2>&1 &
  echo $! >"$DEV/serve.pid"
fi
if up "$WEB_PORT" "index.html"; then echo "browser build already up"; else
  setsid uv run tmodel build-web --watch --seed "$DEV/models" --host 0.0.0.0 --port "$WEB_PORT" >"$DEV/web.log" 2>&1 &
  echo $! >"$DEV/web.pid"
fi
for _ in $(seq 1 240); do up "$PORT" "" && up "$WEB_PORT" "index.html" && break; sleep 0.5; done
up "$PORT" "" || { echo "server did not start; see $DEV/serve.log" >&2; exit 1; }
up "$WEB_PORT" "index.html" || { echo "browser build did not start; see $DEV/web.log" >&2; exit 1; }
cat <<MSG
server         http://127.0.0.1:$PORT/       (models in $DEV/models, git commit per save)
browser build  http://127.0.0.1:$WEB_PORT/       (seeded from $DEV/models; needs jsDelivr)
logs: scripts/dev.sh logs    stop: scripts/dev.sh stop
MSG
