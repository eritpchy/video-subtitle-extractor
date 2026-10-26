# SPDX-License-Identifier: Apache-2.0
"""Offline isolation checks; a mocked API report is not actual disk exhaustion."""
import hashlib
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


builder = load('space_isolation_builder', HERE / 'build.py')
policy = load('space_isolation_policy', HERE / 'zlib_build.py')
server = load('space_isolation_server', HERE / 'tests/windows_server_tests.py')


# Exact four raw map rows relayed from both Server artifacts for f6879d7,
# run 40289193558. These are excerpts, not fabricated full native build evidence.
SYNTHETIC_MAP_ROWS = """ 0001:00000000 000217c0H .text$mn                CODE
 0002:0000f388 000021f8H .xdata                  DATA
 0001:0000b5f8       VseSyntheticGetDiskFreeSpaceExW 000000014000c5f8 f   synthetic-space-api.obj
 0002:0000f3b4       $unwind$VseSyntheticGetDiskFreeSpaceExW 00000001400343b4     synthetic-space-api.obj
"""


def inflate_rows(names=('inflateInit2_', 'inflate', 'inflateEnd')):
    return '\n'.join(f' 0001:00000000 {name} 0000000140001000 f zlib-inflate.obj' for name in names) + '\n'


class SpaceIsolationTests(unittest.TestCase):
    def source_pair(self, directory):
        root = Path(directory)
        (root / 'restorer.cpp').write_text('// ordinary unit source')
        (root / 'bundle_metadata.hpp').write_text('// ordinary unit fixed header')
        return root

    def test_public_builder_refuses_seam_entry_macros_and_options_before_compilation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.source_pair(directory)
            for filename in ('restorer.cpp', 'bundle_metadata.hpp'):
                path = root / filename; original = path.read_bytes()
                for text in ('#define VSE_SYNTHETIC_SPACE_PREFLIGHT 1',
                             '#define GetDiskFreeSpaceExW VseSyntheticGetDiskFreeSpaceExW',
                             '#include "tests/space_preflight_driver.cpp"'):
                    path.write_text(text)
                    with patch.object(builder, '_compile_fixed_impl') as compile_:
                        with self.assertRaisesRegex(RuntimeError, 'refuses'):
                            builder.compile_fixed(root, root, root)
                        compile_.assert_not_called()
                path.write_bytes(original)
            for variable in ('CL', '_CL_', 'LINK', '_LINK_'):
                with patch.dict(builder.os.environ, {variable: '/DVSE_SYNTHETIC_SPACE_PREFLIGHT=1'}):
                    with self.assertRaisesRegex(RuntimeError, 'refuses'):
                        builder.compile_fixed(root, root, root)
            with self.assertRaises(TypeError):
                builder.compile_fixed(root, root, root, synthetic_space_fixture=True)
            with self.assertRaises(TypeError):
                builder.compile_fixed(root, root, root, entry_source='tests/space_preflight_driver.cpp')

    def test_public_default_calls_only_default_impl_and_rejects_test_record(self):
        normal = {'synthetic_space_fixture': False,
                  'restricted_zlib': {'final_link_map': {'synthetic_space_shim_present': False}}}
        with tempfile.TemporaryDirectory() as directory:
            root = self.source_pair(directory)
            with patch.object(builder, '_compile_fixed_impl', return_value=normal) as compile_:
                self.assertEqual(builder.compile_fixed(root, root, root), normal)
                compile_.assert_called_once_with(root, root, root)
            for bad in ({}, {**normal, 'synthetic_space_fixture': True},
                        {'synthetic_space_fixture': False, 'restricted_zlib': {'final_link_map': {'synthetic_space_shim_present': True}}}):
                with patch.object(builder, '_compile_fixed_impl', return_value=bad):
                    with self.assertRaisesRegex(RuntimeError, 'distribution'):
                        builder.compile_fixed(root, root, root)

    def test_private_test_entry_cannot_use_ordinary_header_or_unknown_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.source_pair(directory)
            with self.assertRaisesRegex(RuntimeError, 'synthetic fixture header'):
                builder._compile_fixed_impl(root, root, root, synthetic_space_fixture=True)
            with self.assertRaisesRegex(RuntimeError, 'Invalid build scope'):
                builder._compile_fixed_impl(root, root, root, synthetic_space_fixture='true')

    def test_complete_inflate_map_is_required_in_both_scopes_and_production_forbids_test_symbol(self):
        required = ['inflateInit2_', 'inflate', 'inflateEnd']
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'link-map.txt'
            path.write_text(inflate_rows())
            self.assertFalse(policy.inspect_link_map(path)['synthetic_space_shim_present'])
            with self.assertRaises(RuntimeError): policy.inspect_link_map(path, synthetic_space_fixture=True)
            path.write_text(inflate_rows() + SYNTHETIC_MAP_ROWS)
            with self.assertRaisesRegex(RuntimeError, 'Synthetic space symbols'):
                policy.inspect_link_map(path)
            record = policy.inspect_link_map(path, synthetic_space_fixture=True)
            self.assertTrue(record['required_inflate_symbols_present'])
            self.assertTrue(record['synthetic_space_shim_present'])
            self.assertEqual([row['role'] for row in record['synthetic_space_symbol_evidence']], ['function', 'unwind_metadata'])
            for missing in required:
                path.write_text(inflate_rows([name for name in required if name != missing]) + SYNTHETIC_MAP_ROWS)
                with self.assertRaisesRegex(RuntimeError, 'raw-inflate'):
                    policy.inspect_link_map(path, synthetic_space_fixture=True)
            path.write_text(inflate_rows(required + ['gzwrite']) + SYNTHETIC_MAP_ROWS)
            with self.assertRaisesRegex(RuntimeError, 'Excluded'):
                policy.inspect_link_map(path, synthetic_space_fixture=True)

    def test_exact_native_metadata_is_classified_separately_and_bound_to_its_object_group(self):
        result = policy.synthetic_space_symbol_evidence(SYNTHETIC_MAP_ROWS, enabled=True)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]['group_name'], '.text$mn')
        self.assertEqual(result[0]['group_class'], 'CODE')
        self.assertEqual(result[0]['offset'], 0xb5f8)
        self.assertEqual(result[1]['group_name'], '.xdata')
        self.assertEqual(result[1]['group_class'], 'DATA')
        self.assertEqual(result[1]['offset'], 0xf3b4)
        self.assertEqual(result[1]['group_start'], 0xf388)
        self.assertEqual(result[1]['group_length'], 0x21f8)
        self.assertTrue(all(row['object'] == 'synthetic-space-api.obj' for row in result))
        self.assertEqual([row['map_row'] for row in result], SYNTHETIC_MAP_ROWS.splitlines()[2:])

    def test_other_synthetic_unwind_functions_objects_duplicates_or_malformed_rows_are_rejected(self):
        extra = [
            ' 0002:0000f3b8 $unwind$VseSyntheticOther 00000001400343b8 synthetic-space-api.obj',
            ' 0001:0000b600 VseSyntheticOther 000000014000c600 f synthetic-space-api.obj',
            ' 0002:0000f3b8 $unwind$VseSyntheticGetDiskFreeSpaceExW_extra 00000001400343b8 synthetic-space-api.obj',
            ' VseSyntheticUnparsed',
            SYNTHETIC_MAP_ROWS.splitlines()[2],
            SYNTHETIC_MAP_ROWS.splitlines()[3],
        ]
        for row in extra:
            with self.subTest(row=row), self.assertRaises(RuntimeError):
                policy.synthetic_space_symbol_evidence(SYNTHETIC_MAP_ROWS + row + '\n', enabled=True)
        for replacement in ('other.obj', 'other.lib:synthetic-space-api.obj', 'VseSyntheticOther.obj'):
            with self.subTest(replacement=replacement), self.assertRaises(RuntimeError):
                policy.synthetic_space_symbol_evidence(SYNTHETIC_MAP_ROWS.replace('synthetic-space-api.obj', replacement), enabled=True)
        for line in SYNTHETIC_MAP_ROWS.splitlines()[2:]:
            with self.subTest(line=line), self.assertRaises(RuntimeError):
                policy.synthetic_space_symbol_evidence(SYNTHETIC_MAP_ROWS.replace(line + '\n', ''), enabled=True)
        # Production rejects the shim, metadata-only, and malformed seam names.
        for text in (SYNTHETIC_MAP_ROWS, SYNTHETIC_MAP_ROWS.splitlines()[3], 'VseSyntheticUnparsed'):
            with self.subTest(text=text), self.assertRaisesRegex(RuntimeError, 'forbidden in production'):
                policy.synthetic_space_symbol_evidence(text, enabled=False)

    def test_metadata_cannot_masquerade_as_code_or_escape_the_unique_xdata_range(self):
        cases = [
            SYNTHETIC_MAP_ROWS.replace(' f   synthetic-space-api.obj', '     synthetic-space-api.obj'),
            SYNTHETIC_MAP_ROWS.replace('00000001400343b4     synthetic-space-api.obj', '00000001400343b4 f   synthetic-space-api.obj'),
            SYNTHETIC_MAP_ROWS.replace('.xdata                  DATA', '.text                   CODE'),
            SYNTHETIC_MAP_ROWS.replace('.xdata                  DATA', '.xdata                  CODE'),
            SYNTHETIC_MAP_ROWS.replace('.text$mn                CODE', '.text$mn                DATA'),
            SYNTHETIC_MAP_ROWS.replace('0002:0000f3b4', '0001:0000f3b4'),
            SYNTHETIC_MAP_ROWS.replace('0002:0000f3b4', '0002:0000f387'),
            SYNTHETIC_MAP_ROWS.replace('0002:0000f3b4', '0002:00011580'),  # exact end, excluded
            SYNTHETIC_MAP_ROWS.replace('000021f8H', '00000000H'),
            SYNTHETIC_MAP_ROWS + SYNTHETIC_MAP_ROWS.splitlines()[1] + '\n',
            SYNTHETIC_MAP_ROWS + ' 0002:0000f3b4 00000004H .xdata DATA\n',
            '\n'.join(SYNTHETIC_MAP_ROWS.splitlines()[1:]),
        ]
        for index, text in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(RuntimeError):
                policy.synthetic_space_symbol_evidence(text, enabled=True)

    def test_space_zip_remains_small_real_and_internally_consistent(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'fixture'
            document = server.fixture(target, deflated=True)
            self.assertEqual(document['archive']['uncompressed_bytes'], 8 * 1024 * 1024)
            archive = target / document['archive']['name']
            self.assertLess(archive.stat().st_size, 128 * 1024)
            self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), document['archive']['sha256'])
            with zipfile.ZipFile(archive) as zipped:
                entry = zipped.infolist()[0]
                self.assertEqual(entry.compress_type, zipfile.ZIP_DEFLATED)
                self.assertEqual(entry.file_size, document['validated_zip_entries'][0]['size'])
                self.assertEqual(hashlib.sha256(zipped.read(entry)).hexdigest(), document['validated_zip_entries'][0]['sha256'])
            header = (target / 'bundle_metadata.hpp').read_text()
            self.assertIn('uncompressed_bytes = 8388608ULL;', header)
            self.assertNotIn('9223372036854775807', header)
            self.assertNotIn('VSE_SYNTHETIC_SPACE_PREFLIGHT', header)
            with self.assertRaises(TypeError): server.fixture(Path(directory) / 'old', impossible_space=True)

    def test_test_tus_are_separate_non_ltcg_and_default_production_source_is_unchanged(self):
        driver = (HERE / 'tests/space_preflight_driver.cpp').read_text()
        api = (HERE / 'tests/space_preflight_api.cpp').read_text()
        production = (HERE / 'restorer.cpp').read_text()
        build = (HERE / 'build.py').read_text()
        self.assertIn('#include "../restorer.cpp"', driver)
        self.assertLess(driver.index('#include <windows.h>'), driver.index('#define GetDiskFreeSpaceExW'))
        self.assertIn('extern "C" BOOL WINAPI VseSyntheticGetDiskFreeSpaceExW', driver)
        self.assertIn('if (!GetDiskFreeSpaceExW(directory, available, total, free_bytes)) return FALSE;', api)
        self.assertIn('available->QuadPart = 0;', api)
        self.assertIn('std::fwprintf', api)  # Same wide stderr orientation as restorer.
        self.assertNotIn('VseSynthetic', production)
        self.assertNotIn('VSE_SYNTHETIC', production)
        self.assertIn("'/GL-'", build)
        self.assertIn("command.append('/LTCG:OFF')", build)
        self.assertNotIn('/OPT:NOREF', build)
        self.assertIn('synthetic-space-api-command-record.json', build)
        self.assertIn('synthetic-space-api-object-record.json', build)
        self.assertIn('compile-command-record.json', build)
        self.assertIn('synthetic_translation_units', build)
        self.assertIn('assert_distributable_record(record)', build)
        source = (HERE / 'tests/windows_server_tests.py').read_text()
        self.assertIn("b'Insufficient free space:' in stderr", source)
        self.assertIn("not list(destination.glob('.vse-incomplete-*'))", source)
        self.assertIn('real_disk_exhaustion_tested=False', source)


if __name__ == '__main__': unittest.main()
