import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import windows_app_local_runtime as runtime


def stage(tmp_path):
    dll = tmp_path / runtime.DLL
    dll.parent.mkdir(parents=True)
    content = b'original wheel VCOMP fixture'
    dll.write_bytes(content)
    dist = dll.parent.parent / 'shiboken6-6.12.0.dist-info'
    dist.mkdir()
    (dist / 'METADATA').write_text('Name: shiboken6\nVersion: 6.12.0\n')
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip('=')
    (dist / 'RECORD').write_text(f'shiboken6/vcomp140.dll,sha256={digest},{len(content)}\n')
    return dll, dist


class Loader:
    def __init__(self, before=(), loaded=None):
        self.current = list(before)
        self.loaded = loaded
        self.calls = []
        self.handle = object()

    def paths(self, name):
        assert name == 'vcomp140.dll'
        return self.current

    def load(self, path):
        self.calls.append(path)
        self.current = [path] if self.loaded is None else self.loaded
        return self.handle


@pytest.fixture(autouse=True)
def reset_handles(monkeypatch):
    monkeypatch.setattr(runtime, '_HANDLES', {})
    monkeypatch.setattr(runtime, 'PRELOAD_EVIDENCE', {})


def test_preload_original_wheel_bytes_with_absolute_path_and_retained_handle(tmp_path):
    dll, _ = stage(tmp_path)
    metadata = runtime.write_runtime_metadata(tmp_path)
    loader = Loader()
    result = runtime.preload_runtime(tmp_path, _loader=loader)
    assert loader.calls == [dll]
    assert runtime._HANDLES[str(dll)] is loader.handle
    assert result['path'] == str(dll)
    assert result['sha256'] == metadata['sha256']
    assert result['loaded_paths'] == [str(dll)]
    assert result['retained_handle'] is True
    runtime.preload_runtime(tmp_path, _loader=loader)
    assert loader.calls == [dll]


@pytest.mark.parametrize('corruption', ['dll', 'hash', 'size', 'missing', 'duplicate'])
def test_build_rejects_unverified_or_ambiguous_wheel_record(tmp_path, corruption):
    dll, dist = stage(tmp_path)
    record = dist / 'RECORD'
    if corruption == 'dll': dll.write_bytes(b'changed')
    elif corruption == 'hash': record.write_text('shiboken6/vcomp140.dll,sha256=bad,27\n')
    elif corruption == 'size': record.write_text(record.read_text().rsplit(',', 1)[0] + ',999\n')
    elif corruption == 'missing': record.unlink()
    else: record.write_text(record.read_text() * 2)
    with pytest.raises(RuntimeError, match='original Shiboken wheel RECORD'):
        runtime.write_runtime_metadata(tmp_path)


def test_startup_rejects_modified_runtime_before_loading(tmp_path):
    dll, _ = stage(tmp_path)
    runtime.write_runtime_metadata(tmp_path)
    dll.write_bytes(b'tampered')
    loader = Loader()
    with pytest.raises(RuntimeError, match='checksum mismatch'):
        runtime.preload_runtime(tmp_path, _loader=loader)
    assert loader.calls == []


@pytest.mark.parametrize('before', ['foreign', 'mixed'])
def test_preexisting_foreign_vcomp_is_never_accepted(tmp_path, before):
    dll, _ = stage(tmp_path)
    runtime.write_runtime_metadata(tmp_path)
    foreign = tmp_path.parent / 'System32/vcomp140.dll'
    loader = Loader([foreign] + ([dll] if before == 'mixed' else []))
    with pytest.raises(RuntimeError, match='Foreign VCOMP already loaded'):
        runtime.preload_runtime(tmp_path, _loader=loader)
    assert loader.calls == []


@pytest.mark.parametrize('result', ['foreign', 'missing', 'mixed'])
def test_actual_loaded_path_must_match_even_after_full_path_load(tmp_path, result):
    dll, _ = stage(tmp_path)
    runtime.write_runtime_metadata(tmp_path)
    foreign = tmp_path.parent / 'System32/vcomp140.dll'
    paths = [] if result == 'missing' else [foreign] + ([dll] if result == 'mixed' else [])
    with pytest.raises(RuntimeError, match='resolved outside its authenticated path'):
        runtime.preload_runtime(tmp_path, _loader=Loader(loaded=paths))


def test_release_manifest_must_agree_with_record_derived_preload_metadata(tmp_path):
    dll, _ = stage(tmp_path)
    metadata = runtime.write_runtime_metadata(tmp_path)
    manifest = tmp_path / 'bundle-manifest.json'
    manifest.write_text(json.dumps({'files': {runtime.DLL: metadata['sha256'], runtime.METADATA: metadata['metadata_sha256']}}))
    assert runtime.preload_runtime(tmp_path, _loader=Loader())['path'] == str(dll)
    manifest.write_text(json.dumps({'files': {runtime.DLL: '0' * 64, runtime.METADATA: metadata['metadata_sha256']}}))
    with pytest.raises(RuntimeError, match='disagrees with release manifest'):
        runtime.preload_runtime(tmp_path, _loader=Loader())


@pytest.mark.parametrize('relative', ['../vcomp140.dll', 'runtime/Lib/site-packages/PySide6/vcomp140.dll', r'C:\Windows\System32\vcomp140.dll'])
def test_metadata_cannot_select_a_different_library(tmp_path, relative):
    stage(tmp_path)
    runtime.write_runtime_metadata(tmp_path)
    file = tmp_path / runtime.METADATA
    metadata = json.loads(file.read_text())
    metadata['dll'] = relative
    file.write_text(json.dumps(metadata))
    with pytest.raises(RuntimeError, match='Invalid app-local'):
        runtime.preload_runtime(tmp_path, _loader=Loader())


def test_loader_uses_absolute_path_and_only_dll_directory_plus_system32(tmp_path):
    calls = []
    loader = runtime._WindowsLoader.__new__(runtime._WindowsLoader)
    loader.ctypes = SimpleNamespace(WinDLL=lambda *args, **kwargs: calls.append((args, kwargs)))
    path = tmp_path / 'verified/vcomp140.dll'
    loader.load(path)
    assert calls == [((str(path),), {'use_last_error': True, 'winmode': 0x900})]
