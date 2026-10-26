"""Record actual fresh-venv source checks; never manufacture a package receipt.

This is CI orchestration, not a customer installer. Each named check has one
fixed command, runs in the newly created Python environment, and keeps its real
exit status and logs. Inference reports remain independently validated by
source_acceptance.py; a successful command log alone is not OCR/GPU proof.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PROFILES = {
    'linux-ort-cpu': ('Linux', 'X64', 'ubuntu-24.04', 'requirements-coreml-inference.txt'),
    'macos-ort-cpu-coreml': ('macOS', 'ARM64', 'macos-26', 'requirements-coreml-inference.txt'),
    'windows-cuda11.8': ('Windows', 'X64', 'windows-2022', 'requirements-source-cuda118.txt'),
    'windows-cuda12.6': ('Windows', 'X64', 'windows-2022', 'requirements-source-cuda126.txt'),
}
STEPS = ('pip-install', 'pip-check', 'source-default', 'models-verify', 'vsf-verify', 'gui-smoke',
         'cli-version', 'cli-help', 'image-cpu', 'image-coreml', 'video', 'cuda-environment')


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate source step state field')
        result[key] = value
    return result


def checkout_identity():
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    dirty = subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=ROOT, text=True)
    if not re.fullmatch('[a-f0-9]{40}', revision) or dirty.strip():
        raise ValueError('Source acceptance requires an exact clean tracked checkout')
    from scripts.source_acceptance import contract_files
    files = {}
    for relative in contract_files():
        path = ROOT / relative
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            raise ValueError('Missing/unsafe source contract file: ' + relative)
        files[relative] = sha(path)
    return {'commit': revision, 'dirty': False, 'files': files}


def identity(profile):
    expected_os, expected_arch, runner, _ = PROFILES[profile]
    values = {name: os.environ.get(name, '') for name in
              ('GITHUB_SHA', 'GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT',
               'GITHUB_JOB', 'RUNNER_OS', 'RUNNER_ARCH')}
    if (not re.fullmatch('[a-f0-9]{40}', values['GITHUB_SHA'])
            or not re.fullmatch('[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', values['GITHUB_REPOSITORY'])
            or any(not re.fullmatch('[1-9][0-9]*', values[key]) for key in ('GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT'))
            or not re.fullmatch('[A-Za-z0-9_-]+', values['GITHUB_JOB'])
            or values['RUNNER_OS'] != expected_os or values['RUNNER_ARCH'] != expected_arch):
        raise ValueError('Missing or mismatched actual source CI identity')
    return {'repository': values['GITHUB_REPOSITORY'], 'commit': values['GITHUB_SHA'],
            'run_id': int(values['GITHUB_RUN_ID']), 'run_attempt': int(values['GITHUB_RUN_ATTEMPT']),
            'job': values['GITHUB_JOB'], 'profile': profile, 'runner': runner,
            'runner_os': expected_os, 'runner_arch': expected_arch}


def clean_environment():
    # Source installation/inference must not borrow hosted setup-python loader
    # overrides. The actual new-interpreter probe below proves it can start.
    return {key: value for key, value in os.environ.items()
            if not key.startswith(('LD_', 'DYLD_'))
            and key not in ('PYTHONHOME', 'PYTHONPATH', 'MTL_CAPTURE_ENABLED')}


def _new_path(path):
    path = Path(path).absolute()
    if '..' in path.parts or path.is_symlink() or path.exists():
        raise ValueError('Source checks require a new path, never an existing directory: ' + str(path))
    parent = path.parent.resolve(strict=True)
    return parent / path.name


def _root(path):
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        raise ValueError('Missing/unsafe source evidence root')
    return path.resolve(strict=True)


def _save(root, state):
    pending = root / 'steps.json.pending'
    with pending.open('x', encoding='utf-8') as stream:
        json.dump(state, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(pending, root / 'steps.json')


def _load(root):
    path = root / 'steps.json'
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError('Missing/unsafe source step state')
    state = json.loads(path.read_bytes(), object_pairs_hook=unique)
    if state.get('schema') != 1 or state.get('identity') != identity(state['identity']['profile']):
        raise ValueError('Source step state came from a different run/profile')
    if state.get('checkout') != checkout_identity() or state['checkout']['commit'] != state['identity']['commit']:
        raise ValueError('Source checkout differs from its initial run')
    return state


def _run(root, name, argv, env, *, timeout=5400):
    stdout, stderr = root / (name + '.stdout.log'), root / (name + '.stderr.log')
    row = {'name': name, 'argv': list(map(str, argv)), 'started_utc_ns': time.time_ns()}
    code = -1
    try:
        with stdout.open('xb') as out, stderr.open('xb') as err:
            result = subprocess.run(row['argv'], cwd=ROOT, env=env, stdout=out, stderr=err, timeout=timeout)
            code = result.returncode
    except Exception as error:
        row['error'] = type(error).__name__ + ': ' + str(error)
    finally:
        row.update(returncode=code, ended_utc_ns=time.time_ns())
        for label, path in (('stdout', stdout), ('stderr', stderr)):
            if path.is_file() and not path.is_symlink():
                row[label] = {'path': path.name, 'bytes': path.stat().st_size, 'sha256': sha(path)}
                # Evidence keeps the full originals; bounded console tails do
                # not alter the check's exit status or count as its receipt.
                with path.open('rb') as stream:
                    stream.seek(max(0, path.stat().st_size - 16384))
                    tail = stream.read()
                if tail:
                    print(tail.decode('utf-8', errors='replace'), file=sys.stdout if label == 'stdout' else sys.stderr)
    return row


PROBE = """import hashlib,json,os,site,sys
from pathlib import Path
p=Path(sys.prefix); c=p/'pyvenv.cfg'
settings={k.strip().lower():v.strip().lower() for line in c.read_text().splitlines() if '=' in line for k,v in [line.split('=',1)]}
assert sys.prefix != sys.base_prefix and settings.get('include-system-site-packages')=='false'
assert site.ENABLE_USER_SITE is False and sys.version_info[:2]==(3,12)
assert not {k:v for k,v in os.environ.items() if k.startswith(('LD_','DYLD_')) and v}
with Path(sys.executable).open('rb') as f: digest=hashlib.file_digest(f,'sha256').hexdigest()
print(json.dumps({'interpreter':sys.executable,'interpreter_sha256':digest,'prefix':sys.prefix,'base_prefix':sys.base_prefix,'python':[3,12],'user_site_enabled':site.ENABLE_USER_SITE,'venv_created_new':True,'system_site_packages':False,'loader_overrides':{},'pyvenv_cfg_sha256':hashlib.sha256(c.read_bytes()).hexdigest()}))
"""


def initialize(evidence, profile, venv, models):
    current, checkout = identity(profile), checkout_identity()
    if current['commit'] != checkout['commit']:
        raise ValueError('Source CI SHA does not match the actual checkout')
    evidence, venv = _new_path(evidence), _new_path(venv)
    models = Path(models).absolute()
    if (models.is_symlink() or '..' in models.parts or evidence.is_relative_to(models)
            or models.is_relative_to(evidence) or evidence == venv
            or evidence.is_relative_to(venv) or venv.is_relative_to(evidence)):
        raise ValueError('Source evidence and its new environment must be separate')
    evidence.mkdir()
    # Claim the new directory atomically before venv's otherwise permissive
    # create/reuse operation. A concurrently appearing user directory is never
    # handed to Python's venv initializer.
    venv.mkdir(exist_ok=False)
    state = {'schema': 1, 'identity': current, 'checkout': checkout,
             'environment': None, 'models': str(models), 'steps': [],
             'paths': {'checkout': str(ROOT), 'evidence': str(evidence), 'models': str(models), 'venv': str(venv)},
             'scope': 'Actual source command evidence, not final package or physical GPU certification'}
    _save(evidence, state)
    row = _run(evidence, 'venv-create', [sys.executable, '-m', 'venv', str(venv)], clean_environment())
    state['steps'].append(row)
    _save(evidence, state)
    if row['returncode']:
        raise RuntimeError('Creating the fresh source environment failed')
    python = venv / ('Scripts/python.exe' if profile.startswith('windows-') else 'bin/python')
    probe = _run(evidence, 'venv-probe', [str(python), '-I', '-c', PROBE], clean_environment(), timeout=60)
    state['steps'].append(probe)
    _save(evidence, state)
    if probe['returncode']:
        raise RuntimeError('The new interpreter cannot start in a clean loader environment')
    environment = json.loads((evidence / probe['stdout']['path']).read_bytes(), object_pairs_hook=unique)
    if (Path(environment['prefix']).resolve() != venv or
            Path(environment['interpreter']).absolute() != python or environment['loader_overrides'] != {}):
        raise ValueError('Actual interpreter probe does not belong to the newly created environment')
    state['environment'] = environment
    _save(evidence, state)
    return state


DEFAULT_PROBE = """import json,os,sys
from pathlib import Path
root=Path(sys.argv[1]); sys.path.insert(0,str(root))
for key in list(os.environ):
    if key.startswith('VSE_'): os.environ.pop(key)
from backend.tools.native_ocr import selected_backend
assert selected_backend()=='paddle'
assert not {'paddle','onnxruntime','PySide6'} & set(sys.modules)
workers=[str(path.relative_to(root)) for path in root.rglob('*') if path.is_file() and path.name in ('native-ocr','native-ocr.exe')]
assert not workers, workers
print(json.dumps({'source_default':'paddle','custom_worker_files':workers,'interpreter':sys.executable,'scope':'Source dispatch without project worker; not GPU or inference proof'}))
"""


def command(state, root, step):
    profile = state['identity']['profile']
    python = state['environment']['interpreter']
    models = state['models']
    windows = profile.startswith('windows-')
    report = str(root / 'cpu-all.json')
    commands = {
        'pip-install': ['-m', 'pip', 'install', '-r', PROFILES[profile][3], 'pytest==8.3.5', '--report', str(root / 'pip-install.json')],
        'pip-check': ['-m', 'pip', 'check'],
        'source-default': ['-I', '-S', '-c', DEFAULT_PROBE, state['paths']['checkout']],
        'models-verify': ['-S', 'scripts/prepare_source_models.py', 'verify', '--format', 'paddle' if windows else 'onnx', models],
        'vsf-verify': ['scripts/install_videosubfinder.py', '--verify-only'],
        'gui-smoke': ['gui.py', '--smoke-test'],
        'cli-version': ['-m', 'backend.cli', '--version'],
        'cli-help': ['-m', 'backend.cli', '--help'],
        'image-cpu': (['scripts/check_paddle_models.py', '--app-root', '.', '--fixtures', 'native/ocr/tests/evidence', '--report', report] if windows else
                      ['scripts/check_python_coreml.py', '--models', models, '--fixtures', 'native/ocr/tests/evidence', '--provider', 'cpu', '--case-set', 'all', '--report', report]),
        'video': (['scripts/e2e_smoke.py', '--all-modes', '--provider', 'cpu', '--output', str(root / 'video')] if windows else
                  ['scripts/e2e_python_coreml.py', '--models', models, '--provider', 'both' if profile == 'macos-ort-cpu-coreml' else 'cpu', '--output', str(root / 'video')]),
    }
    if profile == 'macos-ort-cpu-coreml':
        commands['image-coreml'] = ['scripts/check_python_coreml.py', '--models', models, '--fixtures', 'native/ocr/tests/evidence', '--provider', 'coreml', '--case-set', 'all', '--report', str(root / 'coreml-all.json')]
    if windows:
        commands['cuda-environment'] = ['scripts/check_source_cuda_environment.py', '--cuda', profile.removeprefix('windows-cuda'), '--pip-report', str(root / 'pip-install.json'), '--report', str(root / 'environment.json')]
    if step not in commands:
        raise ValueError('This source profile has no such step')
    return [python, *commands[step]]


def run_step(evidence, step):
    root = _root(evidence)
    state = _load(root)
    if (state.get('environment') is None or any(row.get('returncode') != 0 for row in state['steps'])
            or any(row.get('name') == step for row in state['steps'])):
        raise ValueError('Cannot reuse a completed step or continue a failed source run')
    env = {key: value for key, value in clean_environment().items() if not key.startswith('VSE_')}
    profile = state['identity']['profile']
    windows = profile.startswith('windows-')
    env.update(ORT_DISABLE_TELEMETRY='1', VSE_OCR_BACKEND='paddle' if windows else 'python-coreml',
               VSE_NATIVE_OCR_ROOT=str(root / 'absent-native-worker'), VSE_OFFLINE='1',
               PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK='True', PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1')
    env['VSE_MODEL_DIR' if windows else 'VSE_PYTHON_COREML_MODEL_ROOT'] = state['models']
    row = _run(root, step, command(state, root, step), env)
    state['steps'].append(row)
    _save(root, state)
    if row['returncode']:
        raise RuntimeError('Actual source check failed: ' + step)
    if step == 'vsf-verify':
        platform, arch = ('windows', 'x64') if windows else ('macos', 'arm64') if profile.startswith('macos') else ('linux', 'x64')
        source = ROOT / 'backend/subfinder' / platform / arch / 'install.json'
        target = root / 'vsf-install.json'
        if source.is_symlink() or target.exists() or not source.is_file():
            raise ValueError('Missing/unsafe verified VSF provenance')
        with source.open('rb') as stream, target.open('xb') as output:
            shutil.copyfileobj(stream, output)
    elif step == 'source-default':
        with (root / row['stdout']['path']).open('rb') as stream, (root / 'source-default.json').open('xb') as output:
            shutil.copyfileobj(stream, output)
    return row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='operation', required=True)
    init = commands.add_parser('init')
    init.add_argument('--evidence', type=Path, required=True)
    init.add_argument('--profile', choices=PROFILES, required=True)
    init.add_argument('--venv', type=Path, required=True)
    init.add_argument('--models', type=Path, required=True)
    run = commands.add_parser('run-step')
    run.add_argument('--evidence', type=Path, required=True)
    run.add_argument('--step', choices=STEPS, required=True)
    args = parser.parse_args(argv)
    if args.operation == 'init':
        initialize(args.evidence, args.profile, args.venv, args.models)
    else:
        run_step(args.evidence, args.step)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
