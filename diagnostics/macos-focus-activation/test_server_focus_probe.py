import os
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest import mock

import server_focus_probe as probe


class ProbeTests(unittest.TestCase):
    ENV = {'GITHUB_ACTIONS': 'true', 'RUNNER_ENVIRONMENT': 'github-hosted',
           'RUNNER_OS': 'macOS', 'RUNNER_ARCH': 'ARM64', 'QT_QPA_PLATFORM': 'cocoa'}

    def test_exact_hosted_arm64_mac26_contract(self):
        probe.verify_server(self.ENV, 'Darwin', 'arm64', '26.0')
        for key in self.ENV:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                probe.verify_server({**self.ENV, key: 'wrong'}, 'Darwin', 'arm64', '26.0')
        for system, machine, version in [('Linux', 'arm64', '26.0'), ('Darwin', 'x86_64', '26.0'),
                                         ('Darwin', 'arm64', '14.4')]:
            with self.subTest(platform=(system, machine, version)), self.assertRaises(RuntimeError):
                probe.verify_server(self.ENV, system, machine, version)

    def test_foreground_override_rejected_even_if_empty(self):
        for value in ('', '0', '1'):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                probe.verify_server({**self.ENV, 'QT_MAC_DISABLE_FOREGROUND_APPLICATION_TRANSFORM': value},
                                    'Darwin', 'arm64', '26.0')

    def test_activation_abi_exact_no_argument_void_selector(self):
        method = types.SimpleNamespace(signature=b'v16@0:8', __metadata__=lambda: {
            'retval': {'type': b'v'}, 'arguments': ({'type': b'@'}, {'type': b':'})})
        result = probe.verify_activation_abi(method, lambda _: (b'v', b'@', b':'))
        self.assertEqual(result['types'], ['v', '@', ':'])
        for signature in [(b'B', b'@', b':'), (b'v', b'@', b':', b'B')]:
            with self.subTest(signature=signature), self.assertRaises(RuntimeError):
                probe.verify_activation_abi(method, lambda _: signature)
        method.__metadata__ = lambda: {'retval': {'type': b'v'}, 'arguments': ({'type': b'@'},)}
        with self.assertRaises(RuntimeError):
            probe.verify_activation_abi(method, lambda _: (b'v', b'@', b':'))

    def native(self, activate=None):
        native = object.__new__(probe.NativeApplication)
        native.requests = 0
        native.app = types.SimpleNamespace(activate=activate or mock.Mock())
        return native

    def state(self):
        return {'pid': os.getpid(), 'console_uid': os.getuid(), 'quartz_session': {
            'user_id': os.getuid(), 'on_console': True, 'login_done': True}}

    def test_exactly_one_own_process_activation(self):
        native = self.native()
        native.activate_once(self.state())
        self.assertEqual(native.requests, 1)
        native.app.activate.assert_called_once_with()
        with self.assertRaisesRegex(RuntimeError, 'second activation'):
            native.activate_once(self.state())
        native.app.activate.assert_called_once_with()

    def test_bridge_exception_is_counted_and_cannot_be_retried(self):
        native = self.native(mock.Mock(side_effect=ValueError('original bridge failure')))
        with self.assertRaisesRegex(ValueError, 'original bridge failure'):
            native.activate_once(self.state())
        self.assertEqual(native.requests, 1)
        with self.assertRaises(RuntimeError):
            native.activate_once(self.state())
        native.app.activate.assert_called_once_with()

    def test_missing_or_other_console_session_never_calls_activate(self):
        for change in ('missing', 'off-console', 'login-incomplete', 'wrong-user', 'wrong-console', 'wrong-pid'):
            state = self.state()
            if change == 'missing': state['quartz_session'] = None
            if change == 'off-console': state['quartz_session']['on_console'] = False
            if change == 'login-incomplete': state['quartz_session']['login_done'] = False
            if change == 'wrong-user': state['quartz_session']['user_id'] += 1
            if change == 'wrong-console': state['console_uid'] += 1
            if change == 'wrong-pid': state['pid'] += 1
            native = self.native()
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                native.activate_once(state)
            self.assertEqual(native.requests, 0)
            native.app.activate.assert_not_called()

    def test_checkout_identity_and_original_blob_before_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / 'tests').mkdir()
            test = root / probe.TEST_FILE
            test.write_bytes(b'# original test\n')
            subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=fixture', '-c',
                            'user.email=fixture@example.invalid', 'commit', '-qm', 'test-only fixture'], check=True)
            sha = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD']).decode().strip()
            with self.assertRaisesRegex(RuntimeError, 'Wrong source commit'):
                probe.verify_checkout(root)
            with mock.patch.object(probe, 'SOURCE_SHA', sha):
                self.assertEqual(probe.verify_checkout(root)['commit'], sha)
                test.write_bytes(b'# modified test\n')
                with self.assertRaisesRegex(RuntimeError, 'dirty'):
                    probe.verify_checkout(root)

    def test_hook_preserves_original_test_and_teardown_and_one_request(self):
        self.check_hooks(fail_test=False, fail_record=False)

    def test_original_test_failure_is_not_swallowed(self):
        self.check_hooks(fail_test=True, fail_record=False)

    def test_original_teardown_runs_even_if_diagnostic_snapshot_fails(self):
        self.check_hooks(fail_test=False, fail_record=True)

    def check_hooks(self, *, fail_test, fail_record):
        events = []
        class Parent:
            def show(self, window): events.append('original-show')
            def tearDown(self): events.append('original-teardown')
        name = probe.CASES['tab']
        def original_test(case):
            case.show('own-window')
            events.append('original-test-real-gates')
            if fail_test: raise AssertionError('original failed gate')
        Case = type('MacSettingControlsTests', (Parent,), {name: original_test})
        class Native:
            abi = {'types': ['v', '@', ':']}
            def __init__(self):
                self.requests = 0
                events.append('native-after-show')
            def snapshot(self): return {'activation_requests': self.requests}
            def activate_once(self, state):
                self.requests += 1
                events.append('one-native-request')
        nodeid = probe.TEST_FILE + '::MacSettingControlsTests::' + name
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(probe, 'qt_snapshot', return_value={}):
            report = {'events': [], 'pytest_reports': []}
            plugin = probe.ProbePlugin(nodeid, report, Path(tmp) / 'report.json', Native)
            plugin.pytest_collection_modifyitems([types.SimpleNamespace(nodeid=nodeid, cls=Case)])
            self.assertIs(getattr(Case, name), original_test)
            case = Case()
            case.app = 'own-app'
            if fail_test:
                with self.assertRaisesRegex(AssertionError, 'original failed gate'): original_test(case)
            else: original_test(case)
            if fail_record:
                with mock.patch.object(plugin, 'record', side_effect=RuntimeError('snapshot')):
                    with self.assertRaisesRegex(RuntimeError, 'snapshot'): case.tearDown()
            else: case.tearDown()
            self.assertEqual(events, ['original-show', 'native-after-show', 'one-native-request',
                                      'original-test-real-gates', 'original-teardown'])
            self.assertEqual(plugin.native.requests, 1)
            plugin.cleanup()
            self.assertNotIn('show', Case.__dict__)
            self.assertNotIn('tearDown', Case.__dict__)
            self.assertIs(getattr(Case, name), original_test)

    def test_git_identity_checks_disable_implicit_lazy_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'tests').mkdir()
            (root / probe.TEST_FILE).write_bytes(b'original')
            with mock.patch.object(probe.subprocess, 'check_output',
                                   side_effect=[probe.SOURCE_SHA.encode() + b'\n', b'', b'original']) as call:
                probe.verify_checkout(root)
                self.assertEqual(call.call_count, 3)
                for invocation in call.call_args_list:
                    self.assertEqual(invocation.kwargs['env']['GIT_NO_LAZY_FETCH'], '1')
                    self.assertIn('core.fsmonitor=false', invocation.args[0])

    def test_more_than_one_test_or_wrong_test_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = probe.ProbePlugin('expected', {}, Path(tmp) / 'report.json')
            for items in ([], [types.SimpleNamespace(nodeid='wrong')], [None, None]):
                with self.assertRaises(RuntimeError): plugin.pytest_collection_modifyitems(items)

    def test_non_server_exit_preserves_report_without_importing_bridge(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=True):
            output = Path(tmp) / 'new-report'
            self.assertEqual(probe.main(['--checkout', tmp, '--case', 'tab', '--output', str(output)]), 2)
            import json
            report = json.loads((output / 'activation-report.json').read_text())
            self.assertFalse(report['passed'])
            self.assertEqual(report['events'], [])
            self.assertFalse(report['production_allowed'])
            self.assertNotIn('AppKit', __import__('sys').modules)
            with self.assertRaises(FileExistsError):
                probe.main(['--checkout', tmp, '--case', 'tab', '--output', str(output)])


if __name__ == '__main__':
    unittest.main(verbosity=2)
