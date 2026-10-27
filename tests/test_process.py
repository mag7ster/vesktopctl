import subprocess
import unittest
from unittest.mock import call, patch

import vesktop_process


class FlatpakProcessTests(unittest.TestCase):
    def output(self, text):
        return subprocess.CompletedProcess(['flatpak', 'ps', '--json'], 0, stdout=text, stderr='')

    def test_empty_process_list_including_flatpak_empty_output(self):
        for text in ('', ' \n', '[]\n'):
            with self.subTest(text=text), patch.object(vesktop_process.subprocess, 'run', return_value=self.output(text)):
                self.assertEqual(vesktop_process.flatpak_instances(), [])
                self.assertEqual(vesktop_process.running('Flatpak'), {})

    def test_only_vesktop_instances_are_selected(self):
        output = self.output('[{"application":"org.example.Other","instance":"1"},'
                             '{"application":"dev.vencord.Vesktop","instance":"2"}]')
        with patch.object(vesktop_process.subprocess, 'run', return_value=output):
            self.assertEqual(vesktop_process.flatpak_instances(), ['2'])

    def test_stopping_last_flatpak_app_accepts_empty_stdout(self):
        info = {'kind': 'Flatpak', 'instances': ['123', '456']}
        with patch.object(vesktop_process.subprocess, 'run', return_value=self.output('')) as run:
            vesktop_process.stop(info)
        self.assertEqual(run.call_args_list, [
            call(['flatpak', 'kill', '123'], check=True),
            call(['flatpak', 'kill', '456'], check=True),
            call(['flatpak', 'ps', '--json'], check=True, capture_output=True, text=True),
            call(['flatpak', 'ps', '--json'], check=True, capture_output=True, text=True),
        ])

    def test_malformed_output_is_reported_instead_of_assuming_app_is_stopped(self):
        for text in ('not json', '{}', 'null', '[null]',
                     '[{"application":"dev.vencord.Vesktop"}]'):
            with self.subTest(text=text), patch.object(vesktop_process.subprocess, 'run', return_value=self.output(text)):
                with self.assertRaisesRegex(RuntimeError, 'flatpak ps --json'):
                    vesktop_process.flatpak_instances()


if __name__ == '__main__':
    unittest.main()
