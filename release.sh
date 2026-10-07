#!/usr/bin/env bash
# Version bump, release commit and tag; GitHub Actions builds and publishes RPMs.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -x "$ROOT/.venv/bin/python" ]]; then
    PYTHON="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null; then
    PYTHON="$(command -v python3)"
else
    echo "No Python 3 interpreter found. Create .venv or install Python 3." >&2
    exit 1
fi
exec "$PYTHON" "$ROOT/tools/release.py" "$@"
