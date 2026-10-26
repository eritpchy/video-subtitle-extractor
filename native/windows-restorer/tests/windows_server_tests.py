#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Standalone native Server CI exercise; fixtures are NOT release acceptance.

Run on Server 2022 AND 2025, in an x64 MSVC tools environment. --large is
mandatory for the release-candidate transport gate (>2 GiB and real ZIP64).
Does not alter a workflow, publish, require elevation or use a client computer.
"""
from __future__ import annotations
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys
import time
import zipfile

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


def sha(path):
    with path.open('rb') as handle: return hashlib.file_digest(handle, 'sha256').hexdigest()


def fixture(directory: Path, *, large=False, impossible_space=False, deep=False):
    directory.mkdir()
    root = 'vse-restorer-SYNTHETIC-NOT-A-RELEASE'
    archive = directory / (root + '.zip')
    size = (1 << 31) + 17 if large else 8 * 1024 * 1024
    content_hash = hashlib.sha256(); chunk = b'\0' * (1024 * 1024)
    relative = ('deep-' + 'x' * 64 + '/') * 5 + 'data.bin' if deep else 'payload 中文 &!%()/data.bin'
    name = root + '/' + relative
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as output:
        with output.open(name, 'w', force_zip64=True) as handle:
            left = size
            while left:
                value = chunk[:min(left, len(chunk))]; handle.write(value); content_hash.update(value); left -= len(value)
    parts = []; offset = 0
    with archive.open('rb') as source:
        while offset < archive.stat().st_size:
            count = min(2_000_000_000 if large else 3_000_000, archive.stat().st_size - offset)
            part = directory / f'{archive.name}.part{len(parts) + 1:03d}'
            with part.open('xb') as target:
                left = count
                while left:
                    value = source.read(min(left, len(chunk))); target.write(value); left -= len(value)
            parts.append({'name': part.name, 'index': len(parts) + 1, 'offset': offset, 'size': count, 'sha256': sha(part)})
            offset += count
    document = {'schema': 1, 'format': 'vse-zip-byte-volumes',
                'archive': {'name': archive.name, 'root': root, 'size': archive.stat().st_size,
                            'sha256': sha(archive), 'uncompressed_bytes': size,
                            'identity': {'platform': 'windows', 'arch': 'x64', 'synthetic_fixture': True}},
                'parts': parts, 'validated_zip_entries': [{'name': name, 'kind': 'file', 'size': size, 'sha256': content_hash.hexdigest()}]}
    metadata = load('test_metadata_renderer', HERE / 'generate_metadata.py')
    # Test-only fixture generation. Production build.py has no bypass and calls
    # verify_transport. This deliberately carries no release receipt.
    header = metadata.render_verified(document)
    if impossible_space:
        header = header.replace(f'uncompressed_bytes = {size}ULL;', 'uncompressed_bytes = 9223372036854775807ULL;')
    (directory / 'bundle_metadata.hpp').write_text('// SYNTHETIC TEST ONLY. NEVER DISTRIBUTE.\n' + header, encoding='ascii')
    return document


def compile_fixture(directory):
    builder = load('native_builder', HERE / 'build.py')
    source_tools = load('native_source_tools', HERE / 'source_bundle.py')
    snapshot = source_tools.snapshot_sources(ROOT)
    identity = source_tools.git_identity(ROOT)
    result = builder.compile_fixed(HERE, directory, directory)
    source_tools.assert_sources_unchanged(ROOT, snapshot)
    result.update({'scope': 'SYNTHETIC-NOT-FOR-DISTRIBUTION', 'git': identity,
                   'source_sha256': {name: hashlib.sha256(data).hexdigest() for name, data in snapshot.items()},
                   'fixed_header_sha256': sha(directory / 'bundle_metadata.hpp'),
                   'client_restore_verified': False, 'gpu_inference_verified': False})
    (directory / 'fixture-build-record.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    # Exact source files and fixed header can be uploaded as diagnostic evidence;
    # do not upload this synthetic EXE/object or treat it as a release asset.
    companion = directory / 'fixture-source'
    companion.mkdir()
    for name, data in snapshot.items():
        path = companion / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
    (companion / 'bundle_metadata.hpp').write_bytes((directory / 'bundle_metadata.hpp').read_bytes())
    executable = directory / 'fixture-restorer-NOT-FOR-DISTRIBUTION.exe'
    (directory / 'restore.exe').rename(executable)
    return executable


def junction(path: Path, target: Path):
    """Create test-only local junction with Win32; no elevation/cmd.exe."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    path.mkdir()
    substitute = ('\\??\\' + str(target)).encode('utf-16-le'); printable = str(target).encode('utf-16-le')
    names = substitute + b'\0\0' + printable + b'\0\0'
    data = struct.pack('<IHHHHHH', 0xA0000003, len(names) + 8, 0, 0, len(substitute), len(substitute) + 2, len(printable)) + names
    handle = kernel.CreateFileW(str(path), 0x40000000, 0, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value: raise ctypes.WinError(ctypes.get_last_error())
    try:
        buffer = ctypes.create_string_buffer(data); returned = wintypes.DWORD()
        if not kernel.DeviceIoControl(handle, 0x000900A4, buffer, len(data), None, 0, ctypes.byref(returned), None):
            raise ctypes.WinError(ctypes.get_last_error())
    finally: kernel.CloseHandle(handle)


def run_case(exe, source, destination, success, name, records, *, env=None, relative='payload 中文 &!%()/data.bin'):
    destination.mkdir()
    command = [str(exe), '--source', str(source), '--destination-parent', str(destination), '--non-interactive']
    result = subprocess.run(command, capture_output=True, env=env, timeout=1800)
    (destination / 'stdout.log').write_bytes(result.stdout); (destination / 'stderr.log').write_bytes(result.stderr)
    restored = destination / 'vse-restorer-SYNTHETIC-NOT-A-RELEASE'
    okay = result.returncode == 0 if success else result.returncode != 0
    if success: okay = okay and Path('\\\\?\\' + str(restored / relative)).is_file()
    else: okay = okay and not restored.exists()
    records.append({'case': name, 'passed': okay, 'exit_code': result.returncode})
    if not okay: raise AssertionError(f'{name}: native result {result.returncode}; see {destination}')
    return restored


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-directory', type=Path, required=True)
    parser.add_argument('--large', action='store_true')
    args = parser.parse_args()
    if os.name != 'nt' or platform.machine().lower() not in ('amd64', 'x86_64'): raise RuntimeError('Native Windows x64 Server only')
    work = args.work_directory.absolute(); work.mkdir(exist_ok=False)
    records = []
    report = {'schema': 1, 'scope': 'synthetic-native-restorer-tests-not-package-acceptance', 'os': platform.platform(),
              'large_requested': args.large, 'gpu_inference_verified': False, 'tests': records, 'result': 'failed'}
    try:
        source = work / 'source 中文 &!%()'; document = fixture(source); exe = compile_fixture(source)
        environment = {**os.environ, 'PATH': str(work / 'poisoned-path'), 'TAR_OPTIONS': '--invalid-should-never-be-inherited',
                       'SystemRoot': str(work / 'wrong-systemroot'), 'PYTHONHOME': str(work / 'fake-python')}
        restored = run_case(exe, source, work / 'unicode 中文 &!%()', True, 'unicode-and-shell-metacharacters-clean-system-tar', records, env=environment)
        if sha(restored / 'payload 中文 &!%()/data.bin') != document['validated_zip_entries'][0]['sha256']: raise AssertionError('payload hash changed')
        deep_source = work / 'deep-source'; deep = fixture(deep_source, deep=True); deep_exe = compile_fixture(deep_source)
        deep_relative = deep['validated_zip_entries'][0]['name'].split('/', 1)[1]
        deep_result = run_case(deep_exe, deep_source, work / 'deep-result', True, 'over-260-character-member-path', records, relative=deep_relative)
        assert sha(Path('\\\\?\\' + str(deep_result / deep_relative))) == deep['validated_zip_entries'][0]['sha256']
        parts = [source / row['name'] for row in document['parts']]
        backup = parts[-1].with_suffix('.saved'); parts[-1].rename(backup)
        try: run_case(exe, source, work / 'missing', False, 'missing-volume', records)
        finally: backup.rename(parts[-1])
        with parts[0].open('r+b') as handle: original = handle.read(1); handle.seek(0); handle.write(bytes([original[0] ^ 1]))
        try: run_case(exe, source, work / 'tampered', False, 'tampered-volume', records)
        finally:
            with parts[0].open('r+b') as handle: handle.write(original)
        first, second = parts[0].read_bytes(), parts[1].read_bytes()
        parts[0].write_bytes(second); parts[1].write_bytes(first)
        try: run_case(exe, source, work / 'reordered', False, 'swapped-volumes', records)
        finally: parts[0].write_bytes(first); parts[1].write_bytes(second)
        linked = work / 'hardlink'; os.link(parts[0], linked)
        try: run_case(exe, source, work / 'hardlinked', False, 'hardlinked-volume', records)
        finally: linked.unlink()
        link = work / 'junction-source'; junction(link, source)
        try: run_case(exe, link, work / 'junction-source-result', False, 'ancestor-junction', records)
        finally: link.rmdir()
        for label, bad in [('unc', '\\\\localhost\\C$'), ('device', '\\\\?\\' + str(source))]:
            run_case(exe, bad, work / label, False, label + '-namespace', records)
        existing = work / 'existing'; existing.mkdir(); keep = existing / document['archive']['root']; keep.mkdir(); marker = keep / 'USER-DATA'; marker.write_bytes(b'do not overwrite')
        result = subprocess.run([str(exe), '--source', str(source), '--destination-parent', str(existing), '--non-interactive'], capture_output=True)
        assert result.returncode != 0 and marker.read_bytes() == b'do not overwrite' and list(keep.iterdir()) == [marker]
        records.append({'case': 'existing-user-directory-preserved', 'passed': True})
        low_source = work / 'space-fixture'; fixture(low_source, impossible_space=True); low_exe = compile_fixture(low_source)
        run_case(low_exe, low_source, work / 'insufficient-space', False, 'space-preflight', records)
        if args.large:
            large_source = work / 'large-source'; big = fixture(large_source, large=True); big_exe = compile_fixture(large_source)
            recovered = run_case(big_exe, large_source, work / 'large-result', True, 'real-over-2gib-zip64', records)
            assert sha(recovered / 'payload 中文 &!%()/data.bin') == big['validated_zip_entries'][0]['sha256']
            # Abrupt process death must not leave a successful directory. Job
            # closure also prevents a tar child continuing after parent death.
            interrupted = work / 'interrupted'; interrupted.mkdir()
            process = subprocess.Popen([str(big_exe), '--source', str(large_source), '--destination-parent', str(interrupted), '--non-interactive'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 60
            while not list(interrupted.glob('.vse-incomplete-*')) and process.poll() is None and time.monotonic() < deadline: time.sleep(.01)
            assert process.poll() is None and list(interrupted.glob('.vse-incomplete-*'))
            process.kill(); process.wait(timeout=30)
            assert not (interrupted / big['archive']['root']).exists()
            records.append({'case': 'interruption-no-success-directory', 'passed': True})
        report['result'] = 'passed' if args.large else 'partial'
    finally:
        (work / 'native-restorer-report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(work / 'native-restorer-report.json')


if __name__ == '__main__': main()
