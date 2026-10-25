"""No Docker service is needed for deterministic clean-container gate tests."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import verify_clean_linux_bundle as clean


class CleanLinuxTests(unittest.TestCase):
    def test_container_is_offline_readonly_and_uses_bundled_python(self):
        command = clean.container_command(Path('/tmp/bundle with spaces'), Path('/tmp/results'), 'sha256:' + 'a'*64,
                                          'runtime/bin/python3.12', ['scripts/check_runtime.py'], 'runtime')
        self.assertEqual(command[command.index('--network') + 1], 'none')
        self.assertIn('--read-only', command)
        self.assertIn('type=bind,src=/tmp/bundle with spaces,dst=/bundle,readonly', command)
        self.assertIn('PATH=/usr/bin:/bin', command)
        self.assertIn('LD_LIBRARY_PATH=/bundle/native-libs', command)
        self.assertIn('HOME=/results/home', command)
        self.assertIn('-i', command)
        self.assertEqual(command[-2:], ['/bundle/runtime/bin/python3.12', 'scripts/check_runtime.py'])
        self.assertNotIn('apt', command)
        self.assertNotIn('pip', command)
        self.assertNotIn('--privileged', command)
        self.assertNotIn('--ipc=host', command)

    def test_xcb_uses_only_authenticated_local_display(self):
        with tempfile.TemporaryDirectory() as tmp:
            cookie = Path(tmp) / 'cookie'
            cookie.write_bytes(b'fixture')
            command = clean.container_command(Path('/bundle-source'), Path('/results-source'), 'image',
                                              'runtime/bin/python3.12', ['gui.py', '--smoke-test'], 'xcb-gui',
                                              display=':99', xauthority=cookie)
            self.assertEqual(command[command.index('--hostname') + 1], clean.socket.gethostname())
            self.assertIn('XAUTHORITY=/xauthority', command)
            self.assertIn('DISPLAY=:99', command)
            self.assertIn('QT_X11_NO_MITSHM=1', command)
            self.assertIn('QT_QPA_PLATFORM=xcb', command)
            self.assertIn(f'type=bind,src={cookie},dst=/xauthority,readonly', command)
            self.assertNotIn('-ac', command)
            self.assertNotIn('xhost', command)
            with self.assertRaises(ValueError):
                clean.container_command(Path('/a'), Path('/b'), 'image', 'runtime/bin/python3.12', [], 'xcb', display='host.example:0', xauthority=cookie)

    def test_paths_are_checked(self):
        for path in ['../python', '/bin/python', 'runtime/../python', 'runtime\\python', 'runtime//python']:
            with self.assertRaises(ValueError):
                clean.runtime_relative(path)
        with self.assertRaises(ValueError):
            clean.docker_base(Path('/tmp/a,readonly'), Path('/results'), 'image')

    def test_only_actual_loads_count(self):
        text = '  1: trying file=/host/libbad.so\n  1: calling init: /bundle/lib/libgood.so\n  1: calling fini: /lib/x86_64-linux-gnu/libc.so.6 [0]\n'
        paths = clean.loaded_library_paths(text)
        self.assertNotIn('/host/libbad.so', paths)
        self.assertIn('/bundle/lib/libgood.so', paths)
        self.assertIn('/lib/x86_64-linux-gnu/libc.so.6', paths)

    def test_missing_docker_is_fatal(self):
        with patch.object(clean.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'cannot be skipped'):
                clean.verify_clean_bundle(Path('/unused.zip'), Path('/unused'))

    def test_loader_audit_accepts_only_bundle_and_base_libc(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'loader').mkdir()
            log = root / 'loader/runtime.123'
            log.write_text('1: calling init: /bundle/runtime/lib/libpython3.12.so\n1: calling init: /lib/x86_64-linux-gnu/libc.so.6\n')
            self.assertEqual(clean.audit_loader_logs(root, ('runtime',))['result'], 'passed')
            log.write_text(log.read_text() + '1: calling init: /usr/lib/x86_64-linux-gnu/libxcb.so.1\n')
            with self.assertRaisesRegex(RuntimeError, 'libxcb'):
                clean.audit_loader_logs(root, ('runtime',))
            self.assertEqual(json.loads((root/'loader-audit.json').read_text())['result'], 'failed')

    def test_missing_logs_cannot_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, 'Missing actual'):
                clean.audit_loader_logs(Path(tmp), ('runtime',))

    def test_workload_evidence_requires_all_screenshots_and_issue_cases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'xcb-gui-screenshots').mkdir()
            with self.assertRaisesRegex(RuntimeError, 'screenshots incomplete'):
                clean.validate_workload_evidence(root)
            for theme in ('light', 'dark'):
                for page in ('home', 'timeline', 'settings'):
                    (root / 'xcb-gui-screenshots' / f'{theme}-{page}.png').write_bytes(b'fixture')
            (root / 'xcb-gui.log').write_text('GUI_SMOKE_OK')
            (root / 'acceptance/bundle-e2e').mkdir(parents=True)
            (root / 'acceptance/bundle-issues').mkdir()
            (root / 'acceptance/bundle-e2e/report.json').write_text('{"result": "passed"}')
            issue_path = root / 'acceptance/bundle-issues/report.json'
            issue_path.write_text(json.dumps([{'case': name, 'result': 'passed'} for name in ('multiline', 'av1', 'timeline')]))
            clean.validate_workload_evidence(root)
            issue_path.write_text('[{"case": "av1", "result": "passed"}]')
            with self.assertRaisesRegex(RuntimeError, 'incomplete'):
                clean.validate_workload_evidence(root)

    def test_libc_basename_in_untrusted_directory_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'loader').mkdir()
            (root/'loader/runtime.1').write_text('1: calling init: /bundle/lib/libok.so\n1: calling init: /results/libc.so.6\n')
            with self.assertRaisesRegex(RuntimeError, '/results/libc'):
                clean.audit_loader_logs(root, ('runtime',))


if __name__ == '__main__':
    unittest.main()
