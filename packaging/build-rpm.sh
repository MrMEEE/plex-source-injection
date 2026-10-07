#!/usr/bin/env bash
set -euo pipefail

VERSION="${1:?Usage: build-rpm.sh VERSION}"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Version must be MAJOR.MINOR.PATCH" >&2; exit 2; }
case "${EL_VERSION:?EL_VERSION required}" in
    9) PYTHON=/usr/bin/python3.12 ;;
    10) PYTHON=/usr/bin/python3 ;;
    *) echo "Only EL9 and EL10 are supported" >&2; exit 2 ;;
esac
TOP="$(mktemp -d /tmp/plex-rpm.XXXXXX)"
trap 'rm -rf -- "$TOP"' EXIT
mkdir -p "$TOP"/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS}
NAME="plex-source-injection-$VERSION"
mkdir -p "$TOP/$NAME"
# Explicit source allowlist: never include runtime databases, caches, secrets,
# managed executables or arbitrary untracked files in release archives.
cd /source
tar -cf - LICENSE README.md .env.example requirements.txt requirements-dev.txt \
    main.py config.py config_store.py cleanup.py ingest.py search.py runtime.py \
    web_admin.py admin_access.py admin_sessions.py activity_log.py listeners.py \
    dependencies.py plex_libraries.py plex_login.py service.sh release.sh VERSION \
    --exclude='__pycache__' --exclude='*.pyc' --exclude='*.sqlite*' \
    --exclude='*.db*' --exclude='.cache' --exclude='.env' --exclude='.env.*' \
    providers web tests packaging tools \
    | tar -xf - -C "$TOP/$NAME"
mkdir -p "$TOP/$NAME/wheelhouse"
"$PYTHON" -m pip download --only-binary=:all: \
    --dest "$TOP/$NAME/wheelhouse" -r requirements.txt
tar -czf "$TOP/SOURCES/plex-source-injection-wheelhouse-$VERSION.tar.gz" \
    -C "$TOP/$NAME" wheelhouse
tar -czf "$TOP/SOURCES/$NAME.tar.gz" --exclude=wheelhouse -C "$TOP" "$NAME"
sed -E "s/^Version:[[:space:]]+[0-9]+\.[0-9]+\.[0-9]+$/Version:        $VERSION/" \
    /source/packaging/plex-source-injection.spec > "$TOP/SPECS/plex-source-injection.spec"
rpmbuild -ba "$TOP/SPECS/plex-source-injection.spec" \
    --define "_topdir $TOP" --define "dist .el$EL_VERSION"

# Install the actual built RPMs; tests and smoke checks use their private dependencies.
dnf install -y "$TOP"/RPMS/*/*.rpm
"$PYTHON" -m pip install --target "$TOP/testlibs" pytest
LIBDIR="$(rpm --eval '%{_libdir}')/plex-source-injection/pythonlibs"
cd "$TOP/$NAME"
export PYTHONPATH="$LIBDIR:$TOP/testlibs:$TOP/$NAME"
"$PYTHON" -m pytest -q -p no:cacheprovider
/bin/bash /source/packaging/smoke-rpm.sh "$PYTHON"
mkdir -p "/output/el$EL_VERSION"
cp "$TOP"/RPMS/*/*.rpm "$TOP"/SRPMS/*.rpm "$TOP/SOURCES/$NAME.tar.gz" "/output/el$EL_VERSION/"
(cd "/output/el$EL_VERSION" && sha256sum ./*.rpm ./*.tar.gz > SHA256SUMS)
