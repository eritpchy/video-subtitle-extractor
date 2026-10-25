import hashlib
import io
from pathlib import Path
import tarfile

import pytest

from backend.tools import model_download


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    name = 'PP-OCRv6_small_det'
    archive_dir = tmp_path / 'archives'
    archive_dir.mkdir()
    archive = archive_dir / f'{name}_infer.tar'
    content = {'inference.yml': b'Global:\n  model_name: PP-OCRv6_small_det\n',
               'inference.json': b'{}', 'inference.pdiparams': b'real-test-bytes'}
    with tarfile.open(archive, 'w') as tar:
        for filename, data in content.items():
            info = tarfile.TarInfo(f'{name}_infer/{filename}')
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    manifest = {name: {'url': 'https://example.invalid/never-used',
                      'sha256': model_download.file_sha256(archive),
                      'files': {k: hashlib.sha256(v).hexdigest() for k, v in content.items()}}}
    monkeypatch.setattr(model_download, 'load_manifest', lambda: manifest)
    return name, archive_dir, tmp_path / 'models'


def test_verified_atomic_download_is_idempotent(bundle):
    name, archives, output = bundle
    destination = model_download.download_model(name, output, archives)
    assert model_download.verify_model(destination, name)
    assert model_download.download_model(name, output, archives) == destination
    assert not list(output.glob('.*-*/'))


def test_bad_archive_never_becomes_a_model(bundle):
    name, archives, output = bundle
    (archives / f'{name}_infer.tar').write_bytes(b'corrupted')
    with pytest.raises(RuntimeError, match='SHA256'):
        model_download.download_model(name, output, archives)
    assert not (output / f'{name}_infer').exists()


def test_partial_model_is_not_overwritten_silently(bundle):
    name, archives, output = bundle
    destination = output / f'{name}_infer'
    destination.mkdir(parents=True)
    (destination / 'user-data').write_text('keep')
    with pytest.raises(RuntimeError, match='Incomplete or modified'):
        model_download.download_model(name, output, archives)
    assert (destination / 'user-data').read_text() == 'keep'


def test_manifest_only_uses_official_v6_sources():
    # Undoing fixture replacement is unnecessary: no fixture in this test.
    manifest = model_download.load_manifest()
    assert set(manifest) == {f'PP-OCRv6_{tier}_{task}' for tier in ('small', 'medium') for task in ('det', 'rec')}
    for name, item in manifest.items():
        assert item['url'].startswith('https://paddle-model-ecology.bj.bcebos.com/')
        assert name in item['url']
        assert len(item['sha256']) == 64
        assert set(item['files']) == {'inference.yml', 'inference.json', 'inference.pdiparams'}
