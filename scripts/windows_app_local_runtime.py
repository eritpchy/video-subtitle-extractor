"""Load the existing wheel-owned OpenMP runtime before any Windows workload.

No runtime installation, system PATH change or DLL copying. The build checks
the unchanged Shiboken wheel RECORD; the owned startup hook checks the captured
hash, pre-existing loaded modules and the actual result of an absolute load.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import sys

METADATA = 'runtime/windows-app-local-runtime.json'
DLL = 'runtime/Lib/site-packages/shiboken6/vcomp140.dll'
PRELOAD_EVIDENCE = {}
_HANDLES = {}


def _digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _inside_file(root, relative):
    path = root / relative
    if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root):
        raise RuntimeError(f'Missing/unsafe app-local Windows runtime file: {relative}')
    return path.resolve()


def write_runtime_metadata(root):
    """Build-side only: authenticate existing bytes against installed RECORD."""
    from importlib.metadata import distributions
    root = Path(root).resolve(strict=True)
    packages = root / 'runtime/Lib/site-packages'
    matches = [dist for dist in distributions(path=[str(packages)])
               if dist.metadata.get('Name', '').lower().replace('_', '-') == 'shiboken6']
    if len(matches) != 1:
        raise RuntimeError('Exactly one original Shiboken wheel is required for app-local VCOMP')
    distribution = matches[0]
    record = distribution.read_text('RECORD')
    rows = [] if record is None else [row for row in csv.reader(io.StringIO(record))
                                    if row and row[0] == 'shiboken6/vcomp140.dll']
    path = _inside_file(root, DLL)
    digest = _digest(path)
    expected = 'sha256=' + base64.urlsafe_b64encode(bytes.fromhex(digest)).decode().rstrip('=')
    if len(rows) != 1 or rows[0] != ['shiboken6/vcomp140.dll', expected, str(path.stat().st_size)]:
        raise RuntimeError('App-local VCOMP bytes do not match original Shiboken wheel RECORD')
    metadata = {'schema': 1, 'dll': DLL, 'sha256': digest, 'distribution': 'shiboken6',
                'version': distribution.version, 'source': 'unmodified installed wheel RECORD'}
    destination = root / METADATA
    with destination.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(json.dumps(metadata, indent=2) + '\n')
    return {'metadata': METADATA, 'metadata_sha256': _digest(destination), **metadata}


def checked_runtime_metadata(root):
    root = Path(root).resolve(strict=True)
    metadata_path = _inside_file(root, METADATA)
    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
    if (metadata.get('schema') != 1 or metadata.get('dll') != DLL
            or metadata.get('distribution') != 'shiboken6'
            or not re.fullmatch(r'[0-9a-f]{64}', str(metadata.get('sha256', '')))):
        raise RuntimeError('Invalid app-local Windows runtime metadata')
    candidate = _inside_file(root, DLL)
    if _digest(candidate) != metadata['sha256']:
        raise RuntimeError('App-local VCOMP checksum mismatch')
    # During build, this small RECORD-derived inventory exists before the final
    # release manifest. In a release, require agreement with that manifest too.
    manifest_path = root / 'bundle-manifest.json'
    if manifest_path.exists():
        manifest = json.loads(_inside_file(root, 'bundle-manifest.json').read_text(encoding='utf-8'))
        files = manifest.get('files', {})
        if files.get(METADATA) != _digest(metadata_path) or files.get(DLL) != metadata['sha256']:
            raise RuntimeError('App-local Windows runtime disagrees with release manifest')
    return candidate, metadata


class _WindowsLoader:
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.wintypes = ctypes, wintypes
        self.kernel = ctypes.WinDLL('kernel32.dll', use_last_error=True, winmode=0x800)
        self.kernel.GetCurrentProcess.restype = wintypes.HANDLE
        self.kernel.K32EnumProcessModules.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.HMODULE), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.K32EnumProcessModules.restype = wintypes.BOOL
        self.kernel.K32GetModuleFileNameExW.argtypes = [wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
        self.kernel.K32GetModuleFileNameExW.restype = wintypes.DWORD

    def paths(self, name):
        ctypes, wintypes, kernel = self.ctypes, self.wintypes, self.kernel
        process = kernel.GetCurrentProcess()
        capacity = 256
        while capacity <= 65536:
            modules = (wintypes.HMODULE * capacity)()
            needed = wintypes.DWORD()
            if not kernel.K32EnumProcessModules(process, modules, ctypes.sizeof(modules), ctypes.byref(needed)):
                raise ctypes.WinError(ctypes.get_last_error())
            if needed.value <= ctypes.sizeof(modules):
                break
            capacity = needed.value // ctypes.sizeof(wintypes.HMODULE) + 32
        else:
            raise RuntimeError('Unbounded Windows loaded-module inventory')
        result = []
        for module in modules[:needed.value // ctypes.sizeof(wintypes.HMODULE)]:
            buffer = ctypes.create_unicode_buffer(32768)
            length = kernel.K32GetModuleFileNameExW(process, module, buffer, len(buffer))
            if not length or length >= len(buffer):
                raise RuntimeError('Cannot verify loaded Windows runtime path')
            path = Path(buffer.value)
            if path.name.lower() == name.lower():
                result.append(path.resolve(strict=True))
        return result

    def load(self, path):
        # VCOMP's only mandatory imports are Windows KERNEL32/USER32. An
        # absolute filename plus DLL-load-dir/System32 avoids ambient PATH and
        # unrelated AddDllDirectory entries. Do not use a bare DLL filename.
        return self.ctypes.WinDLL(str(path), use_last_error=True, winmode=0x900)


def preload_runtime(root, *, _loader=None):
    """Fail closed on a foreign preloaded copy; retain the authenticated handle."""
    if sys.platform != 'win32' and _loader is None:
        raise RuntimeError('App-local Windows runtime preload requires Windows')
    candidate, metadata = checked_runtime_metadata(root)
    loader = _loader if _loader is not None else _WindowsLoader()
    before = loader.paths('vcomp140.dll')
    if any(path != candidate for path in before):
        raise RuntimeError('Foreign VCOMP already loaded before portable startup: ' + ', '.join(map(str, before)))
    key = str(candidate)
    if key not in _HANDLES:
        _HANDLES[key] = loader.load(candidate)
    after = loader.paths('vcomp140.dll')
    if not after or any(path != candidate for path in after):
        raise RuntimeError('App-local VCOMP load resolved outside its authenticated path: ' + ', '.join(map(str, after)))
    PRELOAD_EVIDENCE.clear()
    PRELOAD_EVIDENCE.update(path=key, sha256=metadata['sha256'], distribution='shiboken6',
                           version=metadata['version'], loaded_paths=list(map(str, after)), retained_handle=True)
    return dict(PRELOAD_EVIDENCE)
