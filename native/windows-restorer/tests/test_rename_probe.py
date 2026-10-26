# SPDX-License-Identifier: Apache-2.0
"""Offline diagnostic collector/source checks, not native Windows execution."""
import copy
import importlib.util
import itertools
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('rename_probe_runner', HERE / 'run_rename_probe.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def record(index, passed=True):
    expected = runner.definition(index)
    value = {**expected, 'schema': 1, 'scope': 'synthetic-rename-api-observation-not-production-or-client-certification',
             'client_restore_verified': False, 'expectation_met': passed,
             'ancestor_identities_unchanged': True, 'source_marker_bytes_unchanged': True,
             'source_and_marker_identities_unchanged': True, 'existing_target_preserved': True,
             'replace_if_exists': False, 'rename_returned_success': expected['expected_rename_returned_success'],
             'rename_win32_error': expected['expected_rename_win32_error'],
             'source_present_after': not expected['expected_rename_returned_success'],
             'target_present_after': expected['existing_target'] or expected['expected_rename_returned_success']}
    identity = {'volume_serial': 1, 'file_index_high': 2, 'file_index_low': 3}
    for prefix in ('source', 'marker', 'destination_parent', 'existing_target', 'existing_sentinel'):
        value[prefix + '_identity_before'] = dict(identity)
        value[prefix + '_identity_after'] = dict(identity)
    return value


class RenameProbeTests(unittest.TestCase):
    def test_full_orthogonal_matrix(self):
        actual = {(row['mode'], row['unicode'], row['deep_child'], row['long_parent'], row['existing_target']) for row in map(runner.definition, range(48))}
        self.assertEqual(actual, set(itertools.product(runner.MODES, (False, True), (False, True), (False, True), (False, True))))
        for invalid in (-1, 48, True, '0'):
            with self.assertRaises(ValueError): runner.definition(invalid)

    def test_each_good_record_has_proof_and_mode_identity(self):
        for index in range(48):
            self.assertIs(runner.validate_record(record(index), runner.definition(index), 0)['expectation_met'], True)

    def test_classification_never_turns_incompatible_control_into_actual_rename_success(self):
        rows = [record(index) for index in range(48)]
        summary = runner.aggregate(rows)
        self.assertTrue(summary['all_expectations_met'])
        self.assertEqual(summary['actual_successful_renames'], 8)
        self.assertEqual(summary['incompatible_relative_controls_met'], 32)
        for index, row in enumerate(rows):
            if index < 32:
                self.assertFalse(row['rename_returned_success'])
                self.assertEqual(row['rename_win32_error'], 87)
                self.assertTrue(row['source_present_after'])
                self.assertEqual(row['target_present_after'], row['existing_target'])
                self.assertEqual(row['classification'], 'relative-form-incompatible-negative-control')
            elif row['existing_target']:
                self.assertFalse(row['rename_returned_success'])
                self.assertEqual(row['rename_win32_error'], 183)
            else:
                self.assertTrue(row['rename_returned_success'])
                self.assertEqual(row['rename_win32_error'], 0)

    def test_wrong_error_unexpected_success_or_changed_paths_fail_each_control(self):
        for index in (0, 1, 16, 17, 32, 33, 46, 47):
            original = record(index)
            for change in ({'rename_returned_success': not original['rename_returned_success']},
                           {'rename_win32_error': 206}, {'rename_win32_error': True},
                           {'rename_win32_error': 183 if original['rename_win32_error'] == 87 else 87},
                           {'source_present_after': not original['source_present_after']},
                           {'target_present_after': not original['target_present_after']},
                           {'classification': 'pretend-restoration-success'},
                           {'expected_rename_win32_error': 206}):
                changed = {**original, **change}
                with self.subTest(index=index, change=change), self.assertRaises(ValueError):
                    runner.validate_record(changed, runner.definition(index), 0)
        for index in (1, 17, 33):
            changed = record(index); changed['existing_sentinel_identity_after'] = None
            with self.assertRaises(ValueError): runner.validate_record(changed, runner.definition(index), 0)

    def test_missing_changed_and_forged_success_rejected(self):
        original = record(0)
        for key in ('ancestor_identities_unchanged', 'source_marker_bytes_unchanged', 'source_and_marker_identities_unchanged',
                    'source_identity_before', 'marker_identity_after', 'destination_parent_identity_after', 'replace_if_exists'):
            value = copy.deepcopy(original); del value[key]
            with self.subTest(key=key), self.assertRaises(ValueError):
                runner.validate_record(value, runner.definition(0), 0)
        value = copy.deepcopy(original); value['marker_identity_after']['file_index_low'] = 99
        with self.assertRaises(ValueError): runner.validate_record(value, runner.definition(0), 0)
        value = copy.deepcopy(original); value['source_identity_before']['file_index_low'] = True
        value['source_identity_after']['file_index_low'] = True
        with self.assertRaises(ValueError): runner.validate_record(value, runner.definition(0), 0)
        with self.assertRaises(ValueError): runner.validate_record(original, runner.definition(1), 0)
        with self.assertRaises(ValueError): runner.validate_record(original, runner.definition(0), 2)

    def test_existing_target_negative_case_cannot_be_relabelled(self):
        for changes in ({'rename_returned_success': True}, {'source_present_after': False}, {'existing_target_preserved': False},
                        {'existing_sentinel_identity_after': None}):
            value = record(1); value.update(changes)
            with self.assertRaises(ValueError): runner.validate_record(value, runner.definition(1), 0)

    def test_baseline_failure_does_not_hide_later_cases_or_pass_total(self):
        rows = []
        def run(command, **kwargs):
            index = int(command[-1])
            document = record(index, passed=index != 0)
            kwargs['stdout'].write(json.dumps(document).encode())
            return subprocess.CompletedProcess(command, 2 if index == 0 else 0)
        with tempfile.TemporaryDirectory() as temporary, patch.object(runner.subprocess, 'run', side_effect=run):
            work = Path(temporary)
            self.assertEqual(len(list(runner.run_cases(work / 'synthetic.exe', work, rows))), 48)
            self.assertTrue(rows[-1]['expectation_met'])
            self.assertTrue((work / 'case-47.stdout.log').is_file())
        self.assertEqual(runner.aggregate(rows), {'all_cases_collected': True, 'all_expectations_met': False,
                                                'observed_expectations_met': 47, 'observed_expectations_not_met': 1,
                                                'actual_successful_renames': 8, 'incompatible_relative_controls_met': 31})

    def test_crash_timeout_bad_json_and_oversized_output_do_not_abort_matrix(self):
        rows = []
        def run(command, **kwargs):
            index = int(command[-1])
            if index == 0:
                return subprocess.CompletedProcess(command, -1073741819)
            if index == 1:
                kwargs['stderr'].write(b'partial error\n')
                raise subprocess.TimeoutExpired(command, 60)
            if index == 2:
                kwargs['stdout'].write(b'not json')
                return subprocess.CompletedProcess(command, 2)
            if index == 3:
                kwargs['stdout'].write(b'x' * (runner.MAX_CASE_LOG_BYTES + 1))
                return subprocess.CompletedProcess(command, 0)
            kwargs['stdout'].write(json.dumps(record(index)).encode())
            return subprocess.CompletedProcess(command, 0)
        with tempfile.TemporaryDirectory() as temporary, patch.object(runner.subprocess, 'run', side_effect=run):
            work = Path(temporary)
            list(runner.run_cases(work / 'synthetic.exe', work, rows))
            self.assertEqual((work / 'case-01.stderr.log').read_bytes(), b'partial error\n')
        self.assertEqual(len(rows), 48)
        self.assertEqual(runner.aggregate(rows)['observed_expectations_not_met'], 4)
        self.assertTrue(rows[-1]['expectation_met'])

    def test_empty_partial_or_duplicate_collection_is_not_success(self):
        for rows in ([], [record(0)], [record(0)] * 48):
            self.assertFalse(runner.aggregate(rows)['all_cases_collected'])
            self.assertFalse(runner.aggregate(rows)['all_expectations_met'])

    def test_cpp_retains_contract_and_is_standalone_synthetic_only(self):
        source = (HERE / 'rename_probe.cpp').read_text(encoding='utf-8')
        self.assertIn('rename->ReplaceIfExists = FALSE;', source)
        self.assertIn('rename->RootDirectory = mode == 2 ? nullptr : destination_parent_handle;', source)
        self.assertIn('mode == 2 ? native(destination) : leaf', source)
        self.assertIn('mode == 0 ? 0 : sizeof(wchar_t)', source)
        self.assertIn('offsetof(FILE_RENAME_INFO, FileName) + filename_bytes + trailing_bytes', source)
        self.assertIn('SetFileInformationByHandle(source_handle.get(), FileRenameInfo', source)
        self.assertIn('incompatible_control ? ERROR_INVALID_PARAMETER : (existing ? ERROR_ALREADY_EXISTS : ERROR_SUCCESS)', source)
        self.assertIn('rename_error == expected_rename_error', source)
        self.assertIn('boolean(row, "rename_returned_success", renamed != FALSE); number(row, "rename_win32_error", rename_error);', source)
        self.assertIn('source_present == !expected_rename_success', source)
        self.assertIn('observed-incompatible-relative-form-not-a-rename', source)
        self.assertIn('FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS', source)
        directory = source.split('Handle directory(', 1)[1].split('std::wstring canonical', 1)[0]
        self.assertIn('FILE_SHARE_READ | FILE_SHARE_WRITE,', directory)
        self.assertNotIn('FILE_SHARE_DELETE', directory)
        self.assertIn('CREATE_NEW', source)
        self.assertIn('make_directory(root); // Must be NEW', source)
        self.assertIn('locks.unchanged()', source)
        self.assertNotIn('DeleteFileW(', source)
        self.assertNotIn('RemoveDirectoryW(', source)
        self.assertNotIn('NtSetInformationFile', source)
        self.assertNotIn('SetCurrentDirectory', source)
        self.assertNotIn('CreateProcess', source)


if __name__ == '__main__':
    unittest.main()
