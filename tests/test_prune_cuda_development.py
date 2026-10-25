import base64
import hashlib
import json
from pathlib import Path

import pytest

from scripts import prune_cuda_development as module


@pytest.fixture
def payload(tmp_path, monkeypatch):
    info = tmp_path / 'nvidia_nvjitlink_cu12-12.6.85.dist-info'
    info.mkdir()
    (info / 'METADATA').write_text('Name: nvidia-nvjitlink-cu12\nVersion: 12.6.85\n')
    (info / 'WHEEL').write_text('Tag: py3-none-win_amd64\n')
    (info / 'License.txt').write_text('license')
    (info / 'RECORD').write_text('original record')
    pins = {}
    for name, data in [(module.STATIC, b'!<arch>\nstatic archive'), (module.DLL, b'MZruntime dll'),
                       ('nvidia/nvjitlink/lib/x64/nvJitLink.lib', b'import library')]:
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        pins[name] = (base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('='), len(data))
    monkeypatch.setattr(module, 'PINS', {'12.6.85': {k: pins[k] for k in (module.STATIC, module.DLL)}})
    return tmp_path


def test_dry_run_preserves_all(payload):
    report = module.prune(payload)
    assert report['status'] == 'dry-run'
    assert (payload / module.STATIC).exists()
    assert not (payload / 'cuda-development-prune.json').exists()


def test_apply_removes_only_static_and_records_evidence(payload):
    result = module.prune(payload, apply=True)
    assert not (payload / module.STATIC).exists()
    assert (payload / module.DLL).read_bytes() == b'MZruntime dll'
    assert (payload / 'nvidia/nvjitlink/lib/x64/nvJitLink.lib').exists()
    assert (payload / 'nvidia_nvjitlink_cu12-12.6.85.dist-info/RECORD').read_text() == 'original record'
    assert json.loads((payload / 'cuda-development-prune.json').read_text()) == result
    assert result['gpu_inference_verified'] is False


@pytest.mark.parametrize('path', [module.STATIC, module.DLL])
def test_corrupt_file_cannot_be_pruned(payload, path):
    (payload / path).write_bytes(b'corrupted')
    with pytest.raises(ValueError, match='corrupted'):
        module.prune(payload, apply=True)
    assert (payload / module.STATIC).exists()


def test_missing_dll_cannot_prune(payload):
    (payload / module.DLL).unlink()
    with pytest.raises(ValueError, match='missing'):
        module.prune(payload, apply=True)
    assert (payload / module.STATIC).exists()


def test_unknown_version_rejected(payload):
    p = payload / 'nvidia_nvjitlink_cu12-12.6.85.dist-info/METADATA'
    p.write_text('Name: nvidia-nvjitlink-cu12\nVersion: 99.0\n')
    with pytest.raises(ValueError, match='Unsupported'):
        module.prune(payload, apply=True)


def test_symlink_archive_rejected(payload, tmp_path):
    p = payload / module.STATIC
    target = payload / 'outside'
    p.rename(target)
    p.symlink_to(target)
    with pytest.raises(ValueError, match='Symlink'):
        module.prune(payload, apply=True)
    assert target.exists()


def test_audit_symlink_cannot_overwrite(payload):
    target = payload / 'victim'
    target.write_text('keep')
    (payload / 'cuda-development-prune.json').symlink_to(target)
    with pytest.raises(ValueError, match='symlink'):
        module.prune(payload, apply=True)
    assert target.read_text() == 'keep'
    assert (payload / module.STATIC).exists()
