#!/usr/bin/env bash
# Linux lifecycle helper for the proxy; run as the same user for every operation.
set -euo pipefail
umask 077

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$ROOT"
STATE_DIR="${SERVICE_STATE_DIR:-$ROOT/.run}"
PYTHON="${PYTHON_BIN:-$ROOT/.venv/bin/python}"
START_TIMEOUT="${START_TIMEOUT:-30}"
STOP_TIMEOUT="${STOP_TIMEOUT:-60}"

usage() {
    echo "Usage: $0 {start|stop|restart|status}"
}

ACTION="${1:-}"
if [[ $# != 1 || ! "$ACTION" =~ ^(start|stop|restart|status)$ ]]; then
    usage >&2
    exit 2
fi
for timeout in "$START_TIMEOUT" "$STOP_TIMEOUT"; do
    if [[ ! "$timeout" =~ ^[1-9][0-9]*$ ]]; then
        echo "START_TIMEOUT and STOP_TIMEOUT must be positive integers." >&2
        exit 2
    fi
done
for utility in flock nohup curl; do
    if ! command -v "$utility" >/dev/null; then
        echo "Required utility not found: $utility" >&2
        exit 1
    fi
done
mkdir -p -- "$STATE_DIR"
STATE_DIR="$(cd -- "$STATE_DIR" && pwd)"
PID_FILE="$STATE_DIR/proxy.pid"
LOG_FILE="$STATE_DIR/proxy.log"
exec 9>"$STATE_DIR/control.lock"
flock -x 9

PID=""
STAMP=""
PROCESS_STAMP=""
PROCESS_STATE=""

process_info() {
    local stat
    local -a fields
    [[ "$PID" =~ ^[1-9][0-9]*$ && -r "/proc/$PID/stat" ]] || return 1
    stat="$(cat -- "/proc/$PID/stat" 2>/dev/null)" || return 1
    # The process name in parentheses may itself contain spaces or parentheses.
    read -r -a fields <<< "${stat##*) }"
    [[ ${#fields[@]} -ge 20 ]] || return 1
    PROCESS_STATE="${fields[0]}"
    PROCESS_STAMP="${fields[19]}"
    [[ "$PROCESS_STATE" != Z && "$PROCESS_STATE" != X ]]
}

running() {
    [[ -f "$PID_FILE" ]] || return 1
    read -r PID STAMP < "$PID_FILE" || return 1
    [[ "$STAMP" =~ ^[0-9]+$ ]] || return 1
    process_info && [[ "$PROCESS_STAMP" == "$STAMP" ]]
}

stop() {
    if ! running; then
        rm -f -- "$PID_FILE"
        echo "Proxy is not running."
        return 0
    fi
    echo "Stopping proxy (PID $PID)..."
    if ! kill -TERM "$PID"; then
        echo "Could not signal PID $PID; PID file retained." >&2
        return 1
    fi
    local deadline=$((SECONDS + STOP_TIMEOUT))
    while running; do
        if (( SECONDS >= deadline )); then
            echo "Proxy is still shutting down after ${STOP_TIMEOUT}s. PID file retained; no forced kill." >&2
            echo "Active streams/downloads may need more time. Retry stop or increase STOP_TIMEOUT." >&2
            return 1
        fi
        sleep 0.2
    done
    rm -f -- "$PID_FILE"
    echo "Proxy stopped."
}

start() {
    if running; then
        echo "Proxy is already running (PID $PID). Log: $LOG_FILE"
        return 0
    fi
    rm -f -- "$PID_FILE"
    if [[ ! -x "$PYTHON" ]]; then
        echo "Python executable not found: $PYTHON" >&2
        echo "Create .venv and install requirements.txt, or set PYTHON_BIN to your interpreter." >&2
        return 1
    fi
    touch -- "$LOG_FILE"
    local offset deadline line port admin_port code
    offset=$(wc -c < "$LOG_FILE")
    printf '\n--- Proxy start: %s ---\n' "$(date -Is)" >> "$LOG_FILE"
    # Close the control lock in the child so later stop/status calls cannot deadlock.
    nohup "$PYTHON" -u "$ROOT/main.py" >> "$LOG_FILE" 2>&1 </dev/null 9>&- &
    PID=$!
    if ! process_info; then
        echo "Proxy exited immediately. See $LOG_FILE" >&2
        return 1
    fi
    STAMP="$PROCESS_STAMP"
    printf '%s %s\n' "$PID" "$STAMP" > "$PID_FILE"
    deadline=$((SECONDS + START_TIMEOUT))
    while running; do
        line="$(tail -c "+$((offset + 1))" "$LOG_FILE" | grep 'Proxy listener ready on port' | tail -n 1 || true)"
        if [[ "$line" =~ Proxy\ listener\ ready\ on\ port\ ([0-9]+)\;\ admin\ listener\ ready\ on\ port\ ([0-9]+) ]]; then
            port="${BASH_REMATCH[1]}"
            admin_port="${BASH_REMATCH[2]}"
            code="$(curl --noproxy '*' --silent --output /dev/null --write-out '%{http_code}' \
                --max-time 2 "http://127.0.0.1:$admin_port/admin/" || true)"
            if [[ "$code" =~ ^(200|303|401|403|503)$ ]]; then
                echo "Proxy started (PID $PID): http://127.0.0.1:$port"
                echo "Administration: http://127.0.0.1:$admin_port/admin/"
                echo "Log: $LOG_FILE"
                return 0
            fi
        fi
        if (( SECONDS >= deadline )); then
            echo "Proxy did not become ready after ${START_TIMEOUT}s. See $LOG_FILE" >&2
            stop || true
            return 1
        fi
        sleep 0.2
    done
    rm -f -- "$PID_FILE"
    echo "Proxy failed to start. See $LOG_FILE" >&2
    tail -n 15 -- "$LOG_FILE" >&2
    return 1
}

case "$ACTION" in
    start) start ;;
    stop) stop ;;
    restart) stop && start ;;
    status)
        if running; then
            echo "Proxy is running (PID $PID). Log: $LOG_FILE"
        else
            echo "Proxy is not running."
            exit 3
        fi
        ;;
esac
