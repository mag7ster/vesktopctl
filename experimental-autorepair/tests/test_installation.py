import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
import install as installer
import uninstall as uninstaller

NODE = os.environ.get("VCC_NODE") or shutil.which("node")


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vcc-experimental-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "user"
        self.home.mkdir()
        self.runtime = Path(self.temp.name) / "run"
        self.runtime.mkdir()
        self.bundle = self.home / ".config/vesktop/sessionData/vencordFiles/vencordDesktopMain.js"
        self.bundle.parent.mkdir(parents=True)
        self.original = b"globalThis.version = 1;\r\n//# sourceURL=VencordDesktopMain"
        self.bundle.write_bytes(self.original)
        self.bundle.chmod(0o640)
        self.override = self.home / ".local/share/flatpak/overrides/dev.vencord.Vesktop"
        self.override.parent.mkdir(parents=True)
        self.old_override = b"[Context]\nsockets=!x11;\n"
        self.override.write_bytes(self.old_override)
        self.cli = self.home / ".local/bin/vesktopctl"
        self.cli.parent.mkdir()
        self.cli.write_bytes(b"previous user CLI\n")
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(Path, "home", return_value=self.home))
        self.patches.enter_context(patch.dict(os.environ, {
            "XDG_RUNTIME_DIR": str(self.runtime),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_DATA_HOME": str(self.home / ".local/share"),
        }))
        self.running = self.patches.enter_context(patch.object(installer.vesktop_process, "running", return_value={}))
        self.stop = self.patches.enter_context(patch.object(installer.vesktop_process, "stop"))
        self.start = self.patches.enter_context(patch.object(installer.vesktop_process, "start"))

    def invoke(self, module, arguments):
        with patch.object(sys, "argv", [str(module.__file__), *arguments]), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return module.main()

    def install(self, *extra):
        return self.invoke(installer, ["--target", "native", *extra])

    def runtime_repair(self):
        if not NODE:
            self.skipTest("Set VCC_NODE or install Node to test runtime autorepair")
        code = f"require({json.dumps(str(SOURCE / 'autorepair.js'))}).repair(process.env.VCC_TEST_BUNDLE, process.env.VCC_TEST_INJECTION)"
        environment = dict(os.environ, VCC_TEST_BUNDLE=str(self.bundle), VCC_TEST_INJECTION=str(SOURCE / "injection.js"))
        subprocess.run([NODE, "-e", code], check=True, env=environment, capture_output=True, text=True)

    def test_roundtrip_and_idempotence(self):
        self.assertEqual(self.install(), 0)
        first = self.bundle.read_bytes()
        self.assertEqual(self.install(), 0)
        self.assertEqual(self.bundle.read_bytes(), first)
        self.assertEqual(first.count(installer.BEGIN.encode()), 1)
        self.assertEqual(self.invoke(uninstaller, []), 0)
        self.assertEqual(self.bundle.read_bytes(), self.original)
        self.assertEqual(self.bundle.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.cli.read_bytes(), b"previous user CLI\n")
        self.assertEqual(self.override.read_bytes(), self.old_override)
        self.assertFalse((self.home / installer.STATE_DIR_NAME).exists())
        self.assertFalse((self.bundle.parent / "vesktopctl-experimental").exists())

    def test_runtime_repair_then_uninstall_preserves_new_bundle_and_manifest(self):
        self.assertEqual(self.install(), 0)
        manifest = self.home / installer.STATE_DIR_NAME / "manifest.json"
        old_manifest = manifest.read_bytes()
        updated = "globalThis.version = 'новая';\r\n// no terminal newline".encode()
        self.bundle.write_bytes(updated)
        self.runtime_repair()
        self.assertEqual(manifest.read_bytes(), old_manifest)
        self.assertEqual(self.invoke(uninstaller, []), 0)
        self.assertEqual(self.bundle.read_bytes(), updated)

    def test_python_repatch_after_runtime_repair_does_not_restore_old_vencord(self):
        self.assertEqual(self.install(), 0)
        updated = b"globalThis.version = 3;\n"
        self.bundle.write_bytes(updated)
        self.runtime_repair()
        self.assertEqual(self.install(), 0)
        self.assertEqual(self.invoke(uninstaller, []), 0)
        self.assertEqual(self.bundle.read_bytes(), updated)

    def test_plain_completed_update_without_repair_is_preserved(self):
        self.assertEqual(self.install(), 0)
        updated = b"globalThis.version = 2;"
        self.bundle.write_bytes(updated)
        self.assertEqual(self.invoke(uninstaller, []), 0)
        self.assertEqual(self.bundle.read_bytes(), updated)

    def test_old_manifest_or_marker_refuses_before_any_install_changes(self):
        for marker in ("/* vesktopctl begin */", "/* === VesktopCustomCommands Injection === */"):
            self.bundle.write_text(marker)
            self.assertEqual(self.install(), 1)
            self.assertFalse((self.home / installer.STATE_DIR_NAME).exists())
        self.bundle.write_bytes(self.original)
        old = self.home / ".vesktopctl/manifest.json"
        old.parent.mkdir()
        old.write_text("{}")
        self.assertEqual(self.install(), 1)
        self.assertEqual(self.bundle.read_bytes(), self.original)
        self.assertFalse((self.home / installer.STATE_DIR_NAME).exists())
        self.running.assert_not_called()
        self.stop.assert_not_called()

    def test_damaged_markers_refuse_without_side_effects(self):
        self.bundle.write_bytes(installer.BEGIN.encode())
        self.assertEqual(self.install(), 1)
        self.assertFalse((self.home / installer.STATE_DIR_NAME).exists())

    def test_running_app_is_restarted_and_patch_only_does_not_restart(self):
        info = {"kind": "Native", "pids": [123], "commands": [["vesktop"]]}
        self.running.return_value = info
        self.assertEqual(self.install(), 0)
        self.stop.assert_called_once_with(info)
        self.start.assert_called_once_with(info)
        self.stop.reset_mock()
        self.start.reset_mock()
        self.running.reset_mock()
        self.assertEqual(self.install("--patch-only"), 0)
        self.running.assert_not_called()
        self.stop.assert_not_called()
        self.start.assert_not_called()
        self.assertEqual(self.invoke(uninstaller, []), 0)
        self.stop.assert_called_once_with(info)
        self.start.assert_called_once_with(info)

    def test_no_restart_uninstall_requires_stopped_app(self):
        self.assertEqual(self.install(), 0)
        patched = self.bundle.read_bytes()
        self.running.return_value = {"kind": "Native"}
        self.assertEqual(self.invoke(uninstaller, ["--no-restart"]), 1)
        self.assertEqual(self.bundle.read_bytes(), patched)
        self.stop.assert_not_called()

    def test_failed_install_reopens_the_app_it_stopped(self):
        info = {"kind": "Native", "pids": [123], "commands": [["vesktop"]]}
        self.running.return_value = info
        with patch.object(installer, "patch_bundle", side_effect=OSError("disk full")):
            self.assertEqual(self.install(), 1)
        self.stop.assert_called_once_with(info)
        self.start.assert_called_once_with(info)

    def test_no_restart_install_keeps_running_app_open(self):
        self.running.return_value = {"kind": "Native"}
        self.assertEqual(self.install("--no-restart"), 0)
        self.stop.assert_not_called()
        self.start.assert_not_called()

    def test_changed_cli_is_kept_and_uninstall_can_be_retried(self):
        self.assertEqual(self.install(), 0)
        self.cli.write_text("user edited CLI")
        self.assertEqual(self.invoke(uninstaller, []), 1)
        self.assertEqual(self.cli.read_text(), "user edited CLI")
        self.assertTrue((self.home / installer.STATE_DIR_NAME).exists())
        self.assertTrue((self.home / ".local/share/vesktopctl-experimental/uninstall.py").is_file())
        self.cli.write_bytes((SOURCE / "vesktopctl").read_bytes())
        self.assertEqual(self.invoke(uninstaller, []), 0)
        self.assertEqual(self.cli.read_bytes(), b"previous user CLI\n")

    def test_restoring_a_file_twice_and_restoring_a_missing_file_are_safe(self):
        self.assertEqual(self.install(), 0)
        state = installer.InstallState(self.home)
        record = state.data["files"][str(self.cli)]
        uninstaller.restore_file(self.cli, record, state.backups)
        uninstaller.restore_file(self.cli, record, state.backups)
        self.assertEqual(self.cli.read_bytes(), b"previous user CLI\n")
        self.cli.unlink()
        uninstaller.restore_file(self.cli, record, state.backups)
        self.assertEqual(self.cli.read_bytes(), b"previous user CLI\n")
        self.assertEqual(self.invoke(uninstaller, []), 0)

    def test_flatpak_grant_restores_previous_override_including_later_changes(self):
        for later_edit in (False, True):
            def flatpak(command, **kwargs):
                self.assertEqual(command, ["flatpak", "override", "--user", "--filesystem=xdg-run/vesktopctl:create", installer.APP_ID])
                self.override.write_bytes(self.old_override + b"filesystems=xdg-run/vesktopctl:create;\n")
            with patch.object(installer.subprocess, "run", side_effect=flatpak):
                self.assertEqual(self.invoke(installer, ["--target", "flatpak", "--bundle", str(self.bundle)]), 0)
            if later_edit:
                self.override.write_bytes(self.old_override + b"filesystems=xdg-run/vesktopctl:create;home;\n")
            self.assertEqual(self.invoke(uninstaller, []), 0)
            expected = self.old_override + (b"filesystems=home;\n" if later_edit else b"")
            self.assertEqual(self.override.read_bytes(), expected)

    def test_installed_tools_are_complete_and_launchers_have_valid_shell_syntax(self):
        self.assertEqual(self.install(), 0)
        data = self.home / ".local/share/vesktopctl-experimental"
        for name in ("install.py", "uninstall.py", "vesktop_process.py", "autorepair.js", "bridge.js", "injection.js"):
            self.assertTrue((data / name).is_file(), name)
        launcher = self.home / ".local/bin/vesktopctl-vesktop"
        subprocess.run(["sh", "-n", str(launcher)], check=True)
        self.assertIn("--patch-only --target native", launcher.read_text())

    def test_native_process_detection_excludes_flatpak_and_rejects_appimage(self):
        proc = Path(self.temp.name) / "123"
        proc.mkdir()
        (proc / "comm").write_text("vesktop\n")
        (proc / "cmdline").write_bytes(b"/usr/bin/vesktop\0--example-flag\0")
        for environment, expected in ((b"FLATPAK_ID=dev.vencord.Vesktop\0", {}), (b"", {123: ["/usr/bin/vesktop", "--example-flag"]})):
            (proc / "environ").write_bytes(environment)
            with patch.object(Path, "iterdir", return_value=iter([proc])):
                self.assertEqual(installer.vesktop_process.native_processes(), expected)
        (proc / "environ").write_bytes(b"APPIMAGE=/tmp/Vesktop.AppImage\0")
        with patch.object(Path, "iterdir", return_value=iter([proc])), self.assertRaisesRegex(ValueError, "AppImage"):
            installer.vesktop_process.native_processes()


if __name__ == "__main__":
    unittest.main()
