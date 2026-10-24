#!/usr/bin/env python3
"""Find Vesktop's active Vencord bundle and install vesktopctl."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile

# Installed helpers must not leave untracked __pycache__ files after uninstall.
sys.dont_write_bytecode = True
import vesktop_process


BEGIN = "/* vesktopctl-experimental begin */"
END = "/* vesktopctl-experimental end */"
START = "\n;" + BEGIN + "\n"
FINISH = END + "\n"
PATCH = (Path(__file__).resolve().parent / "injection.js").read_bytes().decode("utf-8")
APP_ID = "dev.vencord.Vesktop"
BUNDLE_NAME = "vencordDesktopMain.js"
FILES_DIR = Path("sessionData/vencordFiles")
STATE_DIR_NAME = ".vesktopctl-experimental"


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class InstallState:
    def __init__(self, home: Path):
        self.root = home / STATE_DIR_NAME
        self.manifest = self.root / "manifest.json"
        self.backups = self.root / "backups"
        if self.manifest.is_file():
            self.data = json.loads(self.manifest.read_text(encoding="utf-8"))
            if self.data.get("version") != 1:
                raise ValueError(f"Unsupported install state: {self.manifest}")
        else:
            if self.root.exists():
                raise ValueError(f"{self.root} exists without an install manifest; move it aside before installing")
            self.root.mkdir(mode=0o700)
            self.backups.mkdir(mode=0o700)
            self.data = {"version": 1, "files": {}, "directories": {}, "targets": []}
            self.save()

    def save(self) -> None:
        atomic_write(self.manifest, (json.dumps(self.data, indent=2) + "\n").encode(), 0o600)

    def directory(self, path: Path, mode_changed: bool = False) -> None:
        key = str(path)
        if key in self.data["directories"]:
            return
        if path.is_symlink():
            raise ValueError(f"Refusing to manage symlinked directory: {path}")
        existed = path.exists()
        if existed and not path.is_dir():
            raise ValueError(f"Directory path occupied: {path}")
        self.data["directories"][key] = {
            "existed": existed,
            "mode": stat.S_IMODE(path.stat().st_mode) if existed else None,
            "mode_changed": mode_changed,
        }
        self.save()

    def directory_tree(self, path: Path, create: bool = False, mode: int = 0o755, mode_changed: bool = False) -> None:
        missing = []
        cursor = path
        while not cursor.exists() and cursor != cursor.parent:
            missing.append(cursor)
            cursor = cursor.parent
        if cursor.is_symlink():
            raise ValueError(f"Refusing symlinked directory: {cursor}")
        for directory in reversed(missing):
            self.directory(directory, mode_changed=mode_changed and directory == path)
            if create:
                directory.mkdir(mode=mode)
        if not missing:
            self.directory(path, mode_changed=mode_changed)

    def file(self, path: Path, role: str = "file") -> None:
        key = str(path)
        if key in self.data["files"]:
            record = self.data["files"][key]
            if role == "bundle" and path.is_file() and record.get("installed_sha") != file_hash(path):
                metadata = path.stat()
                data = path.read_bytes()
                if BEGIN.encode() in data:
                    data = strip_patch(data.decode("utf-8")).encode("utf-8")
                backup_name = record["backup"]
                atomic_write(self.backups / backup_name, data, stat.S_IMODE(path.stat().st_mode))
                record.update({"mode": stat.S_IMODE(metadata.st_mode), "atime_ns": metadata.st_atime_ns, "mtime_ns": metadata.st_mtime_ns})
                self.save()
            elif role != "bundle" and path.is_file() and record.get("installed_sha") not in (None, file_hash(path)):
                raise ValueError(f"Managed file changed since installation: {path}")
            return
        if path.is_symlink():
            raise ValueError(f"Refusing to replace symlink: {path}")
        existed = path.exists()
        if existed and not path.is_file():
            raise ValueError(f"File path occupied: {path}")
        record = {"existed": existed, "role": role, "installed_sha": None}
        if existed:
            metadata = path.stat()
            backup_name = hashlib.sha256(key.encode()).hexdigest()
            backup = self.backups / backup_name
            data = path.read_bytes()
            if role == "bundle" and BEGIN.encode() in data:
                data = strip_patch(data.decode("utf-8")).encode("utf-8")
            atomic_write(backup, data, stat.S_IMODE(metadata.st_mode))
            record.update({
                "backup": backup_name,
                "mode": stat.S_IMODE(metadata.st_mode),
                "atime_ns": metadata.st_atime_ns,
                "mtime_ns": metadata.st_mtime_ns,
            })
        self.data["files"][key] = record
        self.save()

    def installed(self, path: Path) -> None:
        self.data["files"][str(path)]["installed_sha"] = file_hash(path) if path.is_file() else None
        self.save()

    def target(self, kind: str) -> None:
        targets = self.data.setdefault("targets", [])
        if kind not in targets:
            targets.append(kind)
            self.save()


def strip_patch(content: str) -> str:
    if content.count(BEGIN) != 1 or content.count(END) != 1:
        raise ValueError("Cannot remove an incomplete or duplicated experimental patch")
    start = content.find(START)
    end = content.find(FINISH)
    if start < 0 or end < start:
        raise ValueError("Invalid experimental patch boundaries")
    return content[:start] + content[end + len(FINISH):]


def refuse_conflicts(home: Path, bundle: Path) -> None:
    if (home / ".vesktopctl/manifest.json").exists():
        raise ValueError("The old vesktopctl is installed. Run its uninstall.py before installing the experiment.")
    if bundle.is_symlink() or not bundle.is_file():
        raise ValueError(f"Vencord bundle must be a regular file: {bundle}")
    content = bundle.read_bytes().decode("utf-8")
    if "/* vesktopctl begin */" in content or "VesktopCustomCommands Injection" in content:
        raise ValueError("An old vesktopctl/VCC patch is present. Uninstall it before installing the experiment.")
    if BEGIN in content or END in content:
        strip_patch(content)


def config_roots(home: Path, xdg_config: Path) -> list[tuple[str, Path]]:
    roots = [
        ("Flatpak", home / ".var/app" / APP_ID / "config/vesktop"),
        ("Native", xdg_config / "vesktop"),
    ]
    return [(kind, root) for kind, root in roots if root.is_dir()]


def active_bundle(kind: str, root: Path) -> Path | None:
    state_file = root / "state.json"
    if state_file.is_file():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError) as error:
            raise ValueError(f"Cannot read {state_file}: {error}") from error
        if not isinstance(state, dict):
            raise ValueError(f"Invalid Vesktop state file: {state_file}")
        custom = state.get("vencordDir")
        if custom:
            if not isinstance(custom, str) or not Path(custom).is_absolute():
                raise ValueError(f"Invalid vencordDir in {state_file}")
            candidate = Path(custom) / BUNDLE_NAME
            if not candidate.is_file():
                raise ValueError(
                    f"Vesktop {kind} uses {custom}, but its bundle is unavailable on the host. "
                    "Use --bundle with the host path. Flatpak document portal paths may be temporary."
                )
            return candidate

    current = root / FILES_DIR / BUNDLE_NAME
    return current if current.is_file() else None


def discover_bundle(home: Path, xdg_config: Path, target: str | None = None) -> tuple[Path, str]:
    found = []
    for kind, root in config_roots(home, xdg_config):
        if target and kind.lower() != target:
            continue
        bundle = active_bundle(kind, root)
        if bundle:
            found.append((bundle, kind))

    if not found and (not target or target == "native"):
        legacy = xdg_config / "Vencord/dist" / BUNDLE_NAME
        if legacy.is_file():
            found.append((legacy, "Native"))
    if not found and (not target or target == "flatpak"):
        legacy_flatpak = home / ".var/app" / APP_ID / "config/Vencord/dist" / BUNDLE_NAME
        if legacy_flatpak.is_file():
            found.append((legacy_flatpak, "Flatpak"))

    if not found:
        raise ValueError("No active Vencord bundle found. Start Vesktop once, then retry; or pass --bundle PATH.")
    if len(found) > 1:
        choices = "\n".join(f"  {kind}: {bundle}" for bundle, kind in found)
        raise ValueError(f"Multiple Vesktop installs found:\n{choices}\nUse --target flatpak|native or --bundle PATH.")
    return found[0]


def atomic_write(destination: Path, data: bytes, mode: int) -> None:
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=f".{destination.name}.", delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)


def copy_if_changed(source: Path, destination: Path, mode: int, state: InstallState) -> None:
    state.file(destination)
    data = source.read_bytes()
    if not destination.is_file() or destination.read_bytes() != data:
        atomic_write(destination, data, mode)
    state.installed(destination)


def patch_bundle(bundle: Path, bridge_source: Path, state: InstallState) -> None:
    if not bundle.is_file():
        raise ValueError(f"Vencord bundle not found: {bundle}")
    if bundle.is_symlink():
        raise ValueError("Refusing to patch a symlinked bundle")
    content = bundle.read_bytes().decode("utf-8")
    if "/* vesktopctl begin */" in content or "VesktopCustomCommands Injection" in content:
        raise ValueError("Uninstall the old vesktopctl/VCC patch first")
    clean = strip_patch(content) if BEGIN in content or END in content else content
    if not PATCH.startswith(START) or not PATCH.endswith(FINISH) or strip_patch(PATCH) != "":
        raise ValueError("Invalid injection.js")
    patched = clean + PATCH

    state.file(bundle, "bundle")
    bridge_dir = bundle.parent / "vesktopctl-experimental"
    state.directory(bridge_dir)
    bridge_dir.mkdir(mode=0o700, exist_ok=True)
    for name in ("bridge.js", "autorepair.js", "injection.js"):
        copy_if_changed(bridge_source.parent / name, bridge_dir / name, 0o644, state)

    if patched != content:
        atomic_write(bundle, patched.encode("utf-8"), bundle.stat().st_mode & 0o777)
        print(f"Patched: {bundle}")
    else:
        print(f"Already patched: {bundle}")
    state.installed(bundle)


def setup_runtime(runtime: Path, state: InstallState) -> None:
    if not runtime.is_absolute():
        raise ValueError("XDG_RUNTIME_DIR must be an absolute path")
    directory = runtime / "vesktopctl"
    state.directory_tree(directory, create=True, mode=0o700, mode_changed=True)
    if directory.stat().st_uid != os.getuid():
        raise ValueError(f"Socket directory belongs to another user: {directory}")
    directory.chmod(0o700)


def install_tools(source_dir: Path, bundle: Path, kind: str, home: Path, explicit_bundle: bool, state: InstallState) -> None:
    bin_dir = home / ".local/bin"
    data_dir = home / ".local/share/vesktopctl-experimental"
    state.directory_tree(bin_dir, create=True)
    state.directory_tree(data_dir, create=True)
    copy_if_changed(source_dir / "vesktopctl", bin_dir / "vesktopctl", 0o755, state)
    copy_if_changed(source_dir / "install.py", data_dir / "install.py", 0o755, state)
    copy_if_changed(source_dir / "uninstall.py", data_dir / "uninstall.py", 0o755, state)
    copy_if_changed(source_dir / "vesktop_process.py", data_dir / "vesktop_process.py", 0o644, state)
    for name in ("bridge.js", "autorepair.js", "injection.js"):
        copy_if_changed(source_dir / name, data_dir / name, 0o644, state)
    print(f"CLI: {bin_dir / 'vesktopctl'}")

    launcher = bin_dir / "vesktopctl-vesktop"
    selection = f"--bundle {shlex.quote(str(bundle))} --target {kind.lower()}" if explicit_bundle else f"--target {kind.lower()}"
    launch = f"flatpak run {APP_ID}" if kind == "Flatpak" else "vesktop"
    command = (
        "#!/bin/sh\n"
        f"if ! python3 {shlex.quote(str(data_dir / 'install.py'))} --patch-only {selection}; then\n"
        "  printf '%s\\n' 'vesktopctl: experimental patch unavailable; starting Vesktop without repair' >&2\n"
        "fi\n"
        f'exec {launch} "$@"\n'
    )
    state.file(launcher)
    if not launcher.is_file() or launcher.read_text() != command:
        atomic_write(launcher, command.encode(), 0o755)
    state.installed(launcher)
    print(f"Fallback launcher: {launcher}")


def flatpak_override_path(home: Path) -> Path:
    data_home = Path(os.environ.get("XDG_DATA_HOME", home / ".local/share"))
    return data_home / "flatpak/overrides" / APP_ID


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, help="Vencord bundle file or its directory")
    parser.add_argument("--target", choices=("flatpak", "native"), help="select an installation when several exist")
    parser.add_argument("--patch-only", action="store_true", help="reapply only the bundle patch before launch")
    parser.add_argument("--no-restart", action="store_true", help="leave a running Vesktop open (restart it manually)")
    args = parser.parse_args()
    home = Path.home()
    xdg_config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    source_dir = Path(__file__).resolve().parent
    stopped = None
    result = 0
    try:
        if args.bundle:
            bundle = args.bundle.expanduser().absolute()
            if bundle.is_dir():
                bundle /= BUNDLE_NAME
            if args.target:
                kind = args.target.capitalize()
            elif home / ".var/app" / APP_ID in bundle.parents:
                kind = "Flatpak"
            else:
                installed_kinds = {name for name, _ in config_roots(home, xdg_config)}
                if len(installed_kinds) != 1:
                    raise ValueError("For a custom --bundle path, specify --target flatpak or --target native")
                kind = installed_kinds.pop()
        else:
            bundle, kind = discover_bundle(home, xdg_config, args.target)

        refuse_conflicts(home, bundle)
        print(f"Vesktop {kind} (experimental): {bundle}")
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if not args.patch_only and (not runtime or not Path(runtime).is_absolute()):
            raise ValueError("XDG_RUNTIME_DIR must be an absolute path; run from a logged-in desktop session")
        running = vesktop_process.running(kind) if not args.patch_only else {}
        if running and not args.no_restart:
            # Stop the old in-memory bridge before replacing its files, so its
            # shutdown hook cannot race with this installation.
            vesktop_process.stop(running)
            stopped = running
        state = InstallState(home)
        state.target(kind)

        if not args.patch_only:
            setup_runtime(Path(runtime), state)
            if kind == "Flatpak":
                override = flatpak_override_path(home)
                state.directory_tree(override.parent)
                state.file(override, "override")
                subprocess.run(
                    ["flatpak", "override", "--user", "--filesystem=xdg-run/vesktopctl:create", APP_ID],
                    check=True,
                )
                state.installed(override)
        patch_bundle(bundle, source_dir / "bridge.js", state)
        if not args.patch_only:
            install_tools(source_dir, bundle, kind, home, args.bundle is not None, state)
            if not stopped:
                print("Start or restart Vesktop to load the bridge.")
    except (OSError, UnicodeError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"install.py: {error}", file=sys.stderr)
        result = 1
    finally:
        if stopped:
            try:
                vesktop_process.start(stopped)
            except (OSError, RuntimeError) as error:
                print(f"install.py: Vesktop restart failed: {error}", file=sys.stderr)
                result = 1
    return result


if __name__ == "__main__":
    sys.exit(main())
