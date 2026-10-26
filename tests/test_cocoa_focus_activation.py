"""Pure mocks: no Qt, AppKit, application launch or native call on the local host."""

import builtins
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from helpers import cocoa_focus_activation as helper


class CocoaFocusActivationContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "activation.json"
        thread = object()
        self.qtcore = SimpleNamespace(QThread=SimpleNamespace(currentThread=lambda: thread))
        self.app = SimpleNamespace(platformName=lambda: "cocoa", thread=lambda: thread,
            applicationState=lambda: SimpleNamespace(name="ApplicationActive"),
            activeWindow=lambda: None, focusWindow=lambda: None, focusWidget=lambda: None)
        self.window = SimpleNamespace(isActiveWindow=lambda: False, isVisible=lambda: True,
                                      windowHandle=lambda: SimpleNamespace(isExposed=lambda: True))
        self.native = Mock()
        self.bridge = SimpleNamespace(NSApplication=SimpleNamespace(sharedApplication=Mock(return_value=self.native)))

    def context(self):
        return helper.inactive_cocoa_activation(self.app, self.window, self.qtcore, self.path)

    def read(self):
        return json.loads(self.path.read_text())

    def test_non_mac_and_non_cocoa_never_import_appkit(self):
        original_import = builtins.__import__
        def guarded_import(name, *args, **kwargs):
            if name == "AppKit":
                self.fail("AppKit import on an unsupported platform/QPA")
            return original_import(name, *args, **kwargs)
        for platform, qpa in [("linux", "cocoa"), ("win32", "cocoa"), ("darwin", "offscreen")]:
            with self.subTest(platform=platform, qpa=qpa), patch.object(helper.sys, "platform", platform), \
                    patch.object(self.app, "platformName", return_value=qpa), \
                    patch("builtins.__import__", side_effect=guarded_import):
                with self.context():
                    pass
                self.assertFalse(self.path.exists())

    def test_pre_active_makes_zero_requests(self):
        self.native.isActive.return_value = True
        with patch.object(helper.sys, "platform", "darwin"), patch.dict("sys.modules", {"AppKit": self.bridge}):
            with self.context():
                self.native.activate.assert_not_called()
        report = self.read()
        self.assertEqual(report["activation_requests"], 0)
        self.assertEqual(len(report["events"]), 3)
        self.assertTrue(all(event["native_is_active"] for event in report["events"]))
        self.native.activate.assert_not_called()

    def test_inactive_requests_once_and_does_not_claim_immediate_success(self):
        self.native.isActive.side_effect = [False, False, True]
        with patch.object(helper.sys, "platform", "darwin"), patch.dict("sys.modules", {"AppKit": self.bridge}):
            with self.context():
                self.native.activate.assert_called_once_with()
        report = self.read()
        self.assertEqual(report["activation_requests"], 1)
        self.assertEqual([event["native_is_active"] for event in report["events"]], [False, False, True])
        self.assertEqual(report["events"][-1]["stage"], "after-original-window-wait")
        self.native.activate.assert_called_once_with()

    def test_bridge_exception_is_not_swallowed_or_retried(self):
        error = ValueError("original activation bridge failure")
        self.native.isActive.return_value = False
        self.native.activate.side_effect = error
        with patch.object(helper.sys, "platform", "darwin"), patch.dict("sys.modules", {"AppKit": self.bridge}):
            with self.assertRaises(ValueError) as caught:
                with self.context():
                    self.fail("Caller must not run after bridge failure")
        self.assertIs(caught.exception, error)
        self.native.activate.assert_called_once_with()
        self.assertEqual(self.read()["activation_requests"], 1)

    def test_python_main_and_qapplication_thread_are_both_required(self):
        for wrong in ("python", "qt"):
            with self.subTest(wrong=wrong), patch.object(helper.sys, "platform", "darwin"), \
                    patch.dict("sys.modules", {"AppKit": self.bridge}):
                target = helper.threading if wrong == "python" else self.qtcore.QThread
                name = "current_thread" if wrong == "python" else "currentThread"
                with patch.object(target, name, return_value=object()), self.assertRaisesRegex(RuntimeError, "thread"):
                    with self.context():
                        self.fail("Wrong-thread caller must not run")
                self.bridge.NSApplication.sharedApplication.assert_not_called()
                self.native.activate.assert_not_called()
                self.assertEqual(self.read()["events"], [])

    def test_original_wait_failure_keeps_final_evidence_and_failure(self):
        error = AssertionError("original 5000ms window wait failed")
        self.native.isActive.return_value = True
        with patch.object(helper.sys, "platform", "darwin"), patch.dict("sys.modules", {"AppKit": self.bridge}):
            with self.assertRaises(AssertionError) as caught:
                with self.context():
                    raise error
        self.assertIs(caught.exception, error)
        self.assertEqual(self.read()["events"][-1]["stage"], "after-original-window-wait")

    def test_secondary_snapshot_failure_does_not_replace_bridge_failure(self):
        error = ValueError("original bridge error")
        self.native.isActive.side_effect = [False, RuntimeError("secondary snapshot error")]
        self.native.activate.side_effect = error
        with patch.object(helper.sys, "platform", "darwin"), patch.dict("sys.modules", {"AppKit": self.bridge}):
            with self.assertRaises(ValueError) as caught:
                with self.context():
                    pass
        self.assertIs(caught.exception, error)
        self.assertIn("secondary snapshot error", self.read()["evidence_errors"][0])


if __name__ == "__main__":
    unittest.main()
