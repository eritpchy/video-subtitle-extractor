"""Synthetic offline seed contracts, never successful final-ZIP/OCR evidence.

Only the pins trust digest/path are replaced for tiny synthetic model bytes;
production has no switch to replace or relax its fixed 45-file hashes.
"""
import contextlib
import copy
import errno
import hashlib
import io
import json
from pathlib import Path
import stat
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import warnings
import zipfile

from scripts import paddle_model_seed as seed
from scripts import paddle_package_models as models


SHA = 'a' * 40
RUN = '40259119763'


def digest(data):
    return hashlib.sha256(data).hexdigest()


class PaddleSeedTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='vse-synthetic-seed-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.output = self.root / 'new seed'
        self.archive = self.root / 'vse-2.3.0-windows-x64-cpu.zip'
        self.checksum = self.archive.with_suffix('.zip.sha256')
        self.acceptance = self.archive.with_suffix('.zip.acceptance.json')
        self.source = self.root / 'synthetic-models'
        pins = json.loads(models.BUILD_PINS.read_bytes())
        for row in pins['models']:
            row['archive_bytes'] = 65536
            for name in row['files']:
                data = ('synthetic only: ' + row['name'] + '/' + name + '\n').encode()
                row['files'][name] = digest(data)
                path = self.source / models._model_path(row['name']) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        self.pin_data = models._canonical(pins)
        self.pin_file = self.root / 'synthetic-pins.json'
        self.pin_file.write_bytes(self.pin_data)
        for patcher in (patch.object(models, 'PINNED_SOURCE_SHA256', digest(self.pin_data)),
                        patch.object(models, 'BUILD_PINS', self.pin_file)):
            patcher.start()
            self.addCleanup(patcher.stop)
        (self.source / models.PINS_NAME).write_bytes(self.pin_data)
        self.inventory = models._inventory(self.source, pins)
        (self.source / models.INVENTORY_NAME).write_bytes(models._canonical(self.inventory))
        self.model_files = {path.relative_to(self.source).as_posix(): path.read_bytes()
                            for path in self.source.rglob('*') if path.is_file()}
        self.members = {'backend/models/' + name: data for name, data in self.model_files.items()}
        self.members['runtime/python.exe'] = b'must never be opened or extracted'
        self.manifest = {'format': 2, 'python': '3.12', 'commit': SHA, 'version': '2.3.0',
                         'platform': 'windows', 'arch': 'x64', 'build_profile': 'paddle-cpu',
                         'ocr_backend': 'paddle', 'accelerator': 'cpu', 'gpu_inference_verified': False,
                         'paddle_models': self.inventory,
                         'files': {name: digest(data) for name, data in self.members.items()}}
        self.write_archive()

    def write_sidecars(self):
        self.checksum.write_bytes((seed.guard.sha256(self.archive) + '  ' + self.archive.name + '\n').encode())
        self.receipt = {'schema': 1, 'archive': self.archive.name,
                        'archive_sha256': seed.guard.sha256(self.archive),
                        **{key: self.manifest[key] for key in ('commit', 'version', 'platform', 'arch', 'build_profile')},
                        'checks': {name: 'passed' for name in seed.guard.required_acceptance_checks(self.manifest['build_profile'])},
                        'gpu_inference_verified': False, 'gpu_validation': 'not-run-server-ci',
                        'scope': seed.FINAL_SCOPE,
                        'evidence': {f'package/test-results/{self.archive.stem}/paddle-models/report.json':
                                     {'sha256': 'b' * 64, 'bytes': 1000}}}
        self.write_receipt()

    def write_receipt(self):
        self.acceptance.write_bytes(models._canonical(self.receipt))

    def write_archive(self, extras=(), manifest_data=None):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(self.archive, 'w', zipfile.ZIP_STORED) as archive:
                archive.writestr(self.archive.stem + '/bundle-manifest.json',
                                 manifest_data or models._canonical(self.manifest))
                for name, data in self.members.items():
                    archive.writestr(self.archive.stem + '/' + name, data)
                for entry, data in extras:
                    if isinstance(entry, str):
                        name = entry
                        entry = zipfile.ZipInfo(name)
                        # Preserve hostile raw names on Windows as well.
                        entry.filename = entry.orig_filename = name
                    archive.writestr(entry, data)
        self.write_sidecars()

    def run_seed(self, **kwargs):
        with patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')), \
             patch('scripts.release_guard.urlopen', side_effect=AssertionError('network forbidden')):
            return seed.seed_models(self.archive, self.checksum, self.acceptance, self.output,
                                    source_sha=kwargs.get('source_sha', SHA), artifact_run=kwargs.get('artifact_run', RUN),
                                    archive_sha256=kwargs.get('archive_sha256', seed.guard.sha256(self.archive)),
                                    receipt_sha256=kwargs.get('receipt_sha256', seed.guard.sha256(self.acceptance)))

    def anchors(self):
        return dict(source_sha=SHA, artifact_run=RUN, archive_sha256=seed.guard.sha256(self.archive),
                    receipt_sha256=seed.guard.sha256(self.acceptance))

    def rejected(self, pattern=None):
        if pattern:
            with self.assertRaisesRegex((RuntimeError, ValueError, zipfile.BadZipFile), pattern):
                self.run_seed()
        else:
            with self.assertRaises((RuntimeError, ValueError, zipfile.BadZipFile)):
                self.run_seed()
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob('.paddle-seed-*')))

    def test_only_exact_models_are_streamed_and_atomic_seed_records_honest_provenance(self):
        opened = []
        original = zipfile.ZipFile.open
        def tracked(archive, name, *args, **kwargs):
            member = name.filename if isinstance(name, zipfile.ZipInfo) else name
            opened.append(member)
            self.assertNotIn('/runtime/', member)
            return original(archive, name, *args, **kwargs)
        with patch.object(zipfile.ZipFile, 'open', new=tracked), \
             patch.object(zipfile.ZipFile, 'extractall', side_effect=AssertionError('no broad extraction')), \
             patch.object(zipfile.ZipFile, 'extract', side_effect=AssertionError('no broad extraction')):
            report = self.run_seed()
        self.assertEqual(len(opened), 48)  # manifest + 45 source files + 2 model metadata
        self.assertEqual(models.verify_models(self.output / 'models'), self.inventory)
        self.assertEqual({path.name for path in self.output.iterdir()}, {'models', 'provenance.json'})
        self.assertEqual(report, json.loads((self.output / 'provenance.json').read_bytes()))
        self.assertEqual(report['source_sha'], SHA)
        self.assertEqual(report['artifact_run'], RUN)
        self.assertEqual(report['receipt_sha256'], digest(self.acceptance.read_bytes()))
        self.assertEqual(report['archive_sha256'], seed.guard.sha256(self.archive))
        self.assertEqual(report['model_count'], 15)
        self.assertFalse(report['runtime_included'])
        self.assertFalse(report['inference_executed'])
        self.assertFalse(report['gpu_inference_verified'])
        self.assertIn('not a signed attestation', report['provenance_limit'])

    def test_archive_is_never_read_with_path_read_bytes(self):
        original = Path.read_bytes
        def read_bytes(path):
            self.assertNotEqual(path, self.archive)
            return original(path)
        with patch.object(Path, 'read_bytes', new=read_bytes):
            self.run_seed()

    def test_newer_consumer_or_wrong_source_sha_does_not_relabel_original(self):
        with self.assertRaisesRegex(RuntimeError, 'specified Windows Paddle CPU'):
            self.run_seed(source_sha='c' * 40)
        self.assertFalse(self.output.exists())

    def test_source_identity_and_runtime_must_be_exact(self):
        original = copy.deepcopy(self.manifest)
        for field, value in (('platform', 'linux'), ('arch', 'arm64'), ('ocr_backend', 'native'),
                             ('build_profile', 'paddle-cuda11.8'), ('accelerator', 'cuda11.8'),
                             ('format', 1), ('python', '3.11'), ('gpu_inference_verified', True),
                             ('version', '2.2.0')):
            with self.subTest(field=field):
                self.manifest = {**original, field: value}
                self.write_archive()
                self.rejected()

    def test_invalid_explicit_provenance_identifiers_are_rejected(self):
        for kwargs in ({'source_sha': 'short'}, {'artifact_run': ''}, {'artifact_run': '1/../../x'}, {'artifact_run': 1},
                       {'archive_sha256': 'short'}, {'receipt_sha256': 'F' * 64}, {'receipt_sha256': None}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.run_seed(**kwargs)

    def test_matching_mutable_sidecars_cannot_replace_external_hash_anchors(self):
        for field in ('archive_sha256', 'receipt_sha256'):
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, 'externally trusted SHA256'):
                self.run_seed(**{field: '0' * 64})
            self.assertFalse(self.output.exists())

    def test_checksum_mismatch_or_wrong_filename_fails_before_member_reads(self):
        for data in (b'0' * 64 + b'  wrong.zip\n',
                     (seed.guard.sha256(self.archive) + '  wrong.zip\n').encode()):
            self.checksum.write_bytes(data)
            with patch.object(zipfile.ZipFile, 'open', side_effect=AssertionError('not reached')):
                self.rejected('checksum/name')

    def test_receipt_identity_checks_scope_and_evidence_are_required(self):
        original = copy.deepcopy(self.receipt)
        mutations = (lambda value: value.update(commit='c' * 40),
                     lambda value: value['checks'].pop('paddle_all_models_cpu_modes'),
                     lambda value: value['checks'].update(cpu_mode_auto='failed'),
                     lambda value: value.update(gpu_inference_verified=True),
                     lambda value: value.update(scope='source'),
                     lambda value: value.update(evidence={}))
        for change in mutations:
            with self.subTest(change=change):
                self.receipt = copy.deepcopy(original)
                change(self.receipt)
                self.write_receipt()
                self.rejected()

    def test_duplicate_json_keys_cannot_override_identity(self):
        self.acceptance.write_bytes(b'{"schema":0,"schema":1}')
        self.rejected('Duplicate JSON')
        self.write_archive(manifest_data=b'{"commit":"old","commit":"new"}')
        self.rejected('Duplicate JSON')

    def test_missing_or_extra_model_files_are_rejected(self):
        first = next(name for name in self.members if name.endswith('inference.json'))
        data = self.members.pop(first)
        self.write_archive()
        self.rejected('exactly 45')
        self.members[first] = data
        self.members['backend/models/extra.txt'] = b'extra'
        self.write_archive()
        self.rejected('exactly 45')

    def test_self_consistent_archive_and_inventory_cannot_replace_fixed_model_hashes(self):
        first = next(name for name in self.members if name.endswith('inference.pdiparams'))
        self.members[first] = b'forged model'
        self.manifest['files'][first] = digest(self.members[first])
        self.write_archive()
        self.rejected('Fixed original model hash')

    def test_self_certified_pins_and_noncanonical_inventory_are_rejected(self):
        for name in (models.PINS_NAME, models.INVENTORY_NAME):
            key = 'backend/models/' + name
            old = self.members[key]
            self.members[key] = old + b' '
            self.manifest['files'][key] = digest(self.members[key])
            self.write_archive()
            self.rejected('Fixed original model hash|inventory differs')
            self.members[key] = old
            self.manifest['files'][key] = digest(old)

    def test_bundle_model_inventory_and_payload_hashes_must_match(self):
        self.manifest['paddle_models'] = {}
        self.write_archive()
        self.rejected('inventory does not match')
        self.manifest['paddle_models'] = self.inventory
        key = next(iter(self.manifest['files']))
        self.manifest['files'][key] = '0' * 64
        self.write_archive()
        self.rejected('manifest model hash')

    def test_raw_unsafe_names_case_collisions_and_directory_confusion_are_rejected(self):
        root = self.archive.stem
        for name in (root + '/../../escape', '/absolute', root + '/runtime\\escape',
                     root + '/C:escape', root + '/runtime/CON.txt', root + '/runtime/name.',
                     root + '/runtime/name ', root + '//double', root + '/runtime/python.exe/',
                     root + '/runtime/null\x00hidden',
                     root + '/RUNTIME/PYTHON.EXE', root + '/runtime/python.exe/child'):
            with self.subTest(name=name):
                self.write_archive([(name, b'')])
                self.rejected('Unsafe|colliding|collision')

    def test_duplicate_manifest_and_unexpected_empty_model_directory_are_rejected(self):
        self.write_archive([(self.archive.stem + '/bundle-manifest.json', b'{}')])
        self.rejected('Duplicate')
        self.write_archive([(self.archive.stem + '/nested/bundle-manifest.json', b'{}')])
        self.rejected('unique source bundle manifest')
        self.write_archive([(self.archive.stem + '/backend/models/extra/', b'')])
        self.rejected('Unexpected model directory')

    def test_symlink_special_file_and_conflicting_directory_attributes_are_rejected(self):
        for mode, name in ((stat.S_IFLNK, 'runtime/link'), (stat.S_IFIFO, 'runtime/fifo'),
                           (stat.S_IFDIR, 'runtime/false-file'), (stat.S_IFREG, 'runtime/false-dir/')):
            with self.subTest(mode=mode):
                entry = zipfile.ZipInfo(self.archive.stem + '/' + name)
                entry.create_system = 3
                entry.external_attr = (mode | 0o755) << 16
                self.write_archive([(entry, b'')])
                self.rejected('Unsafe source ZIP')

    def test_model_and_metadata_size_bounds_fail_without_reading_payload(self):
        first = next(name for name in self.members if name.endswith('inference.json'))
        self.members[first] = b'x' * 65537
        self.write_archive()
        self.rejected('Oversized')
        self.members[first] = b'small but wrong'
        self.write_archive(manifest_data=b' ' * (seed.MAX_MANIFEST_BYTES + 1))
        self.rejected('Oversized')

    def test_actual_zip_crc_corruption_is_not_hidden_by_matching_outer_hash(self):
        with zipfile.ZipFile(self.archive) as archive:
            name = next(name for name in archive.namelist() if name.endswith('inference.pdiparams'))
            offset = archive.getinfo(name).header_offset
        with self.archive.open('r+b') as stream:
            stream.seek(offset)
            header = stream.read(30)
            filename_size, extra_size = struct.unpack_from('<HH', header, 26)
            stream.seek(offset + 30 + filename_size + extra_size)
            first = stream.read(1)
            stream.seek(-1, 1)
            stream.write(bytes([first[0] ^ 0x01]))
        self.write_sidecars()
        self.rejected('CRC')

    def test_encrypted_member_is_rejected_before_reading_it(self):
        data = bytearray(self.archive.read_bytes())
        central = data.index(b'PK\x01\x02')
        flags = struct.unpack_from('<H', data, central + 8)[0]
        struct.pack_into('<H', data, central + 8, flags | 1)
        self.archive.write_bytes(data)
        self.write_sidecars()
        with patch.object(zipfile.ZipFile, 'open', side_effect=AssertionError('not reached')):
            self.rejected('Unsafe source ZIP')

    def test_existing_destination_and_symlink_inputs_are_not_overwritten_or_followed(self):
        self.output.mkdir()
        sentinel = self.output / 'user-file'
        sentinel.write_bytes(b'keep')
        with self.assertRaises(FileExistsError):
            self.run_seed()
        self.assertEqual(sentinel.read_bytes(), b'keep')
        sentinel.unlink()
        self.output.rmdir()
        alias = self.root / 'archive-alias.zip'
        try:
            alias.symlink_to(self.archive)
        except OSError as error:
            self.skipTest('Symlink creation unavailable: ' + str(error))
        with self.assertRaisesRegex(RuntimeError, 'unsafe release asset'):
            seed.seed_models(alias, self.checksum, self.acceptance, self.output, source_sha=SHA, artifact_run=RUN,
                             archive_sha256=seed.guard.sha256(self.archive), receipt_sha256=seed.guard.sha256(self.acceptance))

    def test_concurrent_output_or_source_mutation_prevents_publication(self):
        original = models.verify_models
        def create_output(path):
            result = original(path)
            self.output.mkdir()
            (self.output / 'keep').write_bytes(b'user data')
            return result
        with patch.object(models, 'verify_models', side_effect=create_output), self.assertRaises(FileExistsError):
            self.run_seed()
        self.assertEqual((self.output / 'keep').read_bytes(), b'user data')
        (self.output / 'keep').unlink()
        self.output.rmdir()
        for source in (self.archive, self.checksum, self.acceptance):
            with self.subTest(source=source):
                saved = source.read_bytes()
                def mutate(path):
                    result = original(path)
                    with source.open('ab') as stream:
                        stream.write(b'changed')
                    return result
                with patch.object(models, 'verify_models', side_effect=mutate):
                    self.rejected('changed during seed')
                source.write_bytes(saved)

    def test_even_a_late_empty_destination_is_not_replaced(self):
        original = seed._publish_directory
        identity = None
        def create_late_empty_directory(source, destination):
            nonlocal identity
            destination.mkdir()  # After the final _absent check, immediately before rename.
            identity = destination.stat().st_ino
            return original(source, destination)
        with patch.object(seed, '_publish_directory', side_effect=create_late_empty_directory):
            with self.assertRaises(FileExistsError):
                self.run_seed()
        self.assertTrue(self.output.is_dir())
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual(self.output.stat().st_ino, identity)
        self.assertFalse(list(self.root.glob('.paddle-seed-*')))

    def test_darwin_exclusive_rename_uses_official_flag_without_fallback(self):
        source, destination = self.root / 'source', self.root / 'destination'
        function = Mock(return_value=0)
        library = SimpleNamespace(renamex_np=function)
        with patch.object(seed.os, 'name', 'posix'), patch.object(seed.sys, 'platform', 'darwin'), \
             patch.object(seed.ctypes, 'CDLL', return_value=library), \
             patch.object(seed.os, 'rename', side_effect=AssertionError('unsafe fallback forbidden')):
            seed._publish_directory(source, destination)
            function.assert_called_once_with(seed.os.fsencode(source), seed.os.fsencode(destination), 4)
            function.return_value = -1
            for code, error in ((errno.EEXIST, FileExistsError), (errno.ENOTSUP, RuntimeError)):
                with self.subTest(code=code), patch.object(seed.ctypes, 'get_errno', return_value=code):
                    with self.assertRaises(error):
                        seed._publish_directory(source, destination)

    def test_missing_atomic_rename_primitive_fails_closed(self):
        source, destination = self.root / 'source', self.root / 'destination'
        for platform in ('linux', 'darwin'):
            with self.subTest(platform=platform), patch.object(seed.os, 'name', 'posix'), \
                 patch.object(seed.sys, 'platform', platform), \
                 patch.object(seed.ctypes, 'CDLL', return_value=SimpleNamespace()), \
                 patch.object(seed.os, 'rename', side_effect=AssertionError('unsafe fallback forbidden')):
                with self.assertRaisesRegex(RuntimeError, 'no-replace'):
                    seed._publish_directory(source, destination)

    def test_cli_uses_the_same_offline_verifier(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = seed.main(['--archive', str(self.archive), '--checksum', str(self.checksum),
                                '--acceptance', str(self.acceptance), '--output', str(self.output),
                                '--source-sha', SHA, '--artifact-run', RUN,
                                '--archive-sha256', seed.guard.sha256(self.archive),
                                '--receipt-sha256', seed.guard.sha256(self.acceptance)])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())['result'], 'verified')

    def test_cached_seed_verification_needs_no_archive_source_checkout_or_network(self):
        anchors = self.anchors()
        provenance = self.run_seed()
        for path in (self.archive, self.checksum, self.acceptance, self.pin_file):
            path.unlink()
        with patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')), \
             patch.object(models, '_build_tools', side_effect=AssertionError('no source transport')):
            self.assertEqual(seed.verify_seed(self.output, **anchors), provenance)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(seed.main(['--verify-seed', str(self.output), '--source-sha', SHA,
                                        '--artifact-run', RUN, '--archive-sha256', anchors['archive_sha256'],
                                        '--receipt-sha256', anchors['receipt_sha256']]), 0)
        self.assertEqual(json.loads(output.getvalue()), provenance)

    def test_cached_seed_must_match_all_explicit_original_source_anchors(self):
        anchors = self.anchors()
        self.run_seed()
        for key, value in (('source_sha', 'c' * 40), ('artifact_run', '123'),
                           ('archive_sha256', 'd' * 64), ('receipt_sha256', 'e' * 64)):
            with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, 'trusted source'):
                seed.verify_seed(self.output, **{**anchors, key: value})

    def test_cache_provenance_tampering_or_added_runtime_cannot_pass(self):
        anchors = self.anchors()
        provenance = self.run_seed()
        path = self.output / 'provenance.json'
        for key, value in (('source_pins_sha256', '0' * 64), ('model_inventory_sha256', '0' * 64),
                           ('scope', 'final-bundle'), ('runtime_included', True), ('model_count', 14),
                           ('source_model_evidence', {})):
            with self.subTest(key=key):
                path.write_bytes(models._canonical({**provenance, key: value}))
                with self.assertRaises((RuntimeError, ValueError)):
                    seed.verify_seed(self.output, **anchors)
        path.write_bytes(models._canonical(provenance))
        (self.output / 'runtime').mkdir()
        with self.assertRaisesRegex(RuntimeError, 'only models and provenance'):
            seed.verify_seed(self.output, **anchors)

    def test_cached_models_are_independently_hashed_not_approved_by_provenance(self):
        anchors = self.anchors()
        provenance = self.run_seed()
        relative = next(iter(self.inventory['files']))
        path = self.output / 'models' / relative
        path.write_bytes(b'changed original source')
        inventory_path = self.output / 'models' / models.INVENTORY_NAME
        inventory = json.loads(inventory_path.read_bytes())
        inventory['files'][relative] = {'sha256': digest(path.read_bytes()), 'bytes': path.stat().st_size}
        inventory_path.write_bytes(models._canonical(inventory))
        provenance['model_inventory_sha256'] = digest(inventory_path.read_bytes())
        (self.output / 'provenance.json').write_bytes(models._canonical(provenance))
        with self.assertRaisesRegex(ValueError, 'source hash mismatch'):
            seed.verify_seed(self.output, **anchors)

    def test_cached_provenance_and_model_directory_symlinks_are_rejected(self):
        anchors = self.anchors()
        self.run_seed()
        for name in ('provenance.json', 'models'):
            path = self.output / name
            target = self.root / ('external-' + name)
            path.rename(target)
            try:
                path.symlink_to(target, target_is_directory=target.is_dir())
            except OSError as error:
                target.rename(path)
                self.skipTest('Symlink creation unavailable: ' + str(error))
            with self.subTest(name=name), self.assertRaises((RuntimeError, ValueError)):
                seed.verify_seed(self.output, **anchors)
            path.unlink()
            target.rename(path)

    def test_verify_cli_rejects_creation_options_and_missing_external_hashes(self):
        common = ['--verify-seed', str(self.output), '--source-sha', SHA, '--artifact-run', RUN,
                  '--archive-sha256', 'a' * 64, '--receipt-sha256', 'b' * 64]
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            seed.main(common + ['--output', str(self.output)])
        self.assertEqual(raised.exception.code, 2)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            seed.main(common[:-2])
        self.assertEqual(raised.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
