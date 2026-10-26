"""Synthetic source-byte contracts, not real Paddle inference certification."""
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import traceback
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from scripts import paddle_package_models as models


def digest(data):
    return hashlib.sha256(data).hexdigest()


class PinnedOriginalModelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.output = self.root / "original models"
        self.pins = json.loads(models.BUILD_PINS.read_bytes())
        self.pin_path = self.root / "synthetic-pins.json"
        self.contents = {}
        for row in self.pins["models"]:
            data = {name: (row["name"] + ":" + name + "\n").encode() for name in models.SOURCE_FILES}
            self.contents[row["name"]] = data
            row["files"] = {name: digest(value) for name, value in data.items()}
            self.write_archive(row, data)
        self.pin_path.write_bytes(models._canonical(self.pins))
        self.pin_patch = patch.object(models, "BUILD_PINS", self.pin_path)
        self.trust_patch = patch.object(models, "PINNED_SOURCE_SHA256", digest(self.pin_path.read_bytes()))
        self.pin_patch.start()
        self.trust_patch.start()
        self.addCleanup(self.pin_patch.stop)
        self.addCleanup(self.trust_patch.stop)

    def write_archive(self, row, data, malicious=False):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            for name, value in data.items():
                entry = tarfile.TarInfo(row["name"] + "_infer/" + name)
                entry.size = len(value)
                archive.addfile(entry, io.BytesIO(value))
            if malicious:
                entry = tarfile.TarInfo("../escape")
                entry.size = 3
                archive.addfile(entry, io.BytesIO(b"bad"))
        data = stream.getvalue()
        row["archive_bytes"] = len(data)
        row["archive_sha256"] = digest(data)
        (self.cache / (row["archive_sha256"] + ".tar")).write_bytes(data)

    def prepare(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("No network allowed")):
            return models.prepare_models(self.output, self.cache, offline=True)

    def first_source(self):
        row = self.pins["models"][0]
        return self.output / models._model_path(row["name"]) / "inference.pdiparams"

    def test_offline_prepare_and_stage_preserve_all_fifteen_original_models(self):
        report = self.prepare()
        self.assertEqual(report["model_count"], 15)
        self.assertEqual(report["generations"], {"V6": 4, "V5": 10, "V3": 1})
        self.assertFalse(report["inference_verified"])
        self.assertEqual(len(report["files"]), 45)
        self.assertEqual(models.verify_models(self.output), report)
        for name in ("ta_PP-OCRv5_mobile_rec", "te_PP-OCRv5_mobile_rec", "ka_PP-OCRv3_mobile_rec"):
            self.assertTrue((self.output / models._model_path(name) / "inference.json").is_file())
        kannada = next(row for row in report["models"] if row["name"] == "ka_PP-OCRv3_mobile_rec")
        self.assertEqual(kannada["languages"], ["kn", "ka"])
        destination = self.root / "package/backend/models"
        self.assertEqual(models.stage_paddle_models(self.output, destination), report)
        self.assertEqual(models.verify_models(destination), report)
        for path in self.output.rglob("*"):
            if path.is_file():
                self.assertEqual(path.read_bytes(), (destination / path.relative_to(self.output)).read_bytes())

    def test_prepare_reuses_shared_archive_and_extraction_helpers(self):
        helper = models._build_tools()
        with patch.object(models, "_build_tools", return_value=helper), \
             patch.object(helper, "get_archive", wraps=helper.get_archive) as get, \
             patch.object(helper, "extract_sources", wraps=helper.extract_sources) as extract:
            models.prepare_models(self.output, self.cache, offline=False)
        self.assertEqual(get.call_count, 15)
        self.assertEqual(extract.call_count, 15)
        self.assertTrue(all(call.args[2] is False for call in get.call_args_list))

    def test_http_403_preserves_error_and_adds_safe_pinned_identity_without_retry(self):
        row = self.pins["models"][0]
        archive = self.cache / (row["archive_sha256"] + ".tar")
        archive.unlink()
        error = HTTPError("https://secret-user:secret-password@redirect.invalid/file?secret-token=1#secret-fragment",
                          403, "Forbidden", {"X-Test": "original-header"}, None)
        helper = models._build_tools()
        with patch.object(models, "_build_tools", return_value=helper), \
             patch.object(helper, "get_archive", wraps=helper.get_archive) as get, \
             patch.object(helper, "extract_sources") as extract, \
             patch.object(helper.time, "sleep") as sleep, \
             patch("urllib.request.urlopen", side_effect=error) as request:
            try:
                models.prepare_models(self.output, self.cache, offline=False)
            except HTTPError as caught:
                self.assertIs(caught, error)
                self.assertEqual(caught.code, 403)
                self.assertEqual(caught.reason, "Forbidden")
                self.assertEqual(caught.headers, {"X-Test": "original-header"})
                rendered = "".join(traceback.format_exception(caught))
                context = json.loads(caught.__notes__[-1].split(": ", 1)[1])
            else:
                self.fail("HTTP 403 must still fail preparation")
        get.assert_called_once_with(row, self.cache, False)
        request.assert_called_once_with(row["url"], timeout=120)
        sleep.assert_not_called()
        extract.assert_not_called()
        self.assertEqual(context, {"model": row["name"], "archive_sha256": row["archive_sha256"],
                                   "source_url": row["url"], "cache": "missing", "offline": False})
        self.assertIn("HTTP Error 403: Forbidden", rendered)
        self.assertIn(row["archive_sha256"], rendered)
        for secret in ("secret-user", "secret-password", "redirect.invalid", "secret-token", "secret-fragment"):
            self.assertNotIn(secret, rendered)
        self.assertFalse(archive.exists())
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob(".paddle-models-*")))
        self.assertFalse(list(self.cache.glob(".download-*")))

    def test_archive_error_notes_describe_cache_presence_not_download_success(self):
        row = self.pins["models"][0]
        helper = models._build_tools()
        error = OSError("synthetic cache read failure")
        with patch.object(models, "_build_tools", return_value=helper), \
             patch.object(helper, "get_archive", side_effect=error) as get, \
             patch("urllib.request.urlopen", side_effect=AssertionError("No network allowed")) as request:
            with self.assertRaises(OSError) as raised:
                models.prepare_models(self.output, self.cache, offline=True)
        self.assertIs(raised.exception, error)
        get.assert_called_once_with(row, self.cache, True)
        request.assert_not_called()
        context = json.loads(error.__notes__[-1].split(": ", 1)[1])
        self.assertEqual(context["cache"], "present")
        self.assertIs(context["offline"], True)
        self.assertNotIn("success", error.__notes__[-1])
        self.assertFalse(self.output.exists())

    def test_diagnostic_sanitization_does_not_authorize_decorated_download_urls(self):
        row = self.pins["models"][0]
        row["url"] = row["url"].replace("https://", "https://user:password@") + "?token=private#fragment"
        self.pin_path.write_bytes(models._canonical(self.pins))
        with patch.object(models, "PINNED_SOURCE_SHA256", digest(self.pin_path.read_bytes())), \
             patch.object(models, "_build_tools") as build_tools, \
             patch("urllib.request.urlopen", side_effect=AssertionError("No network allowed")) as request:
            with self.assertRaisesRegex(ValueError, "Unapproved original-model source"):
                models.prepare_models(self.output, self.cache)
        build_tools.assert_not_called()
        request.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_offline_missing_archive_never_downloads_or_publishes(self):
        next(self.cache.iterdir()).unlink()
        with self.assertRaisesRegex(FileNotFoundError, "Offline source missing"):
            self.prepare()
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob(".paddle-models-*")))

    def test_corrupt_cache_is_rejected_before_other_downloads(self):
        last = self.pins["models"][-1]
        (self.cache / (last["archive_sha256"] + ".tar")).write_bytes(b"corrupt")
        helper = models._build_tools()
        with patch.object(models, "_build_tools", return_value=helper), \
             patch.object(helper, "get_archive", side_effect=AssertionError("must fail before transport")):
            with self.assertRaisesRegex(ValueError, "Corrupt pinned source archive"):
                models.prepare_models(self.output, self.cache)
        self.assertFalse(self.output.exists())

    def test_cache_hit_still_checks_pinned_size(self):
        self.pins["models"][0]["archive_bytes"] += 1
        self.pin_path.write_bytes(models._canonical(self.pins))
        with patch.object(models, "PINNED_SOURCE_SHA256", digest(self.pin_path.read_bytes())):
            with self.assertRaisesRegex(ValueError, "Corrupt pinned source archive"):
                self.prepare()

    def test_unsafe_archive_is_rejected_without_partial_output(self):
        row = self.pins["models"][0]
        self.write_archive(row, self.contents[row["name"]], malicious=True)
        self.pin_path.write_bytes(models._canonical(self.pins))
        with patch.object(models, "PINNED_SOURCE_SHA256", digest(self.pin_path.read_bytes())):
            with self.assertRaisesRegex(ValueError, "Unsafe source archive member"):
                self.prepare()
        self.assertFalse(self.output.exists())
        self.assertFalse((self.root / "escape").exists())

    def test_modified_source_cannot_be_authorized_by_inventory(self):
        self.prepare()
        source = self.first_source()
        data = source.read_bytes()
        source.write_bytes(b"X" + data[1:])
        inventory = self.output / models.INVENTORY_NAME
        value = json.loads(inventory.read_bytes())
        value["files"][source.relative_to(self.output).as_posix()]["sha256"] = digest(source.read_bytes())
        inventory.write_bytes(models._canonical(value))
        with self.assertRaisesRegex(ValueError, "source hash mismatch"):
            models.verify_models(self.output)

    def test_artifact_cannot_self_certify_replacement_pins(self):
        self.prepare()
        path = self.output / models.PINS_NAME
        changed = json.loads(path.read_bytes())
        changed["models"][0]["files"]["inference.json"] = "0" * 64
        path.write_bytes(models._canonical(changed))
        with self.assertRaisesRegex(ValueError, "fixed trusted SHA256"):
            models.verify_models(self.output)

    def test_missing_model_or_source_fails_closed(self):
        self.prepare()
        self.first_source().unlink()
        with self.assertRaisesRegex(ValueError, "complete pinned 15-model set"):
            models.verify_models(self.output)

    def test_unexpected_file_and_empty_directory_fail_closed(self):
        self.prepare()
        extra = self.output / "unexpected"
        extra.write_bytes(b"extra")
        with self.assertRaisesRegex(ValueError, "complete pinned 15-model set"):
            models.verify_models(self.output)
        extra.unlink()
        extra.mkdir()
        with self.assertRaisesRegex(ValueError, "complete pinned 15-model set"):
            models.verify_models(self.output)

    def test_changed_inventory_metadata_is_rejected(self):
        self.prepare()
        inventory = self.output / models.INVENTORY_NAME
        value = json.loads(inventory.read_bytes())
        value["inference_verified"] = True
        inventory.write_bytes(models._canonical(value))
        with self.assertRaisesRegex(ValueError, "inventory differs"):
            models.verify_models(self.output)

    def test_symlink_model_source_is_rejected(self):
        self.prepare()
        source = self.first_source()
        original = self.root / "external"
        source.rename(original)
        try:
            source.symlink_to(original)
        except OSError as error:
            self.skipTest("Symlink creation unavailable: " + str(error))
        with self.assertRaisesRegex(ValueError, "Invalid original-model asset"):
            models.verify_models(self.output)

    def test_dangling_cache_symlink_is_rejected_before_download(self):
        row = self.pins["models"][0]
        archive = self.cache / (row["archive_sha256"] + ".tar")
        archive.unlink()
        try:
            archive.symlink_to(self.root / "missing")
        except OSError as error:
            self.skipTest("Symlink creation unavailable: " + str(error))
        with self.assertRaisesRegex(ValueError, "Corrupt pinned source archive"):
            self.prepare()

    def test_root_symlink_is_rejected_but_system_temporary_parent_alias_is_supported(self):
        self.prepare()
        root_alias = self.root / "models-alias"
        parent_alias = self.root / "temporary-parent-alias"
        try:
            root_alias.symlink_to(self.output, target_is_directory=True)
            parent_alias.symlink_to(self.root, target_is_directory=True)
        except OSError as error:
            self.skipTest("Symlink creation unavailable: " + str(error))
        with self.assertRaisesRegex(ValueError, "must not be a symlink"):
            models.verify_models(root_alias)
        self.assertEqual(models.verify_models(parent_alias / self.output.name)["model_count"], 15)

    def test_output_created_during_preparation_is_preserved(self):
        helper = models._build_tools()
        original_extract = helper.extract_sources
        sentinel = self.output / "user-file"
        def create_output(archive, row, destination):
            result = original_extract(archive, row, destination)
            self.output.mkdir(exist_ok=True)
            sentinel.write_bytes(b"created while preparation ran")
            return result
        with patch.object(models, "_build_tools", return_value=helper), \
             patch.object(helper, "extract_sources", side_effect=create_output):
            with self.assertRaises(FileExistsError):
                self.prepare()
        self.assertEqual(sentinel.read_bytes(), b"created while preparation ran")
        self.assertEqual(list(self.output.iterdir()), [sentinel])

    def test_nonempty_output_is_never_replaced(self):
        self.output.mkdir()
        sentinel = self.output / "user-file"
        sentinel.write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            self.prepare()
        self.assertEqual(sentinel.read_bytes(), b"keep")

    def test_empty_destination_is_supported_but_staging_will_not_overwrite_existing_tree(self):
        self.output.mkdir()
        self.prepare()
        destination = self.root / "destination"
        destination.mkdir()
        models.stage_paddle_models(self.output, destination)
        with self.assertRaises(FileExistsError):
            models.stage_paddle_models(self.output, destination)

    def test_overlapping_paths_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            models.prepare_models(self.output, self.output / "cache")
        self.prepare()
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            models.stage_paddle_models(self.output, self.output / "copy")

    def test_stage_reverifies_copied_bytes_before_publication(self):
        self.prepare()
        destination = self.root / "destination"
        original_copy = shutil.copytree
        relative = self.first_source().relative_to(self.output)
        def corrupt_copy(source, target, *args, **kwargs):
            result = original_copy(source, target, *args, **kwargs)
            if Path(source) == self.output:
                (Path(target) / relative).write_bytes(b"changed in transit")
            return result
        with patch.object(models.shutil, "copytree", side_effect=corrupt_copy):
            with self.assertRaisesRegex(ValueError, "source hash mismatch"):
                models.stage_paddle_models(self.output, destination)
        self.assertFalse(destination.exists())

    def test_standalone_verify_runs_without_site_packages_or_native_checkout(self):
        self.prepare()
        script = self.root / "isolated/paddle_package_models.py"
        script.parent.mkdir()
        # Only the test fixture's trust root is substituted; production stays fixed.
        source = Path(models.__file__).read_text().replace(
            "db66f8583dd143a21a2949b4a4833da41ce98dcfd5e7837df7c8800f8884c015",
            models.PINNED_SOURCE_SHA256)
        script.write_text(source)
        result = subprocess.run([sys.executable, "-I", "-S", str(script),
                                 "--verify-only", str(self.output)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["model_count"], 15)


class CommittedTrustRootTests(unittest.TestCase):
    def test_diagnostic_url_strips_credentials_query_and_fragment_and_withholds_other_origins(self):
        _, pins = models._read_pins(models.BUILD_PINS)
        row = pins["models"][0]
        original = row["url"]
        decorated = original.replace("https://", "https://secret-user:secret-password@") + "?secret-token=1#secret-fragment"
        self.assertEqual(models._diagnostic_source_url(dict(row, url=decorated)), original)
        for url in (original.replace("https://", "http://"),
                    original.replace(".bj.bcebos.com/", ".bj.bcebos.com.evil.invalid/"),
                    original.replace(".com/", ".com:8443/"),
                    original + "/secret-path", "https://[invalid"):
            with self.subTest(url=url):
                self.assertEqual(models._diagnostic_source_url(dict(row, url=url)), "<untrusted source withheld>")

    def test_real_committed_pins_match_hardcoded_digest_and_complete_model_set(self):
        data, pins = models._read_pins(models.BUILD_PINS)
        self.assertEqual(digest(data), "db66f8583dd143a21a2949b4a4833da41ce98dcfd5e7837df7c8800f8884c015")
        self.assertEqual(len(pins["models"]), 15)
        self.assertEqual(len({models._model_path(row["name"]) for row in pins["models"]}), 15)

    def test_verify_cli_rejects_build_options(self):
        result = subprocess.run([sys.executable, "-I", "-S", models.__file__,
                                 "--verify-only", "unused", "--cache", "unused"],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertIn("always offline", result.stderr)


if __name__ == "__main__":
    unittest.main()
