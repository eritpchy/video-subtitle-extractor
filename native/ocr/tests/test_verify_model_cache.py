"""Tiny synthetic caches only: no model downloads or conversion dependencies."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

import build
import verify_model_cache as verifier

NATIVE = Path(__file__).resolve().parents[1]
REPO = NATIVE.parents[1]


def write_json(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')


@pytest.fixture
def cache(tmp_path, monkeypatch):
    pins = json.loads((NATIVE / 'model-source-pins.json').read_text(encoding='utf-8'))
    trusted = tmp_path / 'committed'
    trusted.mkdir()
    root = tmp_path / 'cached'
    root.mkdir()
    (root / 'models').mkdir()
    (root / 'config').mkdir()
    for config in pins['config_sha256']:
        data = json.dumps({'test_config': config}).encode()
        (root / config).write_bytes(data)
        pins['config_sha256'][config] = hashlib.sha256(data).hexdigest()
    rows = []
    for model in pins['models']:
        name = model['name']
        data = ('tiny-test-model:' + name).encode()
        (root / 'models' / (name + '_infer.onnx')).write_bytes(data)
        model['onnx_bytes'] = len(data)
        model['onnx_sha256'] = hashlib.sha256(data).hexdigest()
        config = (f"config/{name.split('_')[1]}.json" if name.startswith('PP-OCRv6_')
                  else f'config/{name}.json')
        rows.append({'name': name + '_infer', 'rec_model': name if name.endswith('_rec') else None,
                     'onnx_sha256': model['onnx_sha256'], 'onnx_bytes': model['onnx_bytes'],
                     'source_url': model['url'], 'source_archive_sha256': model['archive_sha256'],
                     'source': {k: {'sha256': v} for k, v in model['files'].items()},
                     'languages': model.get('languages', []), 'converter': pins['converter'],
                     'config_file': config, 'config_sha256': pins['config_sha256'][config]})
    write_json(trusted / 'model-source-pins.json', pins)
    write_json(root / 'model-source-pins.json', pins)
    write_json(root / 'model-manifest.json', rows)
    write_json(root / 'unavailable-models.json', [])
    monkeypatch.setattr(build, 'ROOT', trusted)
    monkeypatch.setattr(verifier, 'ROOT', trusted)
    return root, rows, pins, trusted


def test_valid_cache_and_no_conversion_dependencies(cache):
    root, rows, _, trusted = cache
    assert verifier.verify(root) == rows
    # Execute the actual verifier with site-packages disabled, even if the test
    # runner itself has Paddle installed. Only test pins are redirected.
    code = (f'import sys; sys.path.insert(0, {str(NATIVE)!r}); '
            'from pathlib import Path; import build, verify_model_cache as v; '
            f'build.ROOT = v.ROOT = Path({str(trusted)!r}); '
            f'assert len(v.verify({str(root)!r})) == 15; '
            "assert not any(n.split('.')[0] in ('paddle', 'paddle2onnx', 'onnx', 'yaml', 'numpy') for n in sys.modules)")
    subprocess.run([sys.executable, '-I', '-S', '-c', code], check=True)


@pytest.mark.parametrize('asset', ['model', 'config'])
def test_changed_bytes_rejected_even_at_same_size(cache, asset):
    root, rows, _, _ = cache
    row = rows[0]
    path = root / ('models/' + row['name'] + '.onnx' if asset == 'model' else row['config_file'])
    original = path.read_bytes()
    path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    with pytest.raises(ValueError, match='SHA256 mismatch'):
        verifier.verify(root)


@pytest.mark.parametrize('change', ['missing', 'duplicate', 'identity', 'hash', 'byte_count',
                                     'swapped_config', 'provenance'])
def test_untrusted_manifest_cannot_bypass_committed_pins(cache, change):
    root, rows, _, _ = cache
    if change == 'missing':
        rows.pop()
    elif change == 'duplicate':
        rows[-1] = rows[0]
    elif change == 'identity':
        rows[0]['name'] = 'not_reviewed_infer'
    elif change == 'hash':
        rows[0]['onnx_sha256'] = '0' * 64
    elif change == 'byte_count':
        rows[0]['onnx_bytes'] += 1
    elif change == 'swapped_config':
        other = next(r for r in rows if r['config_file'] != rows[0]['config_file'])
        rows[0]['config_file'], rows[0]['config_sha256'] = other['config_file'], other['config_sha256']
    else:
        rows[0]['converter'] = {'optimizer': 'unreviewed'}
    write_json(root / 'model-manifest.json', rows)
    with pytest.raises(ValueError):
        verifier.verify(root)


def test_actual_byte_count_is_checked(cache):
    root, rows, pins, trusted = cache
    rows[0]['onnx_bytes'] += 1
    pins['models'][0]['onnx_bytes'] += 1
    for path in (root, trusted):
        write_json(path / 'model-source-pins.json', pins)
    write_json(root / 'model-manifest.json', rows)
    with pytest.raises(ValueError, match='byte count mismatch'):
        verifier.verify(root)


def test_shared_native_build_verifier_also_rejects_swapped_pinned_config(cache):
    root, rows, _, _ = cache
    other = next(r for r in rows if r['config_file'] != rows[0]['config_file'])
    rows[0]['config_file'], rows[0]['config_sha256'] = other['config_file'], other['config_sha256']
    write_json(root / 'model-manifest.json', rows)
    with pytest.raises(ValueError, match='Config identity differs'):
        build.verify_models(root)


def test_self_certified_cached_pins_are_rejected(cache):
    root, rows, pins, _ = cache
    model = root / 'models' / (rows[0]['name'] + '.onnx')
    model.write_bytes(b'unreviewed')
    pins['models'][0]['onnx_sha256'] = rows[0]['onnx_sha256'] = hashlib.sha256(model.read_bytes()).hexdigest()
    pins['models'][0]['onnx_bytes'] = rows[0]['onnx_bytes'] = model.stat().st_size
    write_json(root / 'model-source-pins.json', pins)
    write_json(root / 'model-manifest.json', rows)
    with pytest.raises(ValueError, match='Cached source pins differ'):
        verifier.verify(root)


@pytest.mark.parametrize('change', ['missing_file', 'extra_file', 'extra_directory', 'unavailable', 'symlink'])
def test_incomplete_or_unexpected_cache_rejected(cache, change):
    root, rows, _, _ = cache
    if change == 'missing_file':
        (root / rows[0]['config_file']).unlink()
    elif change == 'extra_file':
        (root / 'unreviewed.onnx').write_bytes(b'extra')
    elif change == 'extra_directory':
        (root / 'unreviewed').mkdir()
    elif change == 'unavailable':
        write_json(root / 'unavailable-models.json', ['missing'])
    else:
        path = root / rows[0]['config_file']
        target = root.parent / 'external-config.json'
        path.rename(target)
        try:
            path.symlink_to(target)
        except OSError as error:
            pytest.skip('Symlink creation unavailable: ' + str(error))
    with pytest.raises(ValueError):
        verifier.verify(root)


def test_workflow_cache_hit_cannot_skip_verification_or_upload_unverified_bytes():
    workflow = (REPO / '.github/workflows/native-ocr.yml').read_text(encoding='utf-8')
    models = workflow.split('\n  models:\n', 1)[1].split('\n  precision-reference-sources:\n', 1)[0]
    steps = re.split(r'(?=^      - )', models, flags=re.MULTILINE)[1:]

    def step(text):
        matches = [(i, s) for i, s in enumerate(steps) if text in s]
        assert len(matches) == 1
        return matches[0]

    # The separate build-only Paddle reference cache/artifact is intentionally
    # independent; select this model cache by identity, not by action type.
    restore_i, restore = step('id: model-cache')
    install_i, install = step('run: python -m pip install')
    convert_i, convert = step('run: python native/ocr/prepare_models.py')
    key_i, key = step('id: model-inputs')
    producer_i, producer = step('name: Record actual fresh-conversion producer')
    check_i, check = step('name: Check restored or fresh producer input key')
    verify_i, verify = step('run: python -S native/ocr/verify_model_cache.py build/native-models')
    save_i, save = step('name: Cache only independently verified converted models')
    upload_i, upload = step('name: native-ocr-fp32-models-${{ github.sha }}')
    assert 'uses: actions/cache/restore@v4' in restore
    assert 'uses: actions/cache/save@v4' in save
    assert 'uses: actions/upload-artifact@v4' in upload
    assert key_i < restore_i < install_i < convert_i < producer_i < check_i < verify_i < save_i < upload_i
    assert '        if:' not in verify and 'continue-on-error:' not in verify
    assert verify.split('        run: ', 1)[1].strip() == 'python -S native/ocr/verify_model_cache.py build/native-models --require-provenance'
    assert 'always()' not in save + upload and 'continue-on-error:' not in models
    for conditional in (install, convert, producer, save):
        assert "if: steps.model-cache.outputs.cache-hit != 'true'" in conditional
    assert '          restore-keys:' not in restore and 'github.sha' not in restore
    assert 'key: ${{ steps.model-inputs.outputs.key }}' in restore
    assert 'python -S native/ocr/model_cache_identity.py key' in key
    assert '        if:' not in check
    assert '--expected-key "${{ steps.model-inputs.outputs.key }}"' in check
    assert 'hashFiles' not in restore
    receipt_i, receipt = step('name: Record actual upload identity separately')
    assert receipt_i > upload_i
    assert 'steps.model-artifact.outputs.artifact-id' in receipt
    assert 'steps.model-artifact.outputs.artifact-digest' in receipt
    assert 'key: ${{ steps.model-cache.outputs.cache-primary-key }}' in save
    assert 'name: native-ocr-fp32-models-${{ github.sha }}' in upload
    assert "PIP_NO_INDEX: '1'" in convert
    assert "POLYGRAPHY_AUTOINSTALL_DEPS: '0'" in workflow


def test_all_cross_platform_consumers_require_original_provenance():
    native = (REPO / '.github/workflows/native-ocr.yml').read_text()
    consumer = native.split('  native:\n', 1)[1]
    assert 'run: python -S native/ocr/verify_model_cache.py build/native-models --require-provenance' in consumer
    directml = (REPO / '.github/workflows/release.yml').read_text()
    assert 'run: python -S native/ocr/verify_model_cache.py build/native-models --require-provenance' in directml
