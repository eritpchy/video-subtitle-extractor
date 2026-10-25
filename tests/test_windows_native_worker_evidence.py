"""Windows worker audit: filesystem fixtures, plus read-only native API smoke."""
import hashlib
import json
import os
from pathlib import Path
import sys

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
    return exe, snapshot


def test_exact_seven_images_have_actual_paths_hashes_and_read_only_revalidation(tmp_path, monkeypatch):
    exe, snapshot = fixture(tmp_path)
    monkeypatch.setattr(worker, '_snapshot', lambda pid: snapshot)
    monkeypatch.setattr(worker, '_system_windows_directory', lambda: Path(snapshot['windows_root']))
    report = worker.collect_native_worker_evidence(123, exe)
    assert report['available'] and report['passed'] and not report['errors']
    assert report['method'] == worker.METHOD
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
                                   'wrong-executable', 'missing-ort', 'wrong-inventory', 'failed'])
def test_saved_evidence_does_not_trust_passed_flag(tmp_path, change, monkeypatch):
    exe, snapshot = fixture(tmp_path)
    monkeypatch.setattr(worker, '_system_windows_directory', lambda: Path(snapshot['windows_root']))
    report = worker.validate_native_worker_snapshot(123, exe, snapshot)
    assert report['passed']
    if change == 'passed-only': report = {'passed': True}
    elif change == 'missing-files': report.pop('files')
    elif change == 'forged-hash': report['files']['onnxruntime.dll']['sha256'] = 'f' * 64
    elif change == 'wrong-method': report['method'] = 'pretend'
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
    assert "OpenProcess(0x1000, False, pid)" in source
    assert 'os.environ' not in source
    assert all(name not in source for name in ('WriteProcessMemory', 'CreateRemoteThread', 'AdjustTokenPrivileges'))


@pytest.mark.skipif(sys.platform != 'win32', reason='Read-only Toolhelp API smoke requires Windows CI')
def test_real_windows_snapshot_reads_current_process_without_admin():
    snapshot = worker._snapshot(os.getpid())
    assert Path(snapshot['process_executable']).samefile(sys.executable)
    assert any(Path(path).samefile(sys.executable) for path in snapshot['mapped_paths'])
    assert Path(snapshot['windows_root']).is_dir()
