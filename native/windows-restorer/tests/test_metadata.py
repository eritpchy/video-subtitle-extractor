# SPDX-License-Identifier: Apache-2.0
"""Offline Python/header tests, not native Windows runtime certification."""
from __future__ import annotations
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


metadata = module('restorer_metadata', HERE / 'generate_metadata.py')
builder = module('restorer_builder', HERE / 'build.py')


class FixedInflateSourceTests(unittest.TestCase):
    """Structural regression guards, not a substitute for real Server runs."""
    def test_verified_handle_and_fixed_unicode_names_never_use_a_subprocess(self):
        source = (HERE / 'restorer.cpp').read_text(encoding='utf-8')
        body = source.split('void extract_fixed_ranges(HANDLE verified_zip,', 1)[1].split('struct TreeLocks', 1)[0]
        for forbidden in ('CreateProcess', 'SetCurrentDirectory', 'tar.exe', 'ShellExecute', 'system('):
            self.assertNotIn(forbidden, source)
        for required in ('CREATE_NEW', 'FILE_FLAG_OPEN_REPARSE_POINT', 'SetFilePointerEx(verified_zip',
                         'vse::inflate_exact', 'FlushFileBuffers', 'verify_file(output.get()',
                         'directories.push_back(open_directory(path))'):
            self.assertIn(required, body)
        self.assertIn('extract_fixed_ranges(verified_zip.get(), extracted)', source)
        self.assertIn('auto extraction_directory = open_directory(extracted)', source)
        self.assertIn('verify_file(verified_zip.get(), bundle::archive_bytes, bundle::archive_sha256', source)
        self.assertIn('check_tree(extracted,', source)

    def test_no_handle_inheritance_and_fixed_ranges_remain_bounded(self):
        source = (HERE / 'restorer.cpp').read_text(encoding='utf-8')
        self.assertNotIn('DuplicateHandle', source)
        self.assertIn('entry.compressed_bytes > bundle::central_directory_offset - entry.data_offset', source)
        self.assertIn('entry->data_offset < previous_end', source)
        self.assertIn('expanded != bundle::uncompressed_bytes', source)
        self.assertIn('hash.finish() != entry->sha256', source)
        self.assertIn('rename->ReplaceIfExists = FALSE', source)

    def test_space_fixture_keeps_bounds_without_constant_controlling_expression_or_warning_suppression(self):
        source = (HERE / 'restorer.cpp').read_text(encoding='utf-8')
        self.assertIn('if (!vse::fixed_archive_bounds_ok(bundle::uncompressed_bytes, bundle::central_directory_offset,', source)
        self.assertIn('bundle::archive_bytes, file_size(verified_zip))', source)
        core = (HERE / 'core.hpp').read_text(encoding='utf-8')
        helper = core.split('inline bool fixed_archive_bounds_ok(', 1)[1].split('inline bool sha256_text_ok', 1)[0]
        self.assertIn('expanded <= 1024ULL * 1024 * 1024 * 1024', helper)
        self.assertIn('central_offset < expected_bytes && actual_bytes == expected_bytes', helper)
        self.assertNotIn('if (', helper)
        builder_source = (HERE / 'build.py').read_text(encoding='utf-8')
        self.assertIn("'/W4', '/WX'", builder_source)
        self.assertNotIn('/wd4127', builder_source)
        self.assertNotIn('warning(disable', source)

    def test_atomic_publish_uses_probed_absolute_nul_name_and_keeps_parent_locks(self):
        source = (HERE / 'restorer.cpp').read_text(encoding='utf-8')
        rename = source.split('void rename_without_replace(', 1)[1].split('void remove_owned_file(', 1)[0]
        self.assertIn('const auto filename = native_path(destination);', rename)
        self.assertIn('storage(prefix + bytes + sizeof(wchar_t), 0)', rename)
        self.assertIn('rename->RootDirectory = nullptr;', rename)
        self.assertIn('rename->ReplaceIfExists = FALSE;', rename)
        self.assertIn('rename->FileNameLength = static_cast<DWORD>(bytes);', rename)
        self.assertIn('memcpy(rename->FileName, filename.data(), bytes);', rename)
        self.assertIn('MAXDWORD - prefix - sizeof(wchar_t)', rename)
        self.assertIn('SetFileInformationByHandle(root, FileRenameInfo', rename)
        self.assertNotIn('MoveFile', rename)
        restore = source.split('void restore(', 1)[1]
        self.assertIn('auto parent = pin_directory(destination_path);', restore)
        self.assertIn('const auto final_path = vse::join(parent.path, bundle::root_name);', restore)
        self.assertIn('rename_without_replace(restored_root.get(), final_path);', restore)
        self.assertNotIn('parent.locks.clear()', restore)
        self.assertLess(restore.index('assert_absent(final_path); check_cancel();'),
                        restore.index('rename_without_replace(restored_root.get(), final_path);'))
        self.assertLess(restore.index('rename_without_replace(restored_root.get(), final_path);'),
                        restore.index('incomplete.clear();'))

    def test_server_probe_keeps_four_argv_controls_and_unicode_long_cwd_gates(self):
        server = module('restorer_server_source_test', HERE / 'tests/windows_server_tests.py')
        cases = server.tar_probe_cases()
        argv = [row for row in cases if row['transport'] == 'argv']
        self.assertEqual({(row['alphabet'], row['extended']) for row in argv},
                         {('ascii', False), ('ascii', True), ('unicode', False), ('unicode', True)})
        self.assertEqual(len(argv), 4)
        self.assertEqual(len([row for row in cases if row['long_cwd']]), 2)
        self.assertTrue(all(row['alphabet'] == 'unicode' and row['transport'] == 'stdin-cwd'
                            for row in cases if row['long_cwd']))
        source = (HERE / 'tests/windows_server_tests.py').read_text(encoding='utf-8')
        for required in ('GetSystemDirectoryW', 'GetACP', "'version_stdout'", "'tar_unchanged'",
                         'over-260-unicode-destination-cwd', 'real-over-2gib-zip64',
                         'unicode-and-shell-metacharacters-clean-system-tar'):
            self.assertIn(required, source)

    def test_failed_native_case_is_preserved_but_does_not_hide_later_zip64_gate(self):
        server = module('restorer_server_collection_test', HERE / 'tests/windows_server_tests.py')
        records = []
        with server.collect_case(records, 'unicode'):
            records.append({'case': 'unicode', 'passed': False, 'exit_code': 1})
            raise AssertionError('native Unicode failed')
        with server.collect_case(records, 'zip64'):
            records.append({'case': 'zip64', 'passed': True, 'exit_code': 0})
        self.assertEqual([row['case'] for row in records], ['unicode', 'zip64'])
        self.assertFalse(records[0]['passed']); self.assertTrue(records[1]['passed'])
        self.assertEqual(server.result_for_cases(records, large=True), 'failed')
        with server.collect_case(records, 'post-extraction-hash'):
            records.append({'case': 'post-extraction-hash', 'passed': True})
            raise AssertionError('actual bytes differ')
        self.assertFalse(records[-1]['passed'])
        self.assertEqual(server.result_for_cases([{'passed': True}], large=False), 'partial')
        self.assertEqual(server.result_for_cases([{'passed': True}], large=True), 'passed')
        self.assertEqual(server.result_for_cases([], large=True), 'failed')


def fixture():
    return {'schema': 1, 'format': 'vse-zip-byte-volumes',
            'archive': {'name': 'vse-test.zip', 'root': 'vse-test', 'size': 3000000017,
                        'sha256': 'a' * 64, 'uncompressed_bytes': 6, 'identity': {'platform': 'windows', 'arch': 'x64'}},
            'parts': [{'name': 'vse-test.zip.part001', 'index': 1, 'offset': 0, 'size': 2000000000, 'sha256': 'b' * 64},
                      {'name': 'vse-test.zip.part002', 'index': 2, 'offset': 2000000000, 'size': 1000000017, 'sha256': 'c' * 64}],
            'validated_zip_entries': [{'name': 'vse-test/中文 &!%()/data.bin', 'size': 6, 'kind': 'file', 'sha256': 'd' * 64}],
            'fixed_zip_index': {'schema': 1, 'source_archive_sha256': 'a' * 64, 'central_directory_offset': 3000000000,
                                'files': [{'name': 'vse-test/中文 &!%()/data.bin', 'data_offset': 100, 'compressed_bytes': 6, 'method': 0}]}}


class MetadataTests(unittest.TestCase):
    def test_64bit_numeric_encoding_and_implicit_directories(self):
        source = metadata.render_verified(fixture())
        self.assertIn('3000000017ULL', source)
        self.assertIn('entry_count = 3ULL', source)
        self.assertNotIn('中文', source)
        source.encode('ascii')
        self.assertIn('0x4e2d', source)
        self.assertNotIn('data.bin', source)

    def test_invalid_components_digests_and_parts_rejected(self):
        mutations = [lambda d: d['parts'][0].update(index=2), lambda d: d['parts'][1].update(offset=1),
                     lambda d: d['parts'][0].update(name='../part001'), lambda d: d['archive'].update(size=True),
                     lambda d: d['archive'].update(sha256='x' * 64), lambda d: d['archive'].update(root='wrong'),
                     lambda d: d['archive']['identity'].update(platform='linux'), lambda d: d.update(schema=2)]
        for mutate in mutations:
            data = fixture(); mutate(data)
            with self.subTest(data=data), self.assertRaises(ValueError): metadata.render_verified(data)
        for name in ('vse-test/../x', 'vse-test/NUL.txt', 'vse-test/a:b', 'vse-test/COM¹', 'vse-test/a.', 'vse-test/x\\y'):
            data = fixture(); data['validated_zip_entries'][0]['name'] = name
            with self.subTest(name=name), self.assertRaises(ValueError): metadata.render_verified(data)

    def test_case_alias_and_directory_conflicts_rejected(self):
        for name in ('vse-test/中文 &!%()/DATA.BIN', 'vse-test/中文 &!%()'):
            data = fixture(); extra = copy.deepcopy(data['validated_zip_entries'][0]); extra['name'] = name
            data['validated_zip_entries'].append(extra); data['archive']['uncompressed_bytes'] += extra['size']
            with self.subTest(name=name), self.assertRaises(ValueError): metadata.render_verified(data)

    def test_uncompressed_inventory_bound(self):
        data = fixture(); data['archive']['uncompressed_bytes'] = 7
        with self.assertRaises(ValueError): metadata.render_verified(data)

    def test_cpp_literal_injection_never_becomes_code(self):
        text = metadata._wide('safe_name', '\\";#include <evil>\n😀')
        self.assertNotIn('#include', text)
        self.assertIn('0xd83d,0xde00', text)

    def test_dynamic_runtime_imports_fail_closed(self):
        self.assertEqual(builder.assert_static_imports('    KERNEL32.dll\n    bcrypt.dll\n'), ['BCRYPT.DLL', 'KERNEL32.DLL'])
        for value in ('', '    VCRUNTIME140.dll\n', '    python312.dll\n', '    MSVCP140.dll\n', '    UCRTBASE.dll\n'):
            with self.assertRaises(RuntimeError): builder.assert_static_imports(value)

    @unittest.skipUnless(shutil.which('g++'), 'portable C++ compiler unavailable')
    def test_generated_header_compiles_on_portable_compiler(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            (directory / 'bundle_metadata.hpp').write_text(metadata.render_verified(fixture()), encoding='ascii')
            (directory / 'check.cpp').write_text('#include "bundle_metadata.hpp"\nstatic_assert(bundle::archive_bytes > 2147483647ULL);\nstatic_assert(bundle::entry_count == 3);\nint main() { return bundle::parts[0].bytes != 2000000000ULL; }\n')
            subprocess.run(['g++', '-std=c++17', '-Wall', '-Wextra', '-Werror', f'-I{HERE}', str(directory / 'check.cpp'), '-o', str(directory / 'check')], check=True)
            subprocess.run([str(directory / 'check')], check=True)

    def test_real_validator_to_header_pipeline_rejects_changed_part(self):
        # Reuse only explicitly marked synthetic fixture infrastructure. No
        # synthetic acceptance receipt is shipped or treated as real evidence.
        fixture_module = module('volume_fixture', ROOT / 'tests/test_release_volume_transport.py')
        case = fixture_module.ReleaseVolumeTransportTests()
        case.setUp()
        try:
            manifest = case.create()
            output = case.root / 'bundle_metadata.hpp'
            subprocess.run([sys.executable, str(HERE / 'generate_metadata.py'), '--manifest', str(manifest), '--output', str(output), '--provenance-output', str(case.root / 'generator.json')], check=True, capture_output=True)
            self.assertTrue(output.is_file())
            provenance = json.loads((case.root / 'generator.json').read_bytes())
            self.assertIn('third_party_modules_actually_loaded', provenance)
            self.assertFalse(provenance['fixed_header_rebuild_requires_these_modules'])
            document = json.loads(manifest.read_text())
            part = manifest.parent / document['parts'][0]['name']
            content = part.read_bytes(); part.write_bytes(bytes([content[0] ^ 1]) + content[1:])
            other = case.root / 'bad.hpp'
            result = subprocess.run([sys.executable, str(HERE / 'generate_metadata.py'), '--manifest', str(manifest), '--output', str(other)], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(other.exists())
        finally:
            case.doCleanups()


def synthetic_x64_pe(flags=0x800):
    """Only PE structure bytes for parser tests, not a runnable Windows image."""
    data = bytearray(0x800)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 0x3c, 0x80)
    data[0x80:0x84] = b'PE\0\0'
    struct.pack_into('<HHIIIHH', data, 0x84, 0x8664, 1, 0, 0, 0, 0xf0, 0x22)
    optional = 0x98
    struct.pack_into('<H', data, optional, 0x20b)
    struct.pack_into('<II', data, optional + 32, 0x1000, 0x200)
    struct.pack_into('<II', data, optional + 56, 0x2000, 0x400)
    struct.pack_into('<I', data, optional + 108, 16)
    struct.pack_into('<II', data, optional + 112 + 10 * 8, 0x1100, 0x140)
    section = optional + 0xf0
    data[section:section + 8] = b'.rdata\0\0'
    struct.pack_into('<IIII', data, section + 8, 0x400, 0x1000, 0x400, 0x400)
    struct.pack_into('<I', data, 0x500, 0x140)
    # Distinguish the WORD at offset 78 from its neighbouring CSDVersion and
    # EditList fields, so an off-by-two parser cannot accidentally pass.
    struct.pack_into('<HHQ', data, 0x500 + 76, 0x1234, flags, 0x0102030405060708)
    return data


class PeDependencyPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='restorer PE test ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.executable = self.root / 'restore.exe'

    def inspect(self, data):
        self.executable.write_bytes(data)
        return builder.assert_pe_dependency_policy(self.executable)

    def test_binary_x64_loadconfig_field_and_rva_to_raw_mapping(self):
        report = self.inspect(synthetic_x64_pe())
        self.assertEqual(report['machine'], 0x8664)
        self.assertEqual(report['optional_header_magic'], 0x20b)
        self.assertEqual(report['load_config_rva'], 0x1100)
        self.assertEqual(report['load_config_file_offset'], 0x500)
        self.assertEqual(report['load_config_directory_size'], 0x140)
        self.assertEqual(report['load_config_structure_size'], 0x140)
        self.assertEqual(report['dependent_load_flags_offset'], 78)
        self.assertEqual(report['dependent_load_flags'], 0x800)

    def inspect_stat_snapshots(self, *, path_after=None, fd_after=None, fd_before=None):
        # Model CPython's Windows API difference explicitly on every host.
        # The synthetic stat fields are not a claim of native Windows testing;
        # the PE bytes still come from a real open file and strict parser.
        self.executable.write_bytes(synthetic_x64_pe())
        common = {'st_dev': 7, 'st_ino': 11, 'st_size': 0x800,
                  'st_mtime_ns': 1000, 'st_birthtime_ns': 100}
        path_initial = {**common, 'st_ctime_ns': 100}
        fd_initial = {**common, 'st_ctime_ns': 900, **(fd_before or {})}
        path_final = {**path_initial, **(path_after or {})}
        fd_final = {**fd_initial, **(fd_after or {})}
        path = SimpleNamespace(is_symlink=Mock(return_value=False),
                               is_file=Mock(return_value=True),
                               open=self.executable.open,
                               stat=Mock(side_effect=[SimpleNamespace(**path_initial),
                                                      SimpleNamespace(**path_final)]))
        fstat = Mock(side_effect=[SimpleNamespace(**fd_initial), SimpleNamespace(**fd_final)])
        with patch.object(builder, 'os', SimpleNamespace(fstat=fstat)):
            return builder.assert_pe_dependency_policy(path)

    def test_distinct_path_and_fd_ctime_semantics_do_not_fake_a_mutation(self):
        self.assertEqual(self.inspect_stat_snapshots()['dependent_load_flags'], 0x800)

    def test_same_api_path_and_fd_ctime_changes_still_fail_closed(self):
        # ctime must not simply be removed to make the Windows test pass.
        for mutation in ({'path_after': {'st_ctime_ns': 101}},
                         {'fd_after': {'st_ctime_ns': 901}}):
            with self.subTest(mutation=mutation), self.assertRaisesRegex(
                    RuntimeError, 'changed during policy verification'):
                self.inspect_stat_snapshots(**mutation)

    def test_opening_cross_api_identity_mismatch_is_rejected(self):
        for field in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_birthtime_ns'):
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, 'changed while opening'):
                self.inspect_stat_snapshots(fd_before={field: 9999})

    def test_same_api_identity_size_or_write_time_changes_are_rejected(self):
        for scope in ('path_after', 'fd_after'):
            for field in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_birthtime_ns'):
                with self.subTest(scope=scope, field=field), self.assertRaisesRegex(
                        RuntimeError, 'changed during policy verification'):
                    self.inspect_stat_snapshots(**{scope: {field: 9999}})

    def test_rewritten_existing_pe_is_stable_during_each_verification(self):
        # Reuse the same path, just like the failing Windows compile subtests.
        # Do not hide the cross-API issue by allocating a fresh file each time.
        for value in (0x1234, 0x2345, 0x3456, 0x4567):
            data = synthetic_x64_pe()
            struct.pack_into('<H', data, 0x500 + 76, value)
            with self.subTest(csd_version=value):
                self.assertEqual(self.inspect(data)['dependent_load_flags'], 0x800)

    def test_flag_must_equal_system32_only_not_merely_contain_its_bit(self):
        for flags in (0, 0x8, 0x80, 0x100, 0x900, 0x1800, 0xffff):
            with self.subTest(flags=flags), self.assertRaisesRegex(RuntimeError, 'not established in PE'):
                self.inspect(synthetic_x64_pe(flags))

    def test_missing_loadconfig_directory_or_old_structure_fails(self):
        for offset, fmt, value in ((0x98 + 108, '<I', 10),
                                   (0x98 + 192, '<I', 0), (0x98 + 196, '<I', 0),
                                   (0x98 + 196, '<I', 79),
                                   (0x500, '<I', 0), (0x500, '<I', 78),
                                   (0x500, '<I', 0x141)):
            data = synthetic_x64_pe()
            struct.pack_into(fmt, data, offset, value)
            with self.subTest(offset=offset, value=value), self.assertRaises(RuntimeError):
                self.inspect(data)

    def test_wrong_architecture_optional_magic_dll_or_non_executable_fails(self):
        for offset, value in ((0x84, 0x14c), (0x84, 0xaa64), (0x84, 0xa641),
                              (0x98, 0x10b), (0x98, 0),
                              (0x96, 0x2022), (0x96, 0x20), (0x96, 0x122)):
            data = synthetic_x64_pe()
            struct.pack_into('<H', data, offset, value)
            with self.subTest(offset=offset, value=value), self.assertRaises(RuntimeError):
                self.inspect(data)

    def test_bad_signatures_and_truncated_headers_fail(self):
        complete = synthetic_x64_pe()
        cases = [complete[:length] for length in (0, 63, 0x83, 0x97, 0x120, 0x19f, 0x54f, 0x7ff)]
        for offset, value in ((0, b'NO'), (0x80, b'PX\0\0')):
            data = synthetic_x64_pe(); data[offset:offset + len(value)] = value; cases.append(data)
        data = synthetic_x64_pe(); struct.pack_into('<I', data, 0x3c, 0xfffffff0); cases.append(data)
        data = synthetic_x64_pe(); struct.pack_into('<I', data, 0x3c, 0x20); cases.append(data)
        for index, data in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(RuntimeError):
                self.inspect(data)

    def test_bad_optional_directory_and_section_bounds_fail(self):
        for offset, fmt, value in ((0x86, '<H', 0), (0x86, '<H', 97),
                                   (0x94, '<H', 199), (0x98 + 108, '<I', 17),
                                   (0x98 + 60, '<I', 0x190), (0x98 + 60, '<I', 0x1000),
                                   (0x98 + 56, '<I', 0x300),
                                   (0x98 + 192, '<I', 0xfffffff0),
                                   (0x188 + 20, '<I', 0x300),
                                   (0x188 + 20, '<I', 0x700),
                                   (0x188 + 12, '<I', 0x100)):
            data = synthetic_x64_pe()
            struct.pack_into(fmt, data, offset, value)
            with self.subTest(offset=offset, value=value), self.assertRaises(RuntimeError):
                self.inspect(data)

    def test_unmapped_cross_section_or_zero_fill_loadconfig_is_rejected(self):
        cases = []
        for rva in (0x200, 0x1800, 0x13f0):
            data = synthetic_x64_pe(); struct.pack_into('<I', data, 0x98 + 192, rva); cases.append(data)
        data = synthetic_x64_pe()
        struct.pack_into('<I', data, 0x188 + 8, 0x800)  # larger virtual zero-fill tail
        struct.pack_into('<I', data, 0x98 + 192, 0x1500)
        cases.append(data)
        for index, data in enumerate(cases):
            with self.subTest(case=index), self.assertRaisesRegex(RuntimeError, 'file-backed'):
                self.inspect(data)

    def test_overlapping_section_rva_or_raw_aliases_are_rejected(self):
        for address, raw in ((0x1000, 0x800), (0x2000, 0x400)):
            data = synthetic_x64_pe() + bytes(0x400)
            struct.pack_into('<H', data, 0x86, 2)
            struct.pack_into('<I', data, 0x98 + 56, 0x3000)
            struct.pack_into('<IIII', data, 0x188 + 40 + 8, 0x400, address, 0x400, raw)
            with self.subTest(address=address, raw=raw), self.assertRaisesRegex(RuntimeError, 'overlapping'):
                self.inspect(data)

    def test_text_lookalike_cannot_replace_the_binary_field(self):
        data = synthetic_x64_pe(0) + b'\n0800 Dependent Load Flag\n0800 Dependent Load Flags\n'
        with self.assertRaisesRegex(RuntimeError, 'not established in PE'):
            self.inspect(data)

    def fake_compile(self, loadconfig_text, *, flags=0x800, environment=None):
        tools = self.root / 'tools'; tools.mkdir(exist_ok=True)
        for name in ('cl.exe', 'link.exe', 'dumpbin.exe'):
            (tools / name).write_bytes(b'unit tool provenance, not executable')
        include = self.root / 'Windows Kits/10/Include/10.0.26100.0/um/windows.h'
        include.parent.mkdir(parents=True, exist_ok=True); include.write_text('// synthetic SDK provenance')
        library = self.root / 'libcmt.lib'; library.write_bytes(b'unit library provenance')
        output = self.root / 'output'; output.mkdir(exist_ok=True)

        self.observed_compile_environments = []
        def closure(*args):
            self.observed_compile_environments.append(dict(args[-1]))
            return [], {'actual_header_paths': []}
        def run(command, **kwargs):
            self.observed_compile_environments.append(dict(kwargs['env']))
            if command[0] == str(tools / 'cl.exe'):
                (output / 'restore.exe').write_bytes(synthetic_x64_pe(flags))
                (output / 'source-dependencies.json').write_text(json.dumps({'Data': {'Includes': [str(include)]}}))
                return subprocess.CompletedProcess(command, 0, stdout=f'  Searching {library}:\n')
            text = {'/DEPENDENTS': '    KERNEL32.dll\n    BCRYPT.dll\n',
                    '/HEADERS': 'localized diagnostics; architecture checked from bytes instead',
                    '/LOADCONFIG': loadconfig_text}[command[1]]
            return subprocess.CompletedProcess(command, 0, stdout=text)

        with patch.object(builder, 'os', SimpleNamespace(name='nt', environ=dict(environment or {}), fstat=os.fstat)), \
                patch.object(builder.platform, 'machine', return_value='AMD64'), \
                patch.object(builder.shutil, 'which', side_effect=lambda name: str(tools / name)), \
                patch.object(builder.subprocess, 'run', side_effect=run), \
                patch.dict(sys.modules, {'zlib_build': SimpleNamespace(
                    compile_closure=closure,
                    inspect_link_map=lambda *a: {'synthetic_mock_not_native_proof': True}, verify_sources=lambda *a: None)}):
            return builder.compile_fixed(self.root, self.root, output)

    def test_compile_gate_ignores_singular_plural_and_localized_dumpbin_prose(self):
        for text in ('                0800 Dependent Load Flag\n',
                     '                0800 Dependent Load Flags\n',
                     'localized opaque diagnostic text\n', '0000 misleading diagnostic only\n'):
            with self.subTest(text=text):
                report = self.fake_compile(text)
                self.assertEqual(report['pe_dependency_policy']['dependent_load_flags'], 0x800)
                self.assertEqual((self.root / 'output/load-config.txt').read_text(), text)
                self.assertTrue((self.root / 'output/pe-headers.txt').is_file())
                self.assertTrue((self.root / 'output/imports.txt').is_file())

    def test_closure_and_restorer_compile_drop_inherited_option_injection(self):
        poison = {name: '/DUNREVIEWED /Fobad.obj gzwrite.obj' for name in ('CL', '_CL_', 'LINK', '_LINK_')}
        environment = {**poison, 'INCLUDE': 'selected-sdk', 'LIB': 'selected-libraries'}
        self.fake_compile('opaque text', environment=environment)
        self.assertGreaterEqual(len(self.observed_compile_environments), 5)
        for observed in self.observed_compile_environments:
            self.assertTrue(set(poison).isdisjoint(observed))
            self.assertEqual(observed['INCLUDE'], 'selected-sdk')
            self.assertEqual(observed['LIB'], 'selected-libraries')
        self.assertEqual(environment['CL'], poison['CL'])  # Parent process unchanged.

    def test_compile_gate_rejects_bad_binary_even_with_passing_dumpbin_text(self):
        with self.assertRaisesRegex(RuntimeError, 'not established in PE'):
            self.fake_compile('0800 Dependent Load Flags\n', flags=0x1800)
        self.assertTrue((self.root / 'output/load-config.txt').is_file())


if __name__ == '__main__': unittest.main()
