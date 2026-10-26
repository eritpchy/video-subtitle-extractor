"""Synthetic conversion provenance only: no models, installs or remote CI."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

import model_cache_identity as identity
import verify_model_cache as verifier
from test_verify_model_cache import cache  # Shared tiny, hash-pinned 15-model fixture.

ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENT = {'platform': 'Linux', 'architecture': 'x86_64', 'runner': 'ubuntu-24.04',
               'python_implementation': 'CPython', 'python_version': '3.12.9'}
CI = {'GITHUB_REPOSITORY': 'owner/repo', 'GITHUB_SHA': 'a' * 40,
      'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2',
      'GITHUB_WORKFLOW_REF': 'owner/repo/.github/workflows/release.yml@refs/heads/main'}


@pytest.fixture
def conversion(cache, monkeypatch):
    root, rows, pins, trusted = cache
    for name in identity.INPUT_FILES:
        if name != 'model-source-pins.json':
            shutil.copyfile(ROOT / name, trusted / name)
    versions = identity.declared_build_versions(trusted / 'requirements-model-build.txt')
    monkeypatch.setattr(identity.importlib.metadata, 'version', lambda name: versions[name])
    monkeypatch.setenv('GITHUB_REPOSITORY', 'owner/repo')
    return root, trusted


def produce(conversion):
    root, trusted = conversion
    return identity.record_producer(root, source_root=trusted, environment=ENVIRONMENT, ci=CI)


def test_worker_and_consumer_changes_do_not_invalidate_conversion(conversion):
    root, trusted = conversion
    first = identity.input_key(identity.conversion_inputs(trusted, ENVIRONMENT))
    for name in ('build.py', 'worker.cpp', 'build_opencv.py', 'fetch_ort.py', 'fetch_sources.py'):
        (trusted / name).write_text('unrelated native build changed\n')
    assert identity.input_key(identity.conversion_inputs(trusted, ENVIRONMENT)) == first
    assert not set(identity.INPUT_FILES) & {'build.py', 'worker.cpp', 'fetch_ort.py', 'fetch_sources.py', 'build_opencv.py'}


@pytest.mark.parametrize('name', identity.INPUT_FILES)
def test_every_real_conversion_input_invalidates_key(conversion, name):
    _, trusted = conversion
    before = identity.input_key(identity.conversion_inputs(trusted, ENVIRONMENT))
    path = trusted / name
    path.write_bytes(path.read_bytes() + b'\n')
    assert identity.input_key(identity.conversion_inputs(trusted, ENVIRONMENT)) != before


def test_full_conversion_dependency_and_python_patch_changes_invalidate_key(conversion):
    _, trusted = conversion
    before = identity.input_key(identity.conversion_inputs(trusted, ENVIRONMENT))
    newer_python = {**ENVIRONMENT, 'python_version': '3.12.10'}
    assert identity.input_key(identity.conversion_inputs(trusted, newer_python)) != before
    path = trusted / 'requirements-model-build.txt'
    path.write_text(path.read_text().replace('packaging==26.3', 'packaging==26.4'))
    assert identity.input_key(identity.conversion_inputs(trusted, ENVIRONMENT)) != before


@pytest.mark.parametrize('field,value', [('platform', 'Windows'), ('architecture', 'aarch64'),
                                        ('runner', 'ubuntu-22.04'), ('python_implementation', 'PyPy'),
                                        ('python_version', '3.13.1')])
def test_unreviewed_conversion_platform_or_python_cannot_reuse_key(conversion, field, value):
    _, trusted = conversion
    with pytest.raises(ValueError, match='producer environment'):
        identity.conversion_inputs(trusted, {**ENVIRONMENT, field: value})


def test_cache_hit_preserves_old_producer_across_consumers(conversion, monkeypatch):
    root, trusted = conversion
    record = produce(conversion)
    before = (root / identity.PROVENANCE).read_bytes()
    monkeypatch.setenv('GITHUB_SHA', 'b' * 40)
    monkeypatch.setenv('GITHUB_RUN_ID', '999')
    monkeypatch.setattr(identity.platform, 'system', lambda: 'Windows')
    monkeypatch.setattr(identity.platform, 'python_version', lambda: '3.12.99')
    assert verifier.verify(root, require_provenance=True)
    assert identity.verify_provenance(root, source_root=trusted) == record
    assert (root / identity.PROVENANCE).read_bytes() == before
    assert record['producer']['commit'] == 'a' * 40
    assert record['producer']['run_id'] == '123'
    with pytest.raises(FileExistsError, match='Never replace'):
        produce(conversion)


def test_old_caches_without_provenance_are_not_new_producers(conversion):
    root, trusted = conversion
    assert identity.KEY_PREFIX == 'native-ocr-fp32-v2-'
    assert verifier.verify(root)
    with pytest.raises(ValueError, match='file set'):
        verifier.verify(root, require_provenance=True)
    with pytest.raises(ValueError, match='old caches cannot be reused'):
        identity.verify_provenance(root, source_root=trusted)


@pytest.mark.parametrize('change', ['key', 'inputs', 'inventory', 'producer', 'repository',
                                    'workflow', 'extra', 'artifact_id'])
def test_mutated_or_self_referential_provenance_fails(conversion, change):
    root, trusted = conversion
    record = produce(conversion)
    if change == 'key':
        record['input_key'] += '0'
    elif change == 'inputs':
        record['conversion_inputs']['files']['prepare_models.py'] = '0' * 64
    elif change == 'inventory':
        next(iter(record['content'].values()))['sha256'] = '0' * 64
    elif change == 'producer':
        record['producer']['commit'] = 'unknown'
    elif change == 'repository':
        record['producer']['repository'] = 'other/repo'
    elif change == 'workflow':
        record['producer']['workflow_ref'] = 'owner/repo/.github/workflows/unrelated.yml@refs/heads/main'
    else:
        record[change] = 'not part of original producer contract'
    (root / identity.PROVENANCE).write_bytes(identity.canonical(record))
    with pytest.raises(ValueError):
        verifier.verify(root, require_provenance=True)


def test_expected_restore_key_must_match_actual_producer(conversion):
    root, trusted = conversion
    record = produce(conversion)
    assert identity.verify_provenance(root, source_root=trusted, expected_key=record['input_key']) == record
    with pytest.raises(ValueError, match='input contract/key'):
        identity.verify_provenance(root, source_root=trusted, expected_key='wrong-key')


def test_changed_model_bytes_fail_even_with_original_provenance(conversion):
    root, trusted = conversion
    produce(conversion)
    model = next((root / 'models').glob('*.onnx'))
    data = model.read_bytes()
    model.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    with pytest.raises(ValueError, match='SHA256 mismatch'):
        verifier.verify(root, require_provenance=True)
    with pytest.raises(ValueError, match='original producer inventory'):
        identity.verify_provenance(root, source_root=trusted)


def test_actual_tool_mismatch_cannot_create_producer_record(conversion, monkeypatch):
    root, _ = conversion
    monkeypatch.setattr(identity.importlib.metadata, 'version', lambda name: 'unreviewed')
    with pytest.raises(ValueError, match='Actual conversion dependency'):
        produce(conversion)
    assert not (root / identity.PROVENANCE).exists()


def test_post_upload_receipt_separates_actual_consumer_from_original_producer(conversion):
    root, trusted = conversion
    record = produce(conversion)
    before = (root / identity.PROVENANCE).read_bytes()
    consumer = {**CI, 'GITHUB_SHA': 'b' * 40, 'GITHUB_RUN_ID': '999', 'GITHUB_RUN_ATTEMPT': '1'}
    receipt = identity.upload_receipt(root, '456', 'sha256:' + 'c' * 64,
                                      ci=consumer, source_root=trusted)
    assert receipt['consumer']['commit'] == 'b' * 40
    assert receipt['producer'] == record['producer']
    assert receipt['producer']['commit'] == 'a' * 40
    assert receipt['artifact'] == {'id': '456', 'sha256': 'c' * 64}
    assert (root / identity.PROVENANCE).read_bytes() == before
    assert 'artifact' not in record and 'artifact_id' not in record
    destination = root.parent / 'receipt.json'
    identity._write_new(destination, receipt)
    with pytest.raises(FileExistsError):
        identity._write_new(destination, receipt)


@pytest.mark.parametrize('artifact,digest', [('', 'a' * 64), ('1', ''), ('1', 'etag-is-not-sha256')])
def test_missing_upload_result_cannot_be_invented(conversion, artifact, digest):
    root, trusted = conversion
    produce(conversion)
    with pytest.raises(ValueError, match='actual upload'):
        identity.upload_receipt(root, artifact, digest, ci=CI, source_root=trusted)


def test_stdlib_consumer_does_not_import_native_build_or_converter(conversion):
    root, trusted = conversion
    produce(conversion)
    code = (f'import sys; sys.path.insert(0, {str(ROOT)!r}); '
            'from pathlib import Path; import verify_model_cache as v; '
            f'v.ROOT = Path({str(trusted)!r}); '
            f'assert len(v.verify({str(root)!r}, require_provenance=True)) == 15; '
            "assert not any(n.split('.')[0] in ('build', 'fetch_sources', 'fetch_ort', 'build_opencv', "
            "'paddle', 'paddle2onnx', 'onnx', 'yaml', 'numpy') for n in sys.modules)")
    subprocess.run([sys.executable, '-I', '-S', '-c', code], check=True)


def test_real_crlf_git_checkout_preserves_exact_conversion_key(tmp_path):
    git = shutil.which('git')
    if not git:
        pytest.skip('Git is required for the actual checkout regression')
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)

    def run(*args, data=None):
        return subprocess.run([git, '-C', str(tmp_path), '-c', 'core.autocrlf=true',
                               '-c', 'core.eol=crlf', '-c', 'core.attributesFile=' + os.devnull,
                               *args], input=data, env=env, check=True, capture_output=True).stdout

    run('init', '--quiet', '--template=')
    files = {'native/ocr/' + name: (ROOT / name).read_bytes() for name in identity.INPUT_FILES}
    files['.gitattributes'] = (ROOT.parents[1] / '.gitattributes').read_bytes()
    files['control.txt'] = b'first\nsecond\n'
    index = []
    for name, data in files.items():
        blob = run('hash-object', '-w', '--stdin', data=data).strip()
        index.append(b'100644 ' + blob + b'\t' + name.encode() + b'\n')
    run('update-index', '--index-info', data=b''.join(index))
    run('checkout-index', '--all', '--force')
    assert (tmp_path / 'control.txt').read_bytes() == b'first\r\nsecond\r\n'
    for name in identity.INPUT_FILES:
        assert (tmp_path / 'native/ocr' / name).read_bytes() == (ROOT / name).read_bytes()
    assert identity.conversion_inputs(tmp_path / 'native/ocr', ENVIRONMENT) == identity.conversion_inputs(ROOT, ENVIRONMENT)
