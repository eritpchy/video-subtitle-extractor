"""Offline command/report contracts; these mocks do not certify real OCR."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import e2e_smoke


class CpuModeSmokeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name) / "evidence with spaces"
        self.commands = []
        self.configurations = []
        self.generator = patch.object(e2e_smoke, "generate_video", side_effect=lambda path: path.write_bytes(b"video"))
        self.checker = patch.object(e2e_smoke, "check_srt", side_effect=self.check_srt)
        self.runner = patch.object(e2e_smoke.subprocess, "run", side_effect=self.extract)
        self.generate_mock = self.generator.start()
        self.check_mock = self.checker.start()
        self.run_mock = self.runner.start()
        self.addCleanup(self.generator.stop)
        self.addCleanup(self.checker.stop)
        self.addCleanup(self.runner.stop)

    def extract(self, command, **kwargs):
        self.commands.append((command, kwargs))
        mode = command[command.index("--mode") + 1]
        subtitle = Path(command[command.index("--output") + 1])
        self.assertFalse(subtitle.exists(), "A previous SRT must not pass a new run")
        self.configurations.append(json.loads(Path(kwargs["env"]["VSE_CONFIG_FILE"]).read_text()))
        subtitle.write_text("fresh " + mode, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    def check_srt(self, path):
        self.assertTrue(path.read_text(encoding="utf-8").startswith("fresh "))
        return {"recognized": ["HELLO WORLD", "SECOND LINE"], "subtitle_count": 2}

    def invoke(self, *args):
        with contextlib.redirect_stdout(io.StringIO()):
            return e2e_smoke.main(["--output", str(self.output), *args])

    def test_default_keeps_fast_and_original_output_layout(self):
        report = self.invoke()
        self.assertEqual(report["mode"], "fast")
        self.assertTrue(report["cpu_forced"])
        self.assertTrue(Path(report["srt"]).samefile(self.output / "recognized.srt"))
        self.assertEqual(json.loads((self.output / "report.json").read_text()), report)
        self.assertFalse((self.output / "fast").exists())

    def test_each_mode_forces_cpu_and_preserves_conflicting_preferences(self):
        for mode in e2e_smoke.MODES:
            with self.subTest(mode=mode):
                report = self.invoke("--mode", mode, "--timeout", "123")
                command, kwargs = self.commands[-1]
                self.assertEqual(command[command.index("--mode") + 1], mode)
                self.assertIn("--cpu", command)
                self.assertEqual(command[command.index("--language") + 1], "en")
                self.assertEqual(command[command.index("--threads") + 1], "2")
                self.assertEqual(kwargs["timeout"], 123)
                self.assertEqual(kwargs["cwd"], e2e_smoke.ROOT)
                self.assertEqual(kwargs["env"]["VSE_OFFLINE"], "1")
                self.assertEqual(kwargs["env"]["QT_QPA_PLATFORM"], "offscreen")
                saved = self.configurations[-1]
                self.assertEqual(saved["Main"]["Language"], "korean")
                self.assertNotEqual(saved["Main"]["Mode"], mode)
                self.assertEqual(json.loads((self.output / "isolated-config.json").read_text()), saved)
                self.assertEqual(report["mode"], mode)
                self.assertTrue(report["cpu_forced"])

    def test_all_modes_are_isolated_and_aggregated(self):
        report = self.invoke("--all-modes")
        self.assertEqual(report["result"], "passed")
        self.assertEqual(report["modes_requested"], list(e2e_smoke.MODES))
        self.assertEqual([case["mode"] for case in report["cases"]], list(e2e_smoke.MODES))
        self.assertEqual(len(self.commands), 3)
        for case in report["cases"]:
            folder = self.output / case["mode"]
            self.assertTrue(Path(case["srt"]).parent.samefile(folder))
            self.assertEqual(json.loads((folder / "report.json").read_text()), case)
        self.assertEqual(json.loads((self.output / "report.json").read_text()), report)

    def test_output_parent_alias_refers_to_the_same_real_files(self):
        target = self.output.parent / "real temporary parent"
        target.mkdir()
        alias = self.output.parent / "temporary parent alias"
        try:
            alias.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"Directory symlink unavailable: {error}")
        self.output = alias / "evidence with spaces"
        report = self.invoke()
        self.assertNotEqual(Path(report["srt"]), self.output / "recognized.srt")
        self.assertTrue(Path(report["srt"]).samefile(self.output / "recognized.srt"))
        report = self.invoke("--all-modes")
        for case in report["cases"]:
            self.assertTrue(Path(case["srt"]).samefile(self.output / case["mode"] / "recognized.srt"))

    def test_mode_and_all_modes_are_mutually_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            self.invoke("--mode", "fast", "--all-modes")
        self.assertEqual(raised.exception.code, 2)
        self.generate_mock.assert_not_called()

    def test_unknown_mode_is_rejected_before_work(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            self.invoke("--mode", "tiny")
        self.assertEqual(raised.exception.code, 2)
        self.generate_mock.assert_not_called()

    def test_failed_extraction_cannot_reuse_old_success(self):
        self.invoke()
        self.run_mock.side_effect = lambda command, **kwargs: subprocess.CompletedProcess(command, 1)
        with self.assertRaisesRegex(RuntimeError, "Extraction exited 1"):
            self.invoke("--mode", "accurate")
        self.assertFalse((self.output / "report.json").exists())
        self.assertFalse((self.output / "recognized.srt").exists())
        self.assertEqual(self.check_mock.call_count, 1)

    def test_timeout_cannot_retain_success_report(self):
        self.invoke()
        self.run_mock.side_effect = subprocess.TimeoutExpired("backend.cli", 1)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.invoke("--mode", "auto")
        self.assertFalse((self.output / "report.json").exists())

    def test_srt_validation_failure_is_not_reported_as_passed(self):
        self.check_mock.side_effect = AssertionError("Missing recognized text")
        with self.assertRaisesRegex(AssertionError, "Missing recognized text"):
            self.invoke("--mode", "accurate")
        self.assertFalse((self.output / "report.json").exists())

    def test_saved_configuration_mutation_fails(self):
        def mutate(command, **kwargs):
            result = self.extract(command, **kwargs)
            Path(kwargs["env"]["VSE_CONFIG_FILE"]).write_text("{}")
            return result
        self.run_mock.side_effect = mutate
        with self.assertRaisesRegex(AssertionError, "CLI changed saved GUI preferences"):
            self.invoke("--mode", "accurate")
        self.check_mock.assert_not_called()
        self.assertFalse((self.output / "report.json").exists())

    def test_failed_matrix_records_failed_mode_and_clears_unvisited_old_success(self):
        self.invoke("--all-modes")
        def fail_accurate(command, **kwargs):
            if command[command.index("--mode") + 1] == "accurate":
                return subprocess.CompletedProcess(command, 7)
            return self.extract(command, **kwargs)
        self.run_mock.side_effect = fail_accurate
        with self.assertRaisesRegex(RuntimeError, "Extraction exited 7"):
            self.invoke("--all-modes")
        report = json.loads((self.output / "report.json").read_text())
        self.assertEqual(report["result"], "failed")
        self.assertEqual(report["failed_mode"], "accurate")
        self.assertEqual([case["mode"] for case in report["cases"]], ["fast"])
        self.assertFalse((self.output / "auto/report.json").exists())
        self.assertFalse((self.output / "auto/recognized.srt").exists())


if __name__ == "__main__":
    unittest.main()
