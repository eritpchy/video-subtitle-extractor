"""Native closure integrity, attribution and traversal regression tests."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts import bundle_linux_runtime_libs as native
from scripts.package_release import extract_checked


class NativeClosureTests(unittest.TestCase):
    def test_package_license_names_are_portable_deterministic_and_distinct(self):
        packages = ['libatk-bridge2.0-0t64:amd64', 'libatk-bridge2.0-0t64-amd64',
                    'libfoo+bar:amd64', 'libfoo-bar:amd64', 'libfoo:arm64',
                    'con', 'aux', 'nul', 'package.', 'p' * 300]
        names = [native.package_license_directory(package) for package in packages]
        self.assertEqual(len(set(names)), len(packages))
        for package, name in zip(packages, names):
            with self.subTest(package=package):
                self.assertEqual(name, native.package_license_directory(package))
                self.assertRegex(name, r'^pkg-[a-z0-9.-]+-[a-f0-9]{64}$')
                self.assertLess(len(name), 256)
                self.assertNotIn(':', name)
                self.assertFalse(name.endswith(('.', ' ')))

    def test_unsafe_package_ids_are_rejected_before_reading_attribution(self):
        packages = ['', '.', '..', '../escape', '/tmp/package', 'a\\b', '--help',
                    'pkg:amd64:stream', 'pkg:../amd64', 'pkg:amd64/escape',
                    'pkg_name', 'pkg name', 'pkg\x00name', 'PKG:amd64', 'pkg:']
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'libfixture.so'
            for package in packages:
                with self.subTest(package=package), patch.object(native, 'run', return_value=f'{package}: {source}\n'), patch.object(native, 'attribution_source') as attribution:
                    with self.assertRaises((ValueError, RuntimeError)):
                        native.package_provenance(source, Path(tmp) / 'dest')
                    attribution.assert_not_called()

    def test_multiarch_attribution_roundtrips_through_strict_archive_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docs, common = root / 'doc', root / 'common-licenses'
            common.mkdir()
            package = 'libatk-bridge2.0-0t64:amd64'
            copyright_file = docs / 'libatk-bridge2.0-0t64/copyright'
            copyright_file.parent.mkdir(parents=True)
            copyright_bytes = b'Original attribution\r\nSee /usr/share/common-licenses/GPL-2.\n\xff\x00'
            license_bytes = b'Complete GPL-2 fixture\r\n\xff\x00'
            copyright_file.write_bytes(copyright_bytes)
            (common / 'GPL-2').write_bytes(license_bytes)
            source = root / 'libatk-bridge-2.0.so.0'
            dest = root / 'bundle/native-libs'
            def query(*args):
                if args[1] == '-S':
                    return f'{package}: {args[2]}\n'
                self.assertEqual(args[-1], package)
                return package + '\n2.52.0\nat-spi2-core\n2.52.0\nhttps://example.test\n'
            with patch.object(native, 'DOC_ROOT', docs), patch.object(native, 'COMMON_LICENSES_ROOT', common), patch.object(native, 'run', side_effect=query):
                provenance = native.package_provenance(source, dest)
                # Multiple library records from the same dpkg ID share attribution.
                self.assertEqual(native.package_provenance(source, dest), provenance)
            self.assertEqual(provenance['package'], package)
            self.assertEqual(provenance['package_metadata'][0], package)
            self.assertEqual(provenance['copyright_source'], str(copyright_file))
            self.assertEqual(provenance['license_files'], [
                f'licenses/{native.package_license_directory(package)}/{name}'
                for name in ('GPL-2', 'copyright', 'package-id.txt')
            ])
            self.assertTrue(all('\\' not in name for name in provenance['license_files']))
            self.assertEqual(len(list((dest / 'licenses').iterdir())), 1)
            license_dir = dest / 'licenses' / native.package_license_directory(package)
            self.assertEqual((license_dir / 'copyright').read_bytes(), copyright_bytes)
            self.assertEqual((license_dir / 'GPL-2').read_bytes(), license_bytes)
            self.assertEqual((license_dir / 'package-id.txt').read_text(), package + '\n')
            manifest = {'libraries': {}, 'attribution_hashes': {name: native.sha256(dest / name) for name in provenance['license_files']}}
            (dest / 'manifest.json').write_text(json.dumps(manifest))
            archive = root / 'bundle.zip'
            with zipfile.ZipFile(archive, 'w') as output:
                for path in dest.rglob('*'):
                    if path.is_file():
                        output.write(path, path.relative_to(root).as_posix())
            extracted = extract_checked(archive, root / 'extracted')
            self.assertEqual(native.verify_linux_libraries(extracted), manifest)
            for name in provenance['license_files']:
                self.assertEqual((extracted / 'native-libs' / name).read_bytes(), (dest / name).read_bytes())
            # The old multiarch path must still fail; do not weaken ADS protection.
            with zipfile.ZipFile(archive, 'w') as output:
                output.writestr(f'bundle/native-libs/licenses/{package}/GPL-2', license_bytes)
            with self.assertRaisesRegex(RuntimeError, 'Unsafe release archive member'):
                extract_checked(archive, root / 'unsafe-extracted')

    def test_package_directory_collision_fails_without_overwriting_attribution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docs, common, dest = root / 'doc', root / 'common-licenses', root / 'native-libs'
            common.mkdir()
            for package in ('libfoo+bar', 'libfoo-bar'):
                copyright_file = docs / package / 'copyright'
                copyright_file.parent.mkdir(parents=True)
                copyright_file.write_text(package)
            source = root / 'library.so'
            with patch.object(native, 'DOC_ROOT', docs), patch.object(native, 'COMMON_LICENSES_ROOT', common), patch.object(native, 'package_license_directory', return_value='forced-collision'), patch.object(native, 'run', return_value=f'libfoo+bar: {source}\n'):
                native.package_provenance(source, dest)
                with patch.object(native, 'run', return_value=f'libfoo-bar: {source}\n'):
                    with self.assertRaisesRegex(ValueError, 'directory collision'):
                        native.package_provenance(source, dest)
            self.assertEqual((dest / 'licenses/forced-collision/copyright').read_text(), 'libfoo+bar')

    def test_attribution_source_and_destination_symlink_escapes_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docs, common, outside, dest = root / 'doc', root / 'common-licenses', root / 'outside', root / 'native-libs'
            docs.mkdir()
            common.mkdir()
            outside.mkdir()
            (outside / 'copyright').write_bytes(b'must not copy or overwrite')
            (docs / 'libfixture').symlink_to(outside, target_is_directory=True)
            source = root / 'library.so'
            with patch.object(native, 'DOC_ROOT', docs), patch.object(native, 'COMMON_LICENSES_ROOT', common), patch.object(native, 'run', return_value=f'libfixture:amd64: {source}\n'):
                with self.assertRaisesRegex(ValueError, 'Escaping attribution source'):
                    native.package_provenance(source, dest)
                self.assertFalse(dest.exists())
                (docs / 'libfixture').unlink()
                (docs / 'libfixture').mkdir()
                (docs / 'libfixture/copyright').write_bytes(b'valid attribution')
                dest.mkdir()
                (dest / 'licenses').symlink_to(outside, target_is_directory=True)
                with self.assertRaisesRegex(ValueError, 'Escaping license destination'):
                    native.package_provenance(source, dest)
            self.assertEqual(list(outside.iterdir()), [outside / 'copyright'])
            self.assertEqual((outside / 'copyright').read_bytes(), b'must not copy or overwrite')

    def test_attribution_symlinks_stay_inside_document_or_license_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            licenses = root / 'common-licenses'
            licenses.mkdir()
            (licenses / 'GPL-2').write_bytes(b'complete license')
            (licenses / 'GPL').symlink_to('GPL-2')
            resolved = native.attribution_source(licenses, Path('GPL'))
            self.assertEqual(resolved.read_bytes(), b'complete license')
            outside = root / 'outside'
            outside.write_bytes(b'private')
            (licenses / 'escape').symlink_to(outside)
            for name in ('escape', '../outside', str(outside)):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    native.attribution_source(licenses, Path(name))

    def test_copyright_may_link_to_allowlisted_common_license(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docs, common = root / 'doc', root / 'common-licenses'
            (docs / 'libfixture').mkdir(parents=True)
            common.mkdir()
            license_bytes = b'Full copyright and license attribution\r\n\xff'
            (common / 'GPL-2').write_bytes(license_bytes)
            copyright_file = docs / 'libfixture/copyright'
            # Windows symlink targets must use native separators, unlike the
            # portable forward-slash paths recorded in the bundle manifest.
            copyright_file.symlink_to(os.path.relpath(common / 'GPL-2', copyright_file.parent))
            source, dest = root / 'library.so', root / 'native-libs'
            with patch.object(native, 'DOC_ROOT', docs), patch.object(native, 'COMMON_LICENSES_ROOT', common), patch.object(native, 'run', return_value=f'libfixture:amd64: {source}\n'):
                record = native.package_provenance(source, dest)
            copied = dest / next(name for name in record['license_files'] if name.endswith('/copyright'))
            self.assertFalse(copied.is_symlink())
            self.assertEqual(copied.read_bytes(), license_bytes)

    def test_attribution_dedup_rejects_changed_bytes_and_symlink_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, target = root / 'original', root / 'copied'
            source.write_bytes(b'original license\x00\xff')
            native.copy_attribution(source, target)
            native.copy_attribution(source, target)
            self.assertEqual(target.read_bytes(), source.read_bytes())
            source.write_bytes(b'different license')
            with self.assertRaisesRegex(ValueError, 'destination collision'):
                native.copy_attribution(source, target)
            self.assertEqual(target.read_bytes(), b'original license\x00\xff')
            target.unlink()
            target.symlink_to(source)
            with self.assertRaisesRegex(ValueError, 'destination collision'):
                native.copy_attribution(source, target)

    def test_origin_needed_resolves_within_staged_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'stage'
            binary = root / 'runtime/bin/python3.12'
            library = root / 'runtime/lib/libpython3.12.so.1.0'
            binary.parent.mkdir(parents=True)
            library.parent.mkdir(parents=True)
            binary.write_bytes(b'\x7fELFpython')
            library.write_bytes(b'\x7fELFlibrary')
            for name in ('$ORIGIN/../lib/libpython3.12.so.1.0', '${ORIGIN}/../lib/libpython3.12.so.1.0'):
                self.assertEqual(native.staged_origin_dependency(name, binary, root), library.resolve())
            self.assertIsNone(native.staged_origin_dependency('libc.so.6', binary, root))
            with self.assertRaisesRegex(RuntimeError, 'Missing staged'):
                native.staged_origin_dependency('$ORIGIN/missing.so', binary, root)
            for name in ('$ORIGIN/../../../escape.so', '$LIB/libpython.so', '$ORIGIN/$LIB/lib.so'):
                with self.assertRaises(ValueError):
                    native.staged_origin_dependency(name, binary, root)

    def test_missing_linux_os_metadata_is_not_silently_invented(self):
        with patch.object(native.Path, 'read_text', side_effect=FileNotFoundError('missing os-release')):
            with self.assertRaises(FileNotFoundError):
                native.linux_os_release()

    def test_base_policy_keeps_cpp_xcb_and_gomp(self):
        for name in ['libc.so.6', 'libpthread.so.0', 'ld-linux-x86-64.so.2', 'libm.so.6']:
            self.assertTrue(native.BASE.fullmatch(name))
        for name in ['libstdc++.so.6', 'libgcc_s.so.1', 'libxcb.so.1', 'libgomp.so.1', 'libtinfo.so.6']:
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
            attribution_name = 'licenses/pkg-fixture/copyright'
            def provenance(source, destination):
                attribution = destination / attribution_name
                attribution.parent.mkdir(parents=True, exist_ok=True)
                attribution.write_bytes(b'complete fixture attribution')
                return {'package': 'fixture:amd64', 'license_files': [attribution_name]}
            cache = f'libfirst.so (libc6) => {source}/libfirst.so\nlibsecond.so (libc6) => {source}/libsecond.so'
            with patch.object(native, 'elf_info', side_effect=inspect), patch.object(native, 'run', return_value=cache), patch.object(native, 'package_provenance', side_effect=provenance), patch.object(native, 'linux_os_release', return_value='ID=ubuntu\nVERSION_ID=24.04\n'):
                manifest = native.bundle_linux_libraries(root)
            self.assertEqual(set(manifest['libraries']), {'libfirst.so', 'libsecond.so'})
            self.assertEqual(manifest['minimum_glibc'], '2.34')
            self.assertEqual(manifest['build_os_release'], 'ID=ubuntu\nVERSION_ID=24.04\n')
            self.assertEqual(manifest['base_libraries'], ['libc.so.6'])
            self.assertEqual(manifest['attribution_hashes'], {
                attribution_name: native.sha256(root / 'native-libs' / attribution_name)
            })
            self.assertFalse((root / 'native-libs' / 'libfirst.so').is_symlink())
            self.assertEqual(native.verify_linux_libraries(root), manifest)
            (root / 'native-libs' / 'injected.so').write_bytes(b'bad')
            with self.assertRaisesRegex(ValueError, 'Unexpected'):
                native.verify_linux_libraries(root)
            (root / 'native-libs' / 'injected.so').unlink()
            with self.assertRaisesRegex(ValueError, 'overwrite'):
                native.bundle_linux_libraries(root)


class PaddleHostUtilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / 'stage'
        self.root.mkdir()
        (self.root / 'python').write_bytes(b'\x7fELFfixture')
        self.ldd, self.bash = self.parent / 'ldd', self.parent / 'bash'
        self.ldd.write_bytes(b'#!/bin/bash\n# fixture; never executed\n')
        self.bash.write_bytes(b'\x7fELFbash-fixture')
        for name, value in [('PADDLE_LDD', self.ldd), ('PADDLE_LDD_INTERPRETER', self.bash)]:
            mocked = patch.object(native, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.bash_info = {'needed': ['libtinfo.so.6', 'libc.so.6'], 'paths': [], 'glibc': ['9.9']}

    def test_inspects_known_ldd_bash_dependency_without_claiming_to_bundle_tools(self):
        with patch.object(native, 'elf_info', return_value=self.bash_info) as inspect:
            result = native.paddle_host_utility_dependencies()
        inspect.assert_called_once_with(self.bash)
        self.assertEqual(result['seed_libraries'], ['libtinfo.so.6'])
        self.assertIn('/bin/bash', result['os_supplied_not_bundled'])
        self.assertEqual(result['build_ldd']['sha256'], native.sha256(self.ldd))
        self.assertEqual(result['build_interpreter']['needed'], self.bash_info['needed'])

    def test_unreviewed_ldd_interpreter_or_dependencies_fail_closed(self):
        self.ldd.write_text('#!/usr/bin/env bash\n')
        with self.assertRaisesRegex(RuntimeError, 'Unsupported Paddle ldd interpreter'):
            native.paddle_host_utility_dependencies()
        self.ldd.write_text('#!/bin/bash\n')
        self.bash.write_bytes(b'not an ELF')
        with self.assertRaisesRegex(RuntimeError, 'must be an ELF'):
            native.paddle_host_utility_dependencies()
        self.bash.write_bytes(b'\x7fELFfixture')
        for changes in ({'needed': ['libsurprise.so', 'libc.so.6']}, {'needed': ['libc.so.6']}, {'paths': ['/custom']}):
            with self.subTest(changes=changes), patch.object(native, 'elf_info', return_value={**self.bash_info, **changes}):
                with self.assertRaisesRegex(RuntimeError, 'Unreviewed Paddle ldd interpreter'):
                    native.paddle_host_utility_dependencies()
        self.ldd.unlink()
        with self.assertRaises(FileNotFoundError):
            native.paddle_host_utility_dependencies()

    def test_paddle_seed_has_recursive_provenance_and_only_shipped_elf_sets_abi_floor(self):
        libraries = ['libtinfo.so.6', 'libfixture.so.1']
        for name in libraries:
            (self.parent / name).write_bytes(b'\x7fELF' + name.encode())
        docs, common = self.parent / 'doc', self.parent / 'common-licenses'
        (docs / 'libfixture').mkdir(parents=True)
        common.mkdir()
        copyright_bytes = b'Exact fixture copyright\n/usr/share/common-licenses/MIT\n'
        license_bytes = b'Exact complete fixture license\r\n'
        (docs / 'libfixture/copyright').write_bytes(copyright_bytes)
        (common / 'MIT').write_bytes(license_bytes)
        package = 'libfixture:amd64'
        metadata = [package, '1.2.3', 'fixture-source', '1.2.3', 'https://example.test']
        def run(*args):
            if args == ('/sbin/ldconfig', '-p'):
                return '\n'.join(f'{name} (libc6) => {self.parent / name}' for name in libraries)
            if args[:2] == ('dpkg-query', '-S'):
                return f'{package}: {args[2]}\n'
            self.assertEqual(args[0:2], ('dpkg-query', '-W'))
            self.assertEqual(args[-1], package)
            return '\n'.join(metadata) + '\n'
        def inspect(path):
            if path == self.bash:
                return self.bash_info
            needed = ['libfixture.so.1'] if path.name == 'libtinfo.so.6' else ['libc.so.6']
            return {'needed': needed, 'paths': [], 'glibc': ['2.36' if path.name == 'libtinfo.so.6' else '2.34']}
        with patch.object(native, 'run', side_effect=run), patch.object(native, 'elf_info', side_effect=inspect), patch.object(native, 'DOC_ROOT', docs), patch.object(native, 'COMMON_LICENSES_ROOT', common), patch.object(native, 'linux_os_release', return_value='fixture OS'):
            manifest = native.bundle_linux_libraries(self.root, profile='paddle')
        self.assertEqual(set(manifest['libraries']), set(libraries))
        self.assertEqual(manifest['minimum_glibc'], '2.36')  # Not the unbundled Bash's 9.9.
        self.assertEqual(manifest['elf_files_scanned'], 3)
        self.assertEqual(manifest['host_utility_dependencies'][0]['seed_libraries'], ['libtinfo.so.6'])
        self.assertFalse((self.root / 'native-libs/bash').exists())
        for name in libraries:
            record = manifest['libraries'][name]
            self.assertEqual(record['package_metadata'], metadata)
            self.assertEqual(record['source'], str((self.parent / name).resolve()))
            self.assertEqual((self.root / 'native-libs' / name).read_bytes(), (self.parent / name).read_bytes())
            for filename, expected in [('copyright', copyright_bytes), ('MIT', license_bytes)]:
                relative = next(p for p in record['license_files'] if p.endswith('/' + filename))
                self.assertEqual((self.root / 'native-libs' / relative).read_bytes(), expected)
                self.assertIn(relative, manifest['attribution_hashes'])
        self.assertEqual(native.verify_linux_libraries(self.root), manifest)

    def test_native_and_generic_scan_do_not_seed_paddle_dependencies(self):
        for profile in (None, 'native'):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / 'python').write_bytes(b'\x7fELFfixture')
                with patch.object(native, 'paddle_host_utility_dependencies') as utility, patch.object(native, 'run', return_value=''), patch.object(native, 'elf_info', return_value={'needed': ['libc.so.6'], 'paths': [], 'glibc': ['2.34']}), patch.object(native, 'linux_os_release', return_value='fixture OS'):
                    manifest = native.bundle_linux_libraries(root, profile=profile)
                utility.assert_not_called()
                self.assertEqual(manifest['libraries'], {})
                self.assertEqual(manifest['host_utility_dependencies'], [])

    def test_missing_seed_or_provenance_cannot_write_manifest(self):
        for with_library in (False, True):
            with self.subTest(with_library=with_library), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / 'python').write_bytes(b'\x7fELFfixture')
                source = self.parent / 'libtinfo.so.6'
                source.write_bytes(b'\x7fELFtinfo-fixture')
                def run(*args):
                    if args[0] == '/sbin/ldconfig':
                        return f'libtinfo.so.6 (libc6) => {source}' if with_library else ''
                    raise subprocess.CalledProcessError(1, args)
                with patch.object(native, 'run', side_effect=run), patch.object(native, 'elf_info', return_value=self.bash_info):
                    with self.assertRaisesRegex(RuntimeError, 'provenance' if with_library else 'Unresolved Paddle'):
                        native.bundle_linux_libraries(root, profile='paddle')
                self.assertFalse((root / 'native-libs/manifest.json').exists())

    def test_unknown_profile_fails_before_creating_closure(self):
        with self.assertRaisesRegex(ValueError, 'Unknown Linux runtime profile'):
            native.bundle_linux_libraries(self.root, profile='typo')
        self.assertFalse((self.root / 'native-libs').exists())


if __name__ == '__main__':
    unittest.main()
