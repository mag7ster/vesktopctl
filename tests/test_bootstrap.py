import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile

SOURCE = Path(__file__).resolve().parents[1]


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='vesktopctl-bootstrap-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.downloads = self.root / 'downloads'
        self.downloads.mkdir()
        self.archive = self.root / 'fixture.zip'
        self.script = self.root / 'install.sh'
        self.script.write_text((SOURCE / 'install.sh').read_text().replace("REPOSITORY='OWNER/REPO'", "REPOSITORY='test/vesktopctl'"))
        self.fakebin = self.root / 'bin'
        self.fakebin.mkdir()
        curl = self.fakebin / 'curl'
        curl.write_text('#!/usr/bin/env python3\nimport json, os, pathlib, shutil, sys\n'
                        'pathlib.Path(os.environ["CURL_ARGUMENTS"]).write_text(json.dumps(sys.argv[1:]))\n'
                        'if os.environ.get("FAIL_CURL"): sys.exit(22)\n'
                        'shutil.copyfile(os.environ["TEST_ARCHIVE"], sys.argv[sys.argv.index("-o") + 1])\n')
        curl.chmod(0o755)
        self.environment = dict(os.environ, TMPDIR=str(self.downloads), TEST_ARCHIVE=str(self.archive),
                                CURL_ARGUMENTS=str(self.root / 'curl.json'),
                                PATH=str(self.fakebin) + os.pathsep + os.environ['PATH'])

    def write_archive(self, installer=None, missing=None):
        with zipfile.ZipFile(self.archive, 'w') as archive:
            for name in ('install.py', 'uninstall.py', 'vesktop_process.py', 'vesktopctl', 'bridge.js', 'LICENSE'):
                if name != missing:
                    contents = installer if name == 'install.py' else ''
                    archive.writestr('vesktopctl-main/' + name, contents)
            archive.writestr('../../escape.txt', 'must not be extracted')

    def invoke(self, *args):
        return subprocess.run(['sh', str(self.script), *args], env=self.environment,
                              capture_output=True, text=True, timeout=10)

    def test_success_passes_arguments_and_cleans_files(self):
        installer = 'import json, pathlib, sys\nprint(json.dumps(sys.argv[1:]))\nassert pathlib.Path(__file__).with_name("bridge.js").is_file()\n'
        self.write_archive(installer)
        result = self.invoke('--target', 'flatpak', '--bundle', '/path with spaces')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ['--target', 'flatpak', '--bundle', '/path with spaces'])
        arguments = json.loads((self.root / 'curl.json').read_text())
        self.assertIn('https://github.com/test/vesktopctl/archive/refs/heads/main.zip', arguments)
        self.assertIn('=https', arguments)
        self.assertEqual(list(self.downloads.iterdir()), [])
        self.assertFalse((self.root / 'escape.txt').exists())

    def test_download_failure_cleans_temp_directory(self):
        self.environment['FAIL_CURL'] = '1'
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(self.downloads.iterdir()), [])

    def test_corrupt_and_incomplete_archives_fail_without_running_install(self):
        self.archive.write_bytes(b'not a zip')
        self.assertNotEqual(self.invoke().returncode, 0)
        self.assertEqual(list(self.downloads.iterdir()), [])
        self.write_archive('raise AssertionError("installer should not run")', missing='bridge.js')
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('bridge.js', result.stderr)
        self.assertNotIn('installer should not run', result.stderr)
        self.assertEqual(list(self.downloads.iterdir()), [])

    def test_installer_failure_is_returned_and_temp_directory_removed(self):
        self.write_archive('import sys\nsys.exit(7)\n')
        self.assertEqual(self.invoke().returncode, 7)
        self.assertEqual(list(self.downloads.iterdir()), [])

    def test_unconfigured_repository_fails_before_download(self):
        result = subprocess.run(['sh', str(SOURCE / 'install.sh')], env=self.environment,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn('OWNER/REPO', result.stderr)
        self.assertFalse((self.root / 'curl.json').exists())


if __name__ == '__main__':
    unittest.main()
