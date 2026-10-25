"""Windows worker audit: filesystem fixtures, plus read-only native API smoke."""
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from scripts import windows_native_worker_evidence as worker


def fixture(tmp_path):
    root = tmp_path / 'bundle 空间 & bang!' / 'native-ocr'
    root.mkdir(parents=True)
    rows = []
    for name in sorted(worker.REQUIRED_FILES):
        data = ('reviewed fixture ' + name).encode()
        (root / name).write_bytes(data)
        rows.append({'path': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    (root / 'size-audit.json').write_text(json.dumps({'files': rows}), encoding='utf-8')
    windows = tmp_path / 'Windows'
    (windows / 'System32').mkdir(parents=True)
    kernel = windows / 'System32/kernel32.dll'
    kernel.write_bytes(b'OS fixture')
    exe = root / 'native-ocr.exe'
    snapshot = {'process_executable': str(exe), 'windows_root': str(windows),
                'mapped_paths': [str(root / row['path']) for row in rows] + [str(kernel)]}
    snapshot['module_entries'] = [
        {'toolhelp_name': Path(path).name, 'toolhelp_path': path.replace('空间', '??'),
         'module_handle': index + 1, 'base_address': index + 1, 'psapi_path': path}
        for index, path in enumerate(snapshot['mapped_paths'])]
    return exe, snapshot


def test_exact_seven_images_have_actual_paths_hashes_and_read_only_revalidation(tmp_path, monkeypatch):
    exe, snapshot = fixture(tmp_path)
    monkeypatch.setattr(worker, '_snapshot', lambda pid: snapshot)
    monkeypatch.setattr(worker, '_system_windows_directory', lambda: Path(snapshot['windows_root']))
    report = worker.collect_native_worker_evidence(123, exe)
    assert report['available'] and report['passed'] and not report['errors']
    assert report['method'] == worker.METHOD == 'windows-toolhelp32-psapi-unicode-v2'
    assert report['process_query_access'] == 0x410
    assert set(report['files']) == worker.REQUIRED_FILES
    for name, row in report['files'].items():
        assert row['passed'] and row['path_matches'] and row['inventory_match']
        assert row['path'] == str(exe.parent / name)
        assert row['sha256'] == row['expected_sha256']
    report['parent_pid'] = 42  # Parent binding belongs to the integration layer.
    assert worker.validate_native_worker_evidence(report, exe)['passed']


@pytest.mark.parametrize('pid', [0, -1, True, '123', None, 2**32])
def test_invalid_pid_fails_closed_without_snapshot(pid, monkeypatch, tmp_path):
    called = []
    original = worker._snapshot
    # _snapshot validates the PID before any OS API setup even on non-Windows.
    monkeypatch.setattr(worker, '_snapshot', lambda value: (called.append(value), original(value))[1])
    report = worker.collect_native_worker_evidence(pid, tmp_path / 'native-ocr.exe')
    assert not report['available'] and not report['passed'] and report['errors']
    assert 'PID' in report['errors'][0]


@pytest.mark.parametrize('error', [PermissionError('access denied'), OSError('snapshot failed'),
                                  RuntimeError('Truncated Toolhelp path')])
def test_collection_failure_is_explicit_not_an_exception(tmp_path, monkeypatch, error):
    def failed(pid):
        raise error
    monkeypatch.setattr(worker, '_snapshot', failed)
    report = worker.collect_native_worker_evidence(123, tmp_path / 'native-ocr.exe')
    assert not report['available'] and not report['passed']
    assert str(error) in report['errors'][0]


@pytest.mark.parametrize('change', ['missing', 'host', 'mixed', 'tampered', 'wrong-exe',
                                   'unknown-os', 'unknown-local', 'python', 'paddle', 'duplicate'])
def test_snapshot_cannot_accept_incomplete_foreign_or_unreviewed_images(tmp_path, change):
    exe, snapshot = fixture(tmp_path)
    dll = exe.parent / 'concrt140.dll'
    paths = snapshot['mapped_paths']
    if change == 'missing':
        paths.remove(str(dll))
    elif change in ('host', 'mixed'):
        foreign = Path(snapshot['windows_root']) / 'System32/concrt140.dll'
        foreign.write_bytes(dll.read_bytes())  # Same bytes elsewhere still fail.
        paths.append(str(foreign))
        if change == 'host':
            paths.remove(str(dll))
    elif change == 'tampered':
        dll.write_bytes(b'changed bytes')
    elif change == 'wrong-exe':
        foreign = tmp_path / 'other.exe'
        foreign.write_bytes(b'not worker')
        snapshot['process_executable'] = str(foreign)
    elif change == 'duplicate':
        paths.append(paths[0])
    else:
        base = Path(snapshot['windows_root']) / 'System32' if change == 'unknown-os' else exe.parent
        name = {'unknown-os': 'unknown.dll', 'unknown-local': 'extra.dll',
                'python': 'python312.dll', 'paddle': 'libpaddle.dll'}[change]
        foreign = base / name
        foreign.write_bytes(b'unreviewed')
        paths.append(str(foreign))
    report = worker.validate_native_worker_snapshot(123, exe, snapshot)
    assert not report['passed'] and report['errors']


@pytest.mark.parametrize('change', ['missing', 'duplicate', 'hash', 'size', 'escape', 'linked'])
def test_inventory_must_bind_all_required_regular_sibling_files(tmp_path, change):
    exe, snapshot = fixture(tmp_path)
    path = exe.parent / 'size-audit.json'
    inventory = json.loads(path.read_text())
    if change == 'missing': inventory['files'].pop()
    elif change == 'duplicate': inventory['files'].append(inventory['files'][0])
    elif change == 'hash': inventory['files'][0]['sha256'] = '0' * 64
    elif change == 'size': inventory['files'][0]['bytes'] += 1
    elif change == 'escape': inventory['files'][0]['path'] = '../concrt140.dll'
    else:
        dll = exe.parent / 'concrt140.dll'
        target = tmp_path / 'concrt140.dll'
        target.write_bytes(dll.read_bytes())
        dll.unlink()
        dll.symlink_to(target)
    path.write_text(json.dumps(inventory))
    report = worker.validate_native_worker_snapshot(123, exe, snapshot)
    assert not report['passed'] and report['errors']


@pytest.mark.parametrize('change', ['passed-only', 'missing-files', 'forged-hash', 'wrong-method',
                                   'v1-method', 'v1-access', 'wrong-executable', 'missing-ort',
                                   'wrong-inventory', 'failed'])
def test_saved_evidence_does_not_trust_passed_flag(tmp_path, change, monkeypatch):
    exe, snapshot = fixture(tmp_path)
    monkeypatch.setattr(worker, '_system_windows_directory', lambda: Path(snapshot['windows_root']))
    report = worker.validate_native_worker_snapshot(123, exe, snapshot)
    assert report['passed']
    if change == 'passed-only': report = {'passed': True}
    elif change == 'missing-files': report.pop('files')
    elif change == 'forged-hash': report['files']['onnxruntime.dll']['sha256'] = 'f' * 64
    elif change == 'wrong-method': report['method'] = 'pretend'
    elif change == 'v1-method': report['method'] = 'windows-toolhelp32-querylimited-v1'
    elif change == 'v1-access': report['process_query_access'] = 0x1000
    elif change == 'wrong-executable': report['executable'] = '/foreign/native-ocr.exe'
    elif change == 'missing-ort': report['mapped_paths'].remove(str(exe.parent / 'onnxruntime.dll'))
    elif change == 'wrong-inventory': report['inventory_sha256'] = '0' * 64
    else: report['passed'] = False
    result = worker.validate_native_worker_evidence(report, exe)
    assert not result['passed'] and result['errors']


def test_saved_evidence_rehashes_files_after_process_exit(tmp_path, monkeypatch):
    exe, snapshot = fixture(tmp_path)
    monkeypatch.setattr(worker, '_system_windows_directory', lambda: Path(snapshot['windows_root']))
    report = worker.validate_native_worker_snapshot(123, exe, snapshot)
    (exe.parent / 'onnxruntime.dll').write_bytes(b'changed after capture')
    assert not worker.validate_native_worker_evidence(report, exe)['passed']


@pytest.mark.parametrize('entry_pid,name,path', [
    (12, 'module.dll', r'C:\Windows\System32\module.dll'),
    (123, 'module.dll', ''), (123, '', r'C:\module.dll'),
    (123, 'module.dll', 'C:\\' + 'x' * 256),
    (123, 'x' * 255, r'C:\module.dll'), (123, 'other.dll', r'C:\module.dll'),
])
def test_toolhelp_rejects_wrong_pid_truncation_empty_or_mismatched_name(entry_pid, name, path):
    with pytest.raises(RuntimeError):
        worker._checked_module_path(123, entry_pid, name, path)


def test_saved_evidence_cannot_substitute_an_environment_chosen_os_root(tmp_path, monkeypatch):
    exe, snapshot = fixture(tmp_path)
    report = worker.validate_native_worker_snapshot(123, exe, snapshot)
    monkeypatch.setattr(worker.sys, 'platform', 'win32')
    monkeypatch.setattr(worker, '_canonical', lambda path: Path(path).resolve(strict=True))
    monkeypatch.setattr(worker, '_system_windows_directory', lambda: tmp_path / 'actual-Windows')
    result = worker.validate_native_worker_evidence(report, exe)
    assert not result['passed'] and 'actual OS directory' in result['errors'][0]


@pytest.mark.parametrize('path', [r'\\server\share\module.dll', r'\\?\UNC\server\share\module.dll',
                                r'C:\Windows\System32\module.dll:stream', r'\Device\HarddiskVolume1\module.dll'])
def test_windows_paths_reject_remote_devices_and_streams_before_file_access(monkeypatch, path):
    monkeypatch.setattr(worker.sys, 'platform', 'win32')
    with pytest.raises(ValueError, match='Only local absolute'):
        worker._canonical(path)


def test_no_environment_systemroot_override_or_remote_process_write_apis():
    source = Path(worker.__file__).read_text()
    assert 'GetWindowsDirectoryW' in source
    assert worker._report(123, 'fixture.exe')['process_query_access'] == 0x410
    assert 'os.environ' not in source
    assert all(name not in source for name in ('WriteProcessMemory', 'CreateRemoteThread', 'AdjustTokenPrivileges'))


@pytest.mark.skipif(sys.platform != 'win32', reason='Read-only Toolhelp API smoke requires Windows CI')
def test_real_windows_snapshot_reads_current_process_without_admin():
    snapshot = worker._snapshot(os.getpid())
    assert Path(snapshot['process_executable']).samefile(sys.executable)
    assert any(Path(path).samefile(sys.executable) for path in snapshot['mapped_paths'])
    assert Path(snapshot['windows_root']).is_dir()


def test_psapi_reads_actual_wide_path_by_handle_without_toolhelp_spelling():
    expected = r'C:\bundle 环境 & spaces\字幕 worker.exe'
    calls = []
    def read(process, module, buffer, capacity):
        calls.append((process, module, capacity))
        buffer.value = expected
        return len(expected)
    kernel = SimpleNamespace(K32GetModuleFileNameExW=read)
    assert worker._read_module_path(kernel, 123, 456) == expected
    assert calls == [(123, 456, 32768)]


@pytest.mark.parametrize('length', [0, 32768, 32769])
def test_psapi_zero_or_truncated_result_fails_closed(monkeypatch, length):
    def read(process, module, buffer, capacity):
        buffer.value = r'C:\truncated.dll'
        return length
    monkeypatch.setattr(worker.ctypes, 'get_last_error', lambda: 299, raising=False)
    monkeypatch.setattr(worker.ctypes, 'WinError', lambda code=299: OSError(code, 'partial copy'), raising=False)
    with pytest.raises((OSError, RuntimeError)):
        worker._read_module_path(SimpleNamespace(K32GetModuleFileNameExW=read), 123, 456)


@pytest.mark.parametrize('failure', [None, 'zero', 'truncated'])
def test_toolhelp_handles_feed_psapi_even_when_toolhelp_unicode_path_is_lossy(monkeypatch, failure):
    """Exercise the real snapshot control flow with synthetic Win32 APIs only."""
    from ctypes import wintypes
    expected = r'C:\bundle 环境 & spaces\字幕 worker.exe'
    lossy = r'C:\bundle ?? & spaces\?? worker.exe'
    calls, closed = [], []
    def query(process, flags, buffer, length):
        buffer.value = expected
        length._obj.value = len(expected)
        return 1
    def first(snapshot, pointer):
        entry = pointer._obj
        entry.th32ProcessID = 123
        entry.szModule = '?? worker.exe'
        entry.szExePath = lossy
        entry.hModule = 0x100000
        entry.modBaseAddr = worker.ctypes.cast(worker.ctypes.c_void_p(0x100000), type(entry.modBaseAddr))
        return 1
    def read(process, module, buffer, capacity):
        calls.append(('psapi', process, module))
        buffer.value = expected
        if failure == 'zero':
            return 0
        if failure == 'truncated':
            return capacity
        return len(expected)
    def windows(buffer, capacity):
        buffer.value = r'C:\Windows'
        return len(buffer.value)
    def running(process, code):
        code._obj.value = 259
        return 1
    # Function objects permit ctypes' argtypes/restype assignments.
    functions = {
        'OpenProcess': lambda access, inherit, pid: calls.append(('open', access, inherit, pid)) or 99,
        'CloseHandle': lambda handle: closed.append(handle) or 1,
        'GetProcessId': lambda process: 123,
        'GetExitCodeProcess': running,
        'QueryFullProcessImageNameW': query,
        'CreateToolhelp32Snapshot': lambda flags, pid: 88,
        'Module32FirstW': first,
        'Module32NextW': lambda snapshot, entry: 0,
        'GetWindowsDirectoryW': windows,
        'K32GetModuleFileNameExW': read,
    }
    kernel = SimpleNamespace(**functions)
    monkeypatch.setattr(worker.sys, 'platform', 'win32')
    monkeypatch.setattr(worker.ctypes, 'WinDLL', lambda *args, **kwargs: kernel, raising=False)
    monkeypatch.setattr(worker.ctypes, 'get_last_error', lambda: 18, raising=False)
    if failure:
        report = worker.collect_native_worker_evidence(123, expected)
        assert not report['available'] and not report['passed'] and report['errors']
        snapshot = report['raw_snapshot']
        assert snapshot['mapped_paths'] == []  # Never substitute the expected/queried EXE for failed PSAPI evidence.
        assert report['mapped_paths'] == []
    else:
        snapshot = worker._snapshot(123)
        assert snapshot['mapped_paths'] == [expected]
    assert snapshot['process_executable'] == expected
    row, = snapshot['module_entries']
    assert row['toolhelp_path'] == lossy and row['toolhelp_name'] == '?? worker.exe'
    assert row['psapi_path'] == (None if failure else expected)
    assert row['module_handle'] == row['base_address'] == 0x100000
    assert ('open', 0x410, False, 123) in calls
    assert ('psapi', 99, 0x100000) in calls
    assert kernel.K32GetModuleFileNameExW.argtypes == [wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
    assert kernel.K32GetModuleFileNameExW.restype is wintypes.DWORD
    assert closed == [88, 99]


def test_collection_failure_preserves_raw_unicode_snapshot_and_lossy_toolhelp_forensics(tmp_path, monkeypatch):
    exe, snapshot = fixture(tmp_path)
    monkeypatch.setattr(worker, '_snapshot', lambda pid: snapshot)
    def failed(*args):
        raise OSError('OS proof path canonicalization failed')
    monkeypatch.setattr(worker, '_collect_os_security_evidence', failed)
    report = worker.collect_native_worker_evidence(123, exe)
    assert not report['available'] and not report['passed']
    assert 'canonicalization failed' in '\n'.join(report['errors'])
    for field in ('process_executable', 'mapped_paths', 'windows_root', 'module_entries'):
        assert report['raw_snapshot'][field] == snapshot[field]
    assert any('??' in row['toolhelp_path'] for row in report['raw_snapshot']['module_entries'])
    assert any('空间' in path for path in report['raw_snapshot']['mapped_paths'])


@pytest.mark.skipif(sys.platform != 'win32', reason='Unicode Toolhelp/PSAPI regression requires native Windows CI')
def test_real_windows_child_unicode_directory_preserves_actual_image_paths(tmp_path):
    # Copy one OS executable into this test's disposable directory; never edit
    # System32, install a runtime, alter machine settings, or launch via a shell.
    directory = tmp_path / 'worker 环境 & spaces'
    directory.mkdir()
    # Match production: the worker basename is ASCII, its installation path is not.
    executable = directory / 'native-audit-child.exe'
    shutil.copy2(worker._system_windows_directory() / 'System32/cmd.exe', executable)
    process = subprocess.Popen([str(executable), '/d', '/q', '/k'],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               shell=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    ready = queue.Queue(maxsize=1)
    marker = b'VSE_UNICODE_SNAPSHOT_READY'
    def read_ready():
        try:
            for line in iter(process.stdout.readline, b''):
                if marker in line:
                    ready.put(True)
                    return
            ready.put(False)
        except (OSError, ValueError):
            ready.put(False)
    reader = threading.Thread(target=read_ready, daemon=True)
    reader.start()
    try:
        process.stdin.write(b'echo ' + marker + b'\r\n')
        process.stdin.flush()
        assert ready.get(timeout=15), 'Copied cmd.exe did not reach its idle command loop'
        assert process.poll() is None
        snapshot = worker._snapshot(process.pid)
        assert Path(snapshot['process_executable']).samefile(executable)
        assert '环境' in snapshot['process_executable']
        images = [path for path in snapshot['mapped_paths'] if Path(path).name == executable.name]
        assert len(images) == 1 and Path(images[0]).samefile(executable)
        assert '环境' in images[0]
        rows = [row for row in snapshot['module_entries'] if row['psapi_path'] == images[0]]
        assert len(rows) == 1 and rows[0]['module_handle'] and rows[0]['base_address']
        assert 'toolhelp_name' in rows[0] and 'toolhelp_path' in rows[0]
    finally:
        if process.poll() is None:
            try:
                process.stdin.write(b'exit\r\n')
                process.stdin.flush()
                process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
                process.wait(timeout=10)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
        reader.join(timeout=2)
    assert process.returncode == 0


def signed_os_fixture(tmp_path, monkeypatch, name='directxdatabasehelper.dll'):
    """Synthetic signature records test policy; only Server CI proves signatures."""
    from scripts import windows_os_evidence as os_proof
    exe, snapshot = fixture(tmp_path)
    windows = Path(snapshot['windows_root'])
    image = windows / 'System32' / name
    image.write_bytes(b'OS component fixture ' + name.encode())
    snapshot['mapped_paths'].append(str(image))
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    proof = {
        'schema': 1, 'registry_modified': False, 'errors': [], 'amsi_errors': [],
        'windows_directory_api': str(windows),
        'roots': {'windows': str(windows), 'system32': str(windows / 'System32'),
                  'program_data': str(tmp_path / 'ProgramData'), 'program_files': str(tmp_path / 'Program Files')},
        'amsi_providers': [],
        'files': [{'requested_path': str(image), 'canonical_path': str(image), 'errors': [],
                   'reparse_verified': True, 'sha256_before': digest, 'sha256_after': digest,
                   'version_info': {'original_filename': name},
                   'signature': {'status': 'Valid', 'signature_type': 'Catalog', 'is_os_binary': True,
                                 'signer': {'simple_name': 'Microsoft Windows',
                                            'subject': 'CN=Microsoft Windows, O=Microsoft Corporation, C=US',
                                            'issuer': 'Microsoft fixture issuer', 'thumbprint': 'fixture-thumbprint'}}}],
    }
    # Preserve native filesystem fixture paths. Only this pure Windows policy
    # parser gets a synthetic drive on POSIX; no global Path/resolve mocking.
    original_identity = os_proof._identity
    monkeypatch.setattr(os_proof, '_identity', lambda path:
                        original_identity('C:' + path.replace('/', '\\') if path.startswith('/') else path))
    monkeypatch.setattr(worker, '_OS_EVIDENCE_COLLECTOR', None)
    monkeypatch.setattr(worker, '_system_windows_directory', lambda: windows)
    monkeypatch.setattr(worker, '_snapshot', lambda pid: dict(snapshot))
    return exe, snapshot, image, proof


@pytest.mark.parametrize('name', ['directxdatabasehelper.dll', 'coloradapterclient.dll'])
def test_native_signed_os_images_use_existing_proof_and_cached_current_hashes(tmp_path, monkeypatch, name):
    from scripts import windows_os_evidence as os_proof
    from scripts.standalone_runtime_probe import library_violation
    exe, snapshot, image, proof = signed_os_fixture(tmp_path, monkeypatch, name)
    calls = []
    monkeypatch.setattr(os_proof, '_collect', lambda paths, timeout: calls.append(paths) or proof)
    first = worker.collect_native_worker_evidence(123, exe)
    second = worker.collect_native_worker_evidence(123, exe)
    assert first['passed'] and second['passed']
    assert all(row['passed'] for row in first['files'].values())
    assert first['os_security_evidence']['files'][0]['signature'] == proof['files'][0]['signature']
    assert first['observed_os_security_components'][0]['category'] == 'signed_windows_os_component'
    assert worker.validate_native_worker_evidence(first, exe)['passed']
    assert calls == [[str(image)]]  # First/pre-close/final checks reuse only per-process signature work.
    assert library_violation(str(image), str(exe.parent), 'Windows', snapshot['windows_root'])
    image.write_bytes(b'changed after signature verification')
    failed = worker.collect_native_worker_evidence(123, exe)
    assert not failed['passed'] and 'hash differs' in '\n'.join(failed['errors'])
    assert not worker.validate_native_worker_evidence(first, exe)['passed']
    assert len(calls) == 1  # A changed file fails; cached proof cannot bless it.


@pytest.mark.parametrize('change', ['missing', 'signature', 'publisher', 'not-os', 'hash', 'file-changed',
                                     'filename', 'reparse', 'path', 'collector-error', 'os-root'])
def test_native_os_proof_fails_closed_without_relaxing_seven_file_checks(tmp_path, monkeypatch, change):
    exe, snapshot, image, proof = signed_os_fixture(tmp_path, monkeypatch)
    row = proof['files'][0]
    if change == 'missing': proof = None
    elif change == 'signature': row['signature']['status'] = 'NotSigned'
    elif change == 'publisher': row['signature']['signer']['simple_name'] = 'Unknown publisher'
    elif change == 'not-os': row['signature']['is_os_binary'] = False
    elif change == 'hash': row['sha256_after'] = '0' * 64
    elif change == 'file-changed': image.write_bytes(b'changed after proof')
    elif change == 'filename': row['version_info']['original_filename'] = 'unrelated.dll'
    elif change == 'reparse': row['reparse_verified'] = False
    elif change == 'path': row['canonical_path'] = str(image.parent / 'neighbor.dll')
    elif change == 'collector-error': proof['errors'] = ['read-only proof unavailable']
    elif change == 'os-root': proof['windows_directory_api'] = str(tmp_path / 'other-Windows')
    snapshot['os_security_evidence'] = proof
    report = worker.validate_native_worker_snapshot(123, exe, snapshot)
    assert report['available'] and not report['passed']
    assert all(record['passed'] for record in report['files'].values())
    assert report['unverified_os_security_components'] and not report['observed_os_security_components']
    assert 'Unverified OS/security component' in '\n'.join(report['errors'])


def test_saved_native_proof_requires_independent_current_signature_collection(tmp_path, monkeypatch):
    import copy
    from scripts import windows_os_evidence as os_proof
    exe, snapshot, _, proof = signed_os_fixture(tmp_path, monkeypatch)
    snapshot['os_security_evidence'] = proof
    report = worker.validate_native_worker_snapshot(123, exe, snapshot)
    assert report['passed']
    failed = copy.deepcopy(proof)
    failed['files'][0]['signature']['status'] = 'HashMismatch'
    calls = []
    monkeypatch.setattr(os_proof, '_collect', lambda paths, timeout: calls.append(paths) or failed)
    result = worker.validate_native_worker_evidence(report, exe)
    assert calls and not result['passed']
    assert 'Current OS signature evidence failed' in '\n'.join(result['errors'])


def test_native_collection_keeps_actual_modules_and_failed_os_proof(tmp_path, monkeypatch):
    from scripts import windows_os_evidence as os_proof
    exe, snapshot, _, _ = signed_os_fixture(tmp_path, monkeypatch)
    def denied(*args):
        raise RuntimeError('signature collector unavailable')
    monkeypatch.setattr(os_proof, '_collect', denied)
    report = worker.collect_native_worker_evidence(123, exe)
    assert report['available'] and not report['passed']
    assert report['mapped_paths'] == sorted(snapshot['mapped_paths'])
    assert all(row['passed'] for row in report['files'].values())
    assert 'signature collector unavailable' in json.dumps(report['os_security_evidence'])


def test_worker_without_candidate_never_invokes_os_signature_collector(tmp_path, monkeypatch):
    from scripts import windows_os_evidence as os_proof
    exe, snapshot = fixture(tmp_path)
    monkeypatch.setattr(worker, '_snapshot', lambda pid: snapshot)
    def forbidden(*args):
        raise AssertionError('No candidate should launch signature inspection')
    monkeypatch.setattr(os_proof, 'WindowsOSEvidenceCollector', forbidden)
    assert worker.collect_native_worker_evidence(123, exe)['passed']


def test_os_signature_evidence_cannot_allow_foreign_crt_or_unknown_dll(tmp_path, monkeypatch):
    from scripts import windows_os_evidence as os_proof
    exe, snapshot, _, proof = signed_os_fixture(tmp_path, monkeypatch)
    foreign = Path(snapshot['windows_root']) / 'System32/concrt140.dll'
    foreign.write_bytes((exe.parent / 'concrt140.dll').read_bytes())
    snapshot['mapped_paths'].append(str(foreign))
    unknown = foreign.with_name('unknown-signed-vendor.dll')
    unknown.write_bytes(b'unknown')
    snapshot['mapped_paths'].append(str(unknown))
    calls = []
    monkeypatch.setattr(os_proof, '_collect', lambda paths, timeout: calls.append(paths) or proof)
    report = worker.collect_native_worker_evidence(123, exe)
    assert not report['passed']
    assert report['observed_os_security_components'][0]['allowed']
    assert len(calls[0]) == 1 and Path(calls[0][0]).name == 'directxdatabasehelper.dll'
    assert any('concrt140.dll' in error for error in report['errors'])
    assert any('unknown-signed-vendor.dll' in error for error in report['errors'])
