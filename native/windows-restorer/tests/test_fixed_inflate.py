# SPDX-License-Identifier: Apache-2.0
"""Portable raw-inflate/index/closure checks, not native Windows certification."""
import copy
import hashlib
import importlib.util
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
import zlib

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import fixed_index
import zlib_build
import generate_metadata


class ClosureTests(unittest.TestCase):
    def test_exact_official_source_closure_and_license(self):
        pin = zlib_build.verify_sources(HERE)
        self.assertEqual(pin['version'], '1.3.2')
        self.assertFalse(pin['source_modifications'])
        self.assertNotIn('gzwrite.c', pin['files'])
        self.assertNotIn('crc32.c', pin['files'])
        self.assertIn('LICENSE', pin['files'])
        self.assertEqual(pin['build_macros'], ['Z_SOLO', 'NO_GZIP'])
        self.assertIn('zlib-1.3.2/* -text', (HERE / 'third_party/.gitattributes').read_text())

    def test_windows_autocrlf_checkout_preserves_official_upstream_bytes(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / 'repository'; root.mkdir()
            exported = Path(name) / 'exported'; exported.mkdir()
            shutil.copytree(HERE / 'third_party', root / 'third_party')
            subprocess.run(['git', 'init', '-q', str(root)], check=True, capture_output=True)
            subprocess.run(['git', '-c', 'core.autocrlf=true', 'add', '.'], cwd=root, check=True, capture_output=True)
            subprocess.run(['git', '-c', 'core.autocrlf=true', 'checkout-index', '--all', '--prefix=' + str(exported) + '/'],
                           cwd=root, check=True, capture_output=True)
            self.assertEqual(zlib_build.verify_sources(exported)['upstream_archive_sha256'],
                             zlib_build.verify_sources(HERE)['upstream_archive_sha256'])

    def test_source_drift_or_added_gzip_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            path = root / zlib_build.RELATIVE_DIRECTORY
            shutil.copytree(HERE / zlib_build.RELATIVE_DIRECTORY, path)
            zlib_build.verify_sources(root)
            (path / 'inflate.c').write_bytes(b'changed')
            with self.assertRaisesRegex(RuntimeError, 'Changed upstream'):
                zlib_build.verify_sources(root)
            shutil.copyfile(HERE / zlib_build.RELATIVE_DIRECTORY / 'inflate.c', path / 'inflate.c')
            (path / 'gzwrite.c').write_text('must not compile')
            with self.assertRaisesRegex(RuntimeError, 'Unexpected files'):
                zlib_build.verify_sources(root)

    def test_object_symbol_and_final_link_map_guards(self):
        text = ' 001 00000000 SECT3 notype () External | inflate\n 002 00000000 UNDEF notype External | memset\n'
        self.assertEqual(zlib_build.defined_symbols(text), {'inflate'})
        for symbol in ('gzwrite', 'gz_vacate', 'deflateInit2_', 'crc32_combine64', 'unzOpen', 'zipOpen64'):
            with self.assertRaises(RuntimeError): zlib_build.assert_no_excluded_symbols({'inflate', symbol})
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'map.txt'
            valid = '\n'.join(f' 0001:00000000 {name} 0000000140001000 f zlib-inflate.obj' for name in ('inflate', 'inflateInit2_', 'inflateEnd'))
            path.write_text(valid)
            self.assertTrue(zlib_build.inspect_link_map(path)['required_inflate_symbols_present'])
            path.write_text(valid + '\n 0001:00000100 gzwrite 0000000140001100 f bad.obj\n')
            with self.assertRaises(RuntimeError): zlib_build.inspect_link_map(path)
            path.write_text('No actual symbol map')
            with self.assertRaises(RuntimeError): zlib_build.inspect_link_map(path)


class IndexTests(unittest.TestCase):
    def fixture(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('vse-test/plain', b'x')
            archive.writestr('vse-test/中文.txt', b'payload' * 20, compress_type=zipfile.ZIP_DEFLATED)
        data = stream.getvalue()
        document = {'schema': 1, 'format': 'vse-zip-byte-volumes',
                    'archive': {'name': 'vse-test.zip', 'root': 'vse-test', 'size': len(data),
                                'sha256': hashlib.sha256(data).hexdigest(), 'uncompressed_bytes': 141,
                                'identity': {'platform': 'windows', 'arch': 'x64'}},
                    'parts': [{'name': 'vse-test.zip.part001', 'index': 1, 'offset': 0, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}],
                    'validated_zip_entries': [{'name': 'vse-test/plain', 'kind': 'file', 'size': 1, 'sha256': hashlib.sha256(b'x').hexdigest()},
                                              {'name': 'vse-test/中文.txt', 'kind': 'file', 'size': 140, 'sha256': hashlib.sha256(b'payload' * 20).hexdigest()}]}
        return data, document

    def test_fixed_index_maps_exact_original_ranges_to_verified_inventory(self):
        data, document = self.fixture()
        index = fixed_index.inspect_stream(io.BytesIO(data), document)
        self.assertEqual(index['source_archive_sha256'], document['archive']['sha256'])
        self.assertEqual([row['method'] for row in index['files']], [0, 8])
        for row, expected in zip(index['files'], document['validated_zip_entries']):
            raw = data[row['data_offset']:row['data_offset'] + row['compressed_bytes']]
            plain = zlib.decompress(raw, -15) if row['method'] == 8 else raw
            self.assertEqual(hashlib.sha256(plain).hexdigest(), expected['sha256'])
        document['fixed_zip_index'] = index
        self.assertIn('central_directory_offset', generate_metadata.render_verified(document))

    def test_wrong_whole_hash_name_or_size_is_rejected(self):
        data, document = self.fixture()
        for mutate in (lambda d: d['archive'].update(sha256='0' * 64),
                       lambda d: d['validated_zip_entries'][0].update(name='vse-test/different'),
                       lambda d: d['validated_zip_entries'][0].update(size=2)):
            bad = copy.deepcopy(document); mutate(bad)
            with self.assertRaises(ValueError): fixed_index.inspect_stream(io.BytesIO(data), bad)

    def test_joined_volume_boundaries_and_changed_sizes_fail_closed(self):
        data, document = self.fixture()
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            document['parts'] = []
            for index, offset in enumerate(range(0, len(data), 37), 1):
                chunk = data[offset:offset + 37]
                row = {'name': f'vse-test.zip.part{index:03d}', 'index': index, 'offset': offset,
                       'size': len(chunk), 'sha256': hashlib.sha256(chunk).hexdigest()}
                (root / row['name']).write_bytes(chunk); document['parts'].append(row)
            expected = fixed_index.inspect_stream(io.BytesIO(data), document)
            self.assertEqual(fixed_index.from_verified_transport(root / 'unused-manifest.json', document), expected)
            for row in (document['parts'][0], document['parts'][-1]):
                part = root / row['name']; original = part.read_bytes()
                for changed in (original + b'UNEXPECTED TRAILING BYTES', original[:-1]):
                    part.write_bytes(changed)
                    with self.assertRaisesRegex(ValueError, 'changed while opening'):
                        fixed_index.from_verified_transport(root / 'unused-manifest.json', document)
                part.write_bytes(original)

    def test_renderer_rejects_overlap_unknown_methods_bounds_extra_and_missing(self):
        data, document = self.fixture()
        document['fixed_zip_index'] = fixed_index.inspect_stream(io.BytesIO(data), document)
        mutations = [lambda i: i.update(source_archive_sha256='0' * 64),
                     lambda i: i['files'][0].update(method=99),
                     lambda i: i['files'][1].update(data_offset=i['files'][0]['data_offset']),
                     lambda i: i['files'][0].update(data_offset=2 ** 63),
                     lambda i: i['files'][0].update(compressed_bytes=len(data)),
                     lambda i: i['files'].pop(),
                     lambda i: i['files'].append(copy.deepcopy(i['files'][0])),
                     lambda i: i['files'][1].update(name='vse-test/not-in-inventory')]
        for mutate in mutations:
            bad = copy.deepcopy(document); mutate(bad['fixed_zip_index'])
            with self.assertRaises(ValueError): generate_metadata.render_verified(bad)


class ServerFixtureTests(unittest.TestCase):
    def test_synthetic_corrupt_deflate_and_wrong_payload_hash_are_bound_not_accepted(self):
        spec = importlib.util.spec_from_file_location('fixture_server', HERE / 'tests/windows_server_tests.py')
        server = importlib.util.module_from_spec(spec); spec.loader.exec_module(server)
        with tempfile.TemporaryDirectory() as name:
            for index, options in enumerate(({}, {'invalid_deflate': True}, {'wrong_payload_hash': True})):
                directory = Path(name) / str(index)
                document = server.fixture(directory, deflated=True, **options)
                self.assertTrue(document['archive']['identity']['synthetic_fixture'])
                self.assertNotIn('receipt', document)
                raw = (directory / document['archive']['name']).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), document['archive']['sha256'])
                row = document['fixed_zip_index']['files'][0]
                compressed = raw[row['data_offset']:row['data_offset'] + row['compressed_bytes']]
                if options.get('invalid_deflate'):
                    with self.assertRaises(zlib.error): zlib.decompress(compressed, -15)
                else:
                    actual = hashlib.sha256(zlib.decompress(compressed, -15)).hexdigest()
                    self.assertEqual(actual == document['validated_zip_entries'][0]['sha256'], not options.get('wrong_payload_hash', False))
        source = (HERE / 'tests/windows_server_tests.py').read_text()
        for case in ('real-over-2gib-zip64', 'real-over-2gib-expanded-deflate',
                     'interruption-during-fixed-inflate-no-success', 'over-260-unicode-destination-cwd'):
            self.assertIn(case, source)
        self.assertIn('observed_partial_expanded_bytes', source)


@unittest.skipUnless(shutil.which('gcc') and shutil.which('g++'), 'portable C/C++ compilers unavailable')
class InflateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        zlib_build.verify_sources(HERE)
        objects = []
        for name in zlib_build.SOURCES:
            output = cls.root / (name + '.o')
            subprocess.run(['gcc', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', '-DZ_SOLO', '-DNO_GZIP', '-c', str(HERE / zlib_build.RELATIVE_DIRECTORY / name), '-o', str(output)], check=True)
            objects.append(str(output))
        cls.driver = cls.root / 'driver'
        subprocess.run(['g++', '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror', '-DZ_SOLO', '-DNO_GZIP', f'-I{HERE}', str(HERE / 'tests/inflate_driver.cpp'), *objects, '-o', str(cls.driver)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def run_raw(self, raw, expected, *, compressed=None, cancel=0):
        path = self.root / 'fixture.deflate'; path.write_bytes(raw)
        return subprocess.run([str(self.driver), str(path), str(len(raw) if compressed is None else compressed), str(expected), str(cancel)], capture_output=True)

    def test_valid_empty_random_repetitive_and_multibuffer_streams(self):
        import random
        for plain in (b'', b'hello', bytes(range(256)) * 10000, b'\0' * 3000000,
                      random.Random(5).randbytes(2 * 1024 * 1024 + 17)):
            raw = zlib.compress(plain)[2:-4]
            result = self.run_raw(raw, len(plain))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, plain)

    def test_truncated_trailing_invalid_sizes_and_cancel_are_fail_closed(self):
        plain = b'payload' * 100
        raw = zlib.compress(plain)[2:-4]
        for data, expected, compressed, cancel in (
                (raw[:-1], len(plain), None, 0), (raw + b'x', len(plain), None, 0),
                (b'\x07', 0, None, 0), (raw, len(plain) - 1, None, 0),
                (raw, len(plain) + 1, None, 0), (raw, len(plain), len(raw) + 1, 0),
                (raw, len(plain), None, 1), (b'', 0, None, 0)):
            with self.subTest(expected=expected, compressed=compressed, cancel=cancel):
                result = self.run_raw(data, expected, compressed=compressed, cancel=cancel)
                self.assertNotEqual(result.returncode, 0)
                self.assertLessEqual(len(result.stdout), expected)


if __name__ == '__main__': unittest.main()
