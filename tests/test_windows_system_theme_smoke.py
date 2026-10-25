"""Linux-safe mocks only; these tests are not Windows OS round-trip evidence."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ui import windows_system_theme_smoke as smoke

ROOT = Path(__file__).resolve().parents[1]
CI_ENV = {"VSE_CI_WINDOWS_SYSTEM_THEME": "1", "CI": "true", "GITHUB_ACTIONS": "true",
          "RUNNER_ENVIRONMENT": "github-hosted", "RUNNER_OS": "Windows",
          "VSE_CI_WINDOWS_RUNNER": "windows-2022"}


class FakeKey:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


def registry(value=None):
    state = [value]
    fake = Mock(HKEY_CURRENT_USER=123, KEY_QUERY_VALUE=1, KEY_SET_VALUE=2, REG_DWORD=4,
                KEY_CREATE_SUB_KEY=4, KEY_ENUMERATE_SUB_KEYS=8)
    fake.OpenKey.return_value = FakeKey()
    fake.CreateKeyEx.return_value = FakeKey()
    fake.QueryInfoKey.return_value = (0, 0, 0)

    def read(*_):
        if state[0] is None:
            raise FileNotFoundError
        return state[0]

    def write(key, name, reserved, kind, data):
        state[0] = (data, kind)

    def delete(*_):
        state[0] = None

    fake.QueryValueEx.side_effect = read
    fake.SetValueEx.side_effect = write
    fake.DeleteValue.side_effect = delete
    return fake, state


class GuardTests(unittest.TestCase):
    def test_all_ci_opt_ins_and_native_server_measurement_are_required(self):
        with patch.object(smoke.sys, "platform", "win32"), \
                patch.object(smoke.sys, "argv", ["gui.py", "--smoke-test"]), \
                patch.object(smoke.sys, "getwindowsversion", create=True,
                             return_value=SimpleNamespace(product_type=3, build=20348)), \
                patch.dict(os.environ, CI_ENV, clear=True):
            self.assertEqual(smoke.require_hosted_server_ci("windows")["build"], 20348)
            for key in CI_ENV:
                with self.subTest(key=key), patch.dict(os.environ, {key: ""}):
                    with self.assertRaises(PermissionError):
                        smoke.require_hosted_server_ci("windows")
            with self.assertRaises(PermissionError):
                smoke.require_hosted_server_ci("offscreen")
            for version in (SimpleNamespace(product_type=1, build=20348),
                            SimpleNamespace(product_type=3, build=26100)):
                with patch.object(smoke.sys, "getwindowsversion", return_value=version):
                    with self.assertRaises(PermissionError):
                        smoke.require_hosted_server_ci("windows")
            with patch.object(smoke.sys, "argv", ["gui.py"]):
                with self.assertRaises(PermissionError):
                    smoke.require_hosted_server_ci("windows")

    def test_linux_rejection_emits_blocked_not_passed_without_registry_access(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(smoke.sys, "platform", "linux"), \
                patch.dict(os.environ, {**CI_ENV, "VSE_WINDOWS_SYSTEM_THEME_REPORT": str(Path(temp) / "report.json")}), \
                patch.object(smoke, "restored_appearance") as mutation:
            with self.assertRaises(PermissionError):
                smoke.run_windows_system_theme_smoke(SimpleNamespace(platformName=lambda: "windows"),
                                                     None, None, None)
            report = json.loads((Path(temp) / "report.json").read_text())
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(report["restoration"]["status"], "not-needed")
            self.assertFalse(report["transitions"])
            mutation.assert_not_called()

    def test_default_evidence_follows_external_screenshot_directory(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(smoke.sys, "platform", "linux"), \
                patch.dict(os.environ, CI_ENV, clear=True):
            with self.assertRaises(PermissionError):
                smoke.run_windows_system_theme_smoke(SimpleNamespace(platformName=lambda: "windows"),
                                                     None, None, Path(temp))
            self.assertEqual(json.loads((Path(temp) / "system-theme.json").read_text())["status"], "blocked")
            with self.assertRaisesRegex(PermissionError, "external report or screenshot"):
                smoke.run_windows_system_theme_smoke(None, None, None, None)


class RestorationTests(unittest.TestCase):
    def test_original_value_type_or_absence_restored_after_each_failure_point(self):
        for original in (None, (0, 4), (1, 4), ("unexpected", 1), (b"\x00\xff", 3)):
            for fail_after in (None, 0, 1, 2):
                with self.subTest(original=original, fail_after=fail_after):
                    fake, state = registry(original)
                    report, notify = {}, Mock()

                    def exercise():
                        with smoke.restored_appearance(fake, report, notify) as key:
                            for index, light in enumerate((1, 0, 1)):
                                fake.SetValueEx(key, smoke.VALUE_NAME, 0, 4, light)
                                if index == fail_after:
                                    raise AssertionError("response failed")

                    if fail_after is None:
                        exercise()
                    else:
                        with self.assertRaisesRegex(AssertionError, "response failed"):
                            exercise()
                    self.assertEqual(state[0], original)
                    self.assertEqual(report["restoration"]["status"], "restored")
                    notify.assert_called_once_with()
                    fake.OpenKey.assert_called_once_with(fake.HKEY_CURRENT_USER, smoke.PERSONALIZE, 0, 3)
                    # The only value ever changed is the documented appearance preference.
                    self.assertTrue(all(call.args[1] == smoke.VALUE_NAME for call in fake.SetValueEx.call_args_list))
                    json.dumps(report, default=smoke._json_value)

    def test_restoration_runs_on_interruption_and_notify_failure_still_restores_registry(self):
        fake, state = registry((0, 4))
        report = {}
        with self.assertRaises(KeyboardInterrupt):
            with smoke.restored_appearance(fake, report, Mock()) as key:
                fake.SetValueEx(key, smoke.VALUE_NAME, 0, 4, 1)
                raise KeyboardInterrupt
        self.assertEqual(state[0], (0, 4))
        with self.assertRaisesRegex(OSError, "broadcast failed"):
            with smoke.restored_appearance(fake, report, Mock(side_effect=OSError("broadcast failed"))) as key:
                fake.SetValueEx(key, smoke.VALUE_NAME, 0, 4, 1)
        self.assertEqual(state[0], (0, 4))
        self.assertEqual(report["restoration"]["status"], "failed")

    def test_absent_key_and_value_are_created_then_removed_when_empty(self):
        fake, state = registry()
        fake.OpenKey.side_effect = [FileNotFoundError, FakeKey()]
        report, notify = {}, Mock()
        with smoke.restored_appearance(fake, report, notify) as key:
            fake.SetValueEx(key, smoke.VALUE_NAME, 0, 4, 0)
        self.assertIsNone(state[0])
        self.assertFalse(report["original_registry_key_exists"])
        self.assertEqual(report["restoration"]["created_key_cleanup"], "removed-empty-key")
        fake.DeleteKey.assert_called_once()
        notify.assert_called_once_with()

    def test_new_key_with_foreign_entries_is_retained_not_deleted_or_passed(self):
        fake, state = registry()
        fake.OpenKey.side_effect = [FileNotFoundError, FakeKey()]
        fake.QueryInfoKey.return_value = (0, 1, 0)
        report = {}
        with self.assertRaisesRegex(RuntimeError, "other entries"):
            with smoke.restored_appearance(fake, report, Mock()) as key:
                fake.SetValueEx(key, smoke.VALUE_NAME, 0, 4, 0)
        self.assertIsNone(state[0])
        fake.DeleteKey.assert_not_called()
        self.assertEqual(report["restoration"]["status"], "failed")

    def test_missing_parent_does_not_create_untracked_ancestors(self):
        fake, _ = registry()
        fake.OpenKey.side_effect = FileNotFoundError
        report, notify = {}, Mock()
        with self.assertRaisesRegex(RuntimeError, "refusing untracked ancestor"):
            with smoke.restored_appearance(fake, report, notify):
                self.fail("No mutation body may run")
        fake.SetValueEx.assert_not_called()
        fake.CreateKey.assert_not_called()
        fake.CreateKeyEx.assert_not_called()
        notify.assert_not_called()

    def test_failed_snapshot_cleans_up_its_empty_new_key_without_setting_a_value(self):
        fake, _ = registry()
        fake.OpenKey.side_effect = [FileNotFoundError, FakeKey()]
        fake.QueryValueEx.side_effect = PermissionError("snapshot denied")
        report = {}
        with self.assertRaisesRegex(PermissionError, "snapshot denied"):
            with smoke.restored_appearance(fake, report, Mock()):
                self.fail("Snapshot did not succeed")
        fake.SetValueEx.assert_not_called()
        fake.DeleteKey.assert_called_once()
        self.assertEqual(report["restoration"]["created_key_cleanup"], "removed-empty-key")

    def test_failed_restore_cannot_be_claimed_as_restored(self):
        fake, _ = registry((0, 4))
        report = {}
        with self.assertRaisesRegex(PermissionError, "write denied"):
            with smoke.restored_appearance(fake, report, Mock()) as key:
                fake.SetValueEx(key, smoke.VALUE_NAME, 0, 4, 1)
                fake.SetValueEx.side_effect = PermissionError("write denied")
        self.assertEqual(report["restoration"]["status"], "failed")


class WiringTests(unittest.TestCase):
    def test_mutation_is_only_enabled_in_native_server_smoke_step(self):
        for filename in ("ci.yml", "native-ocr.yml"):
            text = (ROOT / ".github/workflows" / filename).read_text()
            self.assertEqual(text.count("VSE_CI_WINDOWS_SYSTEM_THEME:"), 1)
            self.assertIn("VSE_CI_WINDOWS_RUNNER: ${{ matrix.os }}", text)
            start = text.index("id: windows-gui")
            end = text.index("run: python gui.py --smoke-test", start)
            self.assertIn("VSE_CI_WINDOWS_SYSTEM_THEME:", text[start:end])
        text = (ROOT / "ui/smoke_test.py").read_text()
        self.assertLess(text.index("run_windows_system_theme_smoke(app"), text.index("with patch.object(listener"))

    def test_real_probe_does_not_force_qt_or_call_simulated_listener_inputs(self):
        text = (ROOT / "ui/windows_system_theme_smoke.py").read_text()
        for forbidden in (".emit(", "._readSystemTheme", "._onThemeChanged", ".setColorScheme(",
                          ".refresh(", "unittest.mock", "SystemUsesLightTheme", "HKEY_LOCAL_MACHINE"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
