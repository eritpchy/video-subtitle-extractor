#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Exact-source companion ZIPs, not a toolchain or customer Python runtime."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import zipfile

SOURCE_PATHS = (
    'LICENSE', 'native/ocr/vc-runtime-manifest.json', 'native/ocr/vc-runtime-NOTICE.txt',
    'native/windows-restorer/restorer.cpp', 'native/windows-restorer/core.hpp',
    'native/windows-restorer/inflate_fixed.hpp', 'native/windows-restorer/fixed_index.py',
    'native/windows-restorer/inspect_zip_layout.py', 'native/windows-restorer/zlib_build.py',
    'native/windows-restorer/third_party/.gitattributes',
    'native/windows-restorer/build.py', 'native/windows-restorer/generate_metadata.py',
    'native/windows-restorer/source_bundle.py', 'native/windows-restorer/rebuild.py',
    'native/windows-restorer/release_gate.py', 'native/windows-restorer/restored_launcher_audit.py',
    'native/windows-restorer/RELEASE_GATE.md',
    'native/windows-restorer/README.md', 'native/windows-restorer/THIRD_PARTY_NOTICES.txt',
    'native/windows-restorer/tests/core_test.cpp', 'native/windows-restorer/tests/test_metadata.py',
    'native/windows-restorer/tests/test_source_bundle.py', 'native/windows-restorer/tests/windows_server_tests.py',
    'native/windows-restorer/tests/test_zip_layout.py',
    'native/windows-restorer/tests/test_fixed_inflate.py', 'native/windows-restorer/tests/inflate_driver.cpp',
    'native/windows-restorer/tests/rename_probe.cpp', 'native/windows-restorer/tests/run_rename_probe.py',
    'native/windows-restorer/tests/test_rename_probe.py',
    'native/windows-restorer/tests/space_preflight_driver.cpp', 'native/windows-restorer/tests/space_preflight_api.cpp',
    'native/windows-restorer/tests/test_space_preflight_isolation.py',
    'native/windows-restorer/tests/test_release_gate.py',
    'scripts/release_volume_transport.py', 'scripts/release_guard.py',
    'scripts/release_profiles.py', 'scripts/package_release.py',
    'scripts/bundle_bootstrap.py', 'scripts/runtime_dependencies.py',
    'tests/test_release_volume_transport.py',
) + tuple('native/windows-restorer/third_party/zlib-1.3.2/' + name for name in (
    'pin.json', 'LICENSE', 'README', 'inflate.c', 'inftrees.c', 'inffast.c', 'adler32.c', 'zutil.c',
    'zlib.h', 'zconf.h', 'zutil.h', 'inflate.h', 'inftrees.h', 'inffast.h', 'inffixed.h'))


def digest(data: bytes) -> str: return hashlib.sha256(data).hexdigest()


def git_identity(root: Path) -> dict:
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=root).decode('utf-8', errors='surrogateescape')
    commit = git('rev-parse', 'HEAD').strip()
    status = git('status', '--porcelain=v1', '--untracked-files=all')
    return {'commit': commit, 'dirty': bool(status), 'status_porcelain_v1': status,
            'statement': 'Exact accompanying source bytes are authoritative; HEAD alone does not identify a dirty build.'}


def snapshot_sources(root: Path) -> dict[str, bytes]:
    result = {}
    for name in SOURCE_PATHS:
        path = root / name
        if not path.is_file() or path.is_symlink(): raise RuntimeError(f'Missing/linked required build source: {name}')
        result[name] = path.read_bytes()
    return result


def assert_sources_unchanged(root: Path, snapshot: dict[str, bytes]):
    for name, data in snapshot.items():
        path = root / name
        if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
            raise RuntimeError(f'Build source changed during compilation: {name}')


def assert_generator_closure(provenance: dict, snapshot: dict[str, bytes]):
    """Actual imports, not all unreachable lazy imports in package_release."""
    modules = provenance.get('local_modules_actually_loaded', {})
    required = {'scripts.' + name: 'scripts/' + name + '.py' for name in (
        'release_volume_transport', 'release_guard', 'package_release',
        'bundle_bootstrap', 'runtime_dependencies', 'release_profiles')}
    required.update({'__main__': 'native/windows-restorer/generate_metadata.py',
                     'fixed_index': 'native/windows-restorer/fixed_index.py',
                     'inspect_zip_layout': 'native/windows-restorer/inspect_zip_layout.py'})
    if any(modules.get(name, {}).get('relative_path') != path for name, path in required.items()):
        raise RuntimeError('Generator did not load its expected authoritative local source closure')
    for row in modules.values():
        name = row.get('relative_path')
        if name not in snapshot or row.get('sha256') != digest(snapshot[name]):
            raise RuntimeError(f'Actual generator import missing/changed in source companion: {name}')


def _safe_member(name: str):
    path = PurePosixPath(name)
    if path.is_absolute() or '\\' in name or ':' in name or str(path) != name or '..' in path.parts:
        raise ValueError('Unsafe source-bundle member')


def create_source_bundle(destination: Path, *, sources: dict[str, bytes], generated_header: bytes,
                         notices: dict[str, bytes], build_record: dict, transport_manifest: bytes) -> dict:
    """Write a new allowlisted source ZIP; no obj/exe/SDK/Python environment."""
    if set(sources) != set(SOURCE_PATHS): raise ValueError('Source snapshot is incomplete or has unexpected files')
    if set(notices) != {'Microsoft-Visual-Cpp-runtime-NOTICE.txt', 'Apache-2.0.txt', 'THIRD_PARTY_NOTICES.txt', 'zlib-LICENSE.txt'}:
        raise ValueError('Required attribution and third-party notices are missing')
    files = dict(sources)
    files['generated/bundle_metadata.hpp'] = generated_header
    files['evidence/original-build-record.json'] = (json.dumps(build_record, indent=2, ensure_ascii=True) + '\n').encode()
    files['evidence/transport-manifest.json'] = transport_manifest
    files.update({f'notices/{name}': data for name, data in notices.items()})
    files['REBUILD.md'] = b'''# Exact-source companion\n\nThis ZIP contains the precise sources and fixed header used by the original\nbuild, not just a GitHub link. It does not include object files, an SDK/compiler,\nor a Python environment. Project Microsoft attribution and original zlib notices are preserved unchanged. Applicable Microsoft terms remain unverified; this is not publication authorization.\nThe exact official restricted zlib sources, pin hashes and source-selection policy\nare included; build compilation and final link-map checks exclude gzwrite,\ndeflate, crc32 and archive APIs. No dependency is downloaded during rebuilding.\n\nExtract on a Windows x64 build host with an appropriately licensed MSVC/SDK and\nPython 3.12+ (stdlib only for the fixed-header rebuild). From the extracted root:\n\n    python native/windows-restorer/rebuild.py --source-root . --output D:\\rebuild --acknowledge-redistribution-license\n\nThe script verifies every accompanying source/header/notice hash before building\nwith the fixed header. This path does not regenerate acceptance metadata or need\nthe original large ZIP. The original transport manifest, all generator sources\nand their local import dependencies are also included for audit/regeneration.\nRegenerating the header requires the separately supplied exact volume/receipt\nassets and any dependency explicitly recorded by the original build.\n\nRebuildable is NOT a claim of bit-identical output. Compiler/SDK/library versions,\npaths and linker behavior can change bytes. Original toolchain binaries and\nactual included SDK/header/library hashes are recorded, not redistributed.\nA rebuild records its binary hash and whether it actually equals the original.\nA clean Git commit and a dirty/untracked snapshot are distinguished explicitly.\n\nThis source companion is required alongside the restorer; it is not a substitute\nfor native Server validation and grants no publication or GPU-certification right.\n'''
    manifest = {'schema': 1, 'kind': 'vse-native-restorer-exact-source', 'git': build_record['git'],
                'original_exe_sha256': build_record['exe_sha256'],
                'bit_identical_rebuild_verified': False,
                'files': {name: {'bytes': len(data), 'sha256': digest(data)} for name, data in sorted(files.items())}}
    files['source-manifest.json'] = (json.dumps(manifest, indent=2, ensure_ascii=True) + '\n').encode()
    with destination.open('xb') as stream:
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, data in sorted(files.items()):
                _safe_member(name)
                row = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                row.compress_type = zipfile.ZIP_DEFLATED; row.external_attr = 0o100644 << 16
                archive.writestr(row, data)
    return {'name': destination.name, 'bytes': destination.stat().st_size,
            'sha256': digest(destination.read_bytes()), 'source_files': len(sources),
            'generated_header_sha256': digest(generated_header), 'bit_identical_rebuild_verified': False}


def verify_extracted_sources(root: Path) -> dict:
    """Fail closed on changed/extra/linked source files before fixed rebuild."""
    if root.is_symlink(): raise RuntimeError('Linked source root rejected')
    manifest = json.loads((root / 'source-manifest.json').read_bytes())
    if manifest.get('schema') != 1 or manifest.get('kind') != 'vse-native-restorer-exact-source': raise RuntimeError('Unknown source companion')
    actual = set()
    for path in root.rglob('*'):
        if path.is_symlink(): raise RuntimeError('Linked source file rejected')
        if path.is_file(): actual.add(path.relative_to(root).as_posix())
    expected = set(manifest['files']) | {'source-manifest.json'}
    if actual != expected: raise RuntimeError('Source companion files differ from inventory (put build output outside it)')
    for name, row in manifest['files'].items():
        _safe_member(name); data = (root / name).read_bytes()
        if row != {'bytes': len(data), 'sha256': digest(data)}: raise RuntimeError(f'Source companion hash mismatch: {name}')
    return manifest
