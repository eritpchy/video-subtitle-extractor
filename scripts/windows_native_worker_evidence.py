"""Read-only Windows worker module/path evidence, never an installer or injector.

Toolhelp snapshots and a QUERY_LIMITED_INFORMATION process handle establish the
live image paths. Hashes describe those files on disk, not process memory. This
is a point-in-time audit after inference, not a lifetime or network audit.

https://learn.microsoft.com/en-us/windows/win32/api/tlhelp32/nf-tlhelp32-createtoolhelp32snapshot
https://learn.microsoft.com/en-us/windows/win32/api/tlhelp32/ns-tlhelp32-moduleentry32w
https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-queryfullprocessimagenamew
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import ntpath
from pathlib import Path, PurePosixPath
import re
import sys


REQUIRED_FILES = frozenset({
    'native-ocr.exe', 'onnxruntime.dll', 'concrt140.dll', 'msvcp140.dll',
    'msvcp140_1.dll', 'vcruntime140.dll', 'vcruntime140_1.dll',
})
MAX_MODULES = 4096
METHOD = 'windows-toolhelp32-querylimited-v1'


def _valid_pid(pid):
    if type(pid) is not int or not 0 < pid <= 0xffffffff:
        raise ValueError('Worker PID must be a nonzero DWORD integer')


def _canonical(path):
    if not isinstance(path, (str, Path)) or not str(path) or any(ord(c) < 32 for c in str(path)):
        raise ValueError('Invalid worker image path')
    if sys.platform == 'win32':
        local = str(path)
        if local.startswith('\\\\?\\'):
            local = local[4:]
        if not re.match(r'^[A-Za-z]:[\\/]', local) or ':' in local[2:]:
            raise ValueError('Only local absolute Windows worker image paths are accepted')
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('Worker image path is not absolute')
    return path.resolve(strict=True)


def _file_hash(path):
    before = path.stat()
    if not path.is_file():
        raise ValueError('Worker image is not a regular file: ' + str(path))
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise RuntimeError('Worker image changed while hashing: ' + str(path))
    return {'sha256': digest, 'bytes': after.st_size}


def _system_windows_directory():
    """OS-owned directory, never an inherited SystemRoot environment value."""
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32.dll', use_last_error=True, winmode=0x800)
    function = kernel.GetWindowsDirectoryW
    function.argtypes, function.restype = [wintypes.LPWSTR, wintypes.UINT], wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    size = function(buffer, len(buffer))
    if not 0 < size < len(buffer):
        raise RuntimeError('Cannot establish Windows OS directory')
    return _canonical(buffer.value)


def _checked_module_path(pid, entry_pid, module_name, path):
    if entry_pid != pid:
        raise RuntimeError('Toolhelp entry belongs to another PID')
    if not path or len(path) >= 259 or not module_name or len(module_name) >= 255:
        raise RuntimeError('Truncated/empty Toolhelp module name or path')
    if ntpath.basename(path).lower() != module_name.lower():
        raise RuntimeError('Toolhelp module name/path mismatch')
    return path


def _report(pid, executable):
    return {'schema': 1, 'available': False, 'passed': False, 'errors': [],
            'pid': pid, 'executable': str(executable), 'mapped_paths': [], 'files': {},
            'method': METHOD, 'process_query_access': 0x1000,
            'matches_worker': False, 'forbidden_python_paddle_libraries': [],
            'scope': 'Live worker image paths and on-disk inventory hashes after inference; not process-memory, lifetime or network certification.'}


def _inventory(root):
    inventory = root / 'size-audit.json'
    if inventory.is_symlink() or _canonical(inventory).parent != root:
        raise ValueError('Unsafe native worker inventory')
    document = json.loads(inventory.read_text(encoding='utf-8'))
    rows = document['files']
    if not isinstance(rows, list):
        raise ValueError('Native worker inventory files must be a list')
    selected, seen = {}, set()
    for row in rows:
        name = row['path']
        relative = PurePosixPath(name)
        if (not isinstance(name, str) or not name or relative.is_absolute() or '..' in relative.parts
                or '\\' in name or ':' in name or str(relative) != name or name.lower() in seen):
            raise ValueError('Unsafe/duplicate native worker inventory path')
        seen.add(name.lower())
        if name.lower() not in REQUIRED_FILES:
            continue
        if (type(row.get('bytes')) is not int or row['bytes'] <= 0
                or not re.fullmatch(r'[0-9a-f]{64}', str(row.get('sha256', '')))):
            raise ValueError('Invalid native worker inventory hash/size: ' + name)
        selected[name.lower()] = row
    missing = REQUIRED_FILES - selected.keys()
    if missing:
        raise ValueError('Native worker inventory missing required files: ' + ', '.join(sorted(missing)))
    return selected, _file_hash(inventory)['sha256']


def validate_native_worker_snapshot(pid, executable, snapshot):
    """Validate an injectable read-only snapshot against the sibling inventory.

    snapshot is {process_executable, mapped_paths, windows_root}; paths are
    native absolute paths, allowing filesystem-backed fixtures on other hosts.
    No unknown security provider or broad Microsoft-signature exemption exists.
    """
    report = _report(pid, executable)
    try:
        _valid_pid(pid)
        if not isinstance(snapshot, dict):
            raise ValueError('Missing Windows worker snapshot')
        paths = snapshot['mapped_paths']
        if not isinstance(paths, list) or not 0 < len(paths) <= MAX_MODULES:
            raise ValueError('Empty or unbounded Windows worker module snapshot')
        # Lazy import avoids importing a process probe during ordinary startup.
        from scripts.standalone_runtime_probe import _identity, library_violation
        expected_exe = _canonical(executable)
        if expected_exe.name.lower() != 'native-ocr.exe' or Path(executable).is_symlink():
            raise ValueError('Expected the regular native-ocr.exe worker')
        root = expected_exe.parent
        windows = _canonical(snapshot['windows_root'])
        if not windows.is_dir():
            raise ValueError('Windows OS root is not a directory')
        process_exe = _canonical(snapshot['process_executable'])
        canonical = [_canonical(path) for path in paths]
        identities = [_identity(str(path), 'Windows') for path in canonical]
        if len(set(identities)) != len(identities):
            raise ValueError('Duplicate Windows worker module snapshot entry')
        report.update(available=True, process_executable=str(process_exe), windows_root=str(windows),
                      executable=str(expected_exe), mapped_paths=sorted(map(str, canonical)),
                      matches_worker=_identity(str(process_exe), 'Windows') == _identity(str(expected_exe), 'Windows'))
        if not report['matches_worker']:
            report['errors'].append('Live process executable differs from expected native worker')
        rows, report['inventory_sha256'] = _inventory(root)
        groups = {}
        for path in canonical:
            name = path.name.lower()
            groups.setdefault(name, []).append(path)
            if 'python' in name or 'paddle' in name:
                report['forbidden_python_paddle_libraries'].append(str(path))
                report['errors'].append('Forbidden Python/Paddle worker image: ' + str(path))
            if name not in REQUIRED_FILES:
                if path.is_relative_to(root):
                    report['errors'].append('Unreviewed app-local worker image: ' + str(path))
                else:
                    violation = library_violation(str(path), str(root), 'Windows', str(windows))
                    if violation:
                        report['errors'].append(violation)
        for name in sorted(REQUIRED_FILES):
            row = rows[name]
            expected = root / row['path']
            if expected.is_symlink() or _canonical(expected).parent != root:
                raise ValueError('Required worker image escapes its sibling directory: ' + name)
            expected = _canonical(expected)
            expected_hash = _file_hash(expected)
            entry = {'expected_path': str(expected), 'expected_sha256': row['sha256'],
                     'expected_bytes': row['bytes'], 'loaded_files': []}
            report['files'][name] = entry
            if expected_hash != {'sha256': row['sha256'], 'bytes': row['bytes']}:
                report['errors'].append('Worker inventory checksum/size mismatch: ' + name)
            loaded = groups.get(name, [])
            entry['path_matches'] = (len(loaded) == 1 and
                                     _identity(str(loaded[0]), 'Windows') == _identity(str(expected), 'Windows'))
            if not entry['path_matches']:
                report['errors'].append('Missing/mixed/foreign required worker image: ' + name)
            for path in loaded:
                observed = expected_hash if path == expected else _file_hash(path)
                entry['loaded_files'].append({'path': str(path), **observed,
                                             'inventory_match': observed == {'sha256': row['sha256'], 'bytes': row['bytes']}})
            if len(entry['loaded_files']) == 1:
                entry.update(entry['loaded_files'][0])
            entry['passed'] = bool(entry['path_matches'] and entry['loaded_files'][0]['inventory_match'])
            if loaded and not all(item['inventory_match'] for item in entry['loaded_files']):
                report['errors'].append('Loaded worker file checksum/size mismatch: ' + name)
        report['passed'] = not report['errors']
    except Exception as error:
        report['errors'].append(type(error).__name__ + ': ' + str(error))
    return report


def _snapshot(pid):
    """Read target modules with Toolhelp, holding a read-only process handle."""
    _valid_pid(pid)
    if sys.platform != 'win32':
        raise RuntimeError('Windows worker inspection requires native Windows')
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        raise RuntimeError('Windows x64 worker inspection requires a 64-bit auditor')
    from ctypes import wintypes

    class MODULEENTRY32W(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('th32ModuleID', wintypes.DWORD),
                    ('th32ProcessID', wintypes.DWORD), ('GlblcntUsage', wintypes.DWORD),
                    ('ProccntUsage', wintypes.DWORD), ('modBaseAddr', ctypes.POINTER(wintypes.BYTE)),
                    ('modBaseSize', wintypes.DWORD), ('hModule', wintypes.HMODULE),
                    ('szModule', wintypes.WCHAR * 256), ('szExePath', wintypes.WCHAR * 260)]

    kernel = ctypes.WinDLL('kernel32.dll', use_last_error=True, winmode=0x800)
    signatures = {
        'OpenProcess': ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
        'GetProcessId': ([wintypes.HANDLE], wintypes.DWORD),
        'GetExitCodeProcess': ([wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        'QueryFullProcessImageNameW': ([wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        'CreateToolhelp32Snapshot': ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
        'Module32FirstW': ([wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)], wintypes.BOOL),
        'Module32NextW': ([wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)], wintypes.BOOL),
        'GetWindowsDirectoryW': ([wintypes.LPWSTR, wintypes.UINT], wintypes.UINT),
    }
    for name, (args, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result

    def error(action):
        return OSError(ctypes.get_last_error(), action + ': ' + str(ctypes.WinError(ctypes.get_last_error())))

    def running(process):
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(process, ctypes.byref(code)):
            raise error('GetExitCodeProcess')
        if code.value != 259:
            raise RuntimeError('Worker exited during module inspection')

    process = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION only.
    if not process:
        raise error('OpenProcess QUERY_LIMITED_INFORMATION')
    try:
        if kernel.GetProcessId(process) != pid:
            raise RuntimeError('Worker process handle PID mismatch')
        running(process)
        image = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(image))
        if not kernel.QueryFullProcessImageNameW(process, 0, image, ctypes.byref(length)):
            raise error('QueryFullProcessImageNameW')
        if not 0 < length.value < len(image) - 1:
            raise RuntimeError('Truncated/empty worker executable path')
        windows = ctypes.create_unicode_buffer(32768)
        size = kernel.GetWindowsDirectoryW(windows, len(windows))
        if not 0 < size < len(windows):
            raise RuntimeError('Cannot establish Windows OS directory')
        invalid = ctypes.c_void_p(-1).value
        for _ in range(5):
            snapshot = kernel.CreateToolhelp32Snapshot(0x8 | 0x10, pid)
            if snapshot not in (None, 0, invalid):
                break
            if ctypes.get_last_error() != 24:  # ERROR_BAD_LENGTH: loader list changed.
                raise error('CreateToolhelp32Snapshot')
        else:
            raise RuntimeError('Unstable Toolhelp module snapshot after five attempts')
        try:
            entry = MODULEENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            if not kernel.Module32FirstW(snapshot, ctypes.byref(entry)):
                raise error('Module32FirstW')
            paths = []
            while True:
                path = entry.szExePath
                _checked_module_path(pid, entry.th32ProcessID, entry.szModule, path)
                paths.append(path)
                if len(paths) > MAX_MODULES:
                    raise RuntimeError('Unbounded Toolhelp module snapshot')
                entry.dwSize = ctypes.sizeof(entry)
                if not kernel.Module32NextW(snapshot, ctypes.byref(entry)):
                    if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES only.
                        raise error('Module32NextW')
                    break
            running(process)
            return {'process_executable': image.value, 'mapped_paths': paths, 'windows_root': windows.value}
        finally:
            if not kernel.CloseHandle(snapshot):
                raise error('CloseHandle module snapshot')
    finally:
        if not kernel.CloseHandle(process):
            raise error('CloseHandle process query')


def collect_native_worker_evidence(pid, executable):
    """Never hide collection failures or require admin/injection privileges."""
    try:
        return validate_native_worker_snapshot(pid, executable, _snapshot(pid))
    except Exception as error:
        report = _report(pid, executable)
        report['errors'].append(type(error).__name__ + ': ' + str(error))
        return report


def validate_native_worker_evidence(evidence, executable):
    """Revalidate saved evidence after worker exit; never trust its passed flag.

    The caller binds this record to the audited parent/inference request. This
    function checks completeness and rehashes files against the native inventory;
    it cannot independently recreate an exited process's historical snapshot.
    """
    result = _report(evidence.get('pid') if isinstance(evidence, dict) else None, executable)
    try:
        if (not isinstance(evidence, dict) or evidence.get('schema') != 1
                or evidence.get('available') is not True or evidence.get('passed') is not True
                or evidence.get('errors') != [] or evidence.get('method') != METHOD
                or evidence.get('process_query_access') != 0x1000):
            raise ValueError('Incomplete/failed Windows worker evidence')
        if sys.platform == 'win32' and _canonical(evidence['windows_root']) != _system_windows_directory():
            raise ValueError('Saved Windows worker OS root differs from the actual OS directory')
        result = validate_native_worker_snapshot(evidence['pid'], executable, evidence)
        if not result['passed']:
            return result
        for field in ('executable', 'process_executable', 'windows_root', 'matches_worker',
                      'mapped_paths', 'files', 'inventory_sha256', 'forbidden_python_paddle_libraries'):
            if evidence.get(field) != result[field]:
                raise ValueError('Saved Windows worker evidence mismatch: ' + field)
    except Exception as error:
        result['passed'] = False
        result['errors'].append(type(error).__name__ + ': ' + str(error))
    return result
