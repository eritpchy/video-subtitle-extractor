#!/usr/bin/env python3
"""One request to activate this Server test process; never a production fix."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import threading
import time

SOURCE_SHA = 'aeb41d02cfd1257711236c08fc223156c35cc5f6'
TEST_FILE = 'tests/test_macos_setting_controls.py'
CASES = {
    'wait': 'test_focus_wait_does_not_pass_until_expected_widget_really_has_focus',
    'tab': 'test_tab_focus_includes_the_standard_combo',
}
BRIDGE_VERSION = '12.2.2'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def verify_server(env, system, machine, mac_version):
    require(system == 'Darwin' and machine == 'arm64', 'Only macOS arm64 Server is allowed')
    require(mac_version.split('.')[0] == '26', 'This probe is limited to the macos-26 runner')
    for key, value in {'GITHUB_ACTIONS': 'true', 'RUNNER_ENVIRONMENT': 'github-hosted',
                       'RUNNER_OS': 'macOS', 'RUNNER_ARCH': 'ARM64',
                       'QT_QPA_PLATFORM': 'cocoa'}.items():
        require(env.get(key) == value, f'Server prerequisite mismatch: {key}')
    require(env.get('QT_MAC_DISABLE_FOREGROUND_APPLICATION_TRANSFORM') is None,
            'Foreground-transform override is not allowed; do not silently unset it')


def verify_checkout(root):
    def git(*args):
        env = {**os.environ, 'GIT_NO_LAZY_FETCH': '1'}
        return subprocess.check_output(['git', '-c', 'core.fsmonitor=false', '-C', str(root), *args],
                                       stderr=subprocess.STDOUT, env=env)
    require(git('rev-parse', 'HEAD').decode().strip() == SOURCE_SHA, 'Wrong source commit')
    # Check all tracked bytes before importing project code; do not fetch or reset anything.
    require(not git('status', '--porcelain', '--untracked-files=no'), 'Tracked checkout is dirty')
    original = git('show', f'{SOURCE_SHA}:{TEST_FILE}')
    actual = (root / TEST_FILE).read_bytes()
    require(original == actual, 'Original test file differs from the pinned Git blob')
    return {'commit': SOURCE_SHA, 'test_file': TEST_FILE, 'test_sha256': hashlib.sha256(actual).hexdigest()}


def verify_activation_abi(method, split_signature):
    parts = tuple(split_signature(method.signature))
    metadata = method.__metadata__()
    arguments = tuple(arg['type'] for arg in metadata['arguments'])
    require(parts == (b'v', b'@', b':'), f'Unexpected activate signature: {parts!r}')
    require(metadata['retval']['type'] == b'v' and arguments == (b'@', b':'),
            'Unexpected activate metadata: expected void(id, SEL), no explicit arguments')
    return {'signature': method.signature.decode('ascii'),
            'types': [part.decode('ascii') for part in parts],
            'metadata_return': metadata['retval']['type'].decode('ascii'),
            'metadata_arguments': [arg.decode('ascii') for arg in arguments]}


class NativeApplication:
    """Use the already-installed official PyObjC ABI bridge, not variadic ctypes."""
    def __init__(self):
        require(threading.current_thread() is threading.main_thread(), 'Native calls must be on the main thread')
        for name in ('pyobjc-core', 'pyobjc-framework-Cocoa', 'pyobjc-framework-Quartz'):
            require(importlib.metadata.version(name) == BRIDGE_VERSION, f'Unexpected bridge version: {name}')
        import AppKit
        import Foundation
        import Quartz
        import objc
        from PySide6 import QtCore, QtWidgets
        self.kit, self.quartz = AppKit, Quartz
        require(Foundation.NSThread.isMainThread(), 'AppKit main-thread requirement failed')
        app = QtWidgets.QApplication.instance()
        require(app is not None and app.platformName() == 'cocoa', 'Existing Cocoa QApplication required')
        require(QtCore.qVersion() == '6.12.0', 'Unexpected Qt version')
        require(QtCore.QThread.currentThread() == app.thread(), 'Wrong QApplication thread')
        self.app = AppKit.NSApplication.sharedApplication()
        self.current = AppKit.NSRunningApplication.currentApplication()
        require(int(self.current.processIdentifier()) == os.getpid(), 'Native application is not this test process')
        self.abi = verify_activation_abi(self.app.activate, objc.splitSignature)
        self.requests = 0

    def snapshot(self):
        require(threading.current_thread() is threading.main_thread(), 'Snapshot must be on the main thread')
        front = self.kit.NSWorkspace.sharedWorkspace().frontmostApplication()
        key = self.app.keyWindow()
        session = self.quartz.CGSessionCopyCurrentDictionary()
        # Only these documented scalar fields from the CALLER'S session. No names,
        # other-process enumeration, window lists, titles, paths, or screenshots.
        q = self.quartz
        session_info = None if session is None else {
            'user_id': int(session[q.kCGSessionUserIDKey]),
            'on_console': bool(session[q.kCGSessionOnConsoleKey]),
            'login_done': bool(session[q.kCGSessionLoginDoneKey]),
        }
        return {'pid': os.getpid(), 'uid': os.getuid(), 'posix_session_id': os.getsid(0),
                'console_uid': os.stat('/dev/console').st_uid,
                'native_is_active': bool(self.app.isActive()),
                'activation_policy': int(self.app.activationPolicy()),
                'key_window_present': key is not None,
                'key_window_is_key': bool(key.isKeyWindow()) if key is not None else None,
                'frontmost_pid': int(front.processIdentifier()) if front is not None else None,
                'quartz_session': session_info, 'activation_requests': self.requests}

    def activate_once(self, state):
        require(self.requests == 0, 'A second activation request is forbidden')
        require(state['pid'] == os.getpid(), 'Activation target is not this process')
        session = state['quartz_session']
        require(session is not None and session['on_console'] and session['login_done']
                and session['user_id'] == os.getuid() and state['console_uid'] == os.getuid(),
                'No matching logged-in console Quartz session; activation not attempted')
        self.requests += 1  # Count the attempt even if the bridge raises; never retry.
        self.app.activate()  # Public modern void activate(); request, not a guarantee.


def qt_snapshot(app, window):
    def describe(obj):
        return None if obj is None else {'class': obj.metaObject().className(), 'name': obj.objectName()}
    return {'application_state': app.applicationState().name,
            'active_window': describe(app.activeWindow()), 'focus_window': describe(app.focusWindow()),
            'focus_widget': describe(app.focusWidget()),
            'tab_focus_behavior': app.styleHints().tabFocusBehavior().name,
            'window_active': window.isActiveWindow(), 'window_visible': window.isVisible(),
            'window_exposed': bool(window.windowHandle() and window.windowHandle().isExposed())}


class ProbePlugin:
    def __init__(self, nodeid, report, path, native_factory=NativeApplication):
        self.nodeid, self.report, self.path = nodeid, report, path
        self.native_factory = native_factory
        self.native = None
        self.window = self.case = None
        self.started = time.monotonic()
        self.restore = None

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2, sort_keys=True) + '\n', encoding='utf-8')

    def record(self, stage):
        entry = {'stage': stage, 'at_utc': datetime.now(timezone.utc).isoformat(),
                 'elapsed_ms': round((time.monotonic() - self.started) * 1000, 3)}
        if self.native is not None:
            entry['native'] = self.native.snapshot()
        if self.case is not None:
            entry['qt'] = qt_snapshot(self.case.app, self.window)
        self.report['events'].append(entry)
        self.save()
        return entry

    def pytest_collection_modifyitems(self, items):
        require(len(items) == 1 and items[0].nodeid == self.nodeid, 'Exactly one original test is allowed')
        cls = items[0].cls
        show, teardown = cls.show, cls.tearDown
        had_own_show = 'show' in cls.__dict__
        had_own_teardown = 'tearDown' in cls.__dict__
        def wrapped_show(case, window):
            require(self.native is None, 'Only one fixture window/show is allowed')
            show(case, window)  # Original resize/show/activateWindow/processEvents unchanged.
            self.case, self.window = case, window
            self.native = self.native_factory()  # Only AFTER QApplication + original show.
            self.report['activation_abi'] = self.native.abi
            before = self.record('after-original-show-before-native-request')
            self.native.activate_once(before['native'])
            self.record('after-single-native-request')
            # No wait, retry, focus mutation or Qt event injection here. The original
            # test now executes its unchanged 5000ms activation and real key gates.
        def wrapped_teardown(case):
            try:
                self.record('after-original-test-before-original-teardown')
            finally:
                teardown(case)  # Never bypass the original widget/config cleanup.
        self.restore = (cls, show, teardown, had_own_show, had_own_teardown)
        cls.show, cls.tearDown = wrapped_show, wrapped_teardown
        # The original test method itself is NEVER replaced (including pytest's
        # cached unittest bound method). Only fixture show + teardown are wrapped.

    def pytest_runtest_logreport(self, report):
        self.report['pytest_reports'].append({'nodeid': report.nodeid, 'when': report.when,
            'outcome': report.outcome, 'duration': report.duration, 'longrepr': report.longreprtext})
        self.save()

    def cleanup(self):
        if self.restore:
            cls, show, teardown, had_own_show, had_own_teardown = self.restore
            if had_own_show:
                cls.show = show
            else:
                del cls.show
            if had_own_teardown:
                cls.tearDown = teardown
            else:
                del cls.tearDown
            self.restore = None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', required=True, type=Path)
    parser.add_argument('--case', required=True, choices=CASES)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)  # Never overwrite any earlier evidence.
    report = {'schema': 1, 'diagnostic_only': True, 'production_allowed': False,
              'external_ax_verified': False, 'events': [], 'pytest_reports': [],
              'case': args.case, 'python': sys.version, 'python_executable': sys.executable}
    path = output / 'activation-report.json'
    plugin = None
    try:
        verify_server(os.environ, platform.system(), platform.machine(), platform.mac_ver()[0])
        require(sys.dont_write_bytecode, 'Invoke Python with explicit -B; no bytecode writes allowed')
        root = args.checkout.resolve(strict=True)
        report['source_pre'] = verify_checkout(root)
        report['runner'] = {key: os.environ.get(key) for key in
                            ('GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'RUNNER_ENVIRONMENT', 'RUNNER_OS', 'RUNNER_ARCH')}
        os.chdir(root)
        sys.path.insert(0, str(root))
        require(importlib.metadata.version('pytest') == '8.3.5', 'Unexpected pytest version')
        import pytest
        nodeid = TEST_FILE + '::MacSettingControlsTests::' + CASES[args.case]
        plugin = ProbePlugin(nodeid, report, path)
        plugin.save()
        try:
            result = int(pytest.main([nodeid, '-vv', '-s', '-p', 'no:cacheprovider',
                        '--junitxml=' + str(output / 'junit.xml')], plugins=[plugin]))
        finally:
            # Do not let a post-test source mismatch look like a passing diagnosis.
            report['source_post'] = verify_checkout(root)
        report['pytest_exit_code'] = result
        requests = plugin.native.requests if plugin.native else 0
        report['activation_requests'] = requests
        # Skip/empty collection is NOT successful diagnosis, even if pytest exits 0.
        calls = [r for r in report['pytest_reports'] if r['when'] == 'call']
        passed = result == 0 and requests == 1 and len(calls) == 1 and calls[0]['outcome'] == 'passed'
        report['passed'] = passed
        return 0 if passed else (result or 1)
    except Exception as error:
        import traceback
        report['diagnostic_error'] = repr(error)
        report['traceback'] = traceback.format_exc()
        report['passed'] = False
        print(report['traceback'], file=sys.stderr)
        return 2
    finally:
        if plugin:
            plugin.cleanup()
        path.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')


if __name__ == '__main__':
    raise SystemExit(main())
