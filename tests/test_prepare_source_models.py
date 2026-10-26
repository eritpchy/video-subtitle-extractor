"""Tiny synthetic archives only; no download, compiler, worker, or inference."""
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest

from scripts import prepare_source_models as source


@pytest.fixture
def case(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    pins = json.loads(source.paddle.BUILD_PINS.read_bytes())
    files, rows = {}, []
    for name in pins['config_sha256']:
        data = ('tiny-config:' + name).encode()
        files[name] = data
        pins['config_sha256'][name] = hashlib.sha256(data).hexdigest()
    for row in pins['models']:
        name = row['name']
        data = ('tiny-graph:' + name).encode()
        row['onnx_sha256'], row['onnx_bytes'] = hashlib.sha256(data).hexdigest(), len(data)
        files['models/' + name + '_infer.onnx'] = data
        config = f'config/{name.split("_")[1]}.json' if name.startswith('PP-OCRv6_') else f'config/{name}.json'
        rows.append({'name': name + '_infer', 'rec_model': name if name.endswith('_rec') else None,
                     'onnx_sha256': row['onnx_sha256'], 'onnx_bytes': row['onnx_bytes'],
                     'source_url': row['url'], 'source_archive_sha256': row['archive_sha256'],
                     'source': {k: {'sha256': v} for k, v in row['files'].items()},
                     'languages': row.get('languages', []), 'converter': pins['converter'],
                     'config_file': config, 'config_sha256': pins['config_sha256'][config]})
    files['model-source-pins.json'] = source.paddle._canonical(pins)
    files['model-manifest.json'] = source.paddle._canonical(rows)
    trusted = root / 'trusted.json'
    trusted.write_bytes(files['model-source-pins.json'])
    monkeypatch.setattr(source.paddle, 'BUILD_PINS', trusted)
    monkeypatch.setattr(source.paddle, 'PINNED_SOURCE_SHA256', hashlib.sha256(trusted.read_bytes()).hexdigest())
    value = {'root': root, 'archive': root / 'release.zip', 'output': root / 'models',
             'files': files, 'monkeypatch': monkeypatch}
    refresh(value)
    return value


def refresh(case, *, extra=(), omit=(), mutate=None):
    with zipfile.ZipFile(case['archive'], 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in case['files'].items():
            if name not in omit:
                archive.writestr(source.MODEL_PREFIX + name, data)
        # Both files must remain entirely unopened; case differences are valid
        # in a Linux runtime, which is never copied into source model data.
        archive.writestr('package/runtime/terminfo/P/P12', b'NEVER EXTRACT OR EXECUTE')
        archive.writestr('package/runtime/terminfo/p/p12', b'NEVER EXTRACT OR EXECUTE')
        for name, data in extra:
            archive.writestr(name, data)
    if mutate:
        data = bytearray(case['archive'].read_bytes())
        mutate(data)
        case['archive'].write_bytes(data)
    data = case['archive'].read_bytes()
    case['monkeypatch'].setattr(source, 'ARCHIVE_SHA256', hashlib.sha256(data).hexdigest())
    case['monkeypatch'].setattr(source, 'ARCHIVE_BYTES', len(data))


def prepare(case):
    return source.prepare_onnx(case['output'], archive=case['archive'], offline=True)


def test_only_complete_pinned_data_is_opened_or_published(case, monkeypatch):
    opened = []
    original = zipfile.ZipFile.open
    def only_data(self, name, *args, **kwargs):
        item = name.filename if isinstance(name, zipfile.ZipInfo) else name
        assert item.startswith(source.MODEL_PREFIX)
        assert item[len(source.MODEL_PREFIX):] in case['files']
        opened.append(item)
        return original(self, name, *args, **kwargs)
    monkeypatch.setattr(zipfile.ZipFile, 'open', only_data)
    monkeypatch.setattr(source.urllib.request, 'build_opener', lambda *a: pytest.fail('network forbidden'))
    report = prepare(case)
    assert report['model_count'] == 15 and report['config_count'] == 13
    assert report['data_files'] == len(opened) == 30
    assert report['binaries_executed'] is report['inference_verified'] is False
    actual = {p.relative_to(case['output']).as_posix(): p.read_bytes()
              for p in case['output'].rglob('*') if p.is_file()}
    assert actual == case['files']
    assert len(source.verify_onnx(case['output'])['files']) == 28


@pytest.mark.parametrize('change', ['same_size_hash', 'truncation'])
def test_full_original_archive_hash_and_size_are_mandatory(case, change):
    data = bytearray(case['archive'].read_bytes())
    if change == 'same_size_hash':
        data[40] ^= 1
    else:
        data.pop()
    case['archive'].write_bytes(data)
    with pytest.raises(ValueError, match='fixed release'):
        prepare(case)
    assert not case['output'].exists()
    assert not list(case['root'].glob('.source-onnx-*'))


@pytest.mark.parametrize('kind', ['graph', 'config', 'manifest', 'pins'])
def test_self_consistent_untrusted_zip_cannot_change_pinned_data(case, kind):
    relative = {'manifest': 'model-manifest.json', 'pins': 'model-source-pins.json'}.get(kind)
    if relative is None:
        prefix = 'models/' if kind == 'graph' else 'config/'
        relative = next(name for name in case['files'] if name.startswith(prefix))
    data = bytearray(case['files'][relative]); data[0] ^= 1
    case['files'][relative] = bytes(data)
    refresh(case)
    with pytest.raises((ValueError, UnicodeError)):
        prepare(case)
    assert not case['output'].exists()


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'models/../escape', 'config\\escape',
                                  'models/unreviewed.onnx', 'models/CON'])
def test_unsafe_or_additional_model_archive_names_fail(case, name):
    refresh(case, extra=[(source.MODEL_PREFIX + name, b'bad')])
    with pytest.raises(ValueError):
        prepare(case)
    assert not case['output'].exists()


def test_duplicate_and_case_confusable_model_entries_fail(case):
    name, data = next(iter(case['files'].items()))
    for altered in (name, name.upper()):
        if altered == name:
            with pytest.warns(UserWarning, match='Duplicate name'):
                refresh(case, extra=[(source.MODEL_PREFIX + altered, data)])
        else:
            refresh(case, extra=[(source.MODEL_PREFIX + altered, data)])
        with pytest.raises(ValueError):
            prepare(case)


def test_symlink_model_entry_cannot_be_materialized(case):
    name, data = next(iter(case['files'].items()))
    info = zipfile.ZipInfo(source.MODEL_PREFIX + name)
    info.create_system = 3
    info.external_attr = 0o120777 << 16
    refresh(case, omit=[name], extra=[(info, data)])
    with pytest.raises(ValueError, match='non-data'):
        prepare(case)


def test_archive_changed_during_extraction_never_publishes(case, monkeypatch):
    original = source._entries
    def change(stream, limits):
        result = original(stream, limits)
        with case['archive'].open('r+b') as writer:
            writer.seek(4); data = writer.read(1); writer.seek(4); writer.write(bytes([data[0] ^ 1]))
        return result
    monkeypatch.setattr(source, '_entries', change)
    with pytest.raises(ValueError, match='changed'):
        prepare(case)
    assert not case['output'].exists()


@pytest.mark.parametrize('collision', ['empty', 'content', 'late'])
def test_no_existing_or_concurrent_output_is_replaced(case, monkeypatch, collision):
    if collision != 'late':
        case['output'].mkdir()
        if collision == 'content':
            (case['output'] / 'keep').write_text('user data')
    else:
        original = source._publish
        def collide(stage, output):
            output.mkdir(); (output / 'keep').write_text('user data')
            original(stage, output)
        monkeypatch.setattr(source, '_publish', collide)
    with pytest.raises(FileExistsError):
        prepare(case)
    assert case['output'].is_dir()
    if collision != 'empty':
        assert (case['output'] / 'keep').read_text() == 'user data'


def test_symlinked_archive_parent_rejected(case):
    alias = case['root'] / 'alias'
    try:
        alias.symlink_to(case['root'], target_is_directory=True)
    except OSError:
        pytest.skip('Symlink creation unavailable')
    case['archive'] = alias / case['archive'].name
    with pytest.raises(ValueError, match='Symlink'):
        prepare(case)


def test_offline_miss_and_corrupt_cache_do_not_fetch_or_overwrite(case, monkeypatch):
    monkeypatch.setattr(source.urllib.request, 'build_opener', lambda *a: pytest.fail('no network on offline/malformed cache'))
    cache = case['root'] / 'cache'
    with pytest.raises(FileNotFoundError, match='Offline'):
        source.prepare_onnx(case['output'], cache=cache, offline=True)
    bad = cache / (source.ARCHIVE_SHA256 + '.zip'); bad.write_bytes(b'corrupt cache')
    with pytest.raises(ValueError):
        source.prepare_onnx(case['output'], cache=cache)
    assert bad.read_bytes() == b'corrupt cache'


def test_download_uses_fixed_public_url_without_actions_auth(case, monkeypatch):
    data = case['archive'].read_bytes()
    calls = []
    class Response(io.BytesIO):
        def geturl(self):
            return 'https://release-assets.githubusercontent.com/fixed-public-asset'
    class Opener:
        def open(self, url, **kwargs):
            calls.append(url)
            return Response(data)
    monkeypatch.setattr(source.urllib.request, 'build_opener', lambda *a: Opener())
    report = source.prepare_onnx(case['output'], cache=case['root'] / 'cache')
    assert report['model_count'] == 15
    assert calls == [source.ARCHIVE_URL]


@pytest.mark.parametrize('url', ['http://github.com/file', 'https://example.com/file',
                                'https://github.com:8443/file', 'https://token@github.com/file'])
def test_download_redirect_cannot_escape_reviewed_public_hosts(url):
    with pytest.raises(ValueError, match='Unexpected'):
        source._check_download_url(url)


def test_paddle_wrapper_uses_separate_complete_original_data_helper(case, monkeypatch):
    cache = case['root'] / 'source-cache'
    calls = []
    def fake(output, source_cache, offline):
        calls.append((output, source_cache, offline))
        output.mkdir(); (output / 'verified-by-existing-helper').write_text('15 original models')
        return {'model_count': 15}
    monkeypatch.setattr(source.paddle, 'prepare_models', fake)
    assert source.prepare_paddle(case['output'], cache=cache, offline=True) == {'model_count': 15}
    assert calls[0][1:] == (cache, True)
    assert (case['output'] / 'verified-by-existing-helper').is_file()


def test_stdlib_clean_source_cli_never_loads_native_build_or_inference():
    root = Path(source.__file__).resolve().parents[1]
    code = (f'import sys; sys.path.insert(0, {str(root)!r}); '
            'from scripts import prepare_source_models; '
            "assert not any(n.split('.')[0] in ('paddle', 'paddle2onnx', 'onnxruntime', 'onnx', "
            "'numpy', 'PySide6', 'build', 'fetch_ort', 'build_opencv') for n in sys.modules)")
    subprocess.run([sys.executable, '-I', '-S', '-c', code], check=True)
    subprocess.run([sys.executable, '-S', str(root / 'scripts/prepare_source_models.py'), '--help'],
                   cwd=root.parent, check=True, capture_output=True)


def test_clean_python_only_layout_can_prepare_and_verify_all_data(case):
    """A fresh source layout containing no C++, worker, runtime or site packages."""
    clone = case['root'] / 'clean-source'
    scripts = clone / 'scripts'; scripts.mkdir(parents=True)
    for name in ('prepare_source_models.py', 'paddle_package_models.py'):
        shutil.copyfile(Path(source.__file__).parent / name, scripts / name)
    output = case['root'] / 'clean-result'
    code = (f'import sys; sys.path.insert(0, {str(scripts)!r}); '
            'from pathlib import Path; import prepare_source_models as m; '
            f'm.paddle.BUILD_PINS = Path({str(source.paddle.BUILD_PINS)!r}); '
            f'm.paddle.PINNED_SOURCE_SHA256 = {source.paddle.PINNED_SOURCE_SHA256!r}; '
            f'm.ARCHIVE_SHA256 = {source.ARCHIVE_SHA256!r}; m.ARCHIVE_BYTES = {source.ARCHIVE_BYTES}; '
            f'r = m.prepare_onnx(Path({str(output)!r}), archive=Path({str(case["archive"])!r}), offline=True); '
            "assert r['model_count'] == 15 and r['data_files'] == 30; "
            f'assert m.verify_onnx(Path({str(output)!r}))["config_count"] == 13; '
            "assert not any(n.split('.')[0] in ('paddle', 'onnxruntime', 'numpy', 'PySide6', "
            "'build', 'fetch_ort', 'build_opencv') for n in sys.modules)")
    subprocess.run([sys.executable, '-I', '-S', '-c', code], cwd=clone, check=True)
    assert not list(clone.rglob('*.cpp'))
