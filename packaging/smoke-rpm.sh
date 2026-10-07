#!/usr/bin/env bash
set -euo pipefail
PYTHON="${1:?Python interpreter required}"
LIBDIR="$(rpm --eval '%{_libdir}')/plex-source-injection/pythonlibs"
systemd-analyze verify /usr/lib/systemd/system/plex-source-injection.service
test "$(command -v plex-inject-passwd)" = /usr/bin/plex-inject-passwd
if /usr/bin/plex-inject-passwd unexpected; then
    echo "Password helper accepted unexpected arguments" >&2
    exit 1
fi
if runuser -u nobody -- /usr/bin/plex-inject-passwd; then
    echo "Password helper accepted an unauthorized user" >&2
    exit 1
fi
printf '%s\n' rpm-initial-password-123 rpm-initial-password-123 | /usr/bin/plex-inject-passwd
test "$(stat -c %U /var/lib/plex-source-injection/config.sqlite3)" = plex-source-injection
printf '%s\n' rpm-smoke-password-123 rpm-smoke-password-123 | \
    runuser -u plex-source-injection -- /usr/bin/plex-inject-passwd
runuser -u plex-source-injection -- env PYTHONPATH="$LIBDIR:/usr/share/plex-source-injection" \
    CONFIG_DB=/var/lib/plex-source-injection/config.sqlite3 \
    "$PYTHON" -s -c 'import fastapi, uvicorn, plexapi, yt_dlp, spotipy; from config_store import ConfigStore; s=ConfigStore.default(); assert s.verify_password("rpm-smoke-password-123"); assert not s.verify_password("rpm-initial-password-123"); s.save({**s.values(), "ENABLED_PROVIDERS":"","RETENTION_DAYS":"0","DOWNLOAD_DIR":"/var/lib/plex-source-injection/music"})'
runuser -u plex-source-injection -- /usr/bin/plex-source-injection >/tmp/plex-rpm-smoke.log 2>&1 &
PID=$!
cleanup() {
    kill -TERM "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
}
trap cleanup EXIT
export PYTHONPATH="$LIBDIR"
"$PYTHON" -s - <<'PY'
import time
import httpx

with httpx.Client(timeout=2, trust_env=False, follow_redirects=False) as client:
    for attempt in range(100):
        try:
            response = client.get("http://127.0.0.1:32300/admin/login")
            if response.status_code == 200:
                break
        except httpx.ConnectError:
            pass
        time.sleep(0.1)
    else:
        raise SystemExit("Installed service did not become ready")
    assert 'method="post"' in response.text
    assert client.get("http://127.0.0.1:32399/admin/login").status_code == 404
    assert client.get("http://127.0.0.1:32300/identity").status_code == 404
    assert client.get("http://127.0.0.1:32300/admin/api/config").status_code == 401
print("Installed RPM listeners, service user and authentication smoke checks passed")
PY
