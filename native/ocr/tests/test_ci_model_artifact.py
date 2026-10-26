"""Synthetic tiny pinned models/API records only; not a downloaded CI artifact."""
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import zipfile

import pytest

import ci_model_artifact as artifact
import verify_model_cache

NATIVE = Path(__file__).resolve().parents[1]


def write_json(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')


@pytest.fixture
def case(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    pins = json.loads((NATIVE / 'model-source-pins.json').read_bytes())
    trusted = root / 'synthetic-reviewed-pins'; trusted.mkdir()
    assets, rows = {}, []
    for name in pins['config_sha256']:
        data = ('synthetic-config:' + name).encode()
        assets[name] = data
        pins['config_sha256'][name] = hashlib.sha256(data).hexdigest()
    for model in pins['models']:
        name = model['name']; data = ('synthetic-onnx:' + name).encode()
        assets['models/' + name + '_infer.onnx'] = data
        model['onnx_bytes'], model['onnx_sha256'] = len(data), hashlib.sha256(data).hexdigest()
        config = (f"config/{name.split('_')[1]}.json" if name.startswith('PP-OCRv6_') else f'config/{name}.json')
        rows.append({'name': name + '_infer', 'rec_model': name if name.endswith('_rec') else None,
                     'onnx_sha256': model['onnx_sha256'], 'onnx_bytes': model['onnx_bytes'],
                     'source_url': model['url'], 'source_archive_sha256': model['archive_sha256'],
                     'source': {k: {'sha256': v} for k, v in model['files'].items()},
                     'languages': model.get('languages', []), 'converter': pins['converter'],
                     'config_file': config, 'config_sha256': pins['config_sha256'][config]})
    assets.update({'model-source-pins.json': json.dumps(pins).encode(),
                   'model-manifest.json': json.dumps(rows).encode(), 'unavailable-models.json': b'[]'})
    (trusted / 'model-source-pins.json').write_bytes(assets['model-source-pins.json'])
    monkeypatch.setattr(artifact, 'ROOT', trusted)
    monkeypatch.setattr(verify_model_cache, 'ROOT', trusted)
    data = {'root': root, 'trusted': trusted, 'assets': assets, 'zip': root / 'artifact.zip',
            'artifact_metadata': root / 'artifact.json', 'run_metadata': root / 'run.json',
            'job_metadata': root / 'job.json', 'output': root / 'output', 'consumer': 'b' * 40}
    refresh(data)
    return data


def refresh(case, pairs=None, raw=None):
    if raw is None:
        with zipfile.ZipFile(case['zip'], 'w', compression=zipfile.ZIP_DEFLATED) as z:
            for name, value in (case['assets'].items() if pairs is None else pairs):
                z.writestr(name, value)
    else:
        case['zip'].write_bytes(raw)
    blob = case['zip'].read_bytes()
    anchors = {'repository': 'owner/repo', 'run_id': 10, 'run_attempt': 1, 'commit': 'a' * 40,
               'artifact_id': 20, 'job_id': 30, 'artifact_sha256': hashlib.sha256(blob).hexdigest(),
               'artifact_bytes': len(blob)}
    case['anchors'] = anchors
    write_json(case['artifact_metadata'], {'id': 20, 'name': 'native-ocr-fp32-models-' + 'a' * 40,
               'workflow_run': {'id': 10, 'head_sha': 'a' * 40}, 'expired': False,
               'size_in_bytes': len(blob), 'digest': 'sha256:' + anchors['artifact_sha256']})
    write_json(case['run_metadata'], {'id': 10, 'run_attempt': 1, 'head_sha': 'a' * 40,
               'repository': {'full_name': 'owner/repo'}, 'status': 'in_progress'})
    write_json(case['job_metadata'], {'id': 30, 'run_id': 10, 'head_sha': 'a' * 40,
               'status': 'completed', 'conclusion': 'success', 'name': artifact.PRODUCER_JOB_NAME})


def consume(case):
    return artifact.consume(case['zip'], case['artifact_metadata'], case['run_metadata'], case['job_metadata'],
                            anchors=case['anchors'], consumer_commit=case['consumer'], output=case['output'])


def rejected(case, pattern=None):
    with pytest.raises((ValueError, OSError, zipfile.BadZipFile, RuntimeError), match=pattern):
        consume(case)
    assert not case['output'].exists()
    assert not list(case['root'].glob('.model-artifact-*'))


def test_exact_legacy_bytes_verified_without_fake_conversion_provenance(case):
    receipt = consume(case)
    result = case['output'] / 'model-cache'
    assert len(verify_model_cache.verify(result)) == 15
    assert len([p for p in result.rglob('*') if p.is_file()]) == 31
    assert not (result / 'conversion-provenance.json').exists()
    assert receipt['source'] == case['anchors']
    assert receipt['consumer'] == {'commit': 'b' * 40}
    assert receipt['source_format'] == 'legacy-v1-no-conversion-provenance'
    assert receipt['gpu_inference_verified'] is False
    assert 'not conversion provenance' in receipt['scope']
    for name, data in case['assets'].items():
        assert (result / name).read_bytes() == data
        assert receipt['files'][name] == {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    for kind in ('artifact', 'run', 'job'):
        assert receipt['metadata_sha256'][kind] == hashlib.sha256(case[kind + '_metadata'].read_bytes()).hexdigest()


@pytest.mark.parametrize('kind,key,value', [
    ('artifact', 'id', 21), ('artifact', 'name', 'different'), ('artifact', 'expired', True),
    ('artifact', 'expired', 0), ('artifact', 'digest', 'sha256:' + '0' * 64),
    ('artifact', 'size_in_bytes', 1), ('artifact', 'workflow_run', {'id': 11, 'head_sha': 'a' * 40}),
    ('artifact', 'workflow_run', {'id': 10, 'head_sha': 'c' * 40}),
    ('run', 'id', 11), ('run', 'run_attempt', 2), ('run', 'head_sha', 'c' * 40),
    ('run', 'repository', {'full_name': 'other/repo'}), ('run', 'id', True),
    ('job', 'id', 31), ('job', 'run_id', 11), ('job', 'head_sha', 'c' * 40),
    ('job', 'status', 'in_progress'), ('job', 'conclusion', 'failure'), ('job', 'name', 'unrelated'),
    ('job', 'run_attempt', 2),
])
def test_metadata_mismatch_fails_before_extraction(case, kind, key, value):
    path = case[kind + '_metadata']; record = json.loads(path.read_bytes()); record[key] = value
    write_json(path, record); rejected(case)


@pytest.mark.parametrize('kind,key', [('artifact', 'digest'), ('artifact', 'workflow_run'),
                                      ('run', 'run_attempt'), ('run', 'repository'), ('job', 'conclusion')])
def test_missing_metadata_fields_fail(case, kind, key):
    path = case[kind + '_metadata']; record = json.loads(path.read_bytes()); del record[key]
    write_json(path, record); rejected(case)


def test_duplicate_json_keys_fail(case):
    case['run_metadata'].write_text('{"id":10,"id":10}')
    rejected(case, 'Duplicate JSON')


@pytest.mark.parametrize('field,value', [('artifact_bytes', True), ('job_id', 0), ('commit', 'abc'),
                                        ('artifact_sha256', 'G' * 64), ('repository', '../repo')])
def test_invalid_explicit_anchors_fail(case, field, value):
    case['anchors'][field] = value; rejected(case)


@pytest.mark.parametrize('change', ['same_size_digest', 'truncated'])
def test_original_archive_hash_and_size_are_required(case, change):
    data = bytearray(case['zip'].read_bytes())
    if change == 'same_size_digest': data[40] ^= 1
    else: data.pop()
    case['zip'].write_bytes(data); rejected(case, 'Archive')


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'C:/absolute', 'config\\wrong.json',
                                 'models/x:stream', 'config/CON', 'model-manifest.json/child',
                                 'conversion-provenance.json', 'MODEl-manifest.json'])
def test_unexpected_and_unsafe_names_fail(case, name):
    refresh(case, [*case['assets'].items(), (name, b'bad')]); rejected(case)


def test_missing_model_fails(case):
    refresh(case, list(case['assets'].items())[1:]); rejected(case)


def test_duplicate_name_fails(case):
    with pytest.warns(UserWarning, match='Duplicate name'):
        refresh(case, [*case['assets'].items(), ('model-manifest.json', b'[]')])
    rejected(case, 'duplicate')


def test_symlink_zip_member_fails(case):
    pairs = list(case['assets'].items()); name, data = pairs[0]
    info = zipfile.ZipInfo(name); info.create_system = 3; info.external_attr = 0o120777 << 16
    refresh(case, [(info, data), *pairs[1:]]); rejected(case, 'entry type')


@pytest.mark.parametrize('change', ['encryption', 'local_name', 'overlap', 'central_size', 'comment', 'huge_count'])
def test_malformed_zip_structures_fail(case, change):
    data = bytearray(case['zip'].read_bytes()); central = data.index(b'PK\x01\x02'); end = data.rindex(b'PK\x05\x06')
    if change == 'encryption':
        struct.pack_into('<H', data, 6, 1); struct.pack_into('<H', data, central + 8, 1)
    elif change == 'local_name': data[30] ^= 1
    elif change == 'overlap': struct.pack_into('<I', data, central + 20, len(data))
    elif change == 'central_size': struct.pack_into('<I', data, end + 12, 0xffffffff)
    elif change == 'comment': struct.pack_into('<H', data, end + 20, 1); data += b'x'
    else: struct.pack_into('<H', data, end + 10, 65535)
    refresh(case, raw=data); rejected(case)


@pytest.mark.parametrize('name', ['models', 'config', 'manifest', 'pins', 'unavailable'])
def test_self_consistent_zip_cannot_override_reviewed_content(case, name):
    mapping = {'manifest': 'model-manifest.json', 'pins': 'model-source-pins.json', 'unavailable': 'unavailable-models.json'}
    key = next(k for k in case['assets'] if k.startswith(name + '/')) if name in ('models', 'config') else mapping[name]
    data = bytearray(case['assets'][key]); data[0] ^= 1; case['assets'][key] = bytes(data)
    refresh(case); rejected(case)


def test_output_never_overwrites_existing_empty_directory(case):
    case['output'].mkdir(); before = case['output'].stat().st_ino
    with pytest.raises(FileExistsError): consume(case)
    assert case['output'].stat().st_ino == before
    assert not list(case['output'].iterdir())


def test_late_output_collision_keeps_existing_directory(case, monkeypatch):
    original = artifact._publish
    def collide(stage, output):
        output.mkdir(); (output / 'keep').write_text('user content'); original(stage, output)
    monkeypatch.setattr(artifact, '_publish', collide)
    with pytest.raises(OSError): consume(case)
    assert (case['output'] / 'keep').read_text() == 'user content'
    assert not list(case['root'].glob('.model-artifact-*'))


@pytest.mark.parametrize('kind', ['zip', 'artifact_metadata', 'run_metadata', 'job_metadata'])
def test_input_symlinks_fail(case, kind):
    original = case[kind]; target = original.with_suffix('.real'); original.rename(target)
    try: original.symlink_to(target)
    except OSError as error: pytest.skip('Symlink unavailable: ' + str(error))
    rejected(case, 'Symlink/reparse')


def test_no_third_party_dependency_or_network_for_import_and_cli():
    code = (f'import sys; sys.path.insert(0, {str(NATIVE)!r}); import ci_model_artifact; '
            "assert not any(n.split('.')[0] in ('paddle', 'onnx', 'numpy', 'yaml', 'packaging') for n in sys.modules)")
    subprocess.run([sys.executable, '-I', '-S', '-c', code], check=True)
    subprocess.run([sys.executable, '-I', '-S', '-c', f'import sys,runpy;sys.path.insert(0,{str(NATIVE)!r});sys.argv=["ci_model_artifact.py","--help"];runpy.run_module("ci_model_artifact",run_name="__main__")'], check=True, capture_output=True)


def test_raw_nul_name_cannot_be_hidden_by_zipfile_normalization(case):
    data = bytearray(case['zip'].read_bytes())
    central = data.index(b'PK\x01\x02')
    local_length = struct.unpack_from('<H', data, 26)[0]
    central_length = struct.unpack_from('<H', data, central + 28)[0]
    assert local_length == central_length
    data[30 + local_length - 1] = 0
    data[central + 46 + central_length - 1] = 0
    refresh(case, raw=data); rejected(case, 'unsafe raw ZIP name')


def test_archive_mutation_after_initial_hash_cannot_publish(case, monkeypatch):
    original = artifact._zip_entries
    def mutate(stream, size, limits):
        result = original(stream, size, limits)
        with case['zip'].open('r+b') as writer:
            writer.seek(4); old = writer.read(1); writer.seek(4); writer.write(bytes([old[0] ^ 1]))
        return result
    monkeypatch.setattr(artifact, '_zip_entries', mutate)
    rejected(case, 'Archive changed|Input changed')


def test_symlinked_parent_cannot_hide_a_regular_archive(case):
    alias = case['root'] / 'alias'
    try: alias.symlink_to(case['root'], target_is_directory=True)
    except OSError as error: pytest.skip('Symlink unavailable: ' + str(error))
    case['zip'] = alias / case['zip'].name
    rejected(case, 'Symlink/reparse')
