import errno
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

SOURCE = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='vesktopctl-cli-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.environment = dict(os.environ, HOME=str(self.root), XDG_RUNTIME_DIR=str(self.root), PYTHONDONTWRITEBYTECODE='1')

    def run_cli(self, *args, cli=None):
        return subprocess.run([sys.executable, str(cli or SOURCE / 'vesktopctl'), *args],
                              env=self.environment, capture_output=True, text=True, timeout=10)

    def test_help_and_invalid_arguments(self):
        for args in (['--help'], ['patch', '--help'], ['uninstall', '--help']):
            result = self.run_cli(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
        for args in ([], ['unknown'], ['mute', '--patch-only'], ['patch', '--target', 'invalid']):
            self.assertEqual(self.run_cli(*args).returncode, 2)

    def test_missing_runtime_and_missing_server_report_errors(self):
        self.environment['XDG_RUNTIME_DIR'] = 'relative/path'
        result = self.run_cli('mute')
        self.assertEqual(result.returncode, 1)
        self.assertIn('XDG_RUNTIME_DIR', result.stderr)
        self.environment['XDG_RUNTIME_DIR'] = str(self.root)
        result = self.run_cli('deafen')
        self.assertEqual(result.returncode, 1)
        self.assertIn('cannot contact Vesktop', result.stderr)

    def test_management_commands_work_without_runtime_or_socket(self):
        cli = self.root / '.local/bin/vesktopctl'
        cli.parent.mkdir(parents=True)
        cli.write_bytes((SOURCE / 'vesktopctl').read_bytes())
        tools = self.root / '.local/share/vesktopctl'
        tools.mkdir(parents=True)
        helper = 'import json, pathlib, sys\npathlib.Path(__file__).with_name("arguments.json").write_text(json.dumps(sys.argv[1:]))\n'
        for name in ('install.py', 'uninstall.py'):
            (tools / name).write_text(helper)
        self.environment.pop('XDG_RUNTIME_DIR')
        result = self.run_cli('patch', '--bundle', '/path with spaces/bundle.js', '--target', 'native', '--no-restart', cli=cli)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((tools / 'arguments.json').read_text()),
                         ['--patch-only', '--bundle', '/path with spaces/bundle.js', '--target', 'native', '--no-restart'])
        result = self.run_cli('uninstall', '--patch-only', cli=cli)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((tools / 'arguments.json').read_text()), ['--patch-only'])

    def exchange(self, command, response):
        directory = self.root / 'vesktopctl'
        directory.mkdir(exist_ok=True)
        address = directory / 'control.sock'
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        try:
            server.bind(str(address))
        except OSError as error:
            if error.errno in (errno.EPERM, errno.EACCES):
                self.skipTest('Unix socket bind is blocked by this sandbox; rerun outside it')
            raise
        server.settimeout(10)
        server.listen(1)
        received = []
        errors = []

        def serve():
            try:
                connection, _ = server.accept()
                with connection:
                    connection.settimeout(5)
                    received.append(connection.recv(32))
                    for fragment in response:
                        connection.sendall(fragment)
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        result = self.run_cli(command)
        thread.join(timeout=10)
        server.close()
        address.unlink(missing_ok=True)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors, errors)
        self.assertEqual(received, [(command + '\n').encode()])
        return result

    def test_mute_and_deafen_over_real_unix_socket(self):
        for command in ('mute', 'deafen'):
            result = self.exchange(command, [b'O', b'K\n'])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, '')
            self.assertEqual(result.stderr, '')

    def test_server_error_and_disconnect_return_failure(self):
        result = self.exchange('mute', [b'ERR button not found\n'])
        self.assertEqual(result.returncode, 1)
        self.assertIn('button not found', result.stderr)
        result = self.exchange('deafen', [])
        self.assertEqual(result.returncode, 1)
        self.assertIn('no response', result.stderr)


if __name__ == '__main__':
    unittest.main()
