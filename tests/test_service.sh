#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TEMP="$(mktemp -d)"
export SERVICE_STATE_DIR="$TEMP/service"
export CONFIG_DB="$TEMP/config.sqlite3"
export PROXY_PORT="${SERVICE_TEST_PORT:-18766}"
export ADMIN_PORT="${SERVICE_TEST_ADMIN_PORT:-18767}"
export RETENTION_DAYS=0
export ENABLED_PROVIDERS=""
export START_TIMEOUT=10
export STOP_TIMEOUT=10
unrelated=""

cleanup() {
    "$ROOT/service.sh" stop >/dev/null 2>&1 || true
    if [[ -n "$unrelated" ]]; then
        kill "$unrelated" 2>/dev/null || true
        wait "$unrelated" 2>/dev/null || true
    fi
    rm -f -- "$TEMP/config.sqlite3" "$TEMP/config.sqlite3-journal" \
        "$TEMP/service/proxy.pid" "$TEMP/service/proxy.log" "$TEMP/service/control.lock" \
        "$TEMP/conflict/proxy.pid" "$TEMP/conflict/proxy.log" "$TEMP/conflict/control.lock"
    rmdir -- "$TEMP/service" "$TEMP/conflict" "$TEMP" 2>/dev/null || true
}
trap cleanup EXIT

expect_exit() {
    local expected="$1" actual=0
    shift
    "$@" || actual=$?
    if [[ "$actual" != "$expected" ]]; then
        echo "Expected exit $expected, got $actual: $*" >&2
        exit 1
    fi
}

cd -- /tmp
expect_exit 2 "$ROOT/service.sh" invalid
expect_exit 3 "$ROOT/service.sh" status
"$ROOT/service.sh" stop
expect_exit 1 env PYTHON_BIN="$TEMP/missing-python" "$ROOT/service.sh" start

"$ROOT/service.sh" start
read -r first stamp < "$SERVICE_STATE_DIR/proxy.pid"
"$ROOT/service.sh" start
read -r duplicate stamp < "$SERVICE_STATE_DIR/proxy.pid"
[[ "$first" == "$duplicate" ]]
"$ROOT/service.sh" status
[[ "$(curl --noproxy '*' --silent --output /dev/null --write-out '%{http_code}' \
    "http://127.0.0.1:$ADMIN_PORT/admin/")" == 303 ]]
[[ "$(curl --noproxy '*' --silent --output /dev/null --write-out '%{http_code}' \
    "http://127.0.0.1:$PROXY_PORT/admin/login")" == 404 ]]
[[ "$(curl --noproxy '*' --silent --output /dev/null --write-out '%{http_code}' \
    "http://127.0.0.1:$ADMIN_PORT/identity")" == 404 ]]

# A second tracked instance must fail cleanly without stopping the first.
expect_exit 1 env SERVICE_STATE_DIR="$TEMP/conflict" "$ROOT/service.sh" start
"$ROOT/service.sh" status
[[ ! -f "$TEMP/conflict/proxy.pid" ]]

"$ROOT/service.sh" restart
read -r second stamp < "$SERVICE_STATE_DIR/proxy.pid"
[[ "$first" != "$second" ]]
"$ROOT/service.sh" stop
"$ROOT/service.sh" stop
expect_exit 3 "$ROOT/service.sh" status

# An unrelated live process with a different start stamp must never be signaled.
sleep 60 &
unrelated=$!
printf '%s 0\n' "$unrelated" > "$SERVICE_STATE_DIR/proxy.pid"
expect_exit 3 "$ROOT/service.sh" status
"$ROOT/service.sh" stop
kill -0 "$unrelated"
[[ ! -f "$SERVICE_STATE_DIR/proxy.pid" ]]

echo "Lifecycle integration checks passed."
