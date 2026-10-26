# SPDX-License-Identifier: Apache-2.0
"""Synthetic structure tests only; no claim about real CUDA ZIPs or Windows."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile
import zlib

SCRIPT = Path(__file__).resolve().parents[1] / 'inspect_zip_layout.py'
spec = importlib.util.spec_from_file_location('zip_layout_inspector', SCRIPT)
inspector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inspector)


def crafted(*, wide=False, descriptor=False, signature=True, method=0, flags=0, offset=0, name='root/中文 Ω.txt'):
    filename = name.encode('utf-8')
    payload = b'not model data\n' * 10
    compressed = zlib.compress(payload)[2:-4] if method == 8 else payload
    crc = zlib.crc32(payload)
    flags |= 0x800 | (8 if descriptor else 0)
    needed = 45 if wide else 20
    local_extra = struct.pack('<HHQQ', 1, 16, 0 if descriptor else len(payload), 0 if descriptor else len(compressed)) if wide else b''
    size_value = 0xffffffff if wide else (0 if descriptor else len(compressed))
    plain_value = 0xffffffff if wide else (0 if descriptor else len(payload))
    local = struct.pack('<4s5H3I2H', b'PK\x03\x04', needed, flags, method, 0, 0,
                        0 if descriptor else crc, size_value, plain_value, len(filename), len(local_extra)) + filename + local_extra
    dd = b''
    if descriptor:
        dd = (b'PK\x07\x08' if signature else b'') + struct.pack('<IQQ' if wide else '<III', crc, len(compressed), len(payload))
    central_offset = len(local) + len(compressed) + len(dd)
    central_extra = struct.pack('<HHQQQ', 1, 24, len(payload), len(compressed), offset) if wide else b''
    central = struct.pack('<4s6H3I5H2I', b'PK\x01\x02', needed, needed, flags, method, 0, 0, crc,
                          0xffffffff if wide else len(compressed), 0xffffffff if wide else len(payload),
                          len(filename), len(central_extra), 0, 0, 0, 0, 0xffffffff if wide else offset) + filename + central_extra
    ends = b''
    if wide:
        zip64_end = struct.pack('<4sQ2H2I4Q', b'PK\x06\x06', 44, needed, needed, 0, 0, 1, 1, len(central), central_offset)
        ends = zip64_end + struct.pack('<4sIQI', b'PK\x06\x07', 0, central_offset + len(central), 1)
    end = struct.pack('<4s4H2IH', b'PK\x05\x06', 0, 0, 0xffff if wide else 1, 0xffff if wide else 1,
                      0xffffffff if wide else len(central), 0xffffffff if wide else central_offset, 0)
    return local + compressed + dd + central + ends + end


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = self.root / '原ZIP &!%().zip'

    def inspect(self, raw):
        self.archive.write_bytes(raw)
        original_stat = self.archive.stat()
        result = inspector.inspect_archive(self.archive, hashlib.sha256(raw).hexdigest())
        self.assertEqual(self.archive.read_bytes(), raw)
        self.assertEqual(self.archive.stat().st_mtime_ns, original_stat.st_mtime_ns)
        return result

    def reject(self, raw, match=None):
        self.archive.write_bytes(raw)
        with self.assertRaisesRegex(inspector.LayoutError, match or '.'):
            inspector.inspect_archive(self.archive, hashlib.sha256(raw).hexdigest())

    def test_standard_library_store_deflate_unicode_and_force_zip64(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('root/ASCII.txt', b'one', compress_type=zipfile.ZIP_STORED)
            archive.writestr('root/中文 Ω.txt', b'two' * 100, compress_type=zipfile.ZIP_DEFLATED)
            with archive.open('root/force64.bin', 'w', force_zip64=True) as member:
                member.write(b'three')
        report = self.inspect(stream.getvalue())
        self.assertEqual(report['summary']['method_counts'], {'0': 2, '8': 1})
        self.assertEqual(report['summary']['local_zip64_entries'], 1)
        self.assertTrue(report['fixed_index_compression_compatible'])
        self.assertFalse(report['payload_decompressed'])
        self.assertFalse(report['client_restore_verified'])
        self.assertTrue(all(row['local_central_consistent'] for row in report['entries']))

    def test_all_descriptor_width_signature_and_method_combinations(self):
        for wide in (False, True):
            for signature in (False, True):
                for method in (0, 8):
                    with self.subTest(wide=wide, signature=signature, method=method):
                        report = self.inspect(crafted(wide=wide, descriptor=True, signature=signature, method=method))
                        row = report['entries'][0]
                        self.assertEqual(row['descriptor']['zip64'], wide)
                        self.assertEqual(row['descriptor']['signature_present'], signature)
                        self.assertEqual(report['end_records']['zip64'] is not None, wide)
                        self.assertEqual(report['summary']['descriptor_entries'], 1)

    def test_zip64_without_descriptor(self):
        report = self.inspect(crafted(wide=True, method=8))
        self.assertTrue(report['entries'][0]['central_zip64'])
        self.assertTrue(report['entries'][0]['local_zip64'])
        self.assertIsNone(report['entries'][0]['descriptor'])

    def test_unknown_compression_and_encryption_are_never_compatible(self):
        for kwargs, reason in (({'method': 99}, 'compression_method_not_store_or_deflate'),
                               ({'flags': 1}, 'encryption'), ({'flags': 16}, 'unsupported_general_purpose_flags')):
            with self.subTest(kwargs=kwargs):
                report = self.inspect(crafted(**kwargs))
                self.assertFalse(report['fixed_index_compression_compatible'])
                self.assertIn(reason, report['entries'][0]['unsupported_reasons'])

    def test_wrong_or_malformed_expected_sha(self):
        self.archive.write_bytes(crafted())
        for value in ('0' * 64, 'junk', 'a' * 65, ''):
            with self.subTest(value=value), self.assertRaises(inspector.LayoutError):
                inspector.inspect_archive(self.archive, value)

    def test_truncation_and_trailing_data(self):
        original = crafted(wide=True, descriptor=True)
        for raw in (original[:4], original[:-1], original + b'x'):
            self.reject(raw)

    def test_local_name_flags_method_version_crc_size_mismatch(self):
        raw = crafted()
        for offset in (4, 6, 8, 14, 18, 22, 30):
            with self.subTest(offset=offset):
                value = bytearray(raw); value[offset] ^= 1
                self.reject(value, 'disagree')

    def test_header_disagreement_has_exact_fields_offsets_and_raw_hex(self):
        raw = bytearray(crafted())
        central = raw.index(b'PK\x01\x02')
        struct.pack_into('<H', raw, central + 6, 45)
        self.archive.write_bytes(raw)
        with self.assertRaises(inspector.LayoutError) as caught:
            inspector.inspect_archive(self.archive, hashlib.sha256(raw).hexdigest())
        details = caught.exception.details
        self.assertEqual(details['entry_index'], 0)
        self.assertEqual(details['entry_name'], 'root/中文 Ω.txt')
        self.assertEqual(details['local_header_offset'], 0)
        self.assertEqual(details['central_header_offset'], central)
        self.assertEqual(details['local_fields']['version_needed'], 20)
        self.assertEqual(details['central_fields']['version_needed'], 45)
        self.assertEqual(details['differing_fields'], ['version_needed'])
        self.assertEqual(bytes.fromhex(details['local_fixed_header_hex']), raw[:30])
        self.assertEqual(bytes.fromhex(details['central_fixed_header_hex']), raw[central:central + 46])
        output = self.root / 'must-not-exist.json'
        result = subprocess.run([sys.executable, str(SCRIPT), '--archive', str(self.archive), '--expected-sha256', hashlib.sha256(raw).hexdigest(), '--output', str(output)], capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(output.exists())
        document = json.loads(result.stderr)
        self.assertEqual(document['details'], details)
        self.assertFalse(document['layout_accepted'])

    def offset_only_fixture(self):
        # Exercise the official writer's exact promotion path without allocating
        # a multi-GiB unit-test file. Real >2GiB remains a Server/native gate.
        stream = io.BytesIO()
        with patch.object(zipfile, 'ZIP64_LIMIT', 100):
            with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('root/padding', bytes(range(256)) * 2)
                archive.writestr('root/offset-only', b'abc')
        return stream.getvalue()

    def test_cpython_offset_only_zip64_promotes_central_version_not_local(self):
        report = self.inspect(self.offset_only_fixture())
        row = report['entries'][1]
        self.assertGreater(row['local_header_offset'], 100)
        self.assertEqual(row['version_needed'], 45)
        self.assertEqual(row['local_version_needed'], 20)
        self.assertEqual(row['central_zip64_fields'], ['local_header_offset'])
        self.assertEqual(row['local_extra_tags'], [])
        self.assertEqual(row['version_relation'], 'zip64_central_offset_only_20_to_45')

    def test_offset_promotion_does_not_hide_other_field_or_version_changes(self):
        raw = self.offset_only_fixture()
        row = self.inspect(raw)['entries'][1]
        for relative, value in ((4, 19), (4, 21), (6, 0x800), (8, 0), (10, 1), (12, 1)):
            with self.subTest(relative=relative, value=value):
                bad = bytearray(raw)
                struct.pack_into('<H', bad, row['local_header_offset'] + relative, value)
                self.reject(bad, 'fields disagree')
        bad = bytearray(raw); struct.pack_into('<H', bad, row['central_header_offset'] + 6, 46)
        self.reject(bad, 'fields disagree')

    def test_size_zip64_cannot_use_offset_only_version_exception(self):
        raw = bytearray(crafted(wide=True, method=8))
        struct.pack_into('<H', raw, 4, 20)
        self.reject(raw, 'fields disagree')

    def test_data_descriptor_mismatch(self):
        raw = bytearray(crafted(descriptor=True))
        position = raw.index(b'PK\x07\x08')
        for offset in (4, 8, 12):
            value = raw.copy(); value[position + offset] ^= 1
            self.reject(value, 'descriptor disagrees')

    def test_local_descriptor_placeholder_mismatch(self):
        raw = bytearray(crafted(descriptor=True))
        raw[14] = 1
        self.reject(raw, 'placeholders disagree')

    def test_zip64_end_record_count_offset_and_disk_mismatch(self):
        original = bytearray(crafted(wide=True))
        position = original.index(b'PK\x06\x06')
        for offset in (16, 24, 48):
            value = original.copy(); value[position + offset] ^= 1
            self.reject(value)

    def test_multidisk_and_impossible_central_count(self):
        original = bytearray(crafted())
        position = original.index(b'PK\x05\x06')
        for offset in (4, 6, 8, 10):
            value = original.copy(); value[position + offset] ^= 1
            self.reject(value)

    def test_bounds_and_overlap(self):
        self.reject(crafted(offset=1))
        original = bytearray(crafted())
        central = original.index(b'PK\x01\x02')
        struct.pack_into('<I', original, 18, 100000)
        struct.pack_into('<I', original, central + 20, 100000)
        self.reject(original, 'exceeds next record')

    def test_duplicate_names(self):
        stream = io.BytesIO()
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(stream, 'w') as archive:
                archive.writestr('same', b'a'); archive.writestr('same', b'b')
        self.reject(stream.getvalue(), 'Duplicate member name')

    def test_duplicate_and_truncated_extra_fields(self):
        for raw in (b'x', struct.pack('<HH', 1, 8) + b'x', struct.pack('<HHHH', 2, 0, 2, 0)):
            with self.assertRaises(inspector.LayoutError): inspector.extra_fields(raw)

    def test_non_ascii_cp437_name(self):
        value = bytearray(crafted(name='root/A.txt'))
        central = value.index(b'PK\x01\x02')
        struct.pack_into('<H', value, 6, 0); struct.pack_into('<H', value, central + 8, 0)
        value[35] = 0x82; value[central + 51] = 0x82
        self.assertEqual(self.inspect(value)['entries'][0]['name'], 'root/é.txt')

    def test_invalid_utf8_and_nul(self):
        for byte in (0, 0xff):
            value = bytearray(crafted(name='root/A.txt'))
            central = value.index(b'PK\x01\x02')
            value[central + 51] = byte
            self.reject(value)

    def test_no_decompression_or_content_in_report(self):
        raw = crafted()
        with patch.object(zlib, 'decompress', side_effect=AssertionError('must not decompress')):
            report = self.inspect(raw)
        self.assertNotIn('not model data', json.dumps(report))

    def test_second_full_hash_detects_changed_source(self):
        raw = crafted(); self.archive.write_bytes(raw)
        expected = hashlib.sha256(raw).hexdigest()
        with patch.object(inspector, 'digest', side_effect=[expected, '0' * 64]):
            with self.assertRaisesRegex(inspector.LayoutError, 'changed during inspection'):
                inspector.inspect_archive(self.archive, expected)

    def test_stat_fstat_different_ctime_semantics_are_not_false_mutations(self):
        raw = crafted(); self.archive.write_bytes(raw)
        original = self.archive.stat()
        def info(ctime):
            return SimpleNamespace(st_mode=original.st_mode, st_dev=original.st_dev, st_ino=original.st_ino,
                                   st_size=original.st_size, st_mtime_ns=original.st_mtime_ns,
                                   st_birthtime_ns=10, st_ctime_ns=ctime)
        with patch.object(Path, 'stat', return_value=info(100)), patch.object(inspector.os, 'fstat', return_value=info(200)), patch.object(Path, 'is_symlink', return_value=False):
            report = inspector.inspect_archive(self.archive, hashlib.sha256(raw).hexdigest())
        self.assertTrue(report['whole_sha256_verified_before_and_after'])

    def test_either_api_ctime_or_birthtime_mutation_is_rejected(self):
        raw = crafted(); self.archive.write_bytes(raw)
        original = self.archive.stat()
        def info(ctime, birthtime=10):
            return SimpleNamespace(st_mode=original.st_mode, st_dev=original.st_dev, st_ino=original.st_ino,
                                   st_size=original.st_size, st_mtime_ns=original.st_mtime_ns,
                                   st_birthtime_ns=birthtime, st_ctime_ns=ctime)
        for api in ('path', 'fd'):
            for field in ('ctime', 'birthtime'):
                with self.subTest(api=api, field=field):
                    paths, fds = [info(100), info(100)], [info(200), info(200)]
                    target = paths if api == 'path' else fds
                    target[1] = info(999, 10) if field == 'ctime' else info(100 if api == 'path' else 200, 999)
                    with patch.object(Path, 'stat', side_effect=paths), patch.object(inspector.os, 'fstat', side_effect=fds), patch.object(Path, 'is_symlink', return_value=False):
                        with self.assertRaisesRegex(inspector.LayoutError, 'metadata changed'):
                            inspector.inspect_archive(self.archive, hashlib.sha256(raw).hexdigest())

    def test_empty_archive_is_not_compatible_payload(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w'): pass
        report = self.inspect(stream.getvalue())
        self.assertFalse(report['fixed_index_compression_compatible'])

    def test_cli_output_is_exclusive_and_existing_archive_unchanged(self):
        raw = crafted(wide=True); self.archive.write_bytes(raw)
        output = self.root / 'report.json'
        args = [sys.executable, str(SCRIPT), '--archive', str(self.archive), '--expected-sha256', hashlib.sha256(raw).hexdigest(), '--output', str(output)]
        first = subprocess.run(args, capture_output=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        original = output.read_bytes()
        second = subprocess.run(args, capture_output=True)
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(output.read_bytes(), original)
        self.assertEqual(self.archive.read_bytes(), raw)

    def test_sha_failure_does_not_create_report(self):
        self.archive.write_bytes(crafted()); output = self.root / 'report.json'
        result = subprocess.run([sys.executable, str(SCRIPT), '--archive', str(self.archive), '--expected-sha256', '0' * 64, '--output', str(output)], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
