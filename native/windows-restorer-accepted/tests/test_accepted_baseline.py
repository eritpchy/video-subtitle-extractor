# SPDX-License-Identifier: Apache-2.0
"""Offline contracts only; synthetic ZIPs are never real CUDA restoration proof."""
import ast
import copy
import fnmatch
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
import urllib.error
import zipfile

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import accepted_baseline as baseline
spec = importlib.util.spec_from_file_location('accepted_ci_contract', ROOT / 'scripts/run_windows_restorer_accepted_ci.py')
ci = importlib.util.module_from_spec(spec); spec.loader.exec_module(ci)


class AcceptedBaselineTests(unittest.TestCase):
    def test_exact_306_pins_do_not_treat_package_verification_as_final_acceptance(self):
        pins = baseline.load_pins(HERE / 'accepted-baseline-306.json')
        self.assertEqual(pins['commit'], '3064320ec74e1aa71f3702c2ea966485f4b31e33')
        self.assertEqual([r['artifact_id'] for r in pins['profiles']], [12637186682, 12636364950])
        self.assertIn('not that receipt', pins['acceptance_binding'])
        self.assertEqual(pins['profiles'][0]['archive_sha256'], 'e80d5c19f7e67789f67394469e45e253ccb12ed6b5304c63aa5ef8bb0fa5d277')
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'pins.json'
            for field, value in (('commit', '59749ccc02e369f374c69846b2bde6279084577d'), ('run_id', 1), ('repository', 'other/repository')):
                changed = copy.deepcopy(pins); changed[field] = value; path.write_text(json.dumps(changed))
                with self.assertRaises(ValueError): baseline.load_pins(path)
            changed = copy.deepcopy(pins); changed['profiles'][0]['archive_bytes'] = True; path.write_text(json.dumps(changed))
            with self.assertRaises(ValueError): baseline.load_pins(path)
            changed = copy.deepcopy(pins); changed['profiles'][0]['archive_name'] = '../elsewhere.zip'; path.write_text(json.dumps(changed))
            with self.assertRaises(ValueError): baseline.load_pins(path)

    def test_budget_counts_three_zips_expanded_tree_and_exact_native_metadata(self):
        row = baseline.load_pins(HERE / 'accepted-baseline-306.json')['profiles'][0]
        budget = baseline.disk_budget(row, 18000)  # Synthetic count, not an asserted CUDA entry count.
        native = row['archive_bytes'] + row['expanded_bytes'] + 18000 * 65536 + 64 * 1024 ** 2
        self.assertEqual(budget['native_preflight_bytes'], native)
        self.assertEqual(budget['peak_bytes_including_original'], 2 * row['archive_bytes'] + native + 4 * 1024 ** 3)
        self.assertEqual(budget['after_original_download_extra_bytes'], row['archive_bytes'] + native + baseline.RESERVE)
        for count in (False, 0, 200001, -1):
            with self.assertRaises(ValueError): baseline.disk_budget(row, count)
        with patch.object(baseline.shutil, 'disk_usage', return_value=types.SimpleNamespace(free=9)):
            with self.assertRaisesRegex(RuntimeError, 'no host cleanup'): baseline.require_free('.', 10, 'test')
            self.assertEqual(baseline.require_free('.', 9, 'test')['available_bytes'], 9)

    def artifact(self, root, names=None):
        archive = b'fixed synthetic inner zip bytes, not executable'
        name = 'vse-test.zip'
        files = {name: archive, name + '.sha256': b'synthetic checksum', name + '.acceptance.json': b'{"synthetic":true}'}
        if names is not None: files = names
        path = root / 'container.zip'
        with zipfile.ZipFile(path, 'w') as zipped:
            for key, value in files.items(): zipped.writestr(key, value)
        row = {'archive_name': name, 'archive_bytes': len(archive), 'archive_sha256': hashlib.sha256(archive).hexdigest(),
               'artifact_bytes': path.stat().st_size, 'artifact_sha256': baseline.sha(path),
               'checksum_sha256': hashlib.sha256(b'synthetic checksum').hexdigest()}
        return path, row

    def test_outer_extraction_preserves_only_original_bytes_and_requires_final_sidecar(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); container, row = self.artifact(root)
            result = baseline.extract_original(container, root / 'original', row)
            self.assertEqual(len(result), 3)
            self.assertEqual(result[row['archive_name']]['sha256'], row['archive_sha256'])
            for index, mutate in enumerate((lambda r: r.update(artifact_sha256='0'*64), lambda r: r.update(archive_sha256='0'*64),
                                            lambda r: r.update(checksum_sha256='0'*64))):
                changed = dict(row); mutate(changed)
                with self.assertRaises(ValueError): baseline.extract_original(container, root / f'bad-{index}', changed)
        for names in ({'vse-test.zip': b'x', 'vse-test.zip.sha256': b'x', 'package-verification.json': b'{}'},
                      {'../outside': b'x'}, {'vse-test.zip/': b'', 'vse-test.zip.sha256': b'x', 'vse-test.zip.acceptance.json': b'{}'}):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); container, row = self.artifact(root, names)
                with self.assertRaises(ValueError): baseline.extract_original(container, root / 'rejected', row)

    def test_independent_inventory_rejects_mutation_extra_file_and_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary); (parent / 'root/sub').mkdir(parents=True)
            file = parent / 'root/sub/file'; file.write_bytes(b'payload')
            document = {'validated_zip_entries': [{'name': 'root/sub/file', 'kind': 'file', 'size': 7, 'sha256': baseline.sha(file)}]}
            expected = baseline.inventory(document)
            self.assertEqual(baseline.verify_restored_tree(parent, expected)['entries'], 3)
            file.write_bytes(b'changed')
            with self.assertRaises(ValueError): baseline.verify_restored_tree(parent, expected)
            file.write_bytes(b'payload'); extra = parent / 'extra'; extra.write_bytes(b'x')
            with self.assertRaises(ValueError): baseline.verify_restored_tree(parent, expected)
            extra.unlink()
            if sys.platform != 'win32':
                extra.symlink_to(file)
                with self.assertRaisesRegex(ValueError, 'Reparse/symlink'): baseline.verify_restored_tree(parent, expected)

    def test_download_binds_run_digest_and_does_not_forward_token_to_redirect(self):
        pins = baseline.load_pins(HERE / 'accepted-baseline-306.json'); row = dict(pins['profiles'][0])
        body = b'synthetic pinned artifact'; row.update(artifact_bytes=len(body), artifact_sha256=hashlib.sha256(body).hexdigest())
        metadata = {'id': row['artifact_id'], 'name': row['artifact_name'], 'size_in_bytes': len(body), 'expired': False,
                    'digest': 'sha256:' + row['artifact_sha256'], 'workflow_run': {'id': pins['run_id'], 'head_sha': pins['commit']}}
        calls = []
        def open_api(request, timeout):
            calls.append(request)
            if request.full_url.endswith('/zip'):
                raise urllib.error.HTTPError(request.full_url, 302, 'redirect', {'Location': 'https://example.blob.core.windows.net/signed?secret=notlogged'}, None)
            return io.BytesIO(json.dumps(metadata).encode())
        opener = types.SimpleNamespace(open=open_api)
        with tempfile.TemporaryDirectory() as temporary, patch.object(baseline.urllib.request, 'build_opener', return_value=opener), patch.object(baseline.urllib.request, 'urlopen', return_value=io.BytesIO(body)) as blob:
            record = baseline.acquire_artifact(pins, row, Path(temporary) / 'download', 'test-token')
            self.assertEqual(record['sha256'], row['artifact_sha256'])
            self.assertTrue(all(request.get_header('Authorization') == 'Bearer test-token' for request in calls))
            self.assertIsInstance(blob.call_args.args[0], str)  # No Request with auth copied to blob.
            self.assertNotIn('secret', json.dumps(record))
        metadata['workflow_run']['head_sha'] = '0' * 40
        with patch.object(baseline.urllib.request, 'build_opener', return_value=opener), patch.object(baseline.urllib.request, 'urlopen') as blob:
            with self.assertRaisesRegex(RuntimeError, 'differs'): baseline.acquire_artifact(pins, row, Path('unused'), 'test-token')
            blob.assert_not_called()

    def test_separate_baseline_entry_cannot_replace_default_synthetic_or_probe_gate(self):
        old = (ROOT / 'scripts/run_windows_restorer_ci.py').read_text()
        self.assertIn("if not rename_probe_only: command.append('--large')", old)
        self.assertNotIn('accepted_baseline', old)
        command = ci.diagnostic_command(Path('work'), Path('pins'), 'paddle-cuda11.8', Path('source'), 'windows-2022')
        self.assertEqual(Path(command[4]).name, 'accepted_server_tests.py'); self.assertNotIn('--large', command)
        source = (ROOT / 'scripts/run_windows_restorer_accepted_ci.py').read_text()
        self.assertIn('shared.select_installed(', source)
        self.assertIn('shared.load_toolchain()', source)
        self.assertNotIn('inspect_toolset(', source)

    def test_first_launcher_failure_retains_both_scenario_reports_and_still_fails(self):
        spec = importlib.util.spec_from_file_location('mocked_restored_audit', HERE / 'restored_launcher_audit.py')
        audit = importlib.util.module_from_spec(spec); spec.loader.exec_module(audit)
        package = types.ModuleType('scripts'); package.__path__ = []
        portable = types.ModuleType('scripts.verify_portable_environment')
        portable.SCENARIOS = ('clean-host-path', 'contaminated-host-environment')
        portable.write_json = lambda path, data: path.write_text(json.dumps(data))
        portable.fixture_environment = lambda path, contaminated, inherited: ({}, {'cwd': str(path.parent)})
        for name in ('verify_payload', 'audit_environment', 'audit_windows_executables', 'check_environment_evidence'):
            setattr(portable, name, Mock())
        portable.summarize_reports = Mock(return_value={'passed': True, 'errors': []})
        models = types.ModuleType('scripts.check_paddle_models'); models.validate_report = Mock()
        receipt = types.ModuleType('scripts.accept_release_archive'); receipt.Evidence = Mock(); receipt._workload_evidence = Mock()
        launcher = types.ModuleType('scripts.portable_python'); launcher.launcher_command = lambda root, platform, args: ['mock-original-launcher', *args]
        modules = {'scripts': package, 'scripts.verify_portable_environment': portable, 'scripts.check_paddle_models': models,
                   'scripts.accept_release_archive': receipt, 'scripts.portable_python': launcher}
        for name, module in modules.items():
            if name != 'scripts': setattr(package, name.split('.')[-1], module)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / 'source'; app = root / 'app'; app.mkdir()
            fixtures = source / 'native/ocr/tests/evidence'; fixtures.mkdir(parents=True)
            (fixtures / 'fixture-manifest.json').write_bytes(b'synthetic fixture manifest')
            golden = fixtures.parent / 'goldens/smoke.json'; golden.parent.mkdir(); golden.write_bytes(b'synthetic golden')
            models.GOLDEN_SHA256 = baseline.sha(golden); models.FIXTURE_MANIFEST_SHA256 = baseline.sha(fixtures / 'fixture-manifest.json')
            (app / 'bundle-manifest.json').write_text(json.dumps({'commit': baseline.BASELINE, 'platform': 'windows',
                'arch': 'x64', 'ocr_backend': 'paddle', 'runtime_python': 'runtime/python.exe', 'version': '2.3.0', 'build_profile': 'paddle-cuda11.8'}))
            invoked = []
            def run(command, **kwargs):
                evidence = Path(command[command.index('--results-dir') + 1]); invoked.append(evidence.name)
                (evidence / 'paddle-models').mkdir(); (evidence / 'paddle-models/report.json').write_text('{}')
                result = Mock(returncode=1 if len(invoked) == 1 else 0)
                if len(invoked) == 1: result.check_returncode.side_effect = RuntimeError('original clean launcher failed')
                return result
            with patch.dict(sys.modules, modules), patch.object(sys, 'path', list(sys.path)), \
                    patch.object(audit.subprocess, 'check_output', side_effect=lambda command, **kwargs: baseline.BASELINE if command[-1] == 'HEAD' else ''), \
                    patch.object(audit.subprocess, 'run', side_effect=run):
                with self.assertRaisesRegex(RuntimeError, 'One or more'):
                    audit.main(['--baseline-source', str(source), '--baseline-commit', baseline.BASELINE, '--root', str(app),
                                '--output', str(root / 'evidence'), '--runner', 'windows-2022'])
            report = json.loads((root / 'evidence/restored-launcher-report.json').read_bytes())
            self.assertEqual(invoked, list(portable.SCENARIOS)); self.assertEqual(report['result'], 'failed')
            self.assertEqual([case['result'] for case in report['scenarios']], ['failed', 'passed'])
            self.assertIn('original clean launcher failed', report['scenarios'][0]['error'])
            for scenario in portable.SCENARIOS: self.assertTrue((root / 'evidence' / scenario / 'scenario-report.json').is_file())
            models.validate_report.assert_called_once()

    def test_final_report_requires_real_tree_launcher_and_production_scope_not_just_exit_zero(self):
        pins = baseline.load_pins(HERE / 'accepted-baseline-306.json'); row = pins['profiles'][0]
        tree = {'result': 'passed', 'all_file_sha256_checked': True, 'no_extra_or_reparse_entries': True, 'inventory_sha256': 'f' * 64}
        receipt = {'name': row['archive_name'] + '.acceptance.json', 'size': 10, 'sha256': 'a' * 64}
        report = {'schema': 1, 'scope': 'accepted-306-baseline-native-restore-not-final-release', 'result': 'passed',
                  'baseline_commit': pins['commit'], 'profile': row['profile'], 'runner': 'windows-2022',
                  'archive_sha256': row['archive_sha256'], 'restore_returncode': 0,
                  'gpu_inference_verified': False, 'client_windows_10_11_verified': False,
                  'release_distribution_created': False, 'physical_disk_exhaustion_tested': False,
                  'artifact': {'id': row['artifact_id'], 'sha256': row['artifact_sha256'], 'bytes': row['artifact_bytes'],
                               'original_acceptance_not_yet_verified': False},
                  'original_acceptance_validation': 'passed-release-guard-bound-to-original-artifact-and-ZIP',
                  'originals_unchanged_verified': True, 'volumes_unchanged_verified': True,
                  'original_files': {row['archive_name']: {'bytes': row['archive_bytes'], 'sha256': row['archive_sha256']},
                                     receipt['name']: {'bytes': receipt['size'], 'sha256': receipt['sha256']}},
                  'transport': {'receipt': receipt},
                  'build': {'synthetic_space_fixture': False, 'restricted_zlib': {'final_link_map': {
                      'synthetic_space_shim_present': False, 'required_inflate_symbols_present': True,
                      'excluded_gzwrite_deflate_crc_archive_symbols_absent': True}}},
                  'restored_before_launcher': copy.deepcopy(tree), 'restored_after_launcher': copy.deepcopy(tree),
                  'launcher': {'result': 'passed', 'commit': pins['commit'], 'archive_extraction_used': False,
                               'gpu_inference_verified': False, 'scenarios': [
                      {'scenario': scenario, 'result': 'passed', 'returncode': 0, 'audit': {'passed': True, 'errors': []}}
                      for scenario in ('clean-host-path', 'contaminated-host-environment')]}}
        baseline.validate_run_report(report, pins, row['profile'], 'windows-2022')
        mutations = [lambda r: r.update(archive_sha256='0' * 64), lambda r: r.update(restore_returncode=False),
            lambda r: r.update(gpu_inference_verified=True), lambda r: r.update(originals_unchanged_verified=False),
            lambda r: r['artifact'].update(original_acceptance_not_yet_verified=True),
            lambda r: r['transport']['receipt'].update(name='package-verification.json'),
            lambda r: r['build'].update(synthetic_space_fixture=True),
            lambda r: r['build']['restricted_zlib']['final_link_map'].update(synthetic_space_shim_present=True),
            lambda r: r['build']['restricted_zlib']['final_link_map'].update(required_inflate_symbols_present=False),
            lambda r: r['restored_after_launcher'].update(inventory_sha256='0' * 64),
            lambda r: r['restored_after_launcher'].update(all_file_sha256_checked=False),
            lambda r: r['launcher'].update(archive_extraction_used=True),
            lambda r: r['launcher']['scenarios'].pop(),
            lambda r: r['launcher']['scenarios'][1].update(scenario='clean-host-path'),
            lambda r: r['launcher']['scenarios'][0]['audit'].update(errors=['bad dependency'])]
        for index, mutate in enumerate(mutations):
            changed = copy.deepcopy(report); mutate(changed)
            with self.subTest(index=index), self.assertRaises(ValueError):
                baseline.validate_run_report(changed, pins, row['profile'], 'windows-2022')

    def test_exact_branch_four_server_jobs_no_publish_no_payload_upload_and_real_launcher(self):
        workflow = (ROOT / '.github/workflows/windows-restorer-accepted.yml').read_text()
        self.assertIn('  workflow_dispatch:', workflow)
        self.assertIn('    branches: [fix/windows-restorer]', workflow)
        self.assertIn("'native/windows-restorer-accepted/**'", workflow)
        self.assertNotIn("'native/windows-restorer/**'", workflow)
        self.assertIn('os: [windows-2022, windows-2025]', workflow)
        self.assertIn('profile: [paddle-cuda11.8, paddle-cuda12.6]', workflow)
        self.assertIn('actions: read', workflow)
        push = workflow.split('  push:\n', 1)[1].split('  workflow_dispatch:', 1)[0]
        paths = ast.literal_eval(push.split('    paths: ', 1)[1].splitlines()[0])
        self.assertEqual(paths, ['.github/workflows/windows-restorer-accepted.yml', 'native/windows-restorer-accepted/**', 'scripts/run_windows_restorer_accepted_ci.py'])
        old = (ROOT / '.github/workflows/windows-restorer.yml').read_text()
        old_push = old.split('  push:\n', 1)[1].split('  workflow_dispatch:', 1)[0]
        old_paths = ast.literal_eval(old_push.split('    paths: ', 1)[1].splitlines()[0])
        for changed in (paths[0], 'native/windows-restorer-accepted/accepted_baseline.py', paths[2]):
            self.assertFalse(any(fnmatch.fnmatchcase(changed, pattern) for pattern in old_paths))
        for forbidden in ('contents: write', 'continue-on-error', 'Bypass', 'Unblock', 'self-hosted', '--publish'):
            self.assertNotIn(forbidden, workflow)
        upload = workflow.split('uses: actions/upload-artifact@v4', 1)[1]
        self.assertNotIn('.exe', upload); self.assertNotIn('*.zip', upload); self.assertNotIn('*.part', upload)
        audit = (HERE / 'restored_launcher_audit.py').read_text()
        self.assertIn("launcher_command(root, 'windows'", audit)
        self.assertIn("'--paddle-model-fixtures'", audit)
        self.assertIn('acceptance._workload_evidence', audit)
        self.assertIn('check_paddle_models.validate_report', audit)
        self.assertNotIn('extract_checked', audit)
        server = (HERE / 'accepted_server_tests.py').read_text()
        self.assertIn('builder.compile_fixed(RESTORER, build, build)', server)
        self.assertNotIn('synthetic_space_fixture=True', server)
        self.assertIn("os.environ.pop('GITHUB_TOKEN', None)", server)
        self.assertIn("report['restored_after_launcher']", server)


if __name__ == '__main__': unittest.main()
