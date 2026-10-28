# SPDX-License-Identifier: Apache-2.0
"""Small offline contracts only. Mock EXEs/reports are NOT Server evidence."""
from __future__ import annotations
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(ROOT))
import release_gate as gate
import source_bundle
import restored_launcher_audit as launcher_audit


def record():
    return {'schema': 2, 'result': 'built-not-client-certified', 'synthetic_space_fixture': False,
            'restricted_zlib': {'final_link_map': {'synthetic_space_shim_present': False,
                'required_inflate_symbols_present': True, 'excluded_gzwrite_deflate_crc_archive_symbols_absent': True}},
            'git': {'commit': 'c' * 40, 'dirty': False}, 'git_after_build': {'commit': 'c' * 40, 'dirty': False},
            'client_restore_verified': False, 'gpu_inference_verified': False}


def handoff():
    return {'exe_sha256': 'e' * 64, 'source_zip_sha256': 'f' * 64,
            'archive': {'sha256': 'a' * 64, 'identity': {'commit': 'c' * 40}}}


def consumer(runner='windows-2022'):
    expected = handoff()
    tree = {'result': 'passed', 'all_file_sha256_checked': True, 'no_extra_or_reparse_entries': True,
            'entries': 5, 'inventory_sha256': 'd' * 64}
    return {'schema': 1, 'scope': 'same-exe-final-zip-server-restore', 'result': 'passed', 'runner': runner,
            'native_os': {'runner': runner, 'build': gate.RUNNERS[runner], 'product_type': 3},
            'exe_sha256': expected['exe_sha256'], 'source_zip_sha256': expected['source_zip_sha256'],
            'archive_sha256': expected['archive']['sha256'], 'handoff_sha256': 'b' * 64,
            'compile_performed': False, 'restore_returncode': 0, 'post_run_errors': [], 'inputs_unchanged_verified': True,
            'gpu_inference_verified': False, 'client_windows_10_11_verified': False,
            'restored_before_launcher': copy.deepcopy(tree), 'restored_after_launcher': copy.deepcopy(tree),
            'launcher_raw_inventory': {'synthetic.json': {'kind': 'file', 'size': 0, 'sha256': '0' * 64}},
            'raw_logs': {name: {'bytes': 0, 'sha256': '0' * 64} for name in ('restore.stdout.log', 'restore.stderr.log', 'launcher.log')},
            'launcher': {'result': 'passed', 'commit': 'c' * 40, 'archive_extraction_used': False, 'gpu_inference_verified': False,
                'application_source_after': {'result': 'passed'}, 'post_run_errors': [],
                'scenarios': [{'scenario': case, 'result': 'passed', 'returncode': 0, 'audit': {'passed': True, 'errors': []}}
                              for case in sorted(gate.SCENARIOS)]}}


def rebuild():
    raw = record()
    raw.update(scope='fixed-header-source-rebuild-not-client-certification', original_exe_sha256='e' * 64, exe_sha256='1' * 64)
    return {'schema': 1, 'scope': 'independent-source-companion-rebuild-not-bit-identity-or-restore-proof',
            'result': 'passed', 'runner': 'windows-2022', 'source_zip_sha256': 'f' * 64,
            'original_exe_sha256': 'e' * 64, 'rebuilt_exe_sha256': '1' * 64, 'handoff_sha256': 'b' * 64,
            'bit_identical_to_original': False, 'rebuild_record': raw, 'post_run_errors': [],
            'inputs_after': {'result': 'passed'}, 'source_after': {'kind': 'vse-native-restorer-exact-source',
                                                                'original_exe_sha256': 'e' * 64}}


class FinalGateTests(unittest.TestCase):
    def test_foreign_build_module_is_neither_called_nor_replaced(self):
        trap = Mock(side_effect=AssertionError('foreign build must not run'))
        foreign = SimpleNamespace(assert_distributable_record=trap)
        private_foreign = SimpleNamespace(assert_distributable_record=trap)
        with patch.dict(sys.modules, {'build': foreign, '_vse_windows_restorer_gate_build': private_foreign}):
            gate.validate_build(record())
            result = gate.combine(handoff(), [consumer(), consumer('windows-2025')], rebuild())
            self.assertEqual(result['result'], 'passed')
            bad = record(); bad['synthetic_space_fixture'] = True
            with self.assertRaisesRegex(RuntimeError, 'Synthetic or unproven'): gate.validate_build(bad)
            self.assertIs(sys.modules['build'], foreign)
            self.assertIs(sys.modules['_vse_windows_restorer_gate_build'], private_foreign)
        trap.assert_not_called()

    def test_local_helpers_ignore_cwd_pythonpath_and_global_short_names(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ('build', 'generate_metadata', 'source_bundle'):
                (root / (name + '.py')).write_text('raise AssertionError("foreign import executed")\n')
            code = '''import importlib.util, pathlib, sys, types
spec = importlib.util.spec_from_file_location('exact_gate', sys.argv[1])
gate = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)
for name in ('build', 'generate_metadata', 'source_bundle'):
    foreign = types.ModuleType(name)
    sys.modules[name] = foreign
    sys.modules['_vse_windows_restorer_gate_' + name] = foreign
    loaded = gate.local_helper(name)
    assert pathlib.Path(loaded.__file__).resolve() == gate.HERE / (name + '.py')
    assert loaded is not foreign and sys.modules[name] is foreign
    assert sys.modules['_vse_windows_restorer_gate_' + name] is foreign
gate.local_helper('generate_metadata')._component('safe')
gate.local_helper('source_bundle')._safe_member('safe/file.py')
'''
            result = subprocess.run([sys.executable, '-B', '-c', code, str(HERE / 'release_gate.py')], cwd=root,
                env={**__import__('os').environ, 'PYTHONPATH': str(root)}, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in ('../build', 'os', 'zlib_build', 'build.py'):
                with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, 'Unknown restorer helper'):
                    gate.local_helper(name)

    def test_different_rebuild_hash_is_truthfully_not_bit_identical(self):
        result = gate.combine(handoff(), [consumer(), consumer('windows-2025')], rebuild())
        self.assertEqual(result['result'], 'passed')
        self.assertFalse(result['bit_identical_rebuild_verified'])
        self.assertFalse(result['gpu_inference_verified'])

    def test_same_executable_and_zip_on_two_distinct_servers_are_required(self):
        for reports in ([], [consumer()], [consumer(), consumer()], [consumer()] * 3):
            with self.subTest(reports=len(reports)), self.assertRaises(RuntimeError):
                gate.combine(handoff(), reports, rebuild())
        for field in ('exe_sha256', 'source_zip_sha256', 'archive_sha256', 'handoff_sha256'):
            rows = [consumer(), consumer('windows-2025')]; rows[1][field] = '2' * 64
            with self.subTest(field=field), self.assertRaises(RuntimeError): gate.combine(handoff(), rows, rebuild())

    def test_per_os_compile_failed_partial_and_scope_upgrades_rejected(self):
        changes = {'compile_performed': True, 'result': 'failed', 'restore_returncode': 1,
                   'inputs_unchanged_verified': False, 'gpu_inference_verified': True,
                   'client_windows_10_11_verified': True, 'post_run_errors': [{'check': 'changed'}]}
        for key, value in changes.items():
            row = consumer(); row[key] = value
            with self.subTest(key=key), self.assertRaises(RuntimeError): gate.validate_consumer(row, handoff(), row['runner'])
        row = consumer(); row['native_os']['build'] = 26100
        with self.assertRaises(RuntimeError): gate.validate_consumer(row, handoff(), 'windows-2022')

    def test_launcher_exit_only_missing_cpu_evidence_and_archive_reextract_rejected(self):
        changes = [{'result': 'passed'}, {'scenarios': []}, {'archive_extraction_used': True}, {'commit': '9' * 40}]
        for change in changes:
            row = consumer()
            if change == {'result': 'passed'}: row['launcher'] = change
            else: row['launcher'].update(change)
            with self.subTest(change=change), self.assertRaises(RuntimeError): gate.validate_consumer(row, handoff(), row['runner'])
        row = consumer(); row['launcher']['scenarios'][0]['audit']['errors'] = ['bad']
        with self.assertRaises(RuntimeError): gate.validate_consumer(row, handoff(), row['runner'])

    def test_full_after_tree_is_mandatory_and_cannot_change(self):
        for key in ('restored_before_launcher', 'restored_after_launcher'):
            row = consumer(); del row[key]
            with self.assertRaises(RuntimeError): gate.validate_consumer(row, handoff(), row['runner'])
        row = consumer(); row['restored_after_launcher']['inventory_sha256'] = '0' * 64
        with self.assertRaises(RuntimeError): gate.validate_consumer(row, handoff(), row['runner'])
        rows = [consumer(), consumer('windows-2025')]
        for key in ('restored_before_launcher', 'restored_after_launcher'):
            rows[1][key]['inventory_sha256'] = '0' * 64
        with self.assertRaisesRegex(RuntimeError, 'same full payload inventory'):
            gate.combine(handoff(), rows, rebuild())

    def test_source_rebuild_is_required_and_not_interchangeable_with_original(self):
        for change in ({'result': 'failed'}, {'source_zip_sha256': '0' * 64}, {'original_exe_sha256': '0' * 64},
                       {'bit_identical_to_original': True}, {'scope': 'diagnostic'}, {'handoff_sha256': '0' * 64}):
            value = rebuild(); value.update(change)
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                gate.combine(handoff(), [consumer(), consumer('windows-2025')], value)

    def test_production_and_rebuild_cannot_drop_inflate_or_link_synthetic_shim(self):
        for flag in ('synthetic_space_shim_present', 'required_inflate_symbols_present', 'excluded_gzwrite_deflate_crc_archive_symbols_absent'):
            value = record(); value['restricted_zlib']['final_link_map'][flag] = flag == 'synthetic_space_shim_present'
            with self.subTest(flag=flag), self.assertRaises(RuntimeError): gate.validate_build(value)
            rebuilt = rebuild(); rebuilt['rebuild_record']['restricted_zlib'] = value['restricted_zlib']
            with self.assertRaises(RuntimeError): gate.combine(handoff(), [consumer(), consumer('windows-2025')], rebuilt)
        for key, value in (('synthetic_space_fixture', True), ('git_after_build', {}), ('git', {'dirty': True})):
            item = record(); item[key] = value
            with self.assertRaises(RuntimeError): gate.validate_build(item)

    def test_tree_checks_content_empty_directories_and_extra_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / 'empty').mkdir(); (root / 'file').write_bytes(b'original')
            expected = gate.tree_inventory(root)
            self.assertEqual(gate.verify_tree(root, expected)['entries'], 2)
            (root / 'extra').write_bytes(b'bad')
            with self.assertRaises(RuntimeError): gate.verify_tree(root, expected)
            (root / 'extra').unlink(); (root / 'file').write_bytes(b'modified')
            with self.assertRaises(RuntimeError): gate.verify_tree(root, expected)

    def test_source_extractor_rejects_unsafe_paths_links_and_duplicates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for index, name in enumerate(('../outside', '/absolute', 'a\\b', 'CON.txt', 'a/../b')):
                archive = root / f'{index}.zip'
                entry = zipfile.ZipInfo(name)
                # Malicious fixtures must retain the raw archive spelling;
                # ZipInfo otherwise rewrites backslashes on Windows writers.
                entry.orig_filename = entry.filename = name
                with zipfile.ZipFile(archive, 'w') as stream: stream.writestr(entry, b'bad')
                with zipfile.ZipFile(archive) as stream:
                    self.assertEqual(stream.infolist()[0].orig_filename, name)
                with self.subTest(name=name), self.assertRaises((RuntimeError, ValueError)):
                    gate.extract_source(archive, root / f'out-{index}')
                self.assertFalse((root / f'out-{index}').exists())
            archive = root / 'link.zip'
            with zipfile.ZipFile(archive, 'w') as stream:
                info = zipfile.ZipInfo('link'); info.external_attr = 0o120777 << 16; stream.writestr(info, b'target')
            with self.assertRaises(RuntimeError): gate.extract_source(archive, root / 'link-out')

    def test_unsafe_source_zip_fixture_preserves_windows_raw_names(self):
        original_init = zipfile.ZipInfo.__init__

        def windows_init(info, *args, **kwargs):
            # Exercise ZipInfo's Windows separator normalization on every host.
            # Only the constructor sees the simulated separator; filesystem
            # operations continue to use the actual host implementation.
            with patch.object(zipfile.os, 'sep', '\\'):
                original_init(info, *args, **kwargs)

        with patch.object(zipfile.ZipInfo, '__init__', windows_init):
            self.test_source_extractor_rejects_unsafe_paths_links_and_duplicates()

    def test_json_duplicates_and_existing_output_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'record.json'; path.write_text('{"x":1,"x":2}')
            with self.assertRaises(RuntimeError): gate.read_json(path)
            with self.assertRaises(FileExistsError): gate.write_json(path, {})

    def test_external_report_sha_is_mandatory_and_checked_before_json_use(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'report.json'; gate.write_json(path, consumer())
            expected = gate.sha(path)
            self.assertEqual(gate.read_bound_report(path, expected)[0], consumer())
            for value in ('', 'placeholder', '0' * 64):
                with self.subTest(value=value), self.assertRaises(RuntimeError): gate.read_bound_report(path, value)
            path.write_text('{}')
            with self.assertRaises(RuntimeError): gate.read_bound_report(path, expected)

    def test_all_after_checks_run_and_preserve_primary_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'report.json'; called = []
            def broken():
                called.append('broken'); raise ValueError('secondary input changed')
            def later():
                called.append('later'); return {'result': 'passed'}
            with self.assertRaisesRegex(RuntimeError, 'primary compile failed'):
                gate.finalize_report(path, {}, RuntimeError('primary compile failed'), [('bad', broken), ('later', later)])
            result = gate.read_json(path)
            self.assertEqual(called, ['broken', 'later']); self.assertEqual(result['result'], 'failed')
            self.assertIn('primary compile failed', result['error'])
            self.assertIn('secondary input changed', result['post_run_errors'][0]['error'])

    def test_no_hash_defaults_no_workflow_no_306_artifact_constants(self):
        text = (HERE / 'release_gate.py').read_text()
        self.assertNotIn('3064320', text); self.assertNotIn('12636364950', text)
        result = subprocess.run([sys.executable, '-B', str(HERE / 'release_gate.py'), 'prepare'], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'--archive-sha256', result.stderr)
        self.assertIn('compile_performed\': False', text)
        self.assertNotIn('compile_fixed(', text)  # consumer and producer cannot call an alternate builder


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def consume_fixture(self, *, mutate=False, fail=False):
        document = {'archive': {'size': 100, 'uncompressed_bytes': 3, 'root': 'payload',
                                'identity': {'commit': 'c' * 40}},
                    'validated_zip_entries': [{'name': 'payload/a.txt', 'kind': 'file', 'size': 3,
                                               'sha256': hashlib.sha256(b'abc').hexdigest()}]}
        args = SimpleNamespace(runner='windows-2022', handoff=self.root / 'handoff.json', handoff_sha256='b' * 64,
            distribution=self.root / 'distribution', manifest=self.root / 'volumes/manifest.json',
            work=self.root / 'consumer', application_source=self.root / 'application-source')
        commands = []
        def child(command, **kwargs):
            commands.append(command)
            if command[0].endswith('restore.exe'):
                if fail: raise subprocess.CalledProcessError(9, command)
                payload = args.work / 'restored Unicode 中文 &!()/payload'; payload.mkdir()
                (payload / 'a.txt').write_bytes(b'abc')
            else:
                evidence = args.work / 'launcher-evidence'; evidence.mkdir()
                gate.write_json(evidence / 'restored-launcher-report.json', consumer()['launcher'])
                if mutate: (args.work / 'restored Unicode 中文 &!()/payload/a.txt').write_bytes(b'BAD')
            return subprocess.CompletedProcess(command, 0)
        native = {'runner': 'windows-2022', 'build': 20348, 'product_type': 3}
        with patch.object(gate, 'require_server', return_value=native), patch.object(gate, 'require_free', return_value={}), \
                patch.object(gate, 'load_handoff', return_value=(handoff(), document)) as loaded, \
                patch.object(gate.subprocess, 'run', side_effect=child):
            if fail or mutate:
                with self.assertRaises(RuntimeError): gate.consume(args)
            else: gate.consume(args)
        self.assertEqual(loaded.call_count, 2)  # always revalidates original distribution/volumes
        return gate.read_json(args.work / 'consumer-report.json'), commands

    def test_consumer_runs_transferred_exe_then_original_launcher_never_compiles(self):
        report, commands = self.consume_fixture()
        self.assertEqual(report['result'], 'passed'); self.assertFalse(report['compile_performed'])
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[0][0], str(self.root / 'distribution/restore.exe'))
        self.assertTrue(any(value.endswith('restored_launcher_audit.py') for value in commands[1]))
        self.assertIn('-I', commands[1])
        self.assertEqual(set(report['raw_logs']), {'restore.stdout.log', 'restore.stderr.log', 'launcher.log'})
        self.assertTrue(report['launcher_raw_inventory'])

    def test_consumer_failure_still_checks_inputs_and_records_primary_and_tree_failure(self):
        report, commands = self.consume_fixture(fail=True)
        self.assertEqual(report['result'], 'failed'); self.assertEqual(len(commands), 1)
        self.assertIn('CalledProcessError', report['error'])
        self.assertEqual(report['post_run_errors'][0]['check'], 'restored-tree-after')
        self.assertTrue(report['inputs_unchanged_verified'])

    def test_consumer_launcher_success_cannot_hide_payload_change(self):
        report, _ = self.consume_fixture(mutate=True)
        self.assertEqual(report['result'], 'failed')
        self.assertIn('restored-tree-after', [row['check'] for row in report['post_run_errors']])

    def test_source_extract_failure_still_rechecks_exact_distribution(self):
        args = SimpleNamespace(runner='windows-2022', handoff=self.root / 'handoff.json', handoff_sha256='b' * 64,
            distribution=self.root / 'distribution', manifest=self.root / 'volumes/manifest.json',
            work=self.root / 'rebuild', acknowledge_redistribution_license=True)
        with patch.object(gate, 'require_server'), patch.object(gate, 'load_handoff', return_value=(handoff(), {})) as loaded, \
                patch.object(gate, 'extract_source', side_effect=RuntimeError('source ZIP corrupt')), \
                patch.object(gate.subprocess, 'run') as child:
            with self.assertRaisesRegex(RuntimeError, 'source ZIP corrupt'): gate.rebuild_source(args)
        self.assertEqual(loaded.call_count, 2); child.assert_not_called()
        report = gate.read_json(args.work / 'source-rebuild-report.json')
        self.assertEqual(report['result'], 'failed'); self.assertEqual(report['inputs_after']['result'], 'passed')

    def producer_fixture(self):
        spec = importlib.util.spec_from_file_location('volume_fixture_producer', ROOT / 'tests/test_release_volume_transport.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        case = module.ReleaseVolumeTransportTests(); case.setUp(); self.addCleanup(case.doCleanups)
        args = SimpleNamespace(archive=case.archive, archive_sha256=gate.sha(case.archive),
            receipt_sha256=gate.sha(case.archive.with_suffix('.zip.acceptance.json')),
            checksum_sha256=gate.sha(case.archive.with_suffix('.zip.sha256')),
            commit=case.identity['commit'], version=case.identity['version'], profile=case.identity['build_profile'],
            runner='windows-2022', notices=self.root / 'notices', work=self.root / 'producer', acknowledge_redistribution_license=True)
        return case, args

    def test_wrong_external_receipt_or_checksum_fails_before_volume_creation(self):
        from scripts import release_volume_transport as transport
        _, args = self.producer_fixture()
        for field in ('receipt_sha256', 'checksum_sha256'):
            bad = copy.copy(args); setattr(bad, field, '0' * 64)
            with patch.object(gate, 'require_server'), patch.object(transport, 'create_transport') as create:
                with self.assertRaisesRegex(RuntimeError, 'independent CI artifact SHA'): gate.prepare(bad)
                create.assert_not_called()
            self.assertFalse(args.work.exists())

    def test_producer_build_failure_still_checks_originals_no_handoff(self):
        _, args = self.producer_fixture()
        def failed(command, **kwargs):
            args.archive.with_suffix('.zip.acceptance.json').write_text('changed after build started')
            raise RuntimeError('primary build failed')
        with patch.object(gate, 'require_server'), patch.object(gate, 'require_free', return_value={}), \
                patch.object(gate.subprocess, 'run', side_effect=failed) as child:
            with self.assertRaisesRegex(RuntimeError, 'primary build failed'): gate.prepare(args)
        self.assertEqual(child.call_count, 1)
        report = gate.read_json(args.work / 'producer-report.json')
        self.assertEqual(report['result'], 'failed'); self.assertIn('primary build failed', report['error'])
        self.assertEqual(report['post_run_errors'][0]['check'], 'original_inputs_after')
        self.assertIn('transport_after', report)
        self.assertFalse((args.work / 'handoff.json').exists())

    def test_producer_compiles_once_then_binds_all_three_external_hashes(self):
        _, args = self.producer_fixture()
        with patch.object(gate, 'require_server'), patch.object(gate, 'require_free', return_value={}), \
                patch.object(gate.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as child, \
                patch.object(gate, 'bind_distribution', return_value={'synthetic_test_only': True}) as bind:
            gate.prepare(args)
        self.assertEqual(child.call_count, 1); self.assertTrue(child.call_args.args[0][2].endswith('build.py'))
        self.assertEqual(bind.call_count, 1); self.assertIsNone(bind.call_args.args[2])
        for name in ('archive_sha256', 'receipt_sha256', 'checksum_sha256'):
            self.assertEqual(bind.call_args.kwargs[name], getattr(args, name))
        self.assertEqual(gate.read_json(args.work / 'producer-report.json')['result'], 'passed')
        self.assertEqual(gate.read_json(args.work / 'handoff.json'), {'synthetic_test_only': True})

    def test_producer_post_check_failure_never_publishes_handoff(self):
        _, args = self.producer_fixture()
        def binding(*unused, **kwargs):
            args.archive.with_suffix('.zip.sha256').write_text('post-bind changed')
            return {'synthetic_test_only': True}
        with patch.object(gate, 'require_server'), patch.object(gate, 'require_free', return_value={}), \
                patch.object(gate.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)), \
                patch.object(gate, 'bind_distribution', side_effect=binding):
            with self.assertRaisesRegex(RuntimeError, 'Post-run input integrity'): gate.prepare(args)
        self.assertFalse((args.work / 'handoff.json').exists())
        self.assertEqual(gate.read_json(args.work / 'producer-report.json')['result'], 'failed')

    def test_consumer_validator_reason_survives_report(self):
        with patch.object(gate, 'validate_consumer', side_effect=RuntimeError('specific strict validation failure')):
            # The helper expects success unless fail/mutate is requested, so
            # invoke through a wrapper assertion while retaining generated data.
            with self.assertRaisesRegex(RuntimeError, 'Server restoration'): self.consume_fixture()
        report = gate.read_json(self.root / 'consumer/consumer-report.json')
        self.assertEqual(report['result'], 'failed')
        self.assertIn('specific strict validation failure', report['error'])


class SourceClosureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def companion(self):
        sources = source_bundle.snapshot_sources(ROOT)
        notices = {name: b'SYNTHETIC UNIT NOTICE, NOT FOR DISTRIBUTION' for name in (
            'Microsoft-Visual-Cpp-runtime-NOTICE.txt', 'Apache-2.0.txt', 'THIRD_PARTY_NOTICES.txt', 'zlib-LICENSE.txt')}
        archive = self.root / 'source.zip'
        source_bundle.create_source_bundle(archive, sources=sources, generated_header=b'// SYNTHETIC\n',
            notices=notices, build_record={'git': {'commit': '0' * 40, 'dirty': True}, 'exe_sha256': '0' * 64}, transport_manifest=b'{}')
        target = self.root / 'extracted'
        with patch.object(gate, 'require_free', return_value={'synthetic': True}): gate.extract_source(archive, target)
        return sources, target

    def test_isolated_fixed_header_imports_and_zlib_hashes_need_no_site_packages(self):
        _, root = self.companion(); native = root / 'native/windows-restorer'
        code = ('import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); '
                'import build, zlib_build, source_bundle; zlib_build.verify_sources(Path(sys.argv[1])); '
                'assert not any(n == "packaging" or n.startswith("packaging.") for n in sys.modules)')
        result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', code, str(native)], cwd=self.root, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        help_run = subprocess.run([sys.executable, '-I', '-S', '-B', str(native / 'rebuild.py'), '--help'], cwd=self.root, capture_output=True)
        self.assertEqual(help_run.returncode, 0, help_run.stderr)
        source_bundle.verify_extracted_sources(root)

    def test_source_extraction_uses_local_validators_despite_foreign_modules(self):
        trap = Mock(side_effect=AssertionError('foreign source helper must not run'))
        fake_metadata = SimpleNamespace(_component=trap)
        fake_sources = SimpleNamespace(verify_extracted_sources=trap)
        with patch.dict(sys.modules, {'generate_metadata': fake_metadata, 'source_bundle': fake_sources}):
            _, root = self.companion()
            self.assertTrue((root / 'source-manifest.json').is_file())
            self.assertIs(sys.modules['generate_metadata'], fake_metadata)
            self.assertIs(sys.modules['source_bundle'], fake_sources)
        trap.assert_not_called()

    def test_generator_runs_from_extracted_closure_not_original_repository(self):
        sources, root = self.companion()
        spec = importlib.util.spec_from_file_location('volume_test_fixture', ROOT / 'tests/test_release_volume_transport.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        case = module.ReleaseVolumeTransportTests(); case.setUp()
        try:
            manifest = case.create(); provenance = self.root / 'generator.json'
            command = [sys.executable, '-B', str(root / 'native/windows-restorer/generate_metadata.py'),
                       '--manifest', str(manifest), '--output', str(self.root / 'fixed.hpp'), '--provenance-output', str(provenance)]
            result = subprocess.run(command, cwd=self.root, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            document = json.loads(provenance.read_bytes())
            source_bundle.assert_generator_closure(document, sources)
            self.assertEqual(set(document['local_modules_actually_loaded']), {'__main__', 'fixed_index', 'inspect_zip_layout',
                'scripts.release_volume_transport', 'scripts.release_guard', 'scripts.package_release',
                'scripts.bundle_bootstrap', 'scripts.runtime_dependencies', 'scripts.release_profiles'})
            self.assertTrue((self.root / 'fixed.hpp').is_file())
            source_bundle.verify_extracted_sources(root)
            for change in ('missing', 'changed', 'extra'):
                bad = copy.deepcopy(document)
                if change == 'missing': del bad['local_modules_actually_loaded']['scripts.release_guard']
                if change == 'changed': bad['local_modules_actually_loaded']['scripts.release_guard']['sha256'] = '0' * 64
                if change == 'extra': bad['local_modules_actually_loaded']['bad'] = {'relative_path': 'unshipped.py', 'sha256': '0' * 64}
                with self.subTest(change=change), self.assertRaises(RuntimeError): source_bundle.assert_generator_closure(bad, sources)
        finally: case.doCleanups()


class LauncherSourceAfterTests(unittest.TestCase):
    def test_failed_scenarios_still_detect_fixture_and_checkout_mutation(self):
        # Deliberately mocked application helpers/process: not a real launcher,
        # model result, Windows execution or accepted application receipt.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / 'app-source'; source.mkdir()
            fixtures = source / 'native/ocr/tests/evidence'; fixtures.mkdir(parents=True)
            image = fixtures / 'fixture.bin'; image.write_bytes(b'original')
            pin = fixtures / 'fixture-manifest.json'; pin.write_bytes(b'{}')
            golden = fixtures.parent / 'goldens/smoke.json'; golden.parent.mkdir(); golden.write_bytes(b'{}')
            payload = root / 'payload'; payload.mkdir()
            (payload / 'bundle-manifest.json').write_text(json.dumps({'commit': 'c' * 40, 'platform': 'windows',
                'arch': 'x64', 'ocr_backend': 'paddle', 'runtime_python': 'runtime/python.exe'}))
            output = root / 'output'; changed = []
            def fixture_environment(path, contaminated, inherited):
                path.mkdir(); return inherited, {'cwd': str(path)}
            portable = SimpleNamespace(SCENARIOS=tuple(sorted(gate.SCENARIOS)), fixture_environment=fixture_environment,
                write_json=gate.write_json, verify_payload=lambda *a: None, audit_environment=lambda *a: None,
                audit_windows_executables=lambda *a: None)
            paddle = SimpleNamespace(GOLDEN_SHA256=gate.sha(golden), FIXTURE_MANIFEST_SHA256=gate.sha(pin))
            command = SimpleNamespace(launcher_command=lambda *a: ['SYNTHETIC-NOT-EXECUTABLE'])
            def git(argv, **kwargs):
                if argv[-1] == 'HEAD': return 'c' * 40 + '\n'
                return ' M native/ocr/tests/evidence/fixture.bin\n' if changed else ''
            def fail(argv, **kwargs):
                image.write_bytes(b'changed'); changed.append(True)
                raise subprocess.CalledProcessError(9, argv)
            import scripts
            replacements = {'scripts.verify_portable_environment': portable, 'scripts.check_paddle_models': paddle,
                            'scripts.accept_release_archive': SimpleNamespace(), 'scripts.portable_python': command}
            with patch.dict(sys.modules, replacements), \
                    patch.multiple(scripts, verify_portable_environment=portable, check_paddle_models=paddle,
                                   accept_release_archive=replacements['scripts.accept_release_archive'], create=True), \
                    patch.object(launcher_audit, 'audit_source_hashes', return_value={'synthetic': '0' * 64}), \
                    patch.object(launcher_audit.subprocess, 'check_output', side_effect=git), \
                    patch.object(launcher_audit.subprocess, 'run', side_effect=fail):
                with self.assertRaisesRegex(RuntimeError, 'One or more restored-launcher scenarios failed'):
                    launcher_audit.main(['--application-source', str(source), '--application-commit', 'c' * 40,
                                         '--root', str(payload), '--output', str(output), '--runner', 'windows-2022'])
            report = gate.read_json(output / 'restored-launcher-report.json')
            self.assertEqual(report['result'], 'failed'); self.assertEqual(len(report['scenarios']), 2)
            self.assertIn('One or more', report['error'])
            self.assertEqual({row['check'] for row in report['post_run_errors']}, {'fixtures-after', 'application-source-after'})
            self.assertEqual(report['application_source_after']['checks']['golden-after'], 'passed')


if __name__ == '__main__': unittest.main()
