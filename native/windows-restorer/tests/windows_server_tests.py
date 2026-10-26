#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Standalone native Server CI exercise; fixtures are NOT release acceptance.

Run on Server 2022 AND 2025, in an x64 MSVC tools environment. --large is
mandatory for the release-candidate transport gate (>2 GiB and real ZIP64).
Does not alter a workflow, publish, require elevation or use a client computer.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
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


def tar_probe_cases():
    """Four argv controls, four stdin/cwd controls, and true long-cwd probes."""
    cases = []
    for alphabet in ('ascii', 'unicode'):
        for extended in (False, True):
            for transport in ('argv', 'stdin-cwd'):
                cases.append({'alphabet': alphabet, 'extended': extended, 'transport': transport, 'long_cwd': False})
    cases.extend({'alphabet': 'unicode', 'extended': value, 'transport': 'stdin-cwd', 'long_cwd': True}
                 for value in (False, True))
    return cases


def tar_path_probe(work):
    """Observe the installed system tar, without treating controls as a gate pass.

    Separate synthetic ASCII controls diagnose Unicode/prefix handling; no
    production file is relocated or renamed to make extraction pass.
    """
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    def windows_path(api):
        function = getattr(kernel, api)
        function.argtypes = [wintypes.LPWSTR, wintypes.UINT]; function.restype = wintypes.UINT
        buffer = ctypes.create_unicode_buffer(32768)
        count = function(buffer, len(buffer))
        if not 0 < count < len(buffer): raise ctypes.WinError(ctypes.get_last_error())
        return Path(buffer.value)
    system, windows = windows_path('GetSystemDirectoryW'), windows_path('GetWindowsDirectoryW')
    tar = system / 'tar.exe'
    tar_sha256, tar_bytes = sha(tar), tar.stat().st_size
    environment = {'SystemRoot': str(windows), 'WINDIR': str(windows)}
    directory = work / 'system-tar-probes'; directory.mkdir()
    version = subprocess.run([str(tar), '--version'], cwd=system, env=environment, capture_output=True, timeout=30)
    (directory / 'version.stdout.log').write_bytes(version.stdout)
    (directory / 'version.stderr.log').write_bytes(version.stderr)
    report = {'schema': 1, 'scope': 'synthetic-system-tar-path-observations-not-release-acceptance',
              'tar': {'path': str(tar), 'sha256': tar_sha256, 'bytes': tar_bytes,
                      'version_exit_code': version.returncode,
                      'version_stdout': version.stdout.decode('utf-8', errors='replace')},
              'code_pages': {name: getattr(kernel, name)() for name in ('GetACP', 'GetOEMCP', 'GetConsoleCP', 'GetConsoleOutputCP')},
              'cases': []}
    report_path = directory / 'system-tar-probe-report.json'
    payload = b'Synthetic Unicode handoff control; not a release payload.\n'
    member = 'probe-root/payload 中文 Ω &!%()/content.txt'
    archives = {}
    for alphabet, leaf in (('ascii', 'archive-ascii'), ('unicode', 'archive 中文 Ω &!%()')):
        source = directory / leaf; source.mkdir(); archive = source / 'fixture.zip'
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_STORED) as stream:
            stream.writestr(zipfile.ZipInfo(member, date_time=(1980, 1, 1, 0, 0, 0)), payload)
        archives[alphabet] = archive
    try:
        for index, case in enumerate(tar_probe_cases()):
            archive = archives[case['alphabet']]
            label = f"{index}-{case['alphabet']}-{case['transport']}-{'extended' if case['extended'] else 'ordinary'}"
            destination = directory / (label + (' 中文 Ω &!%()' if case['alphabet'] == 'unicode' else ''))
            if case['long_cwd']:
                destination = destination / ('long-' + 'x' * 120) / ('long-' + 'y' * 120)
                assert len(str(destination)) > 260
            path_argument = lambda value: ('\\\\?\\' if case['extended'] else '') + str(value)
            command = [str(tar), '-x', '--no-same-owner', '--no-same-permissions', '-f']
            if case['transport'] == 'argv':
                command += [path_argument(archive), '-C', path_argument(destination)]
                cwd = str(system)
            else:
                command += ['-']
                cwd = path_argument(destination)
            row = {**case, 'command': command, 'cwd': cwd, 'destination': str(destination),
                   'archive_path_is_ascii': str(archive).isascii(), 'destination_path_is_ascii': str(destination).isascii(),
                   'cwd_utf16_units': len(cwd.encode('utf-16-le')) // 2,
                   'archive_sha256': sha(archive), 'expected_member': member,
                   'expected_payload_sha256': hashlib.sha256(payload).hexdigest()}
            stdin = None
            try:
                Path('\\\\?\\' + str(destination)).mkdir(parents=True)
                if case['transport'] == 'stdin-cwd': stdin = archive.open('rb')
                result = subprocess.run(command, stdin=stdin, cwd=cwd, env=environment, capture_output=True, timeout=30)
                (directory / (label + '.stdout.log')).write_bytes(result.stdout)
                (directory / (label + '.stderr.log')).write_bytes(result.stderr)
                output = Path('\\\\?\\' + str(destination / member))
                row.update(exit_code=result.returncode, exact_unicode_file_present=output.is_file(),
                           payload_sha256=sha(output) if output.is_file() else None)
                row['exact_payload_match'] = row['payload_sha256'] == row['expected_payload_sha256']
            except (OSError, subprocess.TimeoutExpired) as error:
                if isinstance(error, subprocess.TimeoutExpired):
                    (directory / (label + '.stdout.log')).write_bytes(error.stdout or b'')
                    (directory / (label + '.stderr.log')).write_bytes(error.stderr or b'')
                row.update(launch_error=repr(error), exact_payload_match=False)
            finally:
                if stdin is not None: stdin.close()
            report['cases'].append(row)
            report_path.write_text(json.dumps(report, indent=2, ensure_ascii=True) + '\n', encoding='utf-8')
    finally:
        report['tar_unchanged'] = sha(tar) == report['tar']['sha256']
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=True) + '\n', encoding='utf-8')
    if not report['tar_unchanged']: raise AssertionError('System tar changed during path diagnostics')
    return report


def fixture(directory: Path, *, large=False, impossible_space=False, deep=False, deflated=False, invalid_deflate=False, wrong_payload_hash=False):
    directory.mkdir()
    root = 'vse-restorer-SYNTHETIC-NOT-A-RELEASE'
    archive = directory / (root + '.zip')
    size = (1 << 31) + 17 if large else 8 * 1024 * 1024
    content_hash = hashlib.sha256(); chunk = b'\0' * (1024 * 1024)
    relative = ('deep-' + 'x' * 64 + '/') * 5 + 'data.bin' if deep else 'payload 中文 &!%()/data.bin'
    name = root + '/' + relative
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED if deflated else zipfile.ZIP_STORED, allowZip64=True) as output:
        with output.open(name, 'w', force_zip64=True) as handle:
            left = size
            while left:
                value = chunk[:min(left, len(chunk))]; handle.write(value); content_hash.update(value); left -= len(value)
    if invalid_deflate:
        if not deflated: raise ValueError('Corruption fixture requires raw DEFLATE')
        # Synthetic only: valid fixed ZIP structure/whole-byte hashes but invalid
        # DEFLATE BTYPE=3. No accepted production receipt is fabricated here.
        with archive.open('r+b') as stream:
            header = stream.read(30)
            name_bytes, extra_bytes = struct.unpack_from('<HH', header, 26)
            stream.seek(30 + name_bytes + extra_bytes); stream.write(b'\x07')
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
                'parts': parts, 'validated_zip_entries': [{'name': name, 'kind': 'file', 'size': size, 'sha256': '0' * 64 if wrong_payload_hash else content_hash.hexdigest()}]}
    metadata = load('test_metadata_renderer', HERE / 'generate_metadata.py')
    indexer = load('synthetic_fixed_index', HERE / 'fixed_index.py')
    with archive.open('rb') as stream:
        document['fixed_zip_index'] = indexer.inspect_stream(stream, document)
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


def visible_output_size(path):
    """Query live attributes without data access or changing the writer's locks.

    CreateFile's documented share rules exclude attribute-only access; directory
    search metadata alone may be stale while the exclusive writer is active.
    https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew
    """
    class Information(ctypes.Structure):
        _fields_ = [('attributes', wintypes.DWORD), ('creation', wintypes.FILETIME),
                    ('access', wintypes.FILETIME), ('write', wintypes.FILETIME),
                    ('volume', wintypes.DWORD), ('size_high', wintypes.DWORD),
                    ('size_low', wintypes.DWORD), ('links', wintypes.DWORD),
                    ('index_high', wintypes.DWORD), ('index_low', wintypes.DWORD)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(Information)]
    kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    # FILE_READ_ATTRIBUTES, all sharing, OPEN_EXISTING, OPEN_REPARSE_POINT.
    handle = kernel.CreateFileW('\\\\?\\' + str(path), 0x80, 7, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        if error in (2, 3): return None
        raise ctypes.WinError(error)
    try:
        value = Information()
        if not kernel.GetFileInformationByHandle(handle, ctypes.byref(value)): raise ctypes.WinError(ctypes.get_last_error())
        if value.attributes & (0x10 | 0x400) or value.links != 1: raise AssertionError('Output became directory/reparse/hardlink')
        return (value.size_high << 32) | value.size_low
    finally: kernel.CloseHandle(handle)


def run_case(exe, source, destination, success, name, records, *, env=None, relative='payload 中文 &!%()/data.bin'):
    destination_io = Path('\\\\?\\' + str(destination))
    destination_io.mkdir()
    command = [str(exe), '--source', str(source), '--destination-parent', str(destination), '--non-interactive']
    try:
        result = subprocess.run(command, capture_output=True, env=env, timeout=1800)
    except subprocess.TimeoutExpired as error:
        (destination_io / 'stdout.log').write_bytes(error.stdout or b'')
        (destination_io / 'stderr.log').write_bytes(error.stderr or b'')
        raise
    (destination_io / 'stdout.log').write_bytes(result.stdout); (destination_io / 'stderr.log').write_bytes(result.stderr)
    restored = destination / 'vse-restorer-SYNTHETIC-NOT-A-RELEASE'
    okay = result.returncode == 0 if success else result.returncode != 0
    if success: okay = okay and Path('\\\\?\\' + str(restored / relative)).is_file()
    else: okay = okay and not Path('\\\\?\\' + str(restored)).exists()
    records.append({'case': name, 'passed': okay, 'exit_code': result.returncode})
    if not okay: raise AssertionError(f'{name}: native result {result.returncode}; see {destination}')
    return restored


@contextmanager
def collect_case(records, name):
    """Preserve a failed gate and continue independent cases, never promote it."""
    before = len(records)
    try:
        yield
    except Exception as error:
        if len(records) == before:
            records.append({'case': name, 'passed': False})
        for row in records[before:]:
            row.update(passed=False, error=f'{type(error).__name__}: {error}')
    else:
        if len(records) == before:
            records.append({'case': name, 'passed': True})


def result_for_cases(records, *, large):
    if not records or any(row.get('passed') is not True for row in records): return 'failed'
    return 'passed' if large else 'partial'


def assert_pristine_parts(source, document):
    for row in document['parts']:
        path = source / row['name']
        if (not path.is_file() or path.stat().st_size != row['size'] or path.stat().st_nlink != 1 or sha(path) != row['sha256']):
            raise AssertionError('Shared source fixture was not restored after the previous case')


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
        # Controls record observations, not successful path-support gates.
        try:
            report['system_tar_probe'] = tar_path_probe(work)
        except Exception as error:
            records.append({'case': 'system-tar-path-observations', 'passed': False, 'error': repr(error)})
        source = work / 'source 中文 &!%()'; document = fixture(source); exe = compile_fixture(source)
        environment = {**os.environ, 'PATH': str(work / 'poisoned-path'), 'TAR_OPTIONS': '--invalid-should-never-be-inherited',
                       'SystemRoot': str(work / 'wrong-systemroot'), 'PYTHONHOME': str(work / 'fake-python')}
        with collect_case(records, 'unicode-and-shell-metacharacters-clean-system-tar'):
            restored = run_case(exe, source, work / 'unicode 中文 &!%()', True, 'unicode-and-shell-metacharacters-clean-system-tar', records, env=environment)
            assert sha(restored / 'payload 中文 &!%()/data.bin') == document['validated_zip_entries'][0]['sha256']
        with collect_case(records, 'over-260-character-member-path'):
            deep_source = work / 'deep-source'; deep = fixture(deep_source, deep=True); deep_exe = compile_fixture(deep_source)
            deep_relative = deep['validated_zip_entries'][0]['name'].split('/', 1)[1]
            deep_result = run_case(deep_exe, deep_source, work / 'deep-result', True, 'over-260-character-member-path', records, relative=deep_relative)
            assert sha(Path("\\\\?\\" + str(deep_result / deep_relative))) == deep['validated_zip_entries'][0]['sha256']
        with collect_case(records, 'unicode-fixed-deflate-stream'):
            compressed_source = work / 'deflate-source 中文 &!%()'
            compressed = fixture(compressed_source, deflated=True)
            compressed_exe = compile_fixture(compressed_source)
            restored = run_case(compressed_exe, compressed_source, work / 'deflate-result 中文 &!%()', True,
                                'unicode-fixed-deflate-stream', records)
            assert sha(restored / 'payload 中文 &!%()/data.bin') == compressed['validated_zip_entries'][0]['sha256']
        for label, options in (('invalid-deflate-never-published', {'invalid_deflate': True}),
                               ('fixed-payload-sha-mismatch-never-published', {'wrong_payload_hash': True})):
            with collect_case(records, label):
                bad_source = work / (label + '-source')
                fixture(bad_source, deflated=True, **options)
                bad_exe = compile_fixture(bad_source)
                run_case(bad_exe, bad_source, work / (label + '-result'), False, label, records)
        parts = [source / row['name'] for row in document['parts']]
        with collect_case(records, 'missing-volume'):
            assert_pristine_parts(source, document)
            backup = parts[-1].with_suffix('.saved'); parts[-1].rename(backup)
            try: run_case(exe, source, work / 'missing', False, 'missing-volume', records)
            finally: backup.rename(parts[-1])
            assert_pristine_parts(source, document)
        with collect_case(records, 'tampered-volume'):
            assert_pristine_parts(source, document)
            with parts[0].open('r+b') as handle: original = handle.read(1); handle.seek(0); handle.write(bytes([original[0] ^ 1]))
            try: run_case(exe, source, work / 'tampered', False, 'tampered-volume', records)
            finally:
                with parts[0].open('r+b') as handle: handle.write(original)
            assert_pristine_parts(source, document)
        with collect_case(records, 'swapped-volumes'):
            assert_pristine_parts(source, document)
            first, second = parts[0].read_bytes(), parts[1].read_bytes()
            parts[0].write_bytes(second); parts[1].write_bytes(first)
            try: run_case(exe, source, work / 'reordered', False, 'swapped-volumes', records)
            finally: parts[0].write_bytes(first); parts[1].write_bytes(second)
            assert_pristine_parts(source, document)
        with collect_case(records, 'hardlinked-volume'):
            assert_pristine_parts(source, document)
            linked = work / 'hardlink'; os.link(parts[0], linked)
            try: run_case(exe, source, work / 'hardlinked', False, 'hardlinked-volume', records)
            finally: linked.unlink()
            assert_pristine_parts(source, document)
        with collect_case(records, 'ancestor-junction'):
            assert_pristine_parts(source, document)
            link = work / 'junction-source'; junction(link, source)
            try: run_case(exe, link, work / 'junction-source-result', False, 'ancestor-junction', records)
            finally: link.rmdir()
        for label, bad in [('unc', "\\\\localhost\\C$"), ('device', "\\\\?\\" + str(source))]:
            with collect_case(records, label + '-namespace'):
                assert_pristine_parts(source, document)
                run_case(exe, bad, work / label, False, label + '-namespace', records)
        with collect_case(records, 'existing-user-directory-preserved'):
            assert_pristine_parts(source, document)
            existing = work / 'existing'; existing.mkdir(); keep = existing / document['archive']['root']; keep.mkdir(); marker = keep / 'USER-DATA'; marker.write_bytes(b'do not overwrite')
            result = subprocess.run([str(exe), '--source', str(source), '--destination-parent', str(existing), '--non-interactive'], capture_output=True, timeout=60)
            (existing / 'stdout.log').write_bytes(result.stdout); (existing / 'stderr.log').write_bytes(result.stderr)
            assert result.returncode != 0 and marker.read_bytes() == b'do not overwrite' and list(keep.iterdir()) == [marker]
        with collect_case(records, 'space-preflight'):
            low_source = work / 'space-fixture'; fixture(low_source, impossible_space=True); low_exe = compile_fixture(low_source)
            run_case(low_exe, low_source, work / 'insufficient-space', False, 'space-preflight', records)
        if args.large:
            big_exe = None
            with collect_case(records, 'real-over-2gib-zip64'):
                large_source = work / 'large-source'; big = fixture(large_source, large=True); big_exe = compile_fixture(large_source)
                recovered = run_case(big_exe, large_source, work / 'large-result', True, 'real-over-2gib-zip64', records)
                assert sha(recovered / 'payload 中文 &!%()/data.bin') == big['validated_zip_entries'][0]['sha256']
            with collect_case(records, 'interruption-no-success-directory'):
                if big_exe is None: raise RuntimeError('Blocked: large fixture did not compile')
                # This is interruption during merging, NOT proof of interruption
                # during extraction; that separate gate is exercised below.
                interrupted = work / 'interrupted'; interrupted.mkdir()
                process = subprocess.Popen([str(big_exe), '--source', str(large_source), '--destination-parent', str(interrupted), '--non-interactive'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    deadline = time.monotonic() + 60
                    while not list(interrupted.glob('.vse-incomplete-*')) and process.poll() is None and time.monotonic() < deadline: time.sleep(.01)
                    assert process.poll() is None and list(interrupted.glob('.vse-incomplete-*'))
                    process.kill(); process.wait(timeout=30)
                    assert not (interrupted / big['archive']['root']).exists()
                finally:
                    if process.poll() is None: process.kill(); process.wait(timeout=30)
        if args.large:
            expanded_exe = None
            with collect_case(records, 'real-over-2gib-expanded-deflate'):
                expanded_source = work / 'expanded-deflate-source'
                expanded = fixture(expanded_source, large=True, deflated=True)
                expanded_exe = compile_fixture(expanded_source)
                recovered = run_case(expanded_exe, expanded_source, work / 'expanded-deflate-result', True,
                                     'real-over-2gib-expanded-deflate', records)
                assert sha(recovered / 'payload 中文 &!%()/data.bin') == expanded['validated_zip_entries'][0]['sha256']
            with collect_case(records, 'interruption-during-fixed-inflate-no-success'):
                if expanded_exe is None: raise RuntimeError('Blocked: large DEFLATE fixture did not compile')
                interrupted = work / 'interrupted-inflate'; interrupted.mkdir()
                command = [str(expanded_exe), '--source', str(expanded_source), '--destination-parent', str(interrupted), '--non-interactive']
                with (interrupted / 'stdout.log').open('xb') as out, (interrupted / 'stderr.log').open('xb') as err:
                    process = subprocess.Popen(command, stdout=out, stderr=err)
                    try:
                        deadline, observed = time.monotonic() + 120, None
                        while process.poll() is None and time.monotonic() < deadline:
                            for stage in interrupted.glob('.vse-incomplete-*'):
                                candidate = stage / 'payload' / expanded['validated_zip_entries'][0]['name']
                                count = visible_output_size(candidate)
                                if count is not None and 0 < count < expanded['archive']['uncompressed_bytes']: observed = count
                            if observed is not None: break
                            time.sleep(.005)
                        assert process.poll() is None and observed is not None, 'Did not observe active inflate output; not a valid interruption test'
                        process.kill(); process.wait(timeout=30)
                        assert not (interrupted / expanded['archive']['root']).exists()
                        records.append({'case': 'interruption-during-fixed-inflate-no-success', 'passed': True,
                                        'observed_partial_expanded_bytes': observed, 'exit_code': process.returncode})
                    finally:
                        if process.poll() is None: process.kill(); process.wait(timeout=30)
        with collect_case(records, 'over-260-unicode-destination-cwd'):
            assert_pristine_parts(source, document)
            long_parent = work / 'parent 中文 Ω &!%()' / ('x' * 120) / ('y' * 120)
            Path("\\\\?\\" + str(long_parent)).mkdir(parents=True)
            assert len(str(long_parent)) > 260
            long_result = run_case(exe, source, long_parent / 'target 中文', True,
                                   'over-260-unicode-destination-cwd', records)
            assert sha(Path("\\\\?\\" + str(long_result / 'payload 中文 &!%()/data.bin'))) == document['validated_zip_entries'][0]['sha256']
        report['result'] = result_for_cases(records, large=args.large)
    finally:
        (work / 'native-restorer-report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(work / 'native-restorer-report.json')
    if report['result'] == 'failed': raise AssertionError('One or more native restorer gates failed; see every recorded case')


if __name__ == '__main__': main()
