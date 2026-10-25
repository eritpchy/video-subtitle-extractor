import hashlib
import io
from pathlib import Path
import tarfile
import urllib.error
import re

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
                      'huggingface': {'repository': 'PaddlePaddle/' + name, 'revision': 'a'*40,
                                      'base_url': f'https://huggingface.co/PaddlePaddle/{name}/resolve/' + 'a'*40},
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


def test_primary_source_is_revision_pinned_huggingface(bundle, monkeypatch):
    name, archives, output = bundle
    with tarfile.open(archives / f'{name}_infer.tar') as archive:
        content = {Path(member.name).name: archive.extractfile(member).read()
                   for member in archive.getmembers()}
    requests = []
    def open_url(url, timeout):
        requests.append(url)
        return io.BytesIO(content[url.rsplit('/', 1)[1]])
    monkeypatch.setattr(model_download.urllib.request, 'urlopen', open_url)
    destination = model_download.download_model(name, output)
    assert model_download.verify_model(destination, name)
    assert len(requests) == 3
    assert all('/resolve/' + 'a'*40 + '/' in url for url in requests)
    assert all(url.startswith('https://huggingface.co/PaddlePaddle/') for url in requests)


def test_primary_checksum_failure_never_falls_back_or_publishes(bundle, monkeypatch):
    name, _, output = bundle
    requests = []
    def open_url(url, timeout):
        requests.append(url)
        return io.BytesIO(b'not the pinned official bytes')
    monkeypatch.setattr(model_download.urllib.request, 'urlopen', open_url)
    with pytest.raises(RuntimeError, match='SHA256'):
        model_download.download_model(name, output)
    assert len(requests) == 1
    assert not (output / f'{name}_infer').exists()


@pytest.mark.parametrize('status', [401, 403, 404, 429, 503])
def test_http_errors_do_not_retry_or_switch_origins(bundle, monkeypatch, status):
    name, _, output = bundle
    requests = []
    def open_url(url, timeout):
        requests.append(url)
        raise urllib.error.HTTPError(url, status, 'test error', {}, None)
    monkeypatch.setattr(model_download.urllib.request, 'urlopen', open_url)
    with pytest.raises(RuntimeError, match=f'HTTP {status}'):
        model_download.download_model(name, output)
    assert len(requests) == 1
    assert not (output / f'{name}_infer').exists()


def test_explicit_bos_source_retains_archive_verification(bundle, monkeypatch):
    name, archives, output = bundle
    requests = []
    def open_url(url, timeout):
        requests.append(url)
        return io.BytesIO((archives / f'{name}_infer.tar').read_bytes())
    monkeypatch.setattr(model_download.urllib.request, 'urlopen', open_url)
    assert model_download.verify_model(model_download.download_model(name, output, source='bos'), name)
    assert requests == ['https://example.invalid/never-used']


def test_official_huggingface_sources_are_immutable():
    for name, entry in model_download.load_manifest().items():
        source = entry['huggingface']
        assert source['repository'] == f'PaddlePaddle/{name}'
        assert re.fullmatch('[a-f0-9]{40}', source['revision'])
        assert source['base_url'] == f'https://huggingface.co/{source["repository"]}/resolve/{source["revision"]}'


def test_network_failure_reports_source_without_retry(bundle, monkeypatch):
    name, _, output = bundle
    requests = []
    def open_url(url, timeout):
        requests.append(url)
        raise urllib.error.URLError('network unavailable')
    monkeypatch.setattr(model_download.urllib.request, 'urlopen', open_url)
    with pytest.raises(RuntimeError, match='Cannot reach official model source https://huggingface.co/'):
        model_download.download_model(name, output)
    assert len(requests) == 1
    assert not (output / f'{name}_infer').exists()
