#!/usr/bin/env python3
"""Undo a vesktopctl installation using ~/.vesktopctl-experimental backups."""

import argparse
import errno
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys

sys.dont_write_bytecode = True
from install import BEGIN, END, atomic_write, file_hash, strip_patch
import vesktop_process


def ensure_stopped(runtime: Path) -> None:
    address = runtime / "vesktopctl/control.sock"
    if not address.exists():
        return
    if not stat.S_ISSOCK(address.lstat().st_mode):
        raise ValueError(f"Socket path occupied by another file: {address}")
    client = socket.socket(socket.AF_UNIX)
    try:
        client.settimeout(1)
        client.connect(str(address))
    except ConnectionRefusedError:
        address.unlink()  # Stale socket left by a crashed Vesktop.
    except OSError as error:
        raise ValueError(f"Cannot determine whether Vesktop is running: {error}") from error
    else:
        raise ValueError("Close Vesktop before uninstalling vesktopctl")
    finally:
        client.close()


def remove_our_override_permission(content: str) -> str:
    lines = content.splitlines(keepends=True)
    in_context = False
    result = []
    for line in lines:
        if line.startswith("["):
            in_context = line.strip() == "[Context]"
        if in_context and line.startswith("filesystems="):
            ending = "\n" if line.endswith("\n") else ""
            entries = line[len("filesystems="):].strip().split(";")
            entries = [item for item in entries if item and item not in ("xdg-run/vesktopctl", "xdg-run/vesktopctl:create")]
            if entries:
                result.append("filesystems=" + ";".join(entries) + ";" + ending)
            continue
        result.append(line)
    return "".join(result)


def has_our_override_permission(content: str) -> bool:
    in_context = False
    for line in content.splitlines():
        if line.startswith("["):
            in_context = line.strip() == "[Context]"
        if in_context and line.startswith("filesystems="):
            entries = line[len("filesystems="):].split(";")
            if any(item in ("xdg-run/vesktopctl", "xdg-run/vesktopctl:create") for item in entries):
                return True
    return False


def restore_file(path: Path, record: dict, backups: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"Managed path is no longer a regular file: {path}")
    current_hash = file_hash(path) if path.is_file() else None
    expected_hash = record.get("installed_sha")
    role = record.get("role")

    # A previous attempt may already have restored the file before saving the
    # manifest failed. Keep the backup until the whole uninstall succeeds.
    already_restored = record["existed"] and current_hash is not None and current_hash == file_hash(backups / record["backup"])
    if current_hash is None or current_hash == expected_hash or already_restored:
        if record["existed"]:
            backup = backups / record["backup"]
            atomic_write(path, backup.read_bytes(), record["mode"])
            if "mtime_ns" in record:
                os.utime(path, ns=(record["atime_ns"], record["mtime_ns"]))
        elif path.exists():
            path.unlink()
    elif role == "bundle":
        if path.is_file():
            content = path.read_bytes().decode("utf-8")
            if BEGIN in content or END in content:
                clean = strip_patch(content)
                atomic_write(path, clean.encode("utf-8"), stat.S_IMODE(path.stat().st_mode))
    elif role == "override":
        if path.is_file():
            content = path.read_bytes().decode("utf-8")
            original = (backups / record["backup"]).read_text(encoding="utf-8") if record["existed"] else ""
            clean = content if has_our_override_permission(original) else remove_our_override_permission(content)
            if clean != content:
                atomic_write(path, clean.encode("utf-8"), stat.S_IMODE(path.stat().st_mode))
    else:
        raise ValueError(f"File changed after installation; left untouched: {path}")


def uninstall(home: Path, runtime: Path) -> None:
    root = home / ".vesktopctl-experimental"
    manifest = root / "manifest.json"
    if not manifest.is_file():
        raise ValueError(f"Installation manifest not found: {manifest}")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if data.get("version") != 1:
        raise ValueError(f"Unsupported installation manifest: {manifest}")
    ensure_stopped(runtime)
    backups = root / "backups"
    errors = []
    tools = home / ".local/share/vesktopctl-experimental"

    # Keep a runnable uninstaller if restoring a bundle, CLI or override fails.
    records = sorted(list(data["files"].items()), key=lambda item: Path(item[0]).is_relative_to(tools))
    for name, record in records:
        path = Path(name)
        if errors and path.is_relative_to(tools):
            continue
        try:
            restore_file(path, record, backups)
            del data["files"][name]
            atomic_write(manifest, (json.dumps(data, indent=2) + "\n").encode(), 0o600)
            print(f"Restored: {path}")
        except (OSError, UnicodeError, ValueError) as error:
            errors.append(str(error))

    for name, record in sorted(list(data["directories"].items()), key=lambda item: len(item[0]), reverse=True):
        path = Path(name)
        try:
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise ValueError(f"Managed directory path changed: {path}")
            if record["existed"]:
                if record.get("mode_changed") and path.exists():
                    path.chmod(record["mode"])
            elif path.exists():
                try:
                    path.rmdir()  # Only removes an empty directory.
                except OSError as error:
                    if error.errno != errno.ENOTEMPTY:
                        raise
                    if any(Path(filename).is_relative_to(path) for filename in data["files"]):
                        raise ValueError(f"Directory still contains a managed file: {path}") from error
                    print(f"Kept nonempty directory: {path}")
            del data["directories"][name]
            atomic_write(manifest, (json.dumps(data, indent=2) + "\n").encode(), 0o600)
        except (OSError, ValueError) as error:
            errors.append(str(error))

    if errors:
        raise ValueError("Uninstall incomplete; backups retained in ~/.vesktopctl-experimental:\n" + "\n".join(errors))
    for backup in backups.iterdir():
        if backup.is_symlink() or not backup.is_file() or len(backup.name) != 64 or any(c not in "0123456789abcdef" for c in backup.name):
            raise ValueError(f"Unexpected file in backup directory; left untouched: {backup}")
        backup.unlink()
    manifest.unlink()
    backups.rmdir()
    root.rmdir()
    print("vesktopctl removed; original files and permissions restored.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-restart", action="store_true", help="do not restart Vesktop; close it manually before uninstalling")
    args = parser.parse_args()
    home = Path.home()
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    stopped = []
    failure = None
    try:
        manifest = home / ".vesktopctl-experimental/manifest.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        targets = data.get("targets") or (["Flatpak"] if any(
            record.get("role") == "override" for record in data["files"].values()
        ) else ["Native"])
        infos = [info for kind in targets if (info := vesktop_process.running(kind))]
        if args.no_restart and infos:
            raise ValueError("Close Vesktop before uninstalling with --no-restart")
        for info in infos:
            vesktop_process.stop(info)
            stopped.append(info)
        uninstall(home, runtime)
    except (OSError, UnicodeError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        failure = error
    finally:
        for info in stopped:
            try:
                vesktop_process.start(info)
            except (OSError, RuntimeError) as error:
                failure = error if failure is None else ValueError(f"{failure}; Vesktop restart failed: {error}")
    if failure:
        print(f"uninstall.py: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
