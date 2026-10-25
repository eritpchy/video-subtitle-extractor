"""Acceptance receipts must be derived from the exact final ZIP's evidence."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts.accept_release_archive import Evidence, SCENARIOS, accept_archives, main
from scripts.bundle_bootstrap import sha256
from scripts.release_guard import ACCEPTANCE_CHECKS, candidate_asset_names, validate_assets
from scripts.release_profiles import PROFILES


SHA = 'a' * 40
VERSION = '2.3.0'


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


class FinalReleaseAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archives = self.root / 'dist'
        self.portable = self.root / 'portable'
        self.archives.mkdir()
        self.portable.mkdir()
        self.name = f'vse-{VERSION}-windows-x64-cuda11.8.zip'
        self.archive = self.create_candidate(self.name)
        self.receipt = self.archive.with_suffix('.zip.acceptance.json')
        self.smoke = self.archives / 'test-results' / self.archive.stem
        self.environment = self.portable / self.archive.stem
        self.receipt.write_text('{"old":"must not survive any failure"}')

    def create_candidate(self, name):
        platform, arch, profile = candidate_asset_names(VERSION)[name]
        manifest = {'format': 2, 'python': '3.12', 'commit': SHA, 'version': VERSION,
                    'platform': platform, 'arch': arch, 'build_profile': profile,
                    'ocr_backend': PROFILES[profile]['backend'],
                    'accelerator': PROFILES[profile]['accelerator'], 'files': {}}
        archive = self.archives / name
        payload = archive.stem + ('/Video Subtitle Extractor.app/Contents/Resources' if platform == 'macos' else '')
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr(payload + '/bundle-manifest.json', json.dumps(manifest))
        digest = sha256(archive)
        archive.with_suffix('.zip.sha256').write_text(f'{digest}  {name}\n')
        smoke = self.archives / 'test-results' / archive.stem
        write_json(smoke / 'package-verification.json', {
            'schema': 1, 'result': 'passed', 'archive': name, 'archive_sha256': digest,
            **{key: manifest[key] for key in ('commit', 'version', 'platform', 'arch', 'build_profile')},
            'payload': 'passed', 'launcher_smoke': 'passed',
        })
        self.workloads(smoke)
        environment = self.portable / archive.stem
        scenarios = []
        for scenario in SCENARIOS:
            case = {'scenario': scenario, 'result': 'passed', 'audit': {'passed': True, 'errors': []}}
            scenarios.append(case)
            write_json(environment / scenario / 'scenario-report.json', case)
            self.workloads(environment / scenario)
        write_json(environment / 'portable-environment-report.json', {
            'result': 'passed', 'archive': str(archive), 'archive_sha256': digest,
            'commit': SHA, 'platform': platform, 'arch': arch,
            'profile': manifest['ocr_backend'], 'scenarios': scenarios,
        })
        return archive

    def workloads(self, root):
        for mode in ('fast', 'accurate', 'auto'):
            directory = 'bundle-e2e' if mode == 'fast' else f'bundle-e2e-{mode}'
            write_json(root / directory / 'report.json', {
                'result': 'passed', 'mode': mode, 'cpu_forced': True,
                'command': ['runtime/python', '-m', 'backend.cli', 'video.mp4', '--mode', mode, '--cpu'],
            })
        write_json(root / 'bundle-issues/report.json', [
            {'case': name, 'result': 'passed'} for name in ('multiline', 'av1', 'timeline')])
        write_json(root / 'dependency-isolation/summary.json', {'passed': True, 'errors': []})

    def mutate(self, path, change):
        data = json.loads(path.read_text())
        change(data)
        write_json(path, data)

    def accept(self):
        return accept_archives(self.archives, self.portable)

    def assert_rejected(self, pattern=None):
        if pattern:
            with self.assertRaisesRegex(RuntimeError, pattern):
                self.accept()
        else:
            with self.assertRaises(RuntimeError):
                self.accept()
        self.assertFalse(self.receipt.exists(), 'stale success receipt survived a failed acceptance')

    def test_receipt_derives_all_checks_and_original_evidence_hashes(self):
        self.assertEqual(self.accept(), [self.receipt])
        receipt = json.loads(self.receipt.read_text())
        self.assertEqual(receipt['archive'], self.name)
        self.assertEqual(receipt['archive_sha256'], sha256(self.archive))
        self.assertEqual(receipt['checks'], {check: 'passed' for check in ACCEPTANCE_CHECKS})
        self.assertIs(receipt['gpu_inference_verified'], False)
        self.assertEqual(receipt['gpu_validation'], 'not-run-server-ci')
        self.assertEqual(len(receipt['evidence']), 19)
        key = f'package/test-results/{self.archive.stem}/package-verification.json'
        path = self.smoke / 'package-verification.json'
        self.assertEqual(receipt['evidence'][key], {'sha256': sha256(path), 'bytes': path.stat().st_size})
        self.assertNotIn('old', receipt)

    def test_all_seven_produced_receipts_pass_release_guard(self):
        for name in candidate_asset_names(VERSION):
            if name != self.name:
                self.create_candidate(name)
        self.assertEqual(len(self.accept()), 7)
        self.assertEqual(len(validate_assets(self.archives, SHA, VERSION)), 21)

    def test_checksum_name_must_match_exact_archive(self):
        self.archive.with_suffix('.zip.sha256').write_text(f'{sha256(self.archive)}  wrong.zip\n')
        self.assert_rejected('checksum/name')

    def test_missing_package_verification_removes_old_receipt(self):
        (self.smoke / 'package-verification.json').unlink()
        self.assert_rejected('Missing or unsafe')

    def test_old_or_failed_package_verification_is_not_accepted(self):
        path = self.smoke / 'package-verification.json'
        original = path.read_bytes()
        for key, value in (('archive_sha256', 'b' * 64), ('archive', 'old.zip'),
                           ('commit', 'b' * 40), ('version', '2.2.0'), ('build_profile', 'paddle-cpu'),
                           ('schema', True), ('result', 'running'), ('payload', 'failed'),
                           ('launcher_smoke', 'skipped')):
            with self.subTest(key=key):
                path.write_bytes(original)
                self.mutate(path, lambda row: row.update({key: value}))
                self.assert_rejected('exact archive')

    def test_old_or_wrong_portable_verification_is_not_accepted(self):
        path = self.environment / 'portable-environment-report.json'
        original = path.read_bytes()
        for key, value in (('archive_sha256', 'b' * 64), ('commit', 'b' * 40), ('platform', 'linux'),
                           ('arch', 'arm64'), ('profile', 'native'), ('result', 'failed')):
            with self.subTest(key=key):
                path.write_bytes(original)
                self.mutate(path, lambda row: row.update({key: value}))
                self.assert_rejected('exact archive')

    def test_all_three_directories_need_all_three_modes(self):
        roots = [self.smoke, *(self.environment / scenario for scenario in SCENARIOS)]
        for root in roots:
            for name in ('bundle-e2e', 'bundle-e2e-accurate', 'bundle-e2e-auto'):
                path = root / name / 'report.json'
                with self.subTest(path=str(path)):
                    original = path.read_bytes()
                    path.unlink()
                    self.assert_rejected('Missing or unsafe')
                    path.write_bytes(original)

    def test_mode_reports_require_true_explicit_cpu_and_correct_mode(self):
        path = self.smoke / 'bundle-e2e-accurate/report.json'
        original = path.read_bytes()
        changes = [lambda row: row.update(result='failed'), lambda row: row.update(cpu_forced=False),
                   lambda row: row.update(cpu_forced=1), lambda row: row.update(mode='fast'),
                   lambda row: row.update(command='--cpu --mode accurate'),
                   lambda row: row['command'].remove('--cpu'),
                   lambda row: row['command'].extend(['--mode', 'fast']),
                   lambda row: row['command'].extend(['--cpu']),
                   lambda row: row['command'].extend(['--mode=auto']),
                   lambda row: row['command'].__setitem__(row['command'].index('accurate'), 'auto')]
        for index, change in enumerate(changes):
            with self.subTest(change=index):
                path.write_bytes(original)
                self.mutate(path, change)
                self.assert_rejected('CPU accurate|CLI mode')

    def test_modes_in_contaminated_environment_must_really_pass(self):
        path = self.environment / 'contaminated-host-environment/bundle-e2e-auto/report.json'
        self.mutate(path, lambda row: row.update(result='running'))
        self.assert_rejected('CPU auto')

    def test_issue_matrix_requires_three_unique_successful_cases(self):
        path = self.smoke / 'bundle-issues/report.json'
        original = path.read_bytes()
        changes = [lambda rows: rows.pop(), lambda rows: rows[0].update(result='failed'),
                   lambda rows: rows[1].update(case='multiline'), lambda rows: rows[2].update(case='unknown'),
                   lambda rows: rows[0].update(case=['bad type'])]
        for index, change in enumerate(changes):
            with self.subTest(change=index):
                path.write_bytes(original)
                self.mutate(path, change)
                self.assert_rejected('Multiline/AV1/timeline')

    def test_all_three_isolation_summaries_are_required_and_passed(self):
        roots = [self.smoke, *(self.environment / scenario for scenario in SCENARIOS)]
        for root in roots:
            path = root / 'dependency-isolation/summary.json'
            with self.subTest(root=str(root)):
                self.mutate(path, lambda row: row.update(passed=True, errors=['foreign DLL']))
                self.assert_rejected('dependency isolation')
                write_json(path, {'passed': True, 'errors': []})

    def test_truthy_isolation_status_is_not_a_boolean_pass(self):
        self.mutate(self.smoke / 'dependency-isolation/summary.json', lambda row: row.update(passed=1))
        self.assert_rejected('dependency isolation')

    def test_both_unique_scenarios_are_required(self):
        path = self.environment / 'portable-environment-report.json'
        original = path.read_bytes()
        changes = [lambda row: row['scenarios'].pop(),
                   lambda row: row['scenarios'][1].update(scenario='clean-host-path'),
                   lambda row: row['scenarios'][0].update(scenario='unknown'),
                   lambda row: row.update(scenarios='passed')]
        for index, change in enumerate(changes):
            with self.subTest(change=index):
                path.write_bytes(original)
                self.mutate(path, change)
                self.assert_rejected('Both unique')

    def test_aggregate_scenario_fail_or_missing_audit_is_not_accepted(self):
        path = self.environment / 'portable-environment-report.json'
        original = path.read_bytes()
        for change in (lambda row: row['scenarios'][0].update(result='failed'),
                       lambda row: row['scenarios'][0].pop('audit'),
                       lambda row: row['scenarios'][0]['audit'].update(errors=['foreign runtime'])):
            path.write_bytes(original)
            self.mutate(path, change)
            self.assert_rejected('exact archive|dependency isolation')

    def test_individual_scenario_report_is_not_replaced_by_aggregate_pass(self):
        path = self.environment / 'clean-host-path/scenario-report.json'
        self.mutate(path, lambda row: row.update(result='failed'))
        self.assert_rejected('exact archive')

    def test_individual_scenario_name_must_match_directory(self):
        path = self.environment / 'clean-host-path/scenario-report.json'
        self.mutate(path, lambda row: row.update(scenario='contaminated-host-environment'))
        self.assert_rejected('exact archive')

    def test_untrusted_evidence_path_in_aggregate_is_not_followed(self):
        path = self.environment / 'portable-environment-report.json'
        self.mutate(path, lambda row: row['scenarios'][0].update(evidence='/arbitrary/not/read'))
        self.assertEqual(self.accept(), [self.receipt])

    def test_evidence_symlink_is_rejected(self):
        path = self.smoke / 'bundle-e2e/report.json'
        outside = self.root / 'outside.json'
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
        self.assert_rejected('Symlink')

    def test_evidence_directory_symlink_is_rejected(self):
        path = self.smoke / 'bundle-e2e'
        outside = self.root / 'outside'
        path.rename(outside)
        path.symlink_to(outside, target_is_directory=True)
        self.assert_rejected('Symlink')

    def test_duplicate_keys_cannot_override_failed_evidence(self):
        path = self.smoke / 'bundle-e2e/report.json'
        path.write_text('{"result":"failed","result":"passed"}')
        self.assert_rejected('Duplicate JSON')

    def test_malformed_json_fails_closed(self):
        (self.smoke / 'package-verification.json').write_text('invalid')
        self.assert_rejected('Invalid evidence JSON')

    def test_missing_portable_root_still_removes_old_success(self):
        with self.assertRaisesRegex(RuntimeError, 'directory'):
            accept_archives(self.archives, self.root / 'missing')
        self.assertFalse(self.receipt.exists())

    def test_old_receipt_symlink_is_unlinked_not_followed(self):
        outside = self.root / 'untouched.json'
        outside.write_text('unrelated file')
        self.receipt.unlink()
        self.receipt.symlink_to(outside)
        self.accept()
        self.assertFalse(self.receipt.is_symlink())
        self.assertEqual(outside.read_text(), 'unrelated file')

    def test_receipt_directory_is_not_recursively_deleted(self):
        self.receipt.unlink()
        self.receipt.mkdir()
        (self.receipt / 'unrelated').write_text('keep')
        with self.assertRaisesRegex(RuntimeError, 'output is a directory'):
            self.accept()
        self.assertEqual((self.receipt / 'unrelated').read_text(), 'keep')

    def test_one_bad_archive_prevents_receipts_for_every_archive(self):
        other = self.create_candidate(f'vse-{VERSION}-windows-x64-directml.zip')
        other_receipt = other.with_suffix('.zip.acceptance.json')
        other_receipt.write_text('{"old":true}')
        (self.portable / other.stem / 'portable-environment-report.json').unlink()
        self.assert_rejected('Missing or unsafe')
        self.assertFalse(other_receipt.exists())

    def test_failure_during_final_write_removes_partial_receipts(self):
        other = self.create_candidate(f'vse-{VERSION}-windows-x64-directml.zip')
        import scripts.accept_release_archive as acceptance
        replace = acceptance.os.replace
        count = 0

        def fail_second(source, destination):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('simulated storage failure')
            return replace(source, destination)

        with patch('scripts.accept_release_archive.os.replace', side_effect=fail_second), \
                self.assertRaisesRegex(OSError, 'storage failure'):
            self.accept()
        self.assertFalse(self.receipt.exists())
        self.assertFalse(other.with_suffix('.zip.acceptance.json').exists())
        self.assertEqual(list(self.archives.glob('*.tmp')), [])

    def test_archive_mutation_during_acceptance_fails(self):
        from scripts import accept_release_archive as acceptance
        prepare = acceptance.prepare_acceptance

        def mutate_archive(*args):
            result = prepare(*args)
            with self.archive.open('ab') as stream:
                stream.write(b'changed')
            return result

        with patch('scripts.accept_release_archive.prepare_acceptance', side_effect=mutate_archive):
            self.assert_rejected('changed during acceptance')

    def test_evidence_mutation_during_acceptance_fails(self):
        evidence = Evidence()
        evidence.read(self.smoke, 'bundle-e2e/report.json', 'fixture')
        self.mutate(self.smoke / 'bundle-e2e/report.json', lambda row: row.update(result='failed'))
        with self.assertRaisesRegex(RuntimeError, 'changed during acceptance'):
            evidence.assert_unchanged()

    def test_no_archives_is_a_failure_not_skip(self):
        empty = self.root / 'empty'
        empty.mkdir()
        with self.assertRaisesRegex(RuntimeError, 'No final ZIPs'):
            accept_archives(empty, self.portable)

    def test_cli_runs_only_acceptance_no_subprocess_or_network(self):
        with patch('scripts.release_guard.subprocess.run', side_effect=AssertionError('must not run')), \
                patch('scripts.release_guard.urlopen', side_effect=AssertionError('must not contact network')):
            self.assertEqual(main(['--archives', str(self.archives), '--portable-evidence', str(self.portable)]), 0)


if __name__ == '__main__':
    unittest.main()
