# SPDX-License-Identifier: Apache-2.0
"""Offline Python/header tests, not native Windows runtime certification."""
from __future__ import annotations
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

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


def fixture():
    return {'schema': 1, 'format': 'vse-zip-byte-volumes',
            'archive': {'name': 'vse-test.zip', 'root': 'vse-test', 'size': 3000000017,
                        'sha256': 'a' * 64, 'uncompressed_bytes': 6, 'identity': {'platform': 'windows', 'arch': 'x64'}},
            'parts': [{'name': 'vse-test.zip.part001', 'index': 1, 'offset': 0, 'size': 2000000000, 'sha256': 'b' * 64},
                      {'name': 'vse-test.zip.part002', 'index': 2, 'offset': 2000000000, 'size': 1000000017, 'sha256': 'c' * 64}],
            'validated_zip_entries': [{'name': 'vse-test/中文 &!%()/data.bin', 'size': 6, 'kind': 'file', 'sha256': 'd' * 64}]}


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


if __name__ == '__main__': unittest.main()
