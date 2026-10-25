"""Pure unit checks: no model binaries, license acceptance, or network access."""
import hashlib
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
import pytest
import build
import fetch_sources


def test_native_target_rejects_cross_compile():
    with patch('platform.system', return_value='Linux'), patch('platform.machine', return_value='x86_64'):
        build.native_target('linux-x64')
        with pytest.raises(RuntimeError):
            build.native_target('windows-x64')


def test_safe_paths():
    for bad in ('', '../escape', '/absolute', 'C:/drive', 'a\\b', 'a//b'):
        with pytest.raises(ValueError):
            fetch_sources.relative(bad)


def test_offline_source_missing_has_no_network(tmp_path):
    pins = tmp_path / 'pins.json'
    pins.write_text(json.dumps([{'file': 'source.tar.gz', 'bytes': 3, 'sha256': '0' * 64, 'url': 'https://unused.invalid'}]))
    with patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')):
        with pytest.raises(FileNotFoundError):
            fetch_sources.fetch(tmp_path/'out', tmp_path/'cache', True, pins)


def test_source_cache_hash_failure(tmp_path):
    cache=tmp_path/'cache';cache.mkdir();(cache/'source.tar.gz').write_bytes(b'bad')
    pins=tmp_path/'pins.json';pins.write_text(json.dumps([{'file':'source.tar.gz','bytes':3,'sha256':'0'*64,'url':'https://unused.invalid'}]))
    with pytest.raises(ValueError):fetch_sources.fetch(tmp_path/'out',cache,True,pins)


def test_inventory_includes_exact_bytes_and_no_python(tmp_path):
    (tmp_path/'native-ocr').write_bytes(b'executable')
    r=build.inventory(tmp_path,'linux-x64',{'test':True})
    assert not r['release_ready'] and r['python_runtime_bytes']==0
    assert r['files'][0]['sha256']==hashlib.sha256(b'executable').hexdigest()
    assert r['assembled_total_bytes']==sum(p.stat().st_size for p in tmp_path.iterdir())


def test_vc_no_rights_never_copies(tmp_path):
    assert not build.vc_runtime(None,None,tmp_path,['MSVCP140.dll'])['complete']
    with pytest.raises(PermissionError):
        build.vc_runtime(tmp_path,tmp_path/'license',tmp_path,['MSVCP140.dll'],False)


def test_wrong_models_rejected_before_hashing(tmp_path):
    (tmp_path/'model-manifest.json').write_text('[]')
    with pytest.raises(ValueError):build.verify_models(tmp_path)


def test_windows_distribution_requires_entitlement_before_build():
    from types import SimpleNamespace
    args=SimpleNamespace(platform='windows',arch='x64',runner_test_only=False,
        acknowledge_vc_redistribution_license=False,vc_runtime_dir=None,vc_license=None)
    with patch.object(build,'native_target'):
        with pytest.raises(PermissionError):build.build(args)


def test_runner_only_cannot_copy_redist():
    from types import SimpleNamespace
    args=SimpleNamespace(platform='windows',arch='x64',runner_test_only=True,
        acknowledge_vc_redistribution_license=True,vc_runtime_dir=None,vc_license=None)
    with patch.object(build,'native_target'):
        with pytest.raises(ValueError):build.build(args)


def test_windows_toolhelp_header_follows_win32_definitions():
    source=(Path(build.__file__).parent/'worker.cpp').read_text(encoding='utf-8')
    assert source.index('#include <windows.h>') < source.index('#include <tlhelp32.h>')
    assert source.index('#define NOMINMAX') < source.index('#include <windows.h>')
