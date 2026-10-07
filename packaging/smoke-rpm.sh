#!/usr/bin/env bash
set -euo pipefail
PYTHON="${1:?Python interpreter required}"
LIBDIR="$(rpm --eval '%{_libdir}')/plex-source-injection/pythonlibs"
systemd-analyze verify /usr/lib/systemd/system/plex-source-injection.service
runuser -u plex-source-injection -- env PYTHONPATH="$LIBDIR:/usr/share/plex-source-injection" \
    CONFIG_DB=/var/lib/plex-source-injection/config.sqlite3 \
    "$PYTHON" -s -c 'import fastapi, uvicorn, plexapi, yt_dlp, spotipy; from config_store import ConfigStore; s=ConfigStore.default(); s.initialize({"ENABLED_PROVIDERS":"","RETENTION_DAYS":"0","DOWNLOAD_DIR":"/var/lib/plex-source-injection/music"}); s.set_password("rpm-smoke-password-123")'
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
