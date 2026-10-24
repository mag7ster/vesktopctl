#!/usr/bin/env python3
"""Find Vesktop's active Vencord bundle and install vesktopctl."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True

import vesktop_process


BEGIN = "/* vesktopctl begin */"
END = "/* vesktopctl end */"
PATCH = (
    f"\n;{BEGIN}\n"
    "try { require(require('path').join(__dirname, 'vesktopctl', 'bridge.js')); } "
    "catch (error) { console.error('[vesktopctl] bridge load failed:', error); }\n"
    f"{END}\n"
)
APP_ID = "dev.vencord.Vesktop"
BUNDLE_NAME = "vencordDesktopMain.js"
FILES_DIR = Path("sessionData/vencordFiles")
STATE_DIR_NAME = ".vesktopctl"


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
        if path.is_symlink():
            raise ValueError(f"Refusing to manage symlinked directory: {path}")
        existed = path.exists()
        if existed and not path.is_dir():
            raise ValueError(f"Directory path occupied: {path}")
        if key in self.data["directories"]:
            return
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
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError(f"Refusing non-regular managed file: {path}")
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
                if role != "override":
                    raise ValueError(f"Managed file changed since installation: {path}")
                from uninstall import has_our_override_permission, remove_our_override_permission
                original = (self.backups / record["backup"]).read_text() if record["existed"] else ""
                current = path.read_text()
                clean = current if has_our_override_permission(original) else remove_our_override_permission(current)
                backup_name = record.get("backup", hashlib.sha256(key.encode()).hexdigest())
                metadata = path.stat()
                atomic_write(self.backups / backup_name, clean.encode(), stat.S_IMODE(metadata.st_mode))
                record.update({"backup": backup_name, "existed": True,
                               "mode": stat.S_IMODE(metadata.st_mode),
                               "atime_ns": metadata.st_atime_ns, "mtime_ns": metadata.st_mtime_ns})
                self.save()
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
        raise ValueError("Cannot remove an incomplete or duplicated vesktopctl patch")
    if PATCH in content:
        return content.replace(PATCH, "", 1)
    start = content.index(BEGIN)
    end = content.index(END, start) + len(END)
    if content[end:end + 1] == "\n":
        end += 1
    return content[:start] + content[end:]


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
    if not destination.is_file() or destination.read_bytes() != data or stat.S_IMODE(destination.stat().st_mode) != mode:
        atomic_write(destination, data, mode)
    state.installed(destination)


def patch_bundle(bundle: Path, bridge_source: Path, state: InstallState) -> None:
    if not bundle.is_file():
        raise ValueError(f"Vencord bundle not found: {bundle}")
    content = bundle.read_bytes().decode("utf-8")
    if BEGIN in content or END in content:
        clean = strip_patch(content)
    else:
        clean = content
    patched = clean + PATCH

    state.file(bundle, "bundle")
    bridge_dir = bundle.parent / "vesktopctl"
    state.directory(bridge_dir)
    bridge_dir.mkdir(mode=0o700, exist_ok=True)
    copy_if_changed(bridge_source, bridge_dir / "bridge.js", 0o644, state)

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


def install_tools(source_dir: Path, home: Path, state: InstallState) -> None:
    bin_dir = home / ".local/bin"
    data_dir = home / ".local/share/vesktopctl"
    state.directory_tree(bin_dir, create=True)
    state.directory_tree(data_dir, create=True)
    copy_if_changed(source_dir / "vesktopctl", bin_dir / "vesktopctl", 0o755, state)
    copy_if_changed(source_dir / "install.py", data_dir / "install.py", 0o755, state)
    copy_if_changed(source_dir / "uninstall.py", data_dir / "uninstall.py", 0o755, state)
    copy_if_changed(source_dir / "vesktop_process.py", data_dir / "vesktop_process.py", 0o644, state)
    copy_if_changed(source_dir / "bridge.js", data_dir / "bridge.js", 0o644, state)
    copy_if_changed(source_dir / "LICENSE", data_dir / "LICENSE", 0o644, state)
    print(f"CLI: {bin_dir / 'vesktopctl'}")

    # Restore any file that existed before the old launcher was installed.
    launcher = bin_dir / "vesktopctl-vesktop"
    record = state.data["files"].get(str(launcher))
    if record:
        from uninstall import restore_file
        restore_file(launcher, record, state.backups)
        del state.data["files"][str(launcher)]
        state.save()
        if record.get("backup"):
            (state.backups / record["backup"]).unlink(missing_ok=True)


def saved_selections(data: dict, home: Path, xdg_config: Path) -> dict:
    """Infer missing selections from manifests written by earlier versions."""
    selections = data.setdefault("selections", {})
    standard = {
        home / ".var/app" / APP_ID / "config/vesktop" / FILES_DIR / BUNDLE_NAME,
        xdg_config / "vesktop" / FILES_DIR / BUNDLE_NAME,
        home / ".var/app" / APP_ID / "config/Vencord/dist" / BUNDLE_NAME,
        xdg_config / "Vencord/dist" / BUNDLE_NAME,
    }
    candidates = {}
    for name, record in data["files"].items():
        if record.get("role") != "bundle":
            continue
        bundle = Path(name)
        kind = "flatpak" if home / ".var/app" / APP_ID in bundle.parents else "native"
        candidates.setdefault(kind, []).append(bundle)
    # A custom Flatpak bundle can live outside ~/.var/app.
    targets = data.get("targets", [])
    if len(targets) == 1:
        candidates = {targets[0].lower(): [Path(name) for name, record in data["files"].items()
                                          if record.get("role") == "bundle"]}
    for kind, bundles in candidates.items():
        if kind not in selections and len(bundles) == 1:
            selections[kind] = {"bundle": None if bundles[0] in standard else str(bundles[0])}
    return selections


def flatpak_override_path(home: Path) -> Path:
    data_home = Path(os.environ.get("XDG_DATA_HOME", home / ".local/share"))
    return data_home / "flatpak/overrides" / APP_ID


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, help="Vencord bundle file or its directory")
    parser.add_argument("--target", choices=("flatpak", "native"), help="select a Vesktop installation")
    parser.add_argument("--patch-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-restart", action="store_true", help="require Vesktop to be closed; do not restart it")
    args = parser.parse_args(argv)
    home = Path.home()
    xdg_config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    source_dir = Path(__file__).resolve().parent
    stopped = None
    failure = None
    try:
        manifest = home / STATE_DIR_NAME / "manifest.json"
        previous = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else None
        if args.patch_only and previous is None:
            raise ValueError("No vesktopctl installation found; run the installer first")
        if previous:
            if previous.get("version") != 1:
                raise ValueError(f"Unsupported install state: {manifest}")
            if any(record.get("restored") for record in previous["files"].values()):
                raise ValueError("Previous removal is incomplete; retry vesktopctl uninstall first")
            selections = saved_selections(previous, home, xdg_config)
            if args.target is None and not args.bundle:
                if len(selections) > 1:
                    raise ValueError("Multiple installations recorded; use --target flatpak|native")
                if len(selections) == 1:
                    args.target = next(iter(selections))
            selection = selections.get(args.target)
            if selection and not args.bundle and selection.get("bundle"):
                args.bundle = Path(selection["bundle"])

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

        if not bundle.is_file() or bundle.is_symlink():
            raise ValueError(f"Vencord bundle is missing or symlinked: {bundle}")
        content = bundle.read_bytes().decode("utf-8")
        if BEGIN in content or END in content:
            strip_patch(content)  # Reject damaged markers before changing anything.
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if not runtime or not Path(runtime).is_absolute():
            raise ValueError("XDG_RUNTIME_DIR is missing or invalid; run in a desktop session")
        print(f"Vesktop {kind}: {bundle}")
        running = vesktop_process.running(kind)
        if args.no_restart and running:
            raise ValueError("Close Vesktop before using --no-restart")
        if running:
            stopped = running
            vesktop_process.stop(running)
        state = InstallState(home)
        state.target(kind)
        saved_selections(state.data, home, xdg_config)[kind.lower()] = {
            "bundle": str(bundle) if args.bundle is not None else None
        }
        state.save()
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
            install_tools(source_dir, home, state)
        if not running:
            print("Start Vesktop to load the patch.")
    except (OSError, UnicodeError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        failure = error
    finally:
        if stopped:
            try:
                vesktop_process.start(stopped)
            except (OSError, RuntimeError) as error:
                failure = error if failure is None else ValueError(f"{failure}; Vesktop restart failed: {error}")
    if failure:
        print(f"vesktopctl: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
