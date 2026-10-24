#!/usr/bin/env python3
"""Undo a vesktopctl installation using ~/.vesktopctl backups."""

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

from install import BEGIN, END, atomic_write, file_hash, strip_patch, saved_selections
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

    if not path.exists() and not record["existed"]:
        return
    if not path.exists() and record["existed"] and role not in ("bundle", "override"):
        atomic_write(path, (backups / record["backup"]).read_bytes(), record["mode"])
        return
    if record["existed"] and path.is_file():
        backup = backups / record["backup"]
        if backup.is_file() and current_hash == file_hash(backup):
            return  # An earlier interrupted attempt may already have restored it.
    if current_hash == expected_hash:
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


def tool_paths(home: Path) -> set[Path]:
    data_dir = home / ".local/share/vesktopctl"
    return {home / ".local/bin/vesktopctl"} | {
        data_dir / name for name in ("install.py", "uninstall.py", "vesktop_process.py", "bridge.js", "LICENSE")
    }


def uninstall(home: Path, runtime: Path, patch_only: bool = False) -> None:
    root = home / ".vesktopctl"
    manifest = root / "manifest.json"
    if not manifest.is_file():
        if root.exists():
            raise ValueError(f"Installation manifest not found: {manifest}")
        print("vesktopctl is already removed.")
        return
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if data.get("version") != 1:
        raise ValueError(f"Unsupported installation manifest: {manifest}")
    ensure_stopped(runtime)
    saved_selections(data, home, Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")))
    backups = root / "backups"
    tools = tool_paths(home)

    def save():
        atomic_write(manifest, (json.dumps(data, indent=2) + "\n").encode(), 0o600)

    def restore_phase(names, directories):
        errors = []
        for name in names:
            record = data["files"][name]
            if record.get("restored"):
                continue
            try:
                restore_file(Path(name), record, backups)
                record["restored"] = True
                save()
                print(f"Restored: {name}")
            except (OSError, UnicodeError, ValueError) as error:
                errors.append(str(error))
        for name in sorted(directories, key=len, reverse=True):
            path = Path(name)
            record = data["directories"][name]
            if record.get("restored"):
                continue
            try:
                if any(not item.get("restored") and Path(filename).is_relative_to(path)
                       for filename, item in data["files"].items()):
                    continue
                if path.is_symlink() or (path.exists() and not path.is_dir()):
                    raise ValueError(f"Managed directory path changed: {path}")
                if record["existed"]:
                    if record.get("mode_changed") and path.exists():
                        path.chmod(record["mode"])
                elif path.exists():
                    try:
                        path.rmdir()
                    except OSError as error:
                        if error.errno != errno.ENOTEMPTY:
                            raise
                        print(f"Kept nonempty directory: {path}")
                record["restored"] = True
                save()
            except (OSError, ValueError) as error:
                errors.append(str(error))
        if errors:
            raise ValueError("Removal incomplete; tools and backups retained in ~/.vesktopctl:\n" + "\n".join(errors))

    integration_files = [name for name in data["files"] if Path(name) not in tools]
    integration_dirs = [name for name in data["directories"]
                        if not any(tool.is_relative_to(Path(name)) for tool in tools)]
    # Complete integration rollback before removing the commands needed to retry.
    restore_phase(integration_files, integration_dirs)
    if not patch_only:
        tool_files = [name for name in data["files"] if Path(name) in tools]
        snapshots = {name: (Path(name).read_bytes(), stat.S_IMODE(Path(name).stat().st_mode))
                     for name in tool_files if Path(name).is_file() and not Path(name).is_symlink()
                     and not data["files"][name].get("restored")}
        try:
            restore_phase(tool_files,
                          [name for name in data["directories"] if name not in integration_dirs])
        except (OSError, UnicodeError, ValueError):
            # Keep the installed entry points usable when another tool conflicts.
            for name, (contents, mode) in snapshots.items():
                if data["files"][name].get("restored"):
                    Path(name).parent.mkdir(parents=True, exist_ok=True)
                    atomic_write(Path(name), contents, mode)
                    data["files"][name].pop("restored", None)
            for name, record in data["directories"].items():
                if name not in integration_dirs:
                    record.pop("restored", None)
            save()
            raise
    removed_files = integration_files if patch_only else list(data["files"])
    removed_dirs = integration_dirs if patch_only else list(data["directories"])
    for name in removed_files:
        record = data["files"][name]
        if record.get("backup"):
            (backups / record["backup"]).unlink(missing_ok=True)
        del data["files"][name]
    for name in removed_dirs:
        del data["directories"][name]
    save()
    if patch_only:
        print("Patch removed; run vesktopctl patch to enable it again.")
    else:
        backups.rmdir()
        manifest.unlink()
        root.rmdir()
        print("vesktopctl removed; original files and permissions restored.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--patch-only", action="store_true", help="remove integration but keep vesktopctl")
    parser.add_argument("--no-restart", action="store_true", help="require Vesktop to be closed; do not restart it")
    args = parser.parse_args(argv)
    home = Path.home()
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    stopped = []
    failure = None
    try:
        manifest = home / ".vesktopctl/manifest.json"
        if manifest.is_file():
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if data.get("version") != 1:
                raise ValueError(f"Unsupported installation manifest: {manifest}")
            targets = data.get("targets") or (["Flatpak"] if any(
                record.get("role") == "override" for record in data["files"].values()
            ) else ["Native"])
            running = [info for kind in targets if (info := vesktop_process.running(kind))]
            if args.no_restart and running:
                raise ValueError("Close Vesktop before using --no-restart")
            for info in running:
                stopped.append(info)
                vesktop_process.stop(info)
        uninstall(home, runtime, args.patch_only)
    except (OSError, UnicodeError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        failure = error
    finally:
        for info in stopped:
            try:
                vesktop_process.start(info)
            except (OSError, RuntimeError) as error:
                failure = error if failure is None else ValueError(f"{failure}; Vesktop restart failed: {error}")
    if failure:
        print(f"vesktopctl: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
