#!/bin/sh
set -eu

REPOSITORY='mag7ster/vesktopctl'
case "$REPOSITORY" in OWNER/*|*/REPO|'')
    printf '%s\n' 'vesktopctl: replace OWNER/REPO in install.sh with the GitHub repository first' >&2
    exit 1
esac
for dependency in curl python3; do
    if ! command -v "$dependency" >/dev/null 2>&1; then
        printf 'vesktopctl: required command not found: %s\n' "$dependency" >&2
        exit 1
    fi
done
workdir=$(mktemp -d "${TMPDIR:-/tmp}/vesktopctl-install.XXXXXXXX")
trap 'rm -rf "$workdir"' 0
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    "https://github.com/$REPOSITORY/archive/refs/heads/main.zip" -o "$workdir/source.zip"
python3 - "$workdir" <<'PY'
from pathlib import Path, PurePosixPath
import sys
import zipfile

root = Path(sys.argv[1])
names = ('install.py', 'uninstall.py', 'vesktop_process.py', 'vesktopctl', 'bridge.js', 'LICENSE')
try:
    with zipfile.ZipFile(root / 'source.zip') as archive:
        for name in names:
            matches = [entry for entry in archive.infolist()
                       if len(PurePosixPath(entry.filename).parts) == 2
                       and PurePosixPath(entry.filename).parts[0] not in ('.', '..')
                       and PurePosixPath(entry.filename).name == name]
            if len(matches) != 1:
                raise ValueError(f'expected one top-level {name} in the source archive')
            # Read only known files; archive paths are never used as destinations.
            (root / name).write_bytes(archive.read(matches[0]))
except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as error:
    print(f'vesktopctl: cannot unpack source archive: {error}', file=sys.stderr)
    sys.exit(1)
PY
python3 -B "$workdir/install.py" "$@"
