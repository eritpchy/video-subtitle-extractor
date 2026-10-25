"""Native closure integrity, attribution and traversal regression tests."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import bundle_linux_runtime_libs as native


class NativeClosureTests(unittest.TestCase):
    def test_base_policy_keeps_cpp_xcb_and_gomp(self):
        for name in ['libc.so.6', 'libpthread.so.0', 'ld-linux-x86-64.so.2', 'libm.so.6']:
            self.assertTrue(native.BASE.fullmatch(name))
        for name in ['libstdc++.so.6', 'libgcc_s.so.1', 'libxcb.so.1', 'libgomp.so.1']:
            self.assertFalse(native.BASE.fullmatch(name))

    def test_library_name_traversal_rejected(self):
        for name in ['../lib.so', '/tmp/lib.so', '..', 'a\\b']:
            with self.assertRaises(ValueError):
                native.safe_name(name)

    def test_elf_versions_only_use_requirements(self):
        with patch.object(native, 'run', side_effect=['(NEEDED) Shared library: [libc.so.6]\n(RUNPATH) Library runpath: [$ORIGIN:/lib]', 'Version definition section\nName: GLIBC_9.9\nVersion needs section\nName: GLIBC_2.34']):
            info = native.elf_info(Path('test'))
        self.assertEqual(info['glibc'], ['2.34'])
        self.assertEqual(info['needed'], ['libc.so.6'])
        self.assertEqual(info['paths'], ['$ORIGIN', '/lib'])

    def test_external_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'escape').symlink_to('/bin/sh')
            with self.assertRaisesRegex(ValueError, 'Escaping'):
                native.bundle_linux_libraries(root)

    def test_unknown_library_provenance_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(native, 'run', side_effect=subprocess.CalledProcessError(1, 'dpkg-query')):
            with self.assertRaisesRegex(RuntimeError, 'provenance'):
                native.package_provenance(Path('/lib/test.so'), Path(tmp))

    def test_integrity_and_license_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / 'native-libs'
            (dest / 'licenses').mkdir(parents=True)
            (dest / 'libtest.so').write_bytes(b'library')
            (dest / 'licenses' / 'copyright').write_text('attribution')
            manifest = {'libraries': {'libtest.so': {'sha256': native.sha256(dest / 'libtest.so')}}, 'attribution_hashes': {'licenses/copyright': native.sha256(dest / 'licenses' / 'copyright')}}
            (dest / 'manifest.json').write_text(json.dumps(manifest))
            native.verify_linux_libraries(root)
            (dest / 'licenses' / 'copyright').write_text('changed')
            with self.assertRaisesRegex(ValueError, 'Attribution'):
                native.verify_linux_libraries(root)
            manifest['attribution_hashes'] = {'../escape': 'x'}
            (dest / 'manifest.json').write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, 'Attribution'):
                native.verify_linux_libraries(root)

    def test_copied_library_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / 'native-libs'
            dest.mkdir()
            (root / 'target').write_bytes(b'library')
            (dest / 'libtest.so').symlink_to(root / 'target')
            manifest = {'libraries': {'libtest.so': {'sha256': native.sha256(root / 'target')}}, 'attribution_hashes': {}}
            (dest / 'manifest.json').write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, 'integrity'):
                native.verify_linux_libraries(root)

    def test_host_qt_substitution_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'plugin.so').write_bytes(b'\x7fELFfixture')
            info = {'needed': ['libQt6Missing.so.6'], 'paths': [], 'glibc': []}
            with patch.object(native, 'elf_info', return_value=info), patch.object(native, 'run', return_value=''):
                with self.assertRaisesRegex(RuntimeError, 'ABI-unsafe'):
                    native.bundle_linux_libraries(root)
            self.assertFalse((root / 'native-libs' / 'manifest.json').exists())

    def test_recursive_closure_copies_regular_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / 'stage'
            root.mkdir()
            source = parent / 'host'
            source.mkdir()
            (root / 'python').write_bytes(b'\x7fELFroot')
            (source / 'libfirst.so').write_bytes(b'\x7fELFfirst')
            (source / 'libsecond.so').write_bytes(b'\x7fELFsecond')
            def inspect(path):
                return {'needed': {'python': ['libfirst.so'], 'libfirst.so': ['libsecond.so'], 'libsecond.so': ['libc.so.6']}[path.name], 'paths': [], 'glibc': ['2.34']}
            cache = f'libfirst.so (libc6) => {source}/libfirst.so\nlibsecond.so (libc6) => {source}/libsecond.so'
            with patch.object(native, 'elf_info', side_effect=inspect), patch.object(native, 'run', return_value=cache), patch.object(native, 'package_provenance', return_value={'package': 'fixture'}):
                manifest = native.bundle_linux_libraries(root)
            self.assertEqual(set(manifest['libraries']), {'libfirst.so', 'libsecond.so'})
            self.assertEqual(manifest['minimum_glibc'], '2.34')
            self.assertEqual(manifest['base_libraries'], ['libc.so.6'])
            self.assertFalse((root / 'native-libs' / 'libfirst.so').is_symlink())
            self.assertEqual(native.verify_linux_libraries(root), manifest)
            (root / 'native-libs' / 'injected.so').write_bytes(b'bad')
            with self.assertRaisesRegex(ValueError, 'Unexpected'):
                native.verify_linux_libraries(root)
            (root / 'native-libs' / 'injected.so').unlink()
            with self.assertRaisesRegex(ValueError, 'overwrite'):
                native.bundle_linux_libraries(root)


if __name__ == '__main__':
    unittest.main()
