import argparse
import ast
import errno
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from backend.cli import build_parser, extract, parse_area, publish_output


class CliTests(unittest.TestCase):
    def test_version_without_optional_dependencies(self):
        root = Path(__file__).resolve().parents[1]
        declarations = ast.parse((root / "backend/config.py").read_text(encoding="utf-8"))
        version = next(
            ast.literal_eval(node.value)
            for node in declarations.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets)
        )
        result = subprocess.run(
            [sys.executable, "-B", "-S", "-m", "backend.cli", "--version"],
            cwd=root, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(f"Video Subtitle Extractor {version}\n", result.stdout)

    def test_help_without_optional_dependencies(self):
        result = subprocess.run([sys.executable, "-m", "backend.cli", "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--area", result.stdout)

    def test_area(self):
        self.assertEqual(parse_area("0.65,1,0,1"), (0.65, 1.0, 0.0, 1.0))
        for value in ("0,1,2", "x,1,0,1", "1,0,0,1", "0,1,0,2", "nan,1,0,1", "0,inf,0,1"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                parse_area(value)

    def test_missing_input_fails_before_model_import(self):
        with tempfile.TemporaryDirectory() as directory:
            args = build_parser().parse_args([str(Path(directory) / "missing.mp4")])
            with self.assertRaisesRegex(ValueError, "does not exist"):
                extract(args)

    def test_existing_output_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.mp4"
            source.touch()
            output = source.with_suffix(".srt")
            output.write_text("existing subtitle", encoding="utf-8")
            args = build_parser().parse_args([str(source)])
            with self.assertRaisesRegex(ValueError, "already exists"):
                extract(args)
            self.assertEqual(output.read_text(encoding="utf-8"), "existing subtitle")

    def test_output_cannot_be_input(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.mp4"
            source.write_bytes(b"video")
            args = build_parser().parse_args([str(source), "--output", str(source), "--overwrite"])
            with self.assertRaisesRegex(ValueError, "must not replace"):
                extract(args)

    def test_keep_work_does_not_delete_existing_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.mp4"
            source.touch()
            args = build_parser().parse_args([str(source), "--keep-work", directory])
            with self.assertRaisesRegex(ValueError, "new directory"):
                extract(args)

    def test_publish_does_not_clobber_output_created_during_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staged, destination = root / "staged.srt", root / "result.srt"
            staged.write_text("our result", encoding="utf-8")
            destination.write_text("concurrent result", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                publish_output(staged, destination)
            self.assertEqual(destination.read_text(encoding="utf-8"), "concurrent result")
            self.assertEqual(list(root.glob(".vse-*")), [])

    def test_publish_new_output_and_explicit_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staged, destination = root / "staged.srt", root / "result.srt"
            staged.write_text("first", encoding="utf-8")
            publish_output(staged, destination)
            self.assertEqual(destination.read_text(encoding="utf-8"), "first")
            staged.write_text("second", encoding="utf-8")
            publish_output(staged, destination, overwrite=True)
            self.assertEqual(destination.read_text(encoding="utf-8"), "second")

    def test_publish_on_filesystem_without_hardlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staged, destination = root / "staged.srt", root / "result.srt"
            staged.write_text("complete result", encoding="utf-8")
            with patch("backend.cli.os.link", side_effect=OSError(errno.EOPNOTSUPP, "no hardlinks")):
                publish_output(staged, destination)
                with self.assertRaises(FileExistsError):
                    publish_output(staged, destination)
            self.assertEqual(destination.read_text(encoding="utf-8"), "complete result")

    def test_failed_exclusive_copy_removes_only_its_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staged, destination = root / "staged.srt", root / "result.srt"
            staged.write_text("complete result", encoding="utf-8")
            with patch("backend.cli.os.link", side_effect=OSError(errno.EOPNOTSUPP, "no hardlinks")), \
                 patch("backend.cli.shutil.copyfileobj", side_effect=RuntimeError("copy interrupted")):
                with self.assertRaisesRegex(RuntimeError, "copy interrupted"):
                    publish_output(staged, destination)
            self.assertFalse(destination.exists())
            self.assertEqual(staged.read_text(encoding="utf-8"), "complete result")

    def test_windows_unsupported_hardlink_errors_use_exclusive_copy(self):
        for winerror in (1, 50):
            with self.subTest(winerror=winerror), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                staged, destination = root / "staged.srt", root / "result.srt"
                staged.write_text("complete result", encoding="utf-8")
                error = OSError(errno.EINVAL, "Windows filesystem does not support hardlinks")
                error.winerror = winerror
                with patch("backend.cli.os.link", side_effect=error):
                    publish_output(staged, destination)
                self.assertEqual(destination.read_text(encoding="utf-8"), "complete result")


if __name__ == "__main__":
    unittest.main()
