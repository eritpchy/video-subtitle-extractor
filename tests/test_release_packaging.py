"""Dependency-light release safety and archive regression tests."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts.bundle_bootstrap import sha256, verify_payload
from scripts.package_release import create_bundle, extract_checked, include_source, read_version, wheel_lock, verify_bundle
from scripts.release_guard import assert_target, remote_tag_commit, validate_tag


class ReleaseGuardTests(unittest.TestCase):
    def test_existing_tag_is_never_retargeted(self):
        with self.assertRaisesRegex(RuntimeError, "immutable"):
            assert_target("2.2.0", "a" * 40, "a" * 40, "b" * 40, False)

    def test_existing_release_is_never_overwritten(self):
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            assert_target("2.2.0", "a" * 40, "a" * 40, "a" * 40, True)

    def test_sha_must_match_checkout(self):
        with self.assertRaisesRegex(RuntimeError, "checked-out"):
            assert_target("2.2.0", "a" * 40, "b" * 40, None, False)

    def test_new_preview_and_same_commit_tag_allowed(self):
        assert_target("2.2.0-preview.1", "a" * 40, "a" * 40, None, False)
        assert_target("2.2.0", "a" * 40, "a" * 40, "a" * 40, False)
        for value in ("2.2.0", "v2.2.0", "2.2.0-preview.1"):
            validate_tag(value, "2.2.0")

    def test_unsafe_or_mismatched_tags_are_rejected(self):
        for value in ("", "2.1.0", "2.2.0; echo bad", "2.2.0-preview..1", "--help", "2.2.0-preview."):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                validate_tag(value, "2.2.0")

    def test_annotated_tag_resolves_to_commit(self):
        output = "b" * 40 + "\trefs/tags/2.2.0\n" + "a" * 40 + "\trefs/tags/2.2.0^{}\n"
        self.assertEqual(remote_tag_commit(output, "2.2.0"), "a" * 40)

    def test_version_does_not_import_ocr_or_gui(self):
        self.assertRegex(read_version(), r"^\d+\.\d+\.\d+")


class PackageTests(unittest.TestCase):
    def test_only_native_verified_subfinder_included(self):
        self.assertTrue(include_source(Path("backend/subfinder/linux/x64/VideoSubFinderCli"), "linux", "x64"))
        for path in ("backend/subfinder/windows/x64/VideoSubFinderCli.exe", "backend/subfinder/linux/VideoSubFinderCli", "backend/__pycache__/config.pyc"):
            self.assertFalse(include_source(Path(path), "linux", "x64"))

    def test_payload_checksum_detects_change_and_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "payload.py"
            path.write_text("print('ok')", encoding="utf-8")
            manifest = {"files": {"payload.py": sha256(path)}}
            verify_payload(root, manifest)
            path.write_text("changed", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "checksum"):
                verify_payload(root, manifest)
            with self.assertRaisesRegex(RuntimeError, "invalid"):
                verify_payload(root, {"files": {"../outside": "bad"}})

    def test_wheel_lock_reads_metadata_and_rejects_duplicates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with zipfile.ZipFile(root / "sample-1.2-py3-none-any.whl", "w") as archive:
                archive.writestr("sample-1.2.dist-info/METADATA", "Name: Sample_Package\nVersion: 1.2\n")
                archive.writestr("sample/vendor/other-1.0.dist-info/METADATA", "Name: Other\nVersion: 1.0\n")
            self.assertEqual(wheel_lock(root), ["sample-package==1.2"])
            with zipfile.ZipFile(root / "sample-1.3-py3-none-any.whl", "w") as archive:
                archive.writestr("sample-1.3.dist-info/METADATA", "Name: sample-package\nVersion: 1.3\n")
            with self.assertRaisesRegex(RuntimeError, "Duplicate"):
                wheel_lock(root)

    def test_bundle_roundtrip_excludes_untracked_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            for directory in ("backend/subfinder/linux/x64", "backend/models/V6", "scripts", "wheelhouse"):
                (root / directory).mkdir(parents=True)
            (root / "backend/config.py").write_text('VERSION = "2.2.0"\n', encoding="utf-8")
            (root / "backend/private-untracked.json").write_text("must not ship", encoding="utf-8")
            (root / "backend/subfinder/linux/x64/VideoSubFinderCli").write_text("runtime", encoding="utf-8")
            (root / "backend/models/V6/model.json").write_text("model", encoding="utf-8")
            (root / "scripts/bundle_bootstrap.py").write_text("# bootstrap", encoding="utf-8")
            with zipfile.ZipFile(root / "wheelhouse/sample-1.0-py3-none-any.whl", "w") as archive:
                archive.writestr("sample-1.0.dist-info/METADATA", "Name: sample\nVersion: 1.0\n")
            outputs = ["a" * 40 + "\n", "", b"backend/config.py\0scripts/bundle_bootstrap.py\0"]
            with patch("scripts.package_release.host_platform", return_value=("linux", "x64")), patch("scripts.package_release.subprocess.check_output", side_effect=outputs), patch("scripts.package_release.subprocess.run"):
                archive_path = create_bundle(root / "wheelhouse", root / "dist", root)
            verify_bundle(archive_path, smoke=False)
            with zipfile.ZipFile(archive_path) as archive:
                names = archive.namelist()
                self.assertFalse(any("private-untracked" in name for name in names))
                manifest = json.loads(archive.read(next(name for name in names if name.endswith("/bundle-manifest.json"))))
                self.assertEqual(manifest["commit"], "a" * 40)
                self.assertIn("backend/models/V6/model.json", manifest["files"])
                self.assertIn("backend/subfinder/linux/x64/VideoSubFinderCli", manifest["files"])

    def test_archive_rejects_escape_and_symlink(self):
        for name in ("../escape", "/absolute", "root/../../escape", "C:/escape", "root\\escape"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive_path = root / "bad.zip"
                with zipfile.ZipFile(archive_path, "w") as archive:
                    archive.writestr(name, "bad")
                with self.assertRaises(RuntimeError):
                    extract_checked(archive_path, root / "out")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "link.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                info = zipfile.ZipInfo("root/link")
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, "/tmp/target")
            with self.assertRaises(RuntimeError):
                extract_checked(archive_path, root / "out")

    @unittest.skipIf(os.name == "nt", "POSIX execute permission is not meaningful on Windows")
    def test_archive_retains_executable_permission(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "bundle.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                info = zipfile.ZipInfo("bundle/launch.sh")
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o755) << 16
                archive.writestr(info, "#!/bin/sh\n")
            bundle = extract_checked(archive_path, root / "out")
            self.assertTrue((bundle / "launch.sh").stat().st_mode & stat.S_IXUSR)


if __name__ == "__main__":
    unittest.main()
