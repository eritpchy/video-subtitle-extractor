# SPDX-License-Identifier: Apache-2.0
"""Offline contracts, not actual Windows or repeated baseline acceptance."""
import ast
import copy
import fnmatch
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PureWindowsPath
import subprocess
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parents[1]
spec = importlib.util.spec_from_file_location('post_os_probe_contract', HERE / 'post_probe.py')
probe = importlib.util.module_from_spec(spec); spec.loader.exec_module(probe)


def marker(stage, tick=1, pid=42, frequency=10000000):
    return f'VSE_OS_STAGE|{stage}|{pid}|{tick}|{frequency}|2026-10-26T11:00:00.0000000Z\r\n'.encode()


class ProbeTests(unittest.TestCase):
    def source(self):
        # The current diagnostic branch's source retains the same 306 OS helper.
        raw = (ROOT / 'scripts/windows_verification.py').read_bytes()
        self.assertIn(hashlib.sha256(raw).hexdigest(), (probe.SOURCE_SHA, probe.SOURCE_CRLF_SHA))
        return raw

    def test_fixed_whole_source_pins_and_only_literal_original_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'windows_verification.py'
            raw = self.source().replace(b'\r\n', b'\n')
            path.write_bytes(raw)
            command = probe.original_command(path)
            self.assertIn('$os = Get-CimInstance Win32_OperatingSystem', command)
            path.write_bytes(raw.replace(b'\n', b'\r\n'))
            self.assertEqual(probe.original_command(path), command)
            for mutation in (raw + b'\n', raw.replace(b'timeout=60', b'timeout=120'), raw.replace(b'Get-CimInstance', b'Get-WmiObject')):
                path.write_bytes(mutation)
                with self.assertRaisesRegex(ValueError, 'exact accepted 306'): probe.original_command(path)

    def test_even_rebound_sha_cannot_allow_nonliteral_or_changed_timeout(self):
        fixtures = [
            b'def windows_os():\n command = make_script()\n subprocess.run([], timeout=60)\n',
            b'def windows_os():\n command = "script"\n subprocess.run([], timeout=True)\n',
            b'def windows_os():\n command = "script"\n subprocess.run([], timeout=61)\n',
            b'def windows_os():\n command = "script"\n subprocess.run([], timeout=60)\n subprocess.run([], timeout=60)\n',
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'source.py'
            for raw in fixtures:
                path.write_bytes(raw)
                with patch.object(probe, 'SOURCE_SHA', hashlib.sha256(raw).hexdigest()):
                    with self.assertRaises(ValueError): probe.original_command(path)

    def test_instrumentation_only_adds_stage_lines_preserves_original_operations(self):
        command = probe.original_command(ROOT / 'scripts/windows_verification.py')
        marked = probe.marked_command(command)
        self.assertEqual(marked.count('Get-CimInstance Win32_OperatingSystem'), 1)
        self.assertEqual(marked.count('Get-ItemProperty'), 1)
        self.assertEqual(marked.count('ConvertTo-Json -Compress'), 1)
        self.assertIn('[Console]::Error', marked)
        tail = marked[marked.index("VseOsStage 'enter'\n") + len("VseOsStage 'enter'\n"):]
        for name in probe.STAGES[1:]: tail = tail.replace(f"VseOsStage '{name}'\n", '')
        self.assertEqual(tail, command)
        for unsafe in ('Invoke-Expression', 'EncodedCommand', 'ExecutionPolicy', 'Bypass', 'Get-WmiObject'):
            self.assertNotIn(unsafe, marked)
        for changed in (command.replace('Get-CimInstance', 'Changed'), command + command):
            with self.assertRaises(ValueError): probe.marked_command(changed)

    def test_partial_marker_prefix_is_retained_but_success_requires_full_sequence(self):
        rows = probe.stages_from(b'ordinary stderr\n' + marker('enter') + marker('before_cim', 2), 42)
        self.assertEqual([r['stage'] for r in rows], ['enter', 'before_cim'])
        self.assertEqual(probe.stages_from(b'no stage at all', 42), [])
        rows = probe.stages_from(b''.join(marker(s, i) for i, s in enumerate(probe.STAGES)), 42)
        self.assertEqual(len(rows), 7)

    def test_bad_marker_schema_order_pid_frequency_and_clock_are_rejected(self):
        variants = [marker('after_cim'), marker('enter') * 2, marker('enter', pid=43), marker('enter', frequency=0),
            marker('enter', 3) + marker('before_cim', 2), marker('enter') + marker('before_cim', frequency=9),
            b'VSE_OS_STAGE|enter|42\n', marker('enter').replace(b'Z', b'+01:00')]
        for raw in variants:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError): probe.stages_from(raw, 42)

    def fake_process(self, responses, returncode=0):
        process = Mock(pid=42, returncode=returncode)
        process.communicate.side_effect = responses
        context = Mock(); context.__enter__ = Mock(return_value=process); context.__exit__ = Mock(return_value=False)
        return process, context

    def capture(self, responses, *, marked=False, returncode=0, validator=None):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / 'control'
        process, context = self.fake_process(responses, returncode)
        with patch.object(probe.subprocess, 'Popen', return_value=context) as launch:
            result = probe.run_capture(Path('C:/Windows/PS.exe'), 'literal original', Path('C:/restored'),
                                       {'SYSTEMROOT': 'C:/Windows'}, path, marked, validator or (lambda value: None))
        self.assertEqual(launch.call_args.args[0], [str(Path('C:/Windows/PS.exe')), '-NoProfile', '-NonInteractive', '-Command', 'literal original'])
        self.assertEqual(launch.call_args.kwargs['cwd'], Path('C:/restored'))
        self.assertIs(launch.call_args.kwargs['stdout'], subprocess.PIPE)
        self.assertNotIn('shell', launch.call_args.kwargs)
        return result, process, path

    def test_original_control_passes_exact_utf8_json_without_marker_requirement(self):
        validator = Mock()
        result, process, path = self.capture([(b'\xef\xbb\xbf{"build":26100}', b'')], validator=validator)
        self.assertEqual(result['result'], 'passed'); validator.assert_called_once_with({'build': 26100})
        process.communicate.assert_called_once_with(timeout=60); process.kill.assert_not_called()
        self.assertEqual((path / 'stdout.bin').read_bytes(), b'\xef\xbb\xbf{"build":26100}')
        self.assertEqual(result['timeout_seconds'], 60)

    def test_timeout_preserves_complete_raw_partial_stream_once_and_fails(self):
        initial = marker('enter')
        final = initial + marker('before_cim', 2) + b'partial invalid utf8 \xff'
        timeout = subprocess.TimeoutExpired('original', 60, output=b'partial', stderr=initial)
        result, process, path = self.capture([timeout, (b'partial-more', final)], marked=True, returncode=1)
        self.assertEqual(result['result'], 'failed'); self.assertTrue(result['timed_out'])
        self.assertEqual(result['stages'][-1]['stage'], 'before_cim')
        process.kill.assert_called_once(); self.assertEqual(process.communicate.call_count, 2)
        self.assertEqual((path / 'stderr.bin').read_bytes(), final)
        self.assertEqual((path / 'stdout.bin').read_bytes(), b'partial-more')
        self.assertEqual(result['stderr']['sha256'], hashlib.sha256(final).hexdigest())

    def test_zero_exit_but_missing_stages_or_bad_os_is_not_success(self):
        result, _, _ = self.capture([(b'{}', marker('enter'))], marked=True)
        self.assertEqual(result['result'], 'failed')
        validator = Mock(side_effect=ValueError('wrong OS'))
        result, _, _ = self.capture([(b'{}', b'')], validator=validator)
        self.assertEqual(result['result'], 'failed')
        result, _, _ = self.capture([(b'{}', b'failure')], returncode=1)
        self.assertEqual(result['result'], 'failed')

    def test_marked_complete_success_is_separate_report(self):
        raw = b''.join(marker(s, i) for i, s in enumerate(probe.STAGES))
        result, _, path = self.capture([(b'{}', raw)], marked=True)
        self.assertEqual(result['result'], 'passed'); self.assertEqual(len(result['stages']), 7)
        self.assertEqual(json.loads((path / 'capture.json').read_bytes())['result'], 'passed')

    def test_no_arbitrary_environment_names_or_secret_values_are_serialized(self):
        inherited = {'SystemRoot': 'C:/Windows', 'GITHUB_TOKEN': 'supersecret', 'MY_PASSWORD': 'password',
                     'TOKEN_IN_NAME_supersecret': 'unknown', 'PATH': 'do-not-dump-host-path'}
        env = {'SYSTEMROOT': 'C:/Windows', 'PATH': 'known-sanitized-path', 'GH_TOKEN': 'supersecret',
               'TOKEN_IN_NAME_supersecret': 'value', 'OTHER': 'private', 'HOME': 'fixture-home'}
        raw = json.dumps(probe.safe_environment_record(env, inherited))
        for private in ('supersecret', 'password', 'private', 'do-not-dump-host-path', 'GH_TOKEN', 'MY_PASSWORD'):
            self.assertNotIn(private, raw)
        record = json.loads(raw)
        self.assertEqual(record['other_inherited_names_and_values_not_recorded_count'], 4)
        self.assertFalse(record['old_full_environment_available'])
        self.assertEqual(record['selected_non_sensitive_values']['HOME'], 'fixture-home')

    def test_old_runtime_audit_hooks_are_removed_case_insensitively(self):
        raw = {'PythonPath': 'old-hook', 'vse_runtime_audit_reports': 'old-reports', 'VSE_RUNTIME_AUDIT_ROOT': 'root',
               'PATH': 'fixed', 'PYTHONUTF8': '1'}
        self.assertEqual(probe.without_hooks(raw), {'PATH': 'fixed', 'PYTHONUTF8': '1'})
        self.assertIn('PythonPath', raw)

    def test_snapshot_detects_changed_added_missing_files_and_reparse(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); path = root / 'report.json'; path.write_bytes(b'old')
            frozen = probe.snapshot([root]); path.write_bytes(b'new')
            self.assertNotEqual(probe.snapshot([root]), frozen)
            path.write_bytes(b'old'); (root / 'extra').write_bytes(b'')
            self.assertNotEqual(probe.snapshot([root]), frozen)
            (root / 'extra').unlink(); path.unlink()
            self.assertNotEqual(probe.snapshot([root]), frozen)
            if hasattr(Path, 'symlink_to'):
                try: path.symlink_to(root / 'missing')
                except OSError: return  # Windows developer-mode/admin not assumed.
                with self.assertRaises(ValueError): probe.snapshot([root])

    def valid_completed(self):
        tree = {'result': 'passed', 'inventory_sha256': 'abc'}
        report = {'profile': probe.PROFILE, 'runner': probe.RUNNER, 'baseline_commit': probe.BASELINE,
                  'restore_returncode': 0, 'original_acceptance_validation': 'passed-release-guard-bound-to-original-artifact-and-ZIP',
                  'originals_unchanged_verified': True, 'volumes_unchanged_verified': True, 'post_run_errors': [],
                  'restored_before_launcher': tree, 'restored_after_launcher': tree, 'result': 'failed'}
        launcher = {'scenarios': [{'scenario': s, 'result': 'failed' if i == 0 else 'passed'} for i, s in enumerate(probe.SCENARIOS)]}
        return report, launcher

    def test_failed_completed_original_can_be_diagnosed_but_incomplete_cannot(self):
        report, launcher = self.valid_completed(); probe.require_completed_attempt(report, launcher)
        for key, value in (('runner', 'windows-2022'), ('profile', 'paddle-cuda11.8'), ('restore_returncode', 1),
                           ('originals_unchanged_verified', False), ('post_run_errors', ['error']), ('restored_after_launcher', {})):
            changed = copy.deepcopy(report); changed[key] = value
            with self.assertRaises(ValueError): probe.require_completed_attempt(changed, launcher)
        for cases in ([], launcher['scenarios'][:1], launcher['scenarios'][::-1], [{'scenario': s, 'result': 'running'} for s in probe.SCENARIOS]):
            with self.assertRaises(ValueError): probe.require_completed_attempt(report, {'scenarios': cases})

    def test_derived_environment_matches_fixture_and_removes_only_declared_hooks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); work = root / 'work'; output = root / 'post'; output.mkdir()
            app = root / 'app'; scenario = probe.SCENARIOS[0]
            template = output / ('template-' + scenario)
            old = work / 'launcher-evidence' / ('fixture-' + scenario)
            case = work / 'launcher-evidence' / scenario; case.mkdir(parents=True)
            fixture = {'fixture_root': str(template), 'cwd': str(template / 'cwd'), 'list': [str(template / 'home')]}
            original = {'fixture_root': str(old), 'cwd': str(old / 'cwd'), 'list': [str(old / 'home')]}
            (case / 'fixture.json').write_text(json.dumps(original))
            old.mkdir(); (old / 'empty').mkdir(); (old / 'existing-cache').write_bytes(b'cache remains')
            selected = {'PATH': 'safe', 'VSE_CONFIG_FILE': str(old / 'config'), 'VSE_WORK_DIR': str(old / 'work'), 'PYTHONPATH': 'oldhook'}
            observed = {'bundle_root': str(app), 'platform': 'windows', 'environment': selected}
            (case / 'environment-report.json').write_text(json.dumps(observed))
            env = {key: str(template / key.lower()) for key in probe.FIXTURE_PATHS}
            env.update(PATH='safe', VSE_CONFIG_FILE=str(template / 'config'), VSE_WORK_DIR=str(template / 'work'), HOME=str(template / 'home'))
            portable = types.SimpleNamespace(fixture_environment=Mock(return_value=(env, fixture)))
            bootstrap = types.SimpleNamespace(sanitize_environment=lambda r, e, target: e)
            derived, record = probe.derive_environment(portable, bootstrap, app, work, output, scenario, {'GITHUB_TOKEN': 'secret', 'SystemRoot': 'C:/Windows'})
            self.assertNotIn('PYTHONPATH', derived); self.assertNotIn('GITHUB_TOKEN', derived)
            self.assertEqual(derived['HOME'], str(output / ('fixture-clone-' + scenario) / 'home'))
            self.assertEqual(probe.tree_inventory(old), probe.tree_inventory(Path(record['fixture_clone']['path'])))
            self.assertEqual(len(record['relocated_fixture_fields']), len(probe.FIXTURE_PATHS))
            self.assertNotIn('secret', json.dumps(record)); self.assertEqual(record['fixture_sha256'], probe.sha(case / 'fixture.json'))
            inherited = portable.fixture_environment.call_args.args[2]
            self.assertNotIn('GITHUB_TOKEN', inherited)
            observed['environment']['GITHUB_TOKEN'] = 'secret'
            (case / 'environment-report.json').write_text(json.dumps(observed))
            with self.assertRaisesRegex(ValueError, 'Unexpected captured'): probe.derive_environment(portable, bootstrap, app, work, output, scenario, {})

    def test_real_fixture_constructor_and_sanitizer_relocate_all_nine_fields(self):
        from scripts import verify_portable_environment as portable
        from scripts import bundle_bootstrap as bootstrap
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); app = base / 'app'; app.mkdir(); work = base / 'original-work'; output = base / 'post'; output.mkdir()
            for scenario in probe.SCENARIOS:
                case = work / 'launcher-evidence' / scenario; case.mkdir(parents=True)
                old = work / 'launcher-evidence' / ('fixture-' + scenario)
                inherited = {'SYSTEMROOT': str(base / 'Windows'), 'VSE_WINDOWS_RUNNER_LABEL': probe.RUNNER,
                             'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUTF8': '1'}
                env, fixture = portable.fixture_environment(old, scenario == probe.SCENARIOS[1], inherited)
                sanitized = bootstrap.sanitize_environment(app, env, 'windows')
                (case / 'fixture.json').write_text(json.dumps(fixture))
                selected = {key: sanitized[key] for key in ('PATH', 'VSE_CONFIG_FILE', 'VSE_WORK_DIR', 'PYTHONNOUSERSITE', 'PYTHONUTF8')}
                selected.update(VSE_OCR_BACKEND='paddle', VSE_MODEL_DIR=str(app / 'backend/models'),
                                VSE_VSF_BINARY=str(app / 'native/vsf.exe'), VSE_VSF_DIR=str(app / 'native'),
                                IMAGEIO_FFMPEG_EXE=str(app / 'native/ffmpeg.exe'))
                (case / 'environment-report.json').write_text(json.dumps({'bundle_root': str(app), 'platform': 'windows', 'environment': selected}))
                before = probe.tree_inventory(old)
                moved, record = probe.derive_environment(portable, bootstrap, app, work, output, scenario, inherited)
                self.assertEqual({r['name'] for r in record['relocated_fixture_fields']}, set(probe.FIXTURE_PATHS))
                self.assertEqual(moved['PATH'], selected['PATH']); self.assertEqual(moved['SYSTEMROOT'], inherited['SYSTEMROOT'])
                self.assertEqual(before, probe.tree_inventory(old))
                self.assertEqual(before, probe.tree_inventory(Path(record['fixture_clone']['path'])))
                for key in probe.FIXTURE_PATHS: self.assertTrue(Path(moved[key]).is_relative_to(Path(record['fixture_clone']['path'])))
                selected['PATH'] = 'altered before relocation'
                (case / 'environment-report.json').write_text(json.dumps({'bundle_root': str(app), 'platform': 'windows', 'environment': selected}))
                negative_output = base / ('negative-' + scenario); negative_output.mkdir()
                with patch.object(probe, 'clone_fixture') as clone:
                    with self.assertRaisesRegex(ValueError, 'PATH/config/work differ'):
                        probe.derive_environment(portable, bootstrap, app, work, negative_output, scenario, inherited)
                    clone.assert_not_called()

    def test_registry_comparison_is_read_only_fixed_values_and_64_bit_view(self):
        registry = types.SimpleNamespace(HKEY_LOCAL_MACHINE=1, KEY_QUERY_VALUE=1, KEY_WOW64_64KEY=256,
                                         REG_SZ=1, REG_DWORD=4, OpenKey=Mock(), QueryValueEx=Mock())
        registry.OpenKey.return_value.__enter__ = Mock(return_value='read-only-key')
        registry.OpenKey.return_value.__exit__ = Mock(return_value=False)
        registry.QueryValueEx.side_effect = [('24H2', 1), ('ServerDatacenter', 1), (123, 4), ('26100', 1)]
        with patch.dict(probe.sys.modules, {'winreg': registry}): record = probe.registry_control()
        self.assertEqual(registry.OpenKey.call_args.args[-1], 257)
        self.assertEqual([c.args[1] for c in registry.QueryValueEx.call_args_list], ['DisplayVersion', 'EditionID', 'UBR', 'CurrentBuildNumber'])
        self.assertTrue(record['read_only']); self.assertNotIn('ProductName', record['values'])
        registry.QueryValueEx.side_effect = [FileNotFoundError(), ('ServerDatacenter', 1), (123, 4), ('26100', 1)]
        with patch.dict(probe.sys.modules, {'winreg': registry}): record = probe.registry_control()
        self.assertEqual(record['values']['DisplayVersion'], {'present': False})
        registry.QueryValueEx.side_effect = PermissionError('read denied')
        with patch.dict(probe.sys.modules, {'winreg': registry}):
            with self.assertRaises(PermissionError): probe.registry_control()

    def test_finally_integrity_and_original_result_are_not_reclassified(self):
        source = (HERE / 'post_probe.py').read_text()
        self.assertIn("if original['result'] != 'passed': raise RuntimeError", source)
        self.assertIn("snapshot(frozen_paths) != frozen", source)
        self.assertIn("policy.verify_restored_tree(destination, expected)", source)
        self.assertIn("policy.verify_originals(work / 'original', original['original_files'])", source)
        self.assertIn('transport.verify_transport(volume)', source)
        self.assertIn("'prior_failure_reclassified': False", source)
        self.assertIn("directory_snapshot(frozen_paths) != frozen_directories", source)
        self.assertLess(source.index("write_json(output / 'frozen-original-evidence.json', frozen)"), source.index("from scripts import release_volume_transport as transport"))
        self.assertNotIn('os.environ.update(', source)
        self.assertNotIn('os.environ.clear(', source)

    def test_output_cannot_write_into_original_work_or_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for protected in ('work', 'reports', 'baseline'):
                (root / protected).mkdir()
            for protected in ('work', 'reports', 'baseline'):
                destination = root / protected / 'post'
                with self.assertRaisesRegex(ValueError, 'separate'):
                    probe.main(['--work-directory', str(root / 'work'), '--report-directory', str(root / 'reports'),
                                '--baseline-source', str(root / 'baseline'), '--output', str(destination)])
                self.assertFalse(destination.exists())
            with self.assertRaisesRegex(ValueError, 'outside'):
                probe.child(root / 'work', root / 'work' / 'post')
            self.assertFalse((root / 'work/post').exists())

    def test_real_unchanged_transport_manifest_suffix(self):
        self.assertIn("glob('*.transport.json')", (HERE / 'post_probe.py').read_text())
        self.assertNotIn("glob('*.volumes.json')", (HERE / 'post_probe.py').read_text())

    def test_child_uses_actual_bundle_python_and_runs_unmarked_before_marked(self):
        source = (HERE / 'post_probe.py').read_text()
        self.assertIn("str(root / 'runtime/python.exe'), '-I', '-B', '-X', 'utf8'", source)
        self.assertIn("cwd=root, env=environment", source)
        self.assertLess(source.index("('original-control', command, False)"), source.index("('stage-marked', marked_command(command), True)"))
        child = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == 'child')
        self.assertLess(next(i for i, n in enumerate(child.body[-2].body) if isinstance(n, ast.For)),
                        next(i for i, n in enumerate(child.body[-2].body) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and getattr(n.value.func, 'id', '') == 'native_control'))


class FixtureCloneTests(unittest.TestCase):
    def test_complete_unicode_clone_empty_dirs_original_bytes_and_independent_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); source = base / 'original 中文 &!()'; source.mkdir()
            (source / 'home/cache/empty').mkdir(parents=True)
            data = b'original cache\x00' + str(source).encode()
            (source / 'home/cache/profile').write_bytes(data)
            before = probe.tree_inventory(source)
            record = probe.clone_fixture(source, base / 'clone 中文 &!()')
            self.assertEqual(record['inventory'], before)
            self.assertEqual(probe.tree_inventory(Path(record['path'])), before)
            self.assertFalse((source / 'home/cache/profile').samefile(Path(record['path']) / 'home/cache/profile'))
            self.assertFalse(record['cache_removed']); self.assertFalse(record['contents_rewritten'])
            (Path(record['path']) / 'home/cache/profile').write_bytes(b'changed clone')
            self.assertEqual((source / 'home/cache/profile').read_bytes(), data)
            self.assertEqual(probe.tree_inventory(source), before)
            with self.assertRaises(FileExistsError): probe.clone_fixture(source, Path(record['path']))
            with self.assertRaisesRegex(ValueError, 'separate'): probe.clone_fixture(source, source / 'clone')

    def test_clone_budget_no_space_has_no_output_and_no_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'original'; source.mkdir()
            destination = Path(temporary) / 'clone'
            with patch.object(probe.shutil, 'disk_usage', return_value=types.SimpleNamespace(free=0)):
                with self.assertRaisesRegex(RuntimeError, 'no host cleanup'): probe.clone_fixture(source, destination)
            self.assertFalse(destination.exists()); self.assertTrue(source.exists())

    def test_source_mutation_during_copy_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); source = base / 'original'; source.mkdir()
            (source / 'file').write_bytes(b'old')
            inventory = probe.tree_inventory
            def mutating(path):
                if path == base / 'clone': (source / 'file').write_bytes(b'new')
                return inventory(path)
            with patch.object(probe, 'tree_inventory', side_effect=mutating):
                with self.assertRaisesRegex(ValueError, 'changed during copy'): probe.clone_fixture(source, base / 'clone')

    def test_hardlink_and_symlink_or_special_entries_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); source = base / 'original'; source.mkdir()
            (source / 'file').write_bytes(b'original')
            os.link(source / 'file', source / 'hardlink')
            with self.assertRaisesRegex(ValueError, 'hardlink'): probe.clone_fixture(source, base / 'clone')
            (source / 'hardlink').unlink()
            try: (source / 'link').symlink_to(source / 'file')
            except OSError: pass  # Windows privilege is not requested for a unit fixture.
            else:
                with self.assertRaisesRegex(ValueError, 'reparse/symlink'): probe.clone_fixture(source, base / 'clone')
                (source / 'link').unlink()
            if hasattr(os, 'mkfifo'):
                os.mkfifo(source / 'fifo')
                with self.assertRaisesRegex(ValueError, 'special'): probe.clone_fixture(source, base / 'clone')

    def test_reparse_attribute_is_refused_before_directory_enumeration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            info = root.lstat()
            fake = types.SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            with patch.object(Path, 'lstat', return_value=fake), patch.object(Path, 'iterdir') as children:
                with self.assertRaisesRegex(ValueError, 'reparse'): probe.tree_inventory(root)
                children.assert_not_called()

    def test_destination_alias_or_injected_hardlink_is_not_a_clone(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); source = base / 'original'; source.mkdir(); (source / 'file').write_bytes(b'old')
            target = base / 'clone'; target.mkdir()
            with self.assertRaises(FileExistsError): probe.clone_fixture(source, target)
            with patch.object(probe.Path, 'samefile', return_value=True):
                with self.assertRaisesRegex(ValueError, 'identity'): probe.clone_fixture(source, base / 'other')

    def environment(self, old):
        return {**{key: str(PureWindowsPath(old) / key.lower()) for key in probe.FIXTURE_PATHS},
                'PATH': r'C:\Windows\System32;D:\payload\runtime', 'SYSTEMROOT': r'C:\Windows',
                'WINDIR': r'C:\Windows', 'UNCHANGED': old + '-sibling'}

    def test_component_remapping_case_slashes_and_nonfixture_values(self):
        old, new = r'D:\Original 中文 &!()\fixture', r'D:\Post 中文 &!()\clone'
        env = self.environment(old)
        env['LOCALAPPDATA'] = str(PureWindowsPath(old) / 'home/AppData/Local').lower().replace('\\', '/')
        moved, changes = probe.relocate_fixture_environment(env, old, new)
        self.assertEqual(len(changes), len(probe.FIXTURE_PATHS))
        for key in probe.FIXTURE_PATHS: self.assertTrue(PureWindowsPath(moved[key]).is_relative_to(PureWindowsPath(new)))
        for key in ('PATH', 'SYSTEMROOT', 'WINDIR', 'UNCHANGED'): self.assertEqual(moved[key], env[key])
        self.assertNotEqual(moved['HOME'], env['HOME'])

    def test_unknown_old_root_path_lists_escapes_and_missing_fields_fail(self):
        old, new = r'D:\Original\fixture', r'D:\Post\clone'
        changes = [('UNKNOWN', old + r'\write'), ('PATH', r'C:\Windows;' + old + r'\bin'),
                   ('HOME', old + r'\home;D:\outside'), ('APPDATA', old + r'\..\outside'),
                   ('LOCALAPPDATA', old + r'-sibling\Local'), ('PATH', 'C:/Windows;' + old + ';C:/Tools'),
                   ('UNKNOWN', '"' + old + '"'), ('UNKNOWN', old + ';suffix')]
        for key, value in changes:
            env = self.environment(old); env[key] = value
            with self.subTest(key=key, value=value):
                with self.assertRaises(ValueError): probe.relocate_fixture_environment(env, old, new)
        env = self.environment(old); del env['LOCALAPPDATA']
        with self.assertRaisesRegex(ValueError, 'Incomplete'): probe.relocate_fixture_environment(env, old, new)

    def test_simulated_ps_cache_writes_only_clone_and_original_empty_dirs_are_guarded(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); original = base / 'original'; original.mkdir(); (original / 'home').mkdir()
            frozen_files, frozen_dirs = probe.snapshot([original]), probe.directory_snapshot([original])
            clone = base / 'clone'; record = probe.clone_fixture(original, clone)
            before = probe.tree_inventory(clone)
            cache = clone / 'home/AppData/Local/Microsoft/Windows/PowerShell/StartupProfileData-NonInteractive'
            cache.parent.mkdir(parents=True); cache.write_bytes(b'\0' * 64)
            after = probe.tree_inventory(clone); changes = probe.tree_changes(before, after)
            self.assertEqual(changes['added'][cache.relative_to(clone).as_posix()]['bytes'], 64)
            self.assertEqual(changes['removed'], {}); self.assertEqual(changes['changed'], {})
            self.assertEqual(probe.snapshot([original]), frozen_files)
            self.assertEqual(probe.directory_snapshot([original]), frozen_dirs)
            (original / 'unexpected-empty').mkdir()
            self.assertEqual(probe.snapshot([original]), frozen_files)
            self.assertNotEqual(probe.directory_snapshot([original]), frozen_dirs)
            (original / 'unauthorized-cache').write_bytes(b'\0' * 64)
            self.assertNotEqual(probe.snapshot([original]), frozen_files)

    def test_all_clone_change_kinds_are_recorded_not_filtered_by_filename(self):
        before = {'gone': {'kind': 'file', 'bytes': 1}, 'type': {'kind': 'directory'}, 'edited': {'kind': 'file', 'sha256': 'a'}}
        after = {'new-empty-dir': {'kind': 'directory'}, 'type': {'kind': 'file', 'bytes': 2}, 'edited': {'kind': 'file', 'sha256': 'b'}}
        changes = probe.tree_changes(before, after)
        self.assertEqual(set(changes['added']), {'new-empty-dir'})
        self.assertEqual(set(changes['removed']), {'gone'})
        self.assertEqual(set(changes['changed']), {'type', 'edited'})


class WorkflowTests(unittest.TestCase):
    def test_only_single_failed_combination_and_independent_paths(self):
        source = (ROOT / '.github/workflows/windows-restorer-os-diagnostic.yml').read_text()
        self.assertIn('branches: [fix/windows-restorer]', source)
        self.assertIn('workflow_dispatch:', source)
        self.assertIn('runs-on: windows-2025', source)
        self.assertNotIn('matrix:', source)
        self.assertIn('--accepted-profile paddle-cuda12.6', source)
        self.assertIn('--runner windows-2025', source)
        self.assertNotIn('continue-on-error', source)
        self.assertIn('contents: read', source); self.assertIn('actions: read', source)
        before, after = source.split('python native/windows-restorer-os-diagnostic/post_probe.py')
        self.assertIn('scripts/run_windows_restorer_accepted_ci.py', before)
        self.assertIn('if: always()', before[before.index('scripts/run_windows_restorer_accepted_ci.py'):])
        self.assertNotIn('post_probe.py', before)
        self.assertIn('if: always()', after)
        for path in ['.github/workflows/windows-restorer-os-diagnostic.yml', 'native/windows-restorer-os-diagnostic/post_probe.py']:
            self.assertFalse(any(fnmatch.fnmatch(path, glob) for glob in ('native/windows-restorer/**', 'native/windows-restorer-accepted/**', '.github/workflows/windows-restorer.yml', '.github/workflows/windows-restorer-accepted.yml')))
        self.assertNotIn('scripts/run_windows_restorer_ci.py --', source)

    def test_upload_keeps_original_workloads_and_exact_raw_output_not_payload(self):
        source = (ROOT / '.github/workflows/windows-restorer-os-diagnostic.yml').read_text()
        self.assertIn('restorer-src/build/restorer-accepted/launcher-evidence/', source)
        self.assertIn('stdout.bin', source); self.assertIn('stderr.bin', source)
        self.assertIn('restorer-src/build/restorer-os-diagnostic/workflow-source.yml', source)
        self.assertNotIn('include-hidden-files: true', source)
        self.assertNotIn('native-build/*.exe', source); self.assertNotIn('volumes/*.part', source)
        self.assertNotIn('original/*.zip', source)
        self.assertEqual(source.count('persist-credentials: false'), 2)
        self.assertEqual(source.count('GITHUB_TOKEN:'), 1)


if __name__ == '__main__': unittest.main()
