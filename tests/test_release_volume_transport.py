"""Synthetic accepted-ZIP fixtures only; no release or Windows restore proof."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import warnings
import zipfile

from scripts import release_volume_transport as transport
from scripts.release_guard import required_acceptance_checks


def digest(data):
    return hashlib.sha256(data).hexdigest()


@unittest.skipUnless(os.name == 'nt' or sys.platform.startswith('linux'),
                     'atomic create intentionally requires Windows or Linux')
class ReleaseVolumeTransportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='transport test ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.archive = self.root / 'vse-2.3.0-windows-x64-cuda11.8.zip'
        self.output = self.root / 'new transport'
        self.identity = {'commit': 'a' * 40, 'version': '2.3.0', 'platform': 'windows',
                         'arch': 'x64', 'build_profile': 'paddle-cuda11.8',
                         'ocr_backend': 'paddle', 'accelerator': 'cuda11.8', 'format': 2, 'python': '3.12'}
        self.payload = {'runtime/python.exe': b'unit fixture, not an executable\n' * 20,
                        'backend/models/chinese/model.bin': bytes(range(256)) * 3}
        self.write_archive()

    def write_archive(self, *, extra=(), zip64=False, manifest_change=None):
        bundle = {**self.identity, 'files': {name: digest(data) for name, data in self.payload.items()},
                  'synthetic_unit_fixture': True}
        if manifest_change:
            manifest_change(bundle)
        limit = 64 if zip64 else zipfile.ZIP64_LIMIT
        with patch.object(zipfile, 'ZIP64_LIMIT', limit), warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(self.archive, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(self.archive.stem + '/bundle-manifest.json', json.dumps(bundle))
                for name, data in self.payload.items():
                    archive.writestr(self.archive.stem + '/' + name, data)
                for name, data, attributes in extra:
                    entry = zipfile.ZipInfo(name)
                    entry.filename = entry.orig_filename = name  # retain raw bad names on Windows
                    entry.external_attr = attributes
                    archive.writestr(entry, data)
        self.sidecars()

    def sidecars(self):
        sha = digest(self.archive.read_bytes())
        self.archive.with_suffix('.zip.sha256').write_text(f'{sha}  {self.archive.name}\n', encoding='utf-8')
        receipt = {'schema': 1, 'archive': self.archive.name, 'archive_sha256': sha,
                   **{key: self.identity[key] for key in ('commit', 'version', 'platform', 'arch', 'build_profile')},
                   'checks': {key: 'passed' for key in required_acceptance_checks('paddle-cuda11.8')},
                   'gpu_inference_verified': False, 'gpu_validation': 'not-run-server-ci',
                   'synthetic_unit_fixture': True, 'note': 'Not real package acceptance evidence.'}
        self.archive.with_suffix('.zip.acceptance.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')

    def create(self, **kwargs):
        return transport.create_transport(self.archive, self.output, part_bytes=kwargs.pop('part_bytes', 251), **kwargs)

    def mutate(self, path, change):
        document = json.loads(path.read_bytes())
        change(document)
        path.write_text(json.dumps(document), encoding='utf-8')

    def test_roundtrip_preserves_original_zip_and_receipt_exact_bytes(self):
        original = self.archive.read_bytes()
        receipt = self.archive.with_suffix('.zip.acceptance.json').read_bytes()
        manifest = self.create()
        document = transport.verify_transport(manifest, include_entries=True)
        rebuilt = b''.join((self.output / part['name']).read_bytes() for part in document['parts'])
        self.assertEqual(rebuilt, original)
        self.assertEqual(self.archive.read_bytes(), original)
        self.assertEqual((self.output / document['receipt']['name']).read_bytes(), receipt)
        self.assertEqual(document['archive']['sha256'], digest(original))
        self.assertEqual(document['archive']['identity'], self.identity)
        self.assertFalse(document['client_restore_verified'])
        self.assertFalse(document['gpu_inference_verified'])
        entries = document['validated_zip_entries']
        self.assertEqual(len(entries), 3)
        for entry in entries:
            with zipfile.ZipFile(io.BytesIO(original)) as archive:
                self.assertEqual(entry['sha256'], digest(archive.read(entry['name'])))
        self.assertEqual(document['archive']['uncompressed_bytes'], sum(entry['size'] for entry in entries))
        self.assertEqual(list(self.output.glob('*.zip')), [], 'verify must not materialize another full ZIP')

    def test_zip64_small_fixture_includes_end_directory_and_roundtrips(self):
        self.write_archive(zip64=True)
        self.assertIn(b'PK\x06\x06', self.archive.read_bytes())
        self.assertIn(b'PK\x06\x07', self.archive.read_bytes())
        document = transport.verify_transport(self.create())
        self.assertEqual(document['archive']['size'], self.archive.stat().st_size)

    def test_default_part_size_is_below_release_limit(self):
        manifest = transport.create_transport(self.archive, self.output)
        document = transport.verify_transport(manifest)
        self.assertEqual(document['part_bytes'], 2_000_000_000)
        self.assertLess(document['part_bytes'], 2 ** 31)

    def test_bad_part_sizes_are_rejected_before_output(self):
        for size in (0, -1, True, 2 ** 31, 2 ** 31 + 1, '2000'):
            with self.subTest(size=size), self.assertRaisesRegex(RuntimeError, 'part size'):
                self.create(part_bytes=size)
        self.assertFalse(self.output.exists())

    def test_missing_acceptance_never_becomes_transport_ready(self):
        self.archive.with_suffix('.zip.acceptance.json').unlink()
        with self.assertRaises(RuntimeError):
            self.create()
        self.assertFalse(self.output.exists())

    def test_failed_old_or_gpu_receipt_cannot_authorize_transport(self):
        path = self.archive.with_suffix('.zip.acceptance.json')
        original = path.read_bytes()
        mutations = (lambda row: row['checks'].update(clean_host_path='failed'),
                     lambda row: row.update(archive_sha256='b' * 64),
                     lambda row: row.update(commit='b' * 40),
                     lambda row: row.update(gpu_inference_verified=True),
                     lambda row: row['checks'].pop('paddle_all_models_cpu_modes'))
        for change in mutations:
            path.write_bytes(original)
            self.mutate(path, change)
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                self.create()
            self.assertFalse(self.output.exists())

    def test_source_checksum_must_bind_exact_name(self):
        self.archive.with_suffix('.zip.sha256').write_text(digest(self.archive.read_bytes()) + '  old.zip\n')
        with self.assertRaisesRegex(RuntimeError, 'Source checksum'):
            self.create()

    def test_changed_part_bytes_or_size_fails(self):
        manifest = self.create()
        document = json.loads(manifest.read_bytes())
        path = self.output / document['parts'][0]['name']
        original = path.read_bytes()
        for data in (b'X' + original[1:], original[:-1], original + b'X'):
            path.write_bytes(data)
            with self.subTest(size=len(data)), self.assertRaisesRegex(RuntimeError, 'Volume checksum/size'):
                transport.verify_transport(manifest)

    def test_self_consistent_part_hash_cannot_change_original_zip_hash(self):
        manifest = self.create()
        document = json.loads(manifest.read_bytes())
        path = self.output / document['parts'][0]['name']
        data = b'X' + path.read_bytes()[1:]
        path.write_bytes(data)
        self.mutate(manifest, lambda row: row['parts'][0].update(sha256=digest(data)))
        with self.assertRaisesRegex(RuntimeError, 'Joined volume SHA'):
            transport.verify_transport(manifest)

    def test_manifest_rejects_order_names_offsets_sizes_and_unknown_fields(self):
        manifest = self.create()
        original = manifest.read_bytes()
        changes = (lambda row: row['parts'][0].update(name='../escape'),
                   lambda row: row['parts'][0].update(name='C:/absolute'),
                   lambda row: row['parts'][0].update(name='part:ads'),
                   lambda row: row['parts'][0].update(name='NUL.txt'),
                   lambda row: row['parts'][0].update(name=row['parts'][1]['name']),
                   lambda row: row['parts'][0].update(index=True),
                   lambda row: row['parts'][0].update(offset=1),
                   lambda row: row['parts'][0].update(size=2 ** 31),
                   lambda row: row['parts'].reverse(),
                   lambda row: row['parts'].pop(),
                   lambda row: row.update(extra='not accepted'),
                   lambda row: row.update(schema=True),
                   lambda row: row.update(client_restore_verified=True),
                   lambda row: row.update(gpu_inference_verified=True),
                   lambda row: row['disk'].update(required_free_bytes=0),
                   lambda row: row['archive']['identity'].update(commit='short'),
                   lambda row: row['archive'].update(root='other'))
        for index, change in enumerate(changes):
            manifest.write_bytes(original)
            self.mutate(manifest, change)
            with self.subTest(change=index), self.assertRaises(RuntimeError):
                transport.verify_transport(manifest)

    def test_extra_or_missing_files_fail(self):
        manifest = self.create()
        extra = self.output / 'unrelated.txt'
        extra.write_text('keep')
        with self.assertRaisesRegex(RuntimeError, 'missing or unexpected'):
            transport.verify_transport(manifest)
        extra.unlink()
        (self.output / json.loads(manifest.read_bytes())['parts'][-1]['name']).unlink()
        with self.assertRaisesRegex(RuntimeError, 'missing or unexpected'):
            transport.verify_transport(manifest)

    def test_original_receipt_hash_and_semantics_both_remain_required(self):
        manifest = self.create()
        document = json.loads(manifest.read_bytes())
        receipt = self.output / document['receipt']['name']
        self.mutate(receipt, lambda row: row.update(gpu_inference_verified=True))
        with self.assertRaisesRegex(RuntimeError, 'acceptance/checksum bytes'):
            transport.verify_transport(manifest)
        self.mutate(manifest, lambda row: row.update(receipt=transport._record(receipt, receipt.read_bytes())))
        with self.assertRaisesRegex(RuntimeError, 'GPU certification'):
            transport.verify_transport(manifest)

    def test_duplicate_json_keys_cannot_override_manifest_failure(self):
        manifest = self.create()
        manifest.write_bytes(manifest.read_bytes().replace(b'"schema": 1', b'"schema": 0, "schema": 1'))
        with self.assertRaisesRegex(RuntimeError, 'Duplicate JSON'):
            transport.verify_transport(manifest)

    def test_symlink_volume_and_ancestor_fail_even_when_target_bytes_match(self):
        manifest = self.create()
        document = json.loads(manifest.read_bytes())
        volume = self.output / document['parts'][0]['name']
        real = self.root / 'real-part'
        volume.rename(real)
        volume.symlink_to(real)
        with self.assertRaisesRegex(RuntimeError, 'Symlink/junction'):
            transport.verify_transport(manifest)
        volume.unlink()
        real.rename(volume)
        alias = self.root / 'alias'
        alias.symlink_to(self.output, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'Symlink/junction'):
            transport.verify_transport(alias / manifest.name)

    def test_windows_reparse_attribute_is_rejected_without_following(self):
        manifest = self.create()
        original = Path.lstat

        def reparse(path):
            info = original(path)
            if path == self.output:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            return info

        with patch.object(Path, 'lstat', reparse), self.assertRaisesRegex(RuntimeError, 'reparse'):
            transport.verify_transport(manifest)

    def test_existing_output_is_never_modified(self):
        self.output.mkdir()
        marker = self.output / 'user.txt'
        marker.write_text('keep')
        with self.assertRaisesRegex(RuntimeError, 'already exists'):
            self.create()
        self.assertEqual(marker.read_text(), 'keep')

    def test_atomic_publication_refuses_even_a_late_empty_directory(self):
        staging = self.root / 'completed staging'
        staging.mkdir()
        (staging / 'complete').write_text('verified')
        self.output.mkdir()
        original_inode = self.output.stat().st_ino
        with self.assertRaises(OSError):
            transport._publish_directory(staging, self.output)
        self.assertEqual(self.output.stat().st_ino, original_inode)
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual((staging / 'complete').read_text(), 'verified')

    def test_failure_before_publication_leaves_no_partial_final_output(self):
        with patch.object(transport, '_publish_directory', side_effect=RuntimeError('simulated interruption')):
            with self.assertRaisesRegex(RuntimeError, 'interruption'):
                self.create()
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob('.new transport.partial-*')), [])

    def test_interrupted_old_staging_is_not_deleted_or_published(self):
        abandoned = self.root / '.new transport.partial-old'
        abandoned.mkdir()
        (abandoned / 'unrelated').write_text('not this invocation')
        self.create()
        self.assertEqual((abandoned / 'unrelated').read_text(), 'not this invocation')

    def test_free_space_is_checked_before_writing_volumes(self):
        with patch.object(transport.shutil, 'disk_usage', return_value=SimpleNamespace(free=0)), \
                self.assertRaisesRegex(RuntimeError, 'Insufficient free disk'):
            self.create()
        self.assertFalse(self.output.exists())

    def test_disk_budget_counts_download_and_restore_separately(self):
        document = transport.verify_transport(self.create())
        budget = document['disk']
        size = document['archive']['size']
        expanded = document['archive']['uncompressed_bytes']
        self.assertEqual(budget['required_free_bytes'], size + expanded + budget['reserve_bytes'])
        self.assertEqual(budget['total_footprint_bytes'], 2 * size + expanded + budget['reserve_bytes'])

    def test_raw_unsafe_zip_names_are_rejected(self):
        for name in ('../escape', '/absolute', 'C:/absolute', 'other/file',
                     self.archive.stem + '/a/../b', self.archive.stem + '/a\\b',
                     self.archive.stem + '/a:stream', self.archive.stem + '/NUL.txt',
                     self.archive.stem + '/COM\u00b9.txt', self.archive.stem + '/COM0.txt',
                     self.archive.stem + '/LPT0', self.archive.stem + '/trail.',
                     self.archive.stem + '/trail ', self.archive.stem + '/LONGFI~1',
                     self.archive.stem + '/a\x00hidden'):
            self.write_archive(extra=[(name, b'x', 0)])
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, 'Unsafe'):
                self.create()
            self.assertFalse(self.output.exists())

    def test_zip_symlink_reparse_and_special_file_types_fail(self):
        for attributes in ((stat.S_IFLNK | 0o777) << 16, (stat.S_IFIFO | 0o600) << 16, 0x400):
            self.write_archive(extra=[(self.archive.stem + '/link', b'runtime/python.exe', attributes)])
            with self.subTest(attributes=attributes), self.assertRaisesRegex(RuntimeError, 'entry type'):
                self.create()

    def test_zip_duplicate_case_unicode_and_prefix_aliases_fail(self):
        pairs = [('new-name', 'NEW-NAME'), ('\u00e9/file', 'e\u0301/file'),
                 ('node', 'node/child')]
        for left, right in pairs:
            self.write_archive(extra=[(self.archive.stem + '/' + name, b'x', 0) for name in (left, right)])
            with self.subTest(left=left), self.assertRaisesRegex(RuntimeError, 'Duplicate|prefix conflict'):
                self.create()

    def test_encrypted_zip_metadata_is_rejected_before_extraction(self):
        raw = bytearray(self.archive.read_bytes())
        central = raw.find(b'PK\x01\x02')
        flags = struct.unpack_from('<H', raw, central + 8)[0]
        struct.pack_into('<H', raw, central + 8, flags | 1)
        self.archive.write_bytes(raw)
        self.sidecars()
        with self.assertRaisesRegex(RuntimeError, 'entry type'):
            self.create()

    def test_explicit_empty_directory_has_no_fabricated_file_hash(self):
        name = self.archive.stem + '/empty/'
        self.write_archive(extra=[(name, b'', (stat.S_IFDIR | 0o755) << 16)])
        report = transport.verify_transport(self.create(), include_entries=True)
        entry = next(row for row in report['validated_zip_entries'] if row['name'] == name)
        self.assertEqual(entry, {'name': name, 'kind': 'directory', 'size': 0})

    def test_zip_payload_must_match_inventory_even_with_synthetic_receipt(self):
        self.write_archive(manifest_change=lambda row: row['files'].pop('runtime/python.exe'))
        with self.assertRaisesRegex(RuntimeError, 'file inventory'):
            self.create()

    def test_zip_trailing_data_is_not_silently_accepted(self):
        with self.archive.open('ab') as stream:
            stream.write(b'trailing bytes')
        self.sidecars()
        with self.assertRaisesRegex(RuntimeError, 'trailing data'):
            self.create()

    def test_hostile_central_directory_size_is_bounded_before_zipfile_allocates(self):
        raw = bytearray(self.archive.read_bytes())
        end = raw.rfind(b'PK\x05\x06')
        struct.pack_into('<L', raw, end + 12, transport.MAX_METADATA_BYTES + 1)
        self.archive.write_bytes(raw)
        self.sidecars()
        with patch.object(transport.zipfile, 'ZipFile', side_effect=AssertionError('must reject before parsing')), \
                self.assertRaisesRegex(RuntimeError, 'central directory size'):
            self.create()

    def test_false_entry_count_cannot_hide_extra_central_directory_records(self):
        raw = bytearray(self.archive.read_bytes())
        end = raw.rfind(b'PK\x05\x06')
        struct.pack_into('<2H', raw, end + 8, 1, 1)
        self.archive.write_bytes(raw)
        self.sidecars()
        with patch.object(transport.zipfile, 'ZipFile', side_effect=AssertionError('must reject before parsing')), \
                self.assertRaisesRegex(RuntimeError, 'entry count/size'):
            self.create()

    def test_part_change_during_metadata_read_fails_closed(self):
        manifest = self.create()
        document = json.loads(manifest.read_bytes())
        part = self.output / document['parts'][0]['name']
        original = transport._inspect_zip

        def mutate_after_inspection(*args):
            result = original(*args)
            data = part.read_bytes()
            part.write_bytes(b'X' + data[1:])
            return result

        with patch.object(transport, '_inspect_zip', side_effect=mutate_after_inspection), \
                self.assertRaisesRegex(RuntimeError, 'changed during verification'):
            transport.verify_transport(manifest)

    def test_cli_reports_transport_only_not_windows_acceptance(self):
        output = io.StringIO()
        with patch('sys.stdout', output):
            result = transport.main(['create', '--archive', str(self.archive), '--output-dir', str(self.output)])
        self.assertEqual(result, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report['scope'], 'build-side-byte-transport-only')
        self.assertIs(report['client_restore_verified'], False)
        self.assertIs(report['gpu_inference_verified'], False)


class PortableTransportValidationTests(unittest.TestCase):
    """Pure schema/ZIP/volume validation also runs on macOS, without create.

    Make tiny synthetic transport bytes directly so unsupported atomic publish
    is not monkeypatched into an apparent macOS create/restore success.
    """
    setUp = ReleaseVolumeTransportTests.setUp
    write_archive = ReleaseVolumeTransportTests.write_archive
    sidecars = ReleaseVolumeTransportTests.sidecars
    mutate = ReleaseVolumeTransportTests.mutate

    def fixture(self):
        self.output.mkdir()
        raw = self.archive.read_bytes()
        parts = []
        for offset in range(0, len(raw), 251):
            data = raw[offset:offset + 251]
            name = f'{self.archive.name}.part{len(parts) + 1:03d}'
            (self.output / name).write_bytes(data)
            parts.append({'name': name, 'index': len(parts) + 1, 'offset': offset,
                          'size': len(data), 'sha256': digest(data)})
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            count = len(archive.infolist())
            expanded = sum(row.file_size for row in archive.infolist())
        sidecars = {}
        for key, suffix in (('receipt', '.zip.acceptance.json'), ('checksum', '.zip.sha256')):
            path = self.archive.with_suffix(suffix)
            data = path.read_bytes()
            (self.output / path.name).write_bytes(data)
            sidecars[key] = {'name': path.name, 'size': len(data), 'sha256': digest(data)}
        reserve = max(transport.RESERVE_BYTES, (len(raw) + expanded + 19) // 20)
        document = {'schema': 1, 'format': 'vse-zip-byte-volumes', 'part_bytes': 251, 'parts': parts,
                    'archive': {'name': self.archive.name, 'size': len(raw), 'sha256': digest(raw),
                                'root': self.archive.stem, 'entry_count': count,
                                'uncompressed_bytes': expanded, 'identity': self.identity},
                    **sidecars, 'gpu_inference_verified': False, 'client_restore_verified': False,
                    'disk': {'download_bytes': len(raw), 'merged_zip_bytes': len(raw),
                             'extracted_bytes': expanded, 'reserve_bytes': reserve,
                             'required_free_bytes': len(raw) + expanded + reserve,
                             'total_footprint_bytes': 2 * len(raw) + expanded + reserve}}
        manifest = self.output / (self.archive.name + '.transport.json')
        manifest.write_text(json.dumps(document), encoding='utf-8')
        return manifest

    def test_valid_cross_volume_zip_metadata_and_receipt(self):
        report = transport.verify_transport(self.fixture(), include_entries=True)
        self.assertEqual(report['archive']['identity'], self.identity)
        self.assertEqual(len(report['validated_zip_entries']), 3)
        self.assertFalse(report['client_restore_verified'])

    def test_zip64_validates_without_atomic_create(self):
        self.write_archive(zip64=True)
        report = transport.verify_transport(self.fixture())
        self.assertEqual(report['archive']['size'], self.archive.stat().st_size)

    def test_schema_rejects_duplicate_unknown_and_false_certification(self):
        path = self.fixture()
        original = path.read_bytes()
        for change in (lambda row: row.update(schema=True), lambda row: row.update(extra='unknown'),
                       lambda row: row.update(client_restore_verified=True),
                       lambda row: row['parts'][0].update(name='../escape')):
            path.write_bytes(original)
            self.mutate(path, change)
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                transport.verify_transport(path)
        path.write_bytes(original.replace(b'"schema": 1', b'"schema": 0, "schema": 1'))
        with self.assertRaisesRegex(RuntimeError, 'Duplicate JSON'):
            transport.verify_transport(path)

    def test_bad_volume_checksum_cannot_pass_on_any_host(self):
        path = self.fixture()
        document = json.loads(path.read_bytes())
        part = self.output / document['parts'][0]['name']
        part.write_bytes(b'wrong')
        with self.assertRaisesRegex(RuntimeError, 'Volume checksum/size'):
            transport.verify_transport(path)

    def test_bad_receipt_cannot_pass_on_any_host(self):
        path = self.fixture()
        document = json.loads(path.read_bytes())
        receipt = self.output / document['receipt']['name']
        self.mutate(receipt, lambda row: row['checks'].update(clean_host_path='failed'))
        self.mutate(path, lambda row: row.update(receipt=transport._record(receipt, receipt.read_bytes())))
        with self.assertRaisesRegex(RuntimeError, 'acceptance is missing or failed'):
            transport.verify_transport(path)

    def test_zip_unsafe_paths_and_devices_are_platform_independent(self):
        for component in ('../escape', '/absolute', 'NUL', 'CON .txt', 'COM\u00b2.dll', 'COM0', 'LPT0.txt',
                          'file:stream', 'longna~1', 'a\\b', 'a//', 'trailing ', 'a\x00hidden'):
            self.write_archive(extra=[(self.archive.stem + '/' + component, b'x', 0)])
            raw = self.archive.read_bytes()
            with self.subTest(component=component), self.assertRaises(RuntimeError):
                transport._inspect_zip(transport._SingleZip(io.BytesIO(raw), len(raw)), self.archive.name)

    def test_zip_alias_prefix_symlink_rules_are_platform_independent(self):
        cases = [ [('e\u0301', b'x', 0), ('\u00e9', b'x', 0)],
                  [('node', b'x', 0), ('node/child', b'x', 0)],
                  [('target', b'x', (stat.S_IFLNK | 0o777) << 16)],
                  [('target', b'x', 0x400)] ]
        for entries in cases:
            self.write_archive(extra=[(self.archive.stem + '/' + name, data, attributes)
                                      for name, data, attributes in entries])
            raw = self.archive.read_bytes()
            with self.subTest(entries=entries), self.assertRaises(RuntimeError):
                transport._inspect_zip(transport._SingleZip(io.BytesIO(raw), len(raw)), self.archive.name)

    def test_zip64_entry_count_is_bounded_before_entry_allocation(self):
        count = transport.MAX_ENTRIES + 1
        central = b'x'
        end64 = struct.pack('<4sQ2H2L4Q', b'PK\x06\x06', 44, 45, 45, 0, 0,
                            count, count, len(central), 0)
        locator = struct.pack('<4sLQL', b'PK\x06\x07', 0, len(central), 1)
        end = struct.pack('<4s4H2LH', b'PK\x05\x06', 0, 0, 65535, 65535, 1, 0, 0)
        raw = central + end64 + locator + end
        with self.assertRaisesRegex(RuntimeError, 'ZIP entry count'):
            transport._zip_directory(transport._SingleZip(io.BytesIO(raw), len(raw)))

    def test_zip_reader_refuses_unbounded_whole_archive_allocation(self):
        reader = transport._SingleZip(io.BytesIO(b''), transport.MAX_METADATA_BYTES + 1)
        with self.assertRaisesRegex(RuntimeError, 'bounded-memory'):
            reader.read()

    @unittest.skipIf(os.name == 'nt', 'Windows has its own native no-replace rename')
    def test_unsupported_create_host_fails_before_any_source_or_volume_io(self):
        with patch.object(transport.sys, 'platform', 'darwin'), \
                patch.object(transport, '_plain', side_effect=AssertionError('must not inspect any paths')), \
                patch.object(transport, '_read', side_effect=AssertionError('must not read source metadata')), \
                patch.object(transport, '_digest', side_effect=AssertionError('must not hash large ZIP')), \
                patch.object(transport.tempfile, 'mkdtemp', side_effect=AssertionError('must not stage volumes')), \
                self.assertRaisesRegex(RuntimeError, 'Windows/Linux only'):
            transport.create_transport(self.archive, self.output)
        self.assertFalse(self.output.exists())

    @unittest.skipIf(os.name == 'nt', 'Windows has its own native no-replace rename')
    def test_unsupported_create_host_fails_closed_without_any_publication(self):
        source = self.root / 'complete-staging'
        source.mkdir()
        with patch.object(transport.sys, 'platform', 'darwin'), \
                self.assertRaisesRegex(RuntimeError, 'Windows/Linux only'):
            transport._publish_directory(source, self.output)
        self.assertTrue(source.is_dir())
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
