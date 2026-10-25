"""Offline fixtures for complete, honest release candidate validation."""
from __future__ import annotations

import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

from scripts.bundle_bootstrap import sha256
from scripts.release_guard import (
    ACCEPTANCE_CHECKS, assert_complete_release_matrix, candidate_asset_names,
    main, validate_assets,
)
from scripts.release_profiles import PROFILES


SHA = 'a' * 40
VERSION = '2.3.0'


class ReleaseAssetGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.manifests = {}
        for name, (platform, arch, profile) in candidate_asset_names(VERSION).items():
            manifest = {'format': 2, 'python': '3.12', 'commit': SHA, 'version': VERSION,
                        'platform': platform, 'arch': arch, 'build_profile': profile,
                        'ocr_backend': PROFILES[profile]['backend'],
                        'accelerator': PROFILES[profile]['accelerator'], 'files': {}}
            self.manifests[name] = manifest
            self.write_archive(name)
        self.name = f'vse-{VERSION}-windows-x64-cuda11.8.zip'
        self.archive = self.directory / self.name

    def manifest_path(self, name):
        platform = self.manifests[name]['platform']
        payload = Path(name).stem + ('/Video Subtitle Extractor.app/Contents/Resources' if platform == 'macos' else '')
        return payload + '/bundle-manifest.json'

    def write_archive(self, name, *, extra=(), manifest_text=None, manifest_path=None):
        archive = self.directory / name
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(archive, 'w') as bundle:
                bundle.writestr(manifest_path or self.manifest_path(name),
                                json.dumps(self.manifests[name]) if manifest_text is None else manifest_text)
                for path, data in extra:
                    bundle.writestr(path, data)
        self.write_sidecars(name)

    def write_sidecars(self, name):
        archive = self.directory / name
        digest = sha256(archive)
        archive.with_suffix('.zip.sha256').write_text(f'{digest}  {name}\n')
        manifest = self.manifests[name]
        receipt = {'schema': 1, 'archive': name, 'archive_sha256': digest,
                   **{key: manifest[key] for key in ('commit', 'version', 'platform', 'arch', 'build_profile')},
                   'checks': {key: 'passed' for key in ACCEPTANCE_CHECKS},
                   'gpu_inference_verified': False, 'gpu_validation': 'not-run-server-ci'}
        archive.with_suffix('.zip.acceptance.json').write_text(json.dumps(receipt))

    def mutate_receipt(self, change):
        path = self.archive.with_suffix('.zip.acceptance.json')
        receipt = json.loads(path.read_text())
        change(receipt)
        path.write_text(json.dumps(receipt))

    def validate(self, **kwargs):
        return validate_assets(self.directory, SHA, VERSION, **kwargs)

    def test_exact_seven_candidates_validate_without_network(self):
        with patch('scripts.release_guard.urlopen', side_effect=AssertionError('network forbidden')):
            assets = self.validate()
        self.assertEqual(len(assets), 21)
        self.assertIn(str(self.directory / f'vse-{VERSION}-windows-x64-cpu.zip'), assets)
        self.assertNotIn('python312', ''.join(assets))
        self.assertEqual(sum(name.endswith('.acceptance.json') for name in assets), 7)

    def test_missing_profile_fails(self):
        self.archive.unlink()
        with self.assertRaisesRegex(RuntimeError, 'matrix mismatch'):
            self.validate()

    def test_extra_or_legacy_archive_fails(self):
        for name in ('extra.zip', 'extra.ZIP', 'vse-v2.2.0-windows-directml.7z', 'vse-cuda.7z.001'):
            with self.subTest(name=name):
                path = self.directory / name
                path.write_bytes(b'old archive')
                with self.assertRaisesRegex(RuntimeError, 'matrix mismatch'):
                    self.validate()
                path.unlink()

    def test_wrong_version_filename_fails(self):
        self.archive.rename(self.directory / self.name.replace('2.3.0', '2.2.0'))
        with self.assertRaisesRegex(RuntimeError, 'matrix mismatch'):
            self.validate()

    def test_archive_mutation_after_acceptance_fails(self):
        with self.archive.open('ab') as stream:
            stream.write(b'changed')
        with self.assertRaisesRegex(RuntimeError, 'checksum/name'):
            self.validate()

    def test_checksum_filename_must_match(self):
        self.archive.with_suffix('.zip.sha256').write_text(f'{sha256(self.archive)}  other.zip\n')
        with self.assertRaisesRegex(RuntimeError, 'checksum/name'):
            self.validate()

    def test_checksum_may_not_have_extra_lines(self):
        checksum = self.archive.with_suffix('.zip.sha256')
        checksum.write_text(checksum.read_text() + 'other digest\n')
        with self.assertRaisesRegex(RuntimeError, 'checksum/name'):
            self.validate()

    def test_missing_checksum_fails(self):
        self.archive.with_suffix('.zip.sha256').unlink()
        with self.assertRaisesRegex(RuntimeError, 'Missing or unsafe'):
            self.validate()

    def test_asset_symlinks_fail(self):
        for suffix in ('.zip', '.zip.sha256', '.zip.acceptance.json'):
            with self.subTest(suffix=suffix):
                path = self.archive.with_suffix(suffix)
                original = path.read_bytes()
                outside = self.directory / 'symlink-target'
                outside.write_bytes(original)
                path.unlink()
                path.symlink_to(outside)
                with self.assertRaisesRegex(RuntimeError, 'Missing or unsafe'):
                    self.validate()
                path.unlink()
                path.write_bytes(original)
                outside.unlink()

    def test_duplicate_zip_entries_fail(self):
        self.write_archive(self.name, extra=[(self.manifest_path(self.name), '{}')])
        with self.assertRaisesRegex(RuntimeError, 'Duplicate ZIP'):
            self.validate()

    def test_windows_case_alias_entries_fail(self):
        self.write_archive(self.name, extra=[(self.manifest_path(self.name).upper(), '{}')])
        with self.assertRaisesRegex(RuntimeError, 'Duplicate ZIP'):
            self.validate()

    def test_multiple_manifests_fail(self):
        self.write_archive(self.name, extra=[(self.archive.stem + '/nested/bundle-manifest.json', '{}')])
        with self.assertRaisesRegex(RuntimeError, 'unique expected bundle manifest'):
            self.validate()

    def test_zip_symlink_fails_even_with_matching_outer_receipt(self):
        link = zipfile.ZipInfo(self.archive.stem + '/runtime/link')
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(self.archive, 'a') as bundle:
            bundle.writestr(link, '../outside')
        self.write_sidecars(self.name)
        with self.assertRaisesRegex(RuntimeError, 'Unsafe or unexpected'):
            self.validate()

    def test_manifest_must_be_at_expected_root(self):
        self.write_archive(self.name, manifest_path=self.archive.stem + '/nested/bundle-manifest.json')
        with self.assertRaisesRegex(RuntimeError, 'unique expected bundle manifest'):
            self.validate()

    def test_zip_traversal_and_unrelated_roots_fail(self):
        for entry in ('../escape', '/absolute', 'other/file', self.archive.stem + '/a/../b', self.archive.stem + '/a\\b'):
            with self.subTest(entry=entry):
                self.write_archive(self.name, extra=[(entry, 'not a runtime')])
                with self.assertRaisesRegex(RuntimeError, 'Unsafe or unexpected'):
                    self.validate()

    def test_manifest_identity_must_match_profile_name(self):
        original = dict(self.manifests[self.name])
        for field, value in (('commit', 'b' * 40), ('version', '2.2.0'), ('platform', 'linux'),
                             ('arch', 'arm64'), ('build_profile', 'paddle-cpu'),
                             ('ocr_backend', 'native'), ('accelerator', 'cpu')):
            with self.subTest(field=field):
                self.manifests[self.name] = {**original, field: value}
                self.write_archive(self.name, manifest_path=self.archive.stem + '/bundle-manifest.json')
                with self.assertRaisesRegex(RuntimeError, 'Bundle identity'):
                    self.validate()
        self.manifests[self.name] = original

    def test_old_manifest_without_new_profile_identity_fails(self):
        del self.manifests[self.name]['build_profile']
        # Sidecar cannot be emitted for an old, incomplete identity either.
        with zipfile.ZipFile(self.archive, 'w') as bundle:
            bundle.writestr(self.manifest_path(self.name), json.dumps(self.manifests[self.name]))
        self.archive.with_suffix('.zip.sha256').write_text(f'{sha256(self.archive)}  {self.name}\n')
        with self.assertRaisesRegex(RuntimeError, 'Bundle identity'):
            self.validate()

    def test_old_python_or_nonportable_format_fails(self):
        for field, value in (('python', '3.11'), ('format', 1)):
            with self.subTest(field=field):
                previous = self.manifests[self.name][field]
                self.manifests[self.name][field] = value
                self.write_archive(self.name)
                with self.assertRaisesRegex(RuntimeError, 'bundle format/runtime'):
                    self.validate()
                self.manifests[self.name][field] = previous

    def test_duplicate_json_keys_fail(self):
        self.write_archive(self.name, manifest_text='{"format":2,"format":2}')
        with self.assertRaisesRegex(RuntimeError, 'Duplicate JSON'):
            self.validate()

    def test_malformed_manifest_fails(self):
        for value in ('not json', '[]', 'null'):
            with self.subTest(value=value):
                self.write_archive(self.name, manifest_text=value)
                with self.assertRaises(RuntimeError):
                    self.validate()

    def test_missing_receipt_fails(self):
        self.archive.with_suffix('.zip.acceptance.json').unlink()
        with self.assertRaisesRegex(RuntimeError, 'Missing or unsafe'):
            self.validate()

    def test_receipt_is_bound_to_exact_archive_and_identity(self):
        for field, value in (('archive', 'other.zip'), ('archive_sha256', '0' * 64),
                             ('commit', 'b' * 40), ('version', '2.2.0'), ('platform', 'linux'),
                             ('arch', 'arm64'), ('build_profile', 'paddle-cpu'), ('schema', True)):
            with self.subTest(field=field):
                self.write_sidecars(self.name)
                self.mutate_receipt(lambda data: data.update({field: value}))
                with self.assertRaisesRegex(RuntimeError, 'receipt does not match'):
                    self.validate()

    def test_every_final_acceptance_stage_is_required(self):
        for check in ACCEPTANCE_CHECKS:
            with self.subTest(check=check):
                self.write_sidecars(self.name)
                self.mutate_receipt(lambda data: data['checks'].pop(check))
                with self.assertRaisesRegex(RuntimeError, 'acceptance is missing or failed'):
                    self.validate()

    def test_failed_skipped_or_truthy_stage_is_not_a_pass(self):
        for value in ('failed', 'skipped', True, 'true', None):
            with self.subTest(value=value):
                self.write_sidecars(self.name)
                self.mutate_receipt(lambda data: data['checks'].update(cpu_mode_auto=value))
                with self.assertRaisesRegex(RuntimeError, 'acceptance is missing or failed'):
                    self.validate()

    def test_server_receipt_may_not_claim_gpu_acceptance(self):
        for value in (True, 'false', 0, None):
            with self.subTest(value=value):
                self.write_sidecars(self.name)
                self.mutate_receipt(lambda data: data.update(gpu_inference_verified=value))
                with self.assertRaisesRegex(RuntimeError, 'Unsupported GPU certification'):
                    self.validate()

    def test_gpu_limitation_must_be_explicit(self):
        self.mutate_receipt(lambda data: data.pop('gpu_validation'))
        with self.assertRaisesRegex(RuntimeError, 'Missing explicit GPU'):
            self.validate()

    def test_full_approved_current_matrix_has_exactly_seven_profiles(self):
        self.assertEqual(len(self.validate(matrix='full')), 21)
        self.assertFalse(any('cuda10.2' in name for name in candidate_asset_names(VERSION)))

    def test_new_unresolved_current_profile_blocker_fails_closed(self):
        with patch('scripts.release_guard.BLOCKED_PROFILES', {'unresolved-profile': 'needs approval'}):
            with self.assertRaisesRegex(RuntimeError, 'unresolved-profile'):
                self.validate(matrix='full')

    def test_candidates_cannot_be_published(self):
        with self.assertRaisesRegex(RuntimeError, 'candidates are build-only'):
            assert_complete_release_matrix('restored-candidates')

    def test_unknown_matrix_and_bad_sha_fail(self):
        with self.assertRaisesRegex(RuntimeError, 'Unknown release matrix'):
            self.validate(matrix='unknown')
        with self.assertRaisesRegex(RuntimeError, 'exact 40-character'):
            validate_assets(self.directory, 'main', VERSION)

    def test_cli_validation_is_offline_and_never_publishes(self):
        argv = ['release_guard.py', '--validate-assets', '--sha', SHA, '--assets', str(self.directory)]
        with patch('sys.argv', argv), patch('scripts.release_guard.read_version', return_value=VERSION), \
                patch('scripts.release_guard.subprocess.check_output', return_value=SHA + '\n'), \
                patch('scripts.release_guard.preflight') as remote, patch('scripts.release_guard.subprocess.run') as publish:
            self.assertEqual(main(), 0)
        remote.assert_not_called()
        publish.assert_not_called()

    def test_cli_rejects_other_checkout_before_asset_validation(self):
        argv = ['release_guard.py', '--validate-assets', '--sha', SHA, '--assets', str(self.directory)]
        with patch('sys.argv', argv), patch('scripts.release_guard.subprocess.check_output', return_value='b' * 40), \
                self.assertRaisesRegex(RuntimeError, 'checked-out'):
            main()

    def test_cli_candidate_publication_is_blocked_before_network_or_mutation(self):
        argv = ['release_guard.py', '--tag', VERSION, '--sha', SHA, '--publish', 'release']
        with patch('sys.argv', argv), patch('scripts.release_guard.preflight') as remote, \
                patch('scripts.release_guard.subprocess.run') as publish, self.assertRaises(RuntimeError):
            main()
        remote.assert_not_called()
        publish.assert_not_called()

    def test_full_matrix_still_requires_immutable_release_preflight(self):
        argv = ['release_guard.py', '--tag', VERSION, '--sha', SHA, '--publish', 'release',
                '--matrix', 'full', '--assets', str(self.directory)]
        with patch('sys.argv', argv), patch('scripts.release_guard.read_version', return_value=VERSION), \
                patch('scripts.release_guard.preflight', side_effect=RuntimeError('release already exists')) as remote, \
                patch('scripts.release_guard.subprocess.run') as publish, \
                self.assertRaisesRegex(RuntimeError, 'already exists'):
            main()
        remote.assert_called_once_with(VERSION, SHA)
        publish.assert_not_called()


if __name__ == '__main__':
    unittest.main()
