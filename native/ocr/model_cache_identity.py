#!/usr/bin/env python3
"""Content-addressed conversion inputs and honest, unsigned CI provenance.

Producer identity is written once, only after a fresh conversion. Cache consumers
never rewrite it. An upload ID is recorded later in a separate receipt, not in
the uploaded content itself. These CI records are not signed attestations.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re

from model_assets import sha256
from prepare_models import canonical, declared_build_versions

ROOT = Path(__file__).resolve().parent
INPUT_FILES = (
    'model-source-pins.json', 'prepare_models.py', 'requirements-model-build.txt',
    'model_assets.py', 'verify_model_cache.py', 'model_cache_identity.py',
)
PROVENANCE = 'conversion-provenance.json'
KEY_PREFIX = 'native-ocr-fp32-v2-'
PRODUCER_WORKFLOWS = ('.github/workflows/release.yml', '.github/workflows/native-ocr.yml')
SCOPE = 'Unsigned CI conversion record; fixed model hashes are the content authority, not this record.'


def producer_environment():
    return {'platform': platform.system(), 'architecture': platform.machine().lower(),
            'runner': 'ubuntu-24.04', 'python_implementation': platform.python_implementation(),
            'python_version': platform.python_version()}


def _environment(environment):
    if (not isinstance(environment, dict)
            or set(environment) != {'platform', 'architecture', 'runner',
                                    'python_implementation', 'python_version'}
            or environment['platform'] != 'Linux'
            or environment['architecture'] != 'x86_64'
            or environment['runner'] != 'ubuntu-24.04'
            or environment['python_implementation'] != 'CPython'
            or not re.fullmatch(r'3\.12\.\d+', str(environment['python_version']))):
        raise ValueError('Unsupported conversion producer environment')
    return environment


def conversion_inputs(source_root=ROOT, environment=None):
    source_root = Path(source_root)
    environment = producer_environment() if environment is None else environment
    _environment(environment)
    files = {}
    for name in INPUT_FILES:
        path = source_root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('Missing/non-regular conversion input: ' + name)
        files[name] = sha256(path)
    return {'schema_version': 2, 'files': files, 'environment': dict(environment),
            'tool_versions': declared_build_versions(source_root / 'requirements-model-build.txt')}


def input_key(inputs):
    return KEY_PREFIX + hashlib.sha256(canonical(inputs)).hexdigest()


def ci_identity(environment=None):
    env = os.environ if environment is None else environment
    record = {'repository': env.get('GITHUB_REPOSITORY', ''),
              'commit': env.get('GITHUB_SHA', ''), 'run_id': env.get('GITHUB_RUN_ID', ''),
              'run_attempt': env.get('GITHUB_RUN_ATTEMPT', ''),
              'workflow_ref': env.get('GITHUB_WORKFLOW_REF', '')}
    return _identity(record)


def _identity(record):
    if (not isinstance(record, dict)
            or set(record) != {'repository', 'commit', 'run_id', 'run_attempt', 'workflow_ref'}
            or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', str(record['repository']))
            or not re.fullmatch(r'[0-9a-f]{40}', str(record['commit']))
            or any(not re.fullmatch(r'[1-9]\d*', str(record[k])) for k in ('run_id', 'run_attempt'))
            or not isinstance(record['workflow_ref'], str)
            or not any(record['workflow_ref'].startswith(record['repository'] + '/' + path + '@refs/')
                       for path in PRODUCER_WORKFLOWS)
            or not re.fullmatch(r'[^\r\n]+@refs/[^\r\n]+', record['workflow_ref'])):
        raise ValueError('Missing/invalid actual GitHub producer or consumer identity')
    return record


def content_inventory(root):
    root = Path(root)
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError('Unsafe converted model asset: ' + str(path))
        if path.is_file() and path.name != PROVENANCE:
            result[path.relative_to(root).as_posix()] = {'bytes': path.stat().st_size, 'sha256': sha256(path)}
    return result


def _write_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(canonical(value))


def record_producer(root, *, source_root=ROOT, environment=None, ci=None):
    from verify_model_cache import verify
    root = Path(root)
    if (root / PROVENANCE).exists() or (root / PROVENANCE).is_symlink():
        raise FileExistsError('Never replace the original conversion producer')
    # Only genuine fresh conversion output (no existing producer) can enter.
    verify(root)
    inputs = conversion_inputs(source_root, environment)
    for name, expected in inputs['tool_versions'].items():
        if importlib.metadata.version(name) != expected:
            raise ValueError('Actual conversion dependency differs from its exact pin: ' + name)
    record = {'schema_version': 1, 'scope': SCOPE, 'producer': ci_identity(ci),
              'input_key': input_key(inputs), 'conversion_inputs': inputs,
              'content': content_inventory(root)}
    _write_new(root / PROVENANCE, record)
    return record


def verify_provenance(root, *, source_root=ROOT, expected_key=None):
    root = Path(root)
    path = root / PROVENANCE
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 128 * 1024:
        raise ValueError('Missing/unsafe original conversion provenance; old caches cannot be reused')
    record = json.loads(path.read_bytes())
    if (not isinstance(record, dict)
            or set(record) != {'schema_version', 'scope', 'producer', 'input_key', 'conversion_inputs', 'content'}
            or record['schema_version'] != 1 or record['scope'] != SCOPE):
        raise ValueError('Invalid conversion provenance schema')
    producer = _identity(record['producer'])
    repository = os.environ.get('GITHUB_REPOSITORY')
    if repository and producer['repository'] != repository:
        raise ValueError('Conversion producer belongs to a different repository')
    inputs = record['conversion_inputs']
    if not isinstance(inputs, dict) or 'environment' not in inputs:
        raise ValueError('Missing conversion input contract')
    # Deliberately use the recorded Linux producer environment. The consumer may
    # be Windows/macOS, with another Python patch: that is not a new conversion.
    actual = conversion_inputs(source_root, inputs['environment'])
    key = input_key(actual)
    if inputs != actual or record['input_key'] != key or (expected_key is not None and key != expected_key):
        raise ValueError('Conversion input contract/key differs from the current reviewed source')
    if record['content'] != content_inventory(root):
        raise ValueError('Converted content differs from its original producer inventory')
    return record


def upload_receipt(root, artifact_id, artifact_digest, *, ci=None, source_root=ROOT):
    from verify_model_cache import verify
    verify(root, require_provenance=True)
    record = verify_provenance(root, source_root=source_root)
    if not re.fullmatch(r'[1-9]\d*', artifact_id):
        raise ValueError('Missing actual upload artifact ID')
    digest = artifact_digest.removeprefix('sha256:')
    if not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise ValueError('Missing actual upload artifact digest')
    return {'schema_version': 1,
            'scope': 'Unsigned post-upload CI receipt, external to the model artifact; not an attestation.',
            'consumer': ci_identity(ci), 'producer': record['producer'], 'input_key': record['input_key'],
            'conversion_provenance_sha256': sha256(Path(root) / PROVENANCE),
            'artifact': {'id': artifact_id, 'sha256': digest}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    key = sub.add_parser('key')
    key.add_argument('--github-output', type=Path)
    record = sub.add_parser('record')
    record.add_argument('models', type=Path)
    check = sub.add_parser('check')
    check.add_argument('models', type=Path)
    check.add_argument('--expected-key', required=True)
    receipt = sub.add_parser('receipt')
    receipt.add_argument('models', type=Path)
    receipt.add_argument('--artifact-id', required=True)
    receipt.add_argument('--artifact-digest', required=True)
    receipt.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'key':
        value = input_key(conversion_inputs())
        if args.github_output:
            with args.github_output.open('a', encoding='utf-8') as stream:
                stream.write('key=' + value + '\n')
        print(value)
    elif args.command == 'record':
        print(json.dumps(record_producer(args.models)['producer'], sort_keys=True))
    elif args.command == 'check':
        print(json.dumps(verify_provenance(args.models, expected_key=args.expected_key)['producer'], sort_keys=True))
    else:
        _write_new(args.output, upload_receipt(args.models, args.artifact_id, args.artifact_digest))


if __name__ == '__main__':
    main()
