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
        def rows(names):
            return '\n'.join(f' 0001:00000000 {name} 0000000140001000 f source.obj' for name in names)
        required = ['inflateInit2_', 'inflate', 'inflateEnd']
        seam = 'VseSyntheticGetDiskFreeSpaceExW'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'link-map.txt'
            path.write_text(rows(required))
            self.assertFalse(policy.inspect_link_map(path)['synthetic_space_shim_present'])
            with self.assertRaises(RuntimeError): policy.inspect_link_map(path, synthetic_space_fixture=True)
            path.write_text(rows(required + [seam]))
            with self.assertRaisesRegex(RuntimeError, 'Synthetic space symbols'):
                policy.inspect_link_map(path)
            self.assertTrue(policy.inspect_link_map(path, synthetic_space_fixture=True)['required_inflate_symbols_present'])
            for missing in required:
                path.write_text(rows([name for name in required if name != missing] + [seam]))
                with self.assertRaisesRegex(RuntimeError, 'raw-inflate'):
                    policy.inspect_link_map(path, synthetic_space_fixture=True)
            path.write_text(rows(required + [seam, 'gzwrite']))
            with self.assertRaisesRegex(RuntimeError, 'Excluded'):
                policy.inspect_link_map(path, synthetic_space_fixture=True)

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
