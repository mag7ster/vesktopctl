import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
import install as installer
import uninstall as uninstaller


class InstallationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='vesktopctl-test-')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / 'home'
        self.home.mkdir()
        self.runtime = Path(temporary.name) / 'run'
        self.runtime.mkdir()
        self.bundle = self.home / '.config/vesktop/sessionData/vencordFiles/vencordDesktopMain.js'
        self.bundle.parent.mkdir(parents=True)
        self.original = b'globalThis.version = 1;\r\n//# sourceURL=VencordDesktopMain'
        self.bundle.write_bytes(self.original)
        self.bundle.chmod(0o640)
        self.cli = self.home / '.local/bin/vesktopctl'
        self.cli.parent.mkdir(parents=True)
        self.cli.write_bytes(b'previous user CLI\n')
        self.tools = self.home / '.local/share/vesktopctl'
        self.override = self.home / '.local/share/flatpak/overrides/dev.vencord.Vesktop'
        self.override.parent.mkdir(parents=True)
        self.override.write_bytes(b'[Context]\nsockets=!x11;\n')
        self.manifest = self.home / '.vesktopctl/manifest.json'
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(Path, 'home', return_value=self.home))
        stack.enter_context(patch.dict(os.environ, {
            'XDG_RUNTIME_DIR': str(self.runtime),
            'XDG_CONFIG_HOME': str(self.home / '.config'),
            'XDG_DATA_HOME': str(self.home / '.local/share'),
        }))
        self.running = stack.enter_context(patch.object(installer.vesktop_process, 'running', return_value={}))
        self.stop = stack.enter_context(patch.object(installer.vesktop_process, 'stop'))
        self.start = stack.enter_context(patch.object(installer.vesktop_process, 'start'))

    def invoke(self, module, args=()):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return module.main(list(args))

    def install(self, *args):
        return self.invoke(installer, ['--target', 'native', *args])

    def flatpak(self, command, **kwargs):
        self.assertEqual(command, ['flatpak', 'override', '--user', '--filesystem=xdg-run/vesktopctl:create', installer.APP_ID])
        content = self.override.read_text() if self.override.exists() else '[Context]\n'
        if not uninstaller.has_our_override_permission(content):
            content += 'filesystems=xdg-run/vesktopctl:create;\n'
        self.override.parent.mkdir(parents=True, exist_ok=True)
        self.override.write_text(content)

    def test_roundtrip_and_idempotence(self):
        self.assertEqual(self.install(), 0)
        first = self.bundle.read_bytes()
        self.assertEqual(self.install(), 0)
        self.assertEqual(self.bundle.read_bytes(), first)
        self.assertEqual(first.count(installer.BEGIN.encode()), 1)
        self.assertEqual(self.invoke(uninstaller), 0)
        self.assertEqual(self.bundle.read_bytes(), self.original)
        self.assertEqual(self.bundle.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.cli.read_bytes(), b'previous user CLI\n')
        self.assertFalse(self.manifest.parent.exists())
        self.assertFalse((self.bundle.parent / 'vesktopctl').exists())
        self.assertEqual(self.invoke(uninstaller), 0)

    def test_partial_remove_patch_and_full_remove(self):
        self.assertEqual(self.install(), 0)
        for _ in range(2):
            self.assertEqual(self.invoke(uninstaller, ['--patch-only']), 0)
            self.assertEqual(self.bundle.read_bytes(), self.original)
            self.assertEqual(self.cli.read_bytes(), (SOURCE / 'vesktopctl').read_bytes())
            self.assertTrue((self.tools / 'install.py').is_file())
            self.assertFalse((self.bundle.parent / 'vesktopctl').exists())
        self.assertEqual(self.invoke(installer, ['--patch-only']), 0)
        self.assertIn(installer.PATCH.encode(), self.bundle.read_bytes())
        self.assertEqual(self.invoke(uninstaller), 0)
        self.assertEqual(self.cli.read_bytes(), b'previous user CLI\n')

    def test_updated_bundle_is_preserved_after_repatch_and_removal(self):
        for repatch in (False, True):
            self.assertEqual(self.install(), 0)
            updated = "globalThis.version = 'новая';\r\n// no final newline".encode()
            self.bundle.write_bytes(updated)
            if repatch:
                self.assertEqual(self.invoke(installer, ['--patch-only']), 0)
            self.assertEqual(self.invoke(uninstaller), 0)
            self.assertEqual(self.bundle.read_bytes(), updated)

    def test_changed_patched_bundle_is_stripped_without_normalizing_lines(self):
        self.assertEqual(self.install(), 0)
        updated = b'// user change\r\n' + self.bundle.read_bytes()
        self.bundle.write_bytes(updated)
        self.assertEqual(self.invoke(uninstaller), 0)
        self.assertEqual(self.bundle.read_bytes(), b'// user change\r\n' + self.original)

    def test_auto_selection_follows_current_vencord_and_custom_path_is_remembered(self):
        self.assertEqual(self.install(), 0)
        custom = self.home / 'custom build/vencordDesktopMain.js'
        custom.parent.mkdir()
        custom.write_bytes(b'globalThis.custom = true;')
        (self.home / '.config/vesktop/state.json').write_text(json.dumps({'vencordDir': str(custom.parent)}))
        self.assertEqual(self.invoke(installer, ['--patch-only']), 0)
        self.assertIn(installer.PATCH.encode(), custom.read_bytes())
        self.assertEqual(self.invoke(uninstaller), 0)
        self.assertEqual(self.install('--bundle', str(custom)), 0)
        (self.home / '.config/vesktop/state.json').unlink()
        self.assertEqual(self.invoke(uninstaller, ['--patch-only']), 0)
        self.assertEqual(self.invoke(installer, ['--patch-only']), 0)
        self.assertIn(installer.PATCH.encode(), custom.read_bytes())
        self.assertEqual(self.bundle.read_bytes(), self.original)

    def test_running_app_stops_before_changes_and_restarts_even_on_failure(self):
        info = {'kind': 'Native', 'pids': [123], 'commands': [['vesktop']]}
        self.running.return_value = info
        self.stop.side_effect = lambda _: self.assertEqual(self.bundle.read_bytes(), self.original)
        self.assertEqual(self.install(), 0)
        self.stop.assert_called_once_with(info)
        self.start.assert_called_once_with(info)
        self.stop.side_effect = None
        self.start.reset_mock()
        self.stop.reset_mock()
        with patch.object(installer, 'patch_bundle', side_effect=OSError('disk full')):
            self.assertEqual(self.invoke(installer, ['--patch-only']), 1)
        self.stop.assert_called_once_with(info)
        self.start.assert_called_once_with(info)
        self.start.reset_mock()
        self.assertEqual(self.invoke(uninstaller), 0)
        self.start.assert_called_once_with(info)

    def test_no_restart_rejects_running_app_without_modifying_files(self):
        self.running.return_value = {'kind': 'Native'}
        self.assertEqual(self.install('--no-restart'), 1)
        self.assertEqual(self.bundle.read_bytes(), self.original)
        self.assertFalse(self.manifest.exists())
        self.running.return_value = {}
        self.assertEqual(self.install(), 0)
        before = self.bundle.read_bytes()
        self.running.return_value = {'kind': 'Native'}
        self.assertEqual(self.invoke(uninstaller, ['--no-restart']), 1)
        self.assertEqual(self.bundle.read_bytes(), before)
        self.stop.assert_not_called()

    def test_changed_cli_keeps_tools_and_backups_for_retry(self):
        self.assertEqual(self.install(), 0)
        self.cli.write_text('user edited CLI')
        self.assertEqual(self.invoke(uninstaller), 1)
        self.assertEqual(self.cli.read_text(), 'user edited CLI')
        for name in ('install.py', 'uninstall.py', 'vesktop_process.py', 'bridge.js'):
            self.assertTrue((self.tools / name).is_file(), name)
        self.cli.write_bytes((SOURCE / 'vesktopctl').read_bytes())
        self.assertEqual(self.invoke(uninstaller), 0)
        self.assertEqual(self.cli.read_bytes(), b'previous user CLI\n')

    def test_changed_bridge_blocks_tools_removal_and_retry_works(self):
        self.assertEqual(self.install(), 0)
        bridge = self.bundle.parent / 'vesktopctl/bridge.js'
        bridge.write_text('// user edit')
        self.assertEqual(self.invoke(uninstaller), 1)
        self.assertTrue((self.tools / 'uninstall.py').exists())
        self.assertEqual(self.cli.read_bytes(), (SOURCE / 'vesktopctl').read_bytes())
        bridge.write_bytes((SOURCE / 'bridge.js').read_bytes())
        self.assertEqual(self.invoke(uninstaller), 0)

    def test_flatpak_partial_removal_restores_and_patch_regrants_permission(self):
        original = self.override.read_bytes()
        with patch.object(installer.subprocess, 'run', side_effect=self.flatpak):
            self.assertEqual(self.invoke(installer, ['--target', 'flatpak', '--bundle', str(self.bundle)]), 0)
            self.assertEqual(self.invoke(uninstaller, ['--patch-only']), 0)
            self.assertEqual(self.override.read_bytes(), original)
            self.assertEqual(self.invoke(installer, ['--patch-only']), 0)
        self.assertTrue(uninstaller.has_our_override_permission(self.override.read_text()))
        self.assertEqual(self.invoke(uninstaller), 0)
        self.assertEqual(self.override.read_bytes(), original)

    def test_flatpak_later_permissions_survive_removal_and_repatch(self):
        with patch.object(installer.subprocess, 'run', side_effect=self.flatpak):
            self.assertEqual(self.invoke(installer, ['--target', 'flatpak', '--bundle', str(self.bundle)]), 0)
            self.override.write_text('[Context]\nfilesystems=xdg-run/vesktopctl:create;home;\n')
            self.assertEqual(self.invoke(installer, ['--patch-only']), 0)
        self.assertEqual(self.invoke(uninstaller), 0)
        self.assertIn('home;', self.override.read_text())
        self.assertFalse(uninstaller.has_our_override_permission(self.override.read_text()))

    def test_preexisting_flatpak_permission_is_kept(self):
        self.override.write_text('[Context]\nfilesystems=xdg-run/vesktopctl:create;\n')
        original = self.override.read_bytes()
        with patch.object(installer.subprocess, 'run', side_effect=self.flatpak):
            self.assertEqual(self.invoke(installer, ['--target', 'flatpak', '--bundle', str(self.bundle)]), 0)
        self.assertEqual(self.invoke(uninstaller), 0)
        self.assertEqual(self.override.read_bytes(), original)

    def test_old_manifest_and_launcher_are_migrated_without_losing_backups(self):
        self.assertEqual(self.install(), 0)
        state = installer.InstallState(self.home)
        launcher = self.cli.parent / 'vesktopctl-vesktop'
        launcher.write_bytes(b'original launcher\n')
        state.file(launcher)
        launcher.write_bytes(b'old managed launcher\n')
        state.installed(launcher)
        state.data.pop('selections')
        state.save()
        baseline = (state.backups / state.data['files'][str(self.cli)]['backup']).read_bytes()
        self.assertEqual(self.invoke(installer), 0)
        self.assertEqual(launcher.read_bytes(), b'original launcher\n')
        self.assertEqual((state.backups / state.data['files'][str(self.cli)]['backup']).read_bytes(), baseline)
        self.assertEqual(self.invoke(uninstaller), 0)
        self.assertEqual(self.cli.read_bytes(), b'previous user CLI\n')

    def test_patch_requires_existing_installation(self):
        self.assertEqual(self.invoke(installer, ['--patch-only']), 1)
        self.assertFalse(self.manifest.exists())
        self.assertEqual(self.bundle.read_bytes(), self.original)

    def test_damaged_markers_are_rejected_before_install_changes(self):
        self.bundle.write_text(installer.BEGIN)
        self.assertEqual(self.install(), 1)
        self.assertFalse(self.manifest.exists())
        self.stop.assert_not_called()

    def test_process_detection_failure_is_reported_without_mutation(self):
        self.assertEqual(self.install(), 0)
        before = self.bundle.read_bytes()
        self.running.side_effect = subprocess.CalledProcessError(1, ['flatpak', 'ps'])
        self.assertEqual(self.invoke(uninstaller), 1)
        self.assertEqual(self.bundle.read_bytes(), before)
        self.stop.assert_not_called()

    def test_multiple_saved_targets_require_selection(self):
        self.assertEqual(self.install(), 0)
        data = json.loads(self.manifest.read_text())
        data['selections']['flatpak'] = {'bundle': None}
        self.manifest.write_text(json.dumps(data))
        self.assertEqual(self.invoke(installer, ['--patch-only']), 1)
        self.assertEqual(self.invoke(installer, ['--patch-only', '--target', 'native']), 0)

    def test_installed_cli_can_remove_its_own_files(self):
        self.assertEqual(self.install(), 0)
        environment = dict(os.environ, HOME=str(self.home), PYTHONDONTWRITEBYTECODE='1')
        # This isolated fixture uses a Flatpak target and a fake executable so
        # the subprocess never inspects or stops a real Vesktop instance.
        fakebin = self.home / 'fakebin'
        fakebin.mkdir()
        fake = fakebin / 'flatpak'
        fake.write_text('#!/bin/sh\nprintf "[]\\n"\n')
        fake.chmod(0o755)
        data = json.loads(self.manifest.read_text())
        data['targets'] = ['Flatpak']
        data['selections'] = {'flatpak': {'bundle': str(self.bundle)}}
        self.manifest.write_text(json.dumps(data))
        environment['PATH'] = str(fakebin) + os.pathsep + environment['PATH']
        result = subprocess.run([sys.executable, str(self.cli), 'uninstall', '--patch-only'], env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.bundle.read_bytes(), self.original)
        result = subprocess.run([sys.executable, str(self.cli), 'patch'], env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(installer.PATCH.encode(), self.bundle.read_bytes())
        result = subprocess.run([sys.executable, str(self.cli), 'uninstall'], env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.cli.read_bytes(), b'previous user CLI\n')
        self.assertFalse(self.tools.exists())


if __name__ == '__main__':
    unittest.main()
