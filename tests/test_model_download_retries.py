"""No real network or waits: exercise transient failures and fail-closed paths."""
import errno
import http.client
import io
import os
import stat
import socket
import ssl
import urllib.error

import pytest

from backend.tools import model_download


@pytest.fixture
def network(monkeypatch):
    monkeypatch.delenv('VSE_OFFLINE', raising=False)
    calls, sleeps = [], []
    monkeypatch.setattr(model_download.time, 'sleep', sleeps.append)

    def install(outcomes):
        outcomes = iter(outcomes)

        def open_url(url, timeout):
            calls.append((url, timeout))
            result = next(outcomes)
            if isinstance(result, BaseException):
                raise result
            return result

        monkeypatch.setattr(model_download.urllib.request, 'urlopen', open_url)
    return install, calls, sleeps


def http_error(status):
    return urllib.error.HTTPError('https://example.invalid/pinned', status, 'test', {}, None)


@pytest.mark.parametrize('failure', [
    urllib.error.URLError(ConnectionResetError(errno.ECONNRESET, 'reset')),
    ConnectionResetError(errno.ECONNRESET, 'reset'),
    TimeoutError('timeout'),
    urllib.error.URLError(socket.gaierror(socket.EAI_AGAIN, 'temporary DNS')),
    http.client.IncompleteRead(b'partial', 20),
    http.client.RemoteDisconnected('closed'),
    *[http_error(code) for code in (429, 500, 502, 503, 504)],
])
def test_transient_retry_is_bounded_same_source(tmp_path, network, failure):
    install, calls, sleeps = network
    install([failure, failure, io.BytesIO(b'complete')])
    destination = tmp_path / 'inference.yml'
    model_download._download_file('https://example.invalid/pinned', destination)
    assert destination.read_bytes() == b'complete'
    assert calls == [('https://example.invalid/pinned', 120)] * 3
    assert sleeps == [1, 2]
    assert list(tmp_path.iterdir()) == [destination]


def test_exhaustion_preserves_existing_file_and_cleans_partial(tmp_path, network):
    install, calls, sleeps = network
    install([TimeoutError('timeout')] * 3)
    destination = tmp_path / 'inference.yml'
    destination.write_bytes(b'existing')
    with pytest.raises(RuntimeError, match='Cannot reach official model source'):
        model_download._download_file('https://example.invalid/pinned', destination)
    assert destination.read_bytes() == b'existing'
    assert len(calls) == 3
    assert sleeps == [1, 2]
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.parametrize('failure', [
    *[http_error(code) for code in (400, 401, 403, 404, 407, 501)],
    ssl.SSLCertVerificationError('bad certificate'),
    urllib.error.URLError(ssl.SSLCertVerificationError('bad certificate')),
    ssl.SSLError('TLS protocol error'),
    urllib.error.URLError('unknown error'),
    urllib.error.URLError(socket.gaierror(socket.EAI_NONAME, 'no such host')),
    PermissionError(errno.EACCES, 'denied'),
])
def test_permanent_security_and_unknown_errors_do_not_retry(tmp_path, network, failure):
    install, calls, sleeps = network
    install([failure])
    with pytest.raises(RuntimeError):
        model_download._download_file('https://example.invalid/pinned', tmp_path / 'file')
    assert len(calls) == 1
    assert sleeps == []
    assert list(tmp_path.iterdir()) == []


def test_body_reset_discards_partial_before_retry(tmp_path, network):
    install, calls, sleeps = network
    destination = tmp_path / 'file'

    class BrokenBody(io.BytesIO):
        def read(self, size=-1):
            if self.tell():
                assert not destination.exists()
                raise ConnectionResetError(errno.ECONNRESET, 'reset mid-body')
            return super().read(size)

    install([BrokenBody(b'partial'), io.BytesIO(b'complete')])
    model_download._download_file('https://example.invalid/pinned', destination)
    assert destination.read_bytes() == b'complete'
    assert len(calls) == 2
    assert sleeps == [1]
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.parametrize('source', ['huggingface', 'bos'])
def test_offline_missing_model_never_accesses_network(tmp_path, monkeypatch, network, source):
    install, calls, sleeps = network
    install([])
    monkeypatch.setenv('VSE_OFFLINE', '1')
    with pytest.raises(RuntimeError, match='Offline mode'):
        model_download.download_model('PP-OCRv6_medium_rec', tmp_path, source=source)
    assert calls == sleeps == []
    assert not list(tmp_path.glob('.*-*/'))
    assert not (tmp_path / 'PP-OCRv6_medium_rec_infer').exists()


def test_existing_real_models_verify_without_network(monkeypatch, network):
    install, calls, sleeps = network
    install([])
    monkeypatch.setenv('VSE_OFFLINE', '1')
    root = model_download.MODEL_ROOT / 'V6'
    if not all(model_download.verify_model(root / f'{name}_infer', name)
               for name in model_download.load_manifest()):
        pytest.skip('Pinned model bundles not installed in this environment')
    for name in model_download.load_manifest():
        assert model_download.download_model(name) == root / f'{name}_infer'
    assert calls == sleeps == []


@pytest.mark.skipif(os.name == 'nt', reason='POSIX public model mode contract')
def test_public_download_mode_is_set_before_atomic_publish(tmp_path, network, monkeypatch):
    install, calls, sleeps = network
    install([io.BytesIO(b'public model')])
    destination = tmp_path / 'inference.yml'
    destination.write_bytes(b'old complete file')
    destination.chmod(0o640)
    replace = model_download.os.replace

    def assert_replace(source, target):
        assert stat.S_IMODE(source.stat().st_mode) == 0o644
        assert destination.read_bytes() == b'old complete file'
        assert stat.S_IMODE(destination.stat().st_mode) == 0o640
        replace(source, target)

    monkeypatch.setattr(model_download.os, 'replace', assert_replace)
    model_download._download_file('https://example.invalid/pinned', destination)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o644
    assert destination.read_bytes() == b'public model'


@pytest.mark.skipif(os.name == 'nt', reason='POSIX public model mode contract')
def test_chmod_failure_does_not_publish_or_remove_old_file(tmp_path, network, monkeypatch):
    install, calls, sleeps = network
    install([io.BytesIO(b'public model')])
    destination = tmp_path / 'inference.yml'
    destination.write_bytes(b'old complete file')
    destination.chmod(0o640)

    def fail_chmod(self, mode):
        raise PermissionError(errno.EPERM, 'cannot set model permissions')

    monkeypatch.setattr(type(destination), 'chmod', fail_chmod)
    with pytest.raises(RuntimeError):
        model_download._download_file('https://example.invalid/pinned', destination)
    assert destination.read_bytes() == b'old complete file'
    assert stat.S_IMODE(destination.stat().st_mode) == 0o640
    assert list(tmp_path.iterdir()) == [destination]
    assert len(calls) == 1
    assert sleeps == []
