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

from scripts.bundle_bootstrap import sha256, verify_payload, write_size_report, main as bootstrap_main, configure_user_paths
from scripts.package_release import create_bundle, extract_checked, include_source, read_version, wheel_lock, verify_bundle, verification_path, finalize_portable_runtime
from scripts.release_guard import assert_target, remote_tag_commit, validate_tag


class ReleaseGuardTests(unittest.TestCase):
    def test_existing_tag_is_never_retargeted(self):
        with self.assertRaisesRegex(RuntimeError, "immutable"):
            assert_target("2.3.0", "a" * 40, "a" * 40, "b" * 40, False)

    def test_existing_release_is_never_overwritten(self):
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            assert_target("2.3.0", "a" * 40, "a" * 40, "a" * 40, True)

    def test_sha_must_match_checkout(self):
        with self.assertRaisesRegex(RuntimeError, "checked-out"):
            assert_target("2.3.0", "a" * 40, "b" * 40, None, False)

    def test_new_preview_and_same_commit_tag_allowed(self):
        assert_target("2.3.0-preview.1", "a" * 40, "a" * 40, None, False)
        assert_target("2.3.0", "a" * 40, "a" * 40, "a" * 40, False)
        for value in ("2.3.0", "v2.3.0", "2.3.0-preview.1"):
            validate_tag(value, "2.3.0")

    def test_unsafe_or_mismatched_tags_are_rejected(self):
        for value in ("", "2.1.0", "2.3.0; echo bad", "2.3.0-preview..1", "--help", "2.3.0-preview."):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                validate_tag(value, "2.3.0")

    def test_annotated_tag_resolves_to_commit(self):
        output = "b" * 40 + "\trefs/tags/2.3.0\n" + "a" * 40 + "\trefs/tags/2.3.0^{}\n"
        self.assertEqual(remote_tag_commit(output, "2.3.0"), "a" * 40)

    def test_version_does_not_import_ocr_or_gui(self):
        self.assertRegex(read_version(), r"^\d+\.\d+\.\d+")


class PackageTests(unittest.TestCase):
    def test_interpreter_pruning_uses_canonical_stage_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            (parent / 'alias-parent').mkdir()
            stage = parent / 'stage'
            runtime = stage / 'runtime'
            purelib = runtime / 'lib/python3.12/site-packages'
            stdlib = runtime / 'lib/python3.12'
            scripts = runtime / 'bin'
            (purelib / 'pip').mkdir(parents=True)
            (stdlib / 'ensurepip').mkdir()
            scripts.mkdir()
            python = scripts / 'python3.12'
            python.write_bytes(b'interpreter')
            (purelib / 'pip/__init__.py').write_bytes(b'build tool')
            (purelib / 'pip/LICENSE').write_text('license retained')
            (stdlib / 'ensurepip/tool').write_bytes(b'installer')
            paths = {'purelib': str(purelib.resolve()), 'stdlib': str(stdlib.resolve()), 'scripts': str(scripts.resolve())}
            with patch('scripts.package_release.subprocess.check_output', return_value=json.dumps(paths)):
                report = finalize_portable_runtime(parent / 'alias-parent/../stage', python, {})
            self.assertGreater(report['removed_bytes'], 0)
            self.assertFalse((purelib / 'pip/__init__.py').exists())
            self.assertTrue((purelib / 'pip/LICENSE').is_file())
            recorded = json.loads((runtime / 'build-tool-prune.json').read_text())
            self.assertTrue(all('..' not in Path(item['path']).parts for item in recorded['removed']))

    def test_only_native_verified_subfinder_included(self):
        self.assertTrue(include_source(Path("backend/subfinder/linux/x64/VideoSubFinderCli"), "linux", "x64"))
        for path in ("backend/subfinder/windows/x64/VideoSubFinderCli.exe", "backend/subfinder/linux/VideoSubFinderCli", "backend/__pycache__/config.pyc"):
            self.assertFalse(include_source(Path(path), "linux", "x64"))

    def test_models_follow_every_language_and_mode(self):
        from scripts.package_release import inference_model_directories
        selected = inference_model_directories()
        self.assertIn('V6/PP-OCRv6_small_rec_infer', selected)
        self.assertIn('V6/PP-OCRv6_medium_rec_infer', selected)
        for model in ('korean', 'arabic', 'cyrillic', 'latin', 'eslav', 'devanagari', 'th', 'el'):
            self.assertTrue(include_source(Path(f'backend/models/V5/{model}_PP-OCRv5_mobile_rec_infer/inference.pdiparams'), 'linux', 'x64'))
        self.assertTrue(include_source(Path('backend/models/V3/ka_PP-OCRv3_mobile_rec_infer/inference.pdiparams'), 'linux', 'x64'))
        for model in ('server_det', 'server_rec', 'mobile_det', 'mobile_rec'):
            self.assertFalse(include_source(Path(f'backend/models/V5/PP-OCRv5_{model}_infer/inference.pdiparams'), 'linux', 'x64'))

    def test_design_keeps_rounded_icon_not_marketing_media(self):
        for name in ("vse.ico", "icon_1024.png"):
            self.assertTrue(include_source(Path("design") / name, "linux", "x64"))
        for name in ("demo.gif", "paper (2020).pdf", "demo.png"):
            self.assertFalse(include_source(Path("design") / name, "linux", "x64"))

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
            for directory in ("backend/subfinder/linux/x64", "backend/models/V6/PP-OCRv6_small_rec_infer", "scripts", "wheelhouse"):
                (root / directory).mkdir(parents=True)
            (root / "backend/config.py").write_text('VERSION = "2.3.0"\n', encoding="utf-8")
            (root / "backend/private-untracked.json").write_text("must not ship", encoding="utf-8")
            (root / "backend/subfinder/linux/x64/VideoSubFinderCli").write_text("runtime", encoding="utf-8")
            (root / "backend/models/V6/PP-OCRv6_small_rec_infer/model.json").write_text("model", encoding="utf-8")
            (root / "scripts/bundle_bootstrap.py").write_text("# bootstrap", encoding="utf-8")
            with zipfile.ZipFile(root / "wheelhouse/sample-1.0-py3-none-any.whl", "w") as archive:
                archive.writestr("sample-1.0.dist-info/METADATA", "Name: sample\nVersion: 1.0\n")
            def fake_runtime(stage, *args):
                (stage / 'runtime/bin').mkdir(parents=True)
                (stage / 'runtime/bin/python3.12').write_bytes(b'portable Python')
                return {'python': 'runtime/bin/python3.12', 'provider': 'test'}
            outputs = ["a" * 40 + "\n", "", b"backend/config.py\0scripts/bundle_bootstrap.py\0"]
            with patch("scripts.package_release.host_platform", return_value=("linux", "x64")), patch("scripts.package_release.subprocess.check_output", side_effect=outputs), patch("scripts.package_release.subprocess.run"), patch("scripts.package_release.runtime_wheels", return_value=([root / "wheelhouse/sample-1.0-py3-none-any.whl"], ["sample==1.0"])), patch("scripts.package_release.build_runtime", side_effect=fake_runtime), patch("scripts.package_release.portable_launchers"), patch("scripts.bundle_linux_runtime_libs.bundle_linux_libraries", return_value={}) as native_closure:
                archive_path = create_bundle(root / "wheelhouse", root / "dist", root)
                self.assertEqual(native_closure.call_args.kwargs, {"profile": "paddle"})
                self.assertEqual(native_closure.call_count, 1)
            verify_bundle(archive_path, smoke=False)
            with zipfile.ZipFile(archive_path) as archive:
                names = archive.namelist()
                self.assertFalse(any("private-untracked" in name for name in names))
                manifest = json.loads(archive.read(next(name for name in names if name.endswith("/bundle-manifest.json"))))
                self.assertEqual(manifest["commit"], "a" * 40)
                self.assertEqual(manifest['format'], 2)
                self.assertEqual(manifest['runtime_python'], 'runtime/bin/python3.12')
                self.assertFalse(any('/wheelhouse/' in name or '/.venv/' in name for name in names))
                self.assertIn("backend/models/V6/PP-OCRv6_small_rec_infer/model.json", manifest["files"])
                self.assertIn("backend/subfinder/linux/x64/VideoSubFinderCli", manifest["files"])

    def test_size_report_separates_runtime_models_and_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for directory, name, data in (('runtime', 'python', b'abc'), ('backend/models', 'weights', b'12345'), ('wheelhouse', 'runtime.whl', b'xx')):
                (root / directory).mkdir(parents=True)
                (root / directory / name).write_bytes(data)
            (root / 'original.zip').write_bytes(b'not counted')
            write_size_report(root, {'platform': 'linux', 'arch': 'x64', 'commit': 'a' * 40}, 'test')
            report = json.loads((root / 'test-results/runtime-size.json').read_text())
            measurement = report['measurements'][0]
            self.assertEqual(measurement['runtime_bytes'], 3)
            self.assertEqual(measurement['model_bytes'], 5)
            self.assertEqual(measurement['wheelhouse_bytes'], 2)
            self.assertEqual(measurement['total_installed_bytes'], 10)




    def test_packaged_preferences_use_writable_user_location(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {'XDG_CONFIG_HOME': temporary, 'XDG_CACHE_HOME': temporary}, clear=True), patch('scripts.bundle_bootstrap.sys.platform', 'linux'), patch('scripts.bundle_bootstrap.Path.home', return_value=Path(temporary) / 'fixture-home'):
                configure_user_paths()
                config = Path(os.environ['VSE_CONFIG_FILE'])
                self.assertEqual(config, Path(temporary) / 'video-subtitle-extractor/config.json')
                self.assertTrue(config.parent.is_dir())
                self.assertEqual(Path(os.environ["VSE_WORK_DIR"]), Path(temporary) / "video-subtitle-extractor/work")
            with patch.dict(os.environ, {'VSE_CONFIG_FILE': '/explicit/preferences.json', 'VSE_WORK_DIR': '/explicit/work'}, clear=True):
                configure_user_paths()
                self.assertEqual(os.environ['VSE_CONFIG_FILE'], '/explicit/preferences.json')

    @unittest.skipIf(os.name == 'nt', 'POSIX base-tool symlinks are only used on Unix')
    def test_verification_path_excludes_host_python(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env_path = verification_path(root / 'runtime/bin/python3.12', root / 'evidence', 'linux')
            parts = env_path.split(os.pathsep)
            self.assertEqual(parts, [str(root / 'runtime/bin'), str(root / 'evidence/base-os-tools')])
            self.assertEqual([p.name for p in (root / 'evidence/base-os-tools').iterdir()], ['dirname'])
            self.assertFalse((root / 'evidence/base-os-tools/python').exists())
            records = json.loads((root / 'evidence/base-os-tools.json').read_text())
            self.assertEqual(records[0]['command'], 'dirname')

    def test_self_contained_launcher_never_installs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "app"
            root.mkdir()
            (root / 'runtime').mkdir()
            python = root / 'runtime/python'
            python.write_bytes(b'portable interpreter')
            media_files = {}
            for name in ('backend/subfinder/linux/x64/VideoSubFinderCli', 'runtime/lib/python3.12/site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-fixture'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'fixture media executable')
                media_files[name] = sha256(path)
            manifest = {'format': 2, 'distribution': 'self-contained CPU inference runtime', 'platform': 'linux', 'arch': 'x64', 'runtime_python': 'runtime/python', 'files': media_files}
            (root / 'bundle-manifest.json').write_text(json.dumps(manifest))
            with patch('scripts.bundle_bootstrap.__file__', str(root / 'bootstrap.py')), patch('scripts.bundle_bootstrap.host_platform', return_value=('linux', 'x64')), patch('scripts.bundle_bootstrap.sys.executable', str(python)), patch('scripts.bundle_bootstrap.os.chdir'), patch.dict(os.environ, {'VSE_CONFIG_FILE': str(root.parent / 'user-config.json'), 'VSE_WORK_DIR': str(root.parent / 'user-work')}), patch('scripts.bundle_bootstrap.subprocess.run') as run, patch('scripts.bundle_bootstrap.subprocess.call', return_value=0) as launch:
                self.assertEqual(bootstrap_main([]), 0)
                run.assert_not_called()
                launch.assert_called_once()
                command = launch.call_args.args[0]
                self.assertTrue(Path(command[0]).samefile(python))
                self.assertEqual(command[1:], ['gui.py'])
                self.assertEqual(bootstrap_main(['--verify']), 0)
                commands = [call.args[0] for call in run.call_args_list]
                self.assertTrue(commands)
                self.assertTrue(all(Path(command[0]).samefile(python) for command in commands))
                self.assertFalse(any('pip' in command or 'venv' in command or 'install' in command for command in commands))

    def test_archive_rejects_escape_and_symlink(self):
        for name in ("../escape", "/absolute", "root/../../escape", "C:/escape", "root\\escape", "root/file\x00hidden"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive_path = root / "bad.zip"
                with zipfile.ZipFile(archive_path, "w") as archive:
                    # ZipInfo(name) normalizes backslashes on Windows. Set the
                    # actual on-disk member name after construction instead.
                    info = zipfile.ZipInfo("placeholder")
                    info.filename = info.orig_filename = name
                    archive.writestr(info, "bad")
                self.assertGreaterEqual(archive_path.read_bytes().count(name.encode("ascii")), 2)
                with zipfile.ZipFile(archive_path) as archive:
                    self.assertEqual(archive.infolist()[0].orig_filename, name)
                with self.assertRaisesRegex(RuntimeError, "Unsafe release archive member"):
                    extract_checked(archive_path, root / "out")
                self.assertFalse((root / "out").exists())
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

    def test_archive_checks_raw_name_before_platform_normalization(self):
        info = zipfile.ZipInfo("placeholder")
        info.orig_filename = "root\\escape"
        info.filename = "root/escape"  # Shape returned by zipfile on Windows.
        with patch("scripts.package_release.zipfile.ZipFile") as archive_type:
            archive = archive_type.return_value.__enter__.return_value
            archive.infolist.return_value = [info]
            with self.assertRaisesRegex(RuntimeError, "Unsafe release archive member"):
                extract_checked(Path("unused.zip"), Path("unused-output"))
            archive.extractall.assert_not_called()

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
