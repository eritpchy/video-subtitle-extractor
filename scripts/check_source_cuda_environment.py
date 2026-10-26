#!/usr/bin/env python3
"""Verify a fresh Python CUDA source install; never certify physical GPU OCR."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import re
import site
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
PINS = ROOT / 'docs/audits/source-cuda-wheel-pins.json'


def canonical(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def load_profile(cuda):
    if cuda not in ('11.8', '12.6'):
        raise ValueError('Choose the reviewed CUDA 11.8 or 12.6 source profile')
    document = json.loads(PINS.read_bytes())
    profile = document['profiles']['cuda' + cuda]
    paddle = profile['paddle']
    expected = ('https://paddle-whl.cdn.bcebos.com/stable/cu' + cuda.replace('.', '')
                + '/paddlepaddle-gpu/paddlepaddle_gpu-3.3.1-cp312-cp312-win_amd64.whl')
    if paddle['url'] != expected:
        raise ValueError('Paddle source wheel is not the reviewed official CUDA URL')
    wheels = {'paddlepaddle-gpu': paddle}
    if cuda == '12.6':
        wheels['nvidia-nvjitlink-cu12'] = profile['nvjitlink']
    for name, row in wheels.items():
        url = urlsplit(row['url'])
        if (url.scheme != 'https' or url.username or url.password or url.query or url.fragment
                or url.hostname not in ('paddle-whl.cdn.bcebos.com', 'files.pythonhosted.org')
                or not url.path.endswith('/' + row['filename'])
                or not re.fullmatch('[a-f0-9]{64}', row['sha256'])
                or type(row['bytes']) is not int or row['bytes'] <= 0):
            raise ValueError('Invalid reviewed whole-wheel identity: ' + name)
    return profile, wheels


def validate_install_report(document, wheels):
    """Pip checked the URL #sha256 against the complete downloaded archive."""
    rows = {}
    for entry in document.get('install', []):
        name = canonical(entry['metadata']['name'])
        if name in rows:
            raise ValueError('Duplicate pip installation record: ' + name)
        rows[name] = entry
    if 'paddlepaddle' in rows:
        raise ValueError('CPU Paddle was mixed into the CUDA source installation')
    verified = {}
    for name, expected in wheels.items():
        entry = rows.get(name, {})
        download = entry.get('download_info', {})
        hashes = download.get('archive_info', {}).get('hashes', {})
        version = '3.3.1' if name == 'paddlepaddle-gpu' else '12.6.85'
        if (download.get('url') != expected['url'] or hashes.get('sha256') != expected['sha256']
                or entry.get('metadata', {}).get('version') != version
                or entry.get('is_direct') is not True or entry.get('requested') is not True):
            raise ValueError('Pip report lacks the exact requested official wheel: ' + name)
        verified[name] = dict(expected, hash_verification='pip URL whole-archive SHA256 validation')
    return verified


def runtime_identity():
    distributions = {}
    for distribution in importlib.metadata.distributions():
        name = canonical(distribution.metadata['Name'])
        if name in distributions:
            raise ValueError('Duplicate installed distribution: ' + name)
        distributions[name] = distribution.version
    import paddle
    paddle.set_device('cpu')  # This is explicitly a CPU environment check, not a GPU probe.
    return {'platform': sys.platform, 'architecture': platform.machine().lower(),
            'implementation': platform.python_implementation(),
            'python': list(sys.version_info[:2]), 'interpreter': sys.executable,
            'prefix': sys.prefix, 'base_prefix': sys.base_prefix,
            'user_site_enabled': site.ENABLE_USER_SITE,
            'distributions': distributions, 'paddle_origin': str(Path(paddle.__file__).resolve()),
            'paddle_version': paddle.__version__, 'compiled_with_cuda': paddle.is_compiled_with_cuda(),
            'compiled_cuda_version': paddle.version.cuda(), 'actual_device': paddle.get_device()}


def validate_runtime_identity(identity, cuda):
    if (identity['platform'] != 'win32' or identity['architecture'] not in ('amd64', 'x86_64')
            or identity['implementation'] != 'CPython' or identity['python'] != [3, 12]):
        raise ValueError('Windows x64 CPython 3.12 is required for this source recipe')
    prefix = Path(identity['prefix']).resolve()
    if (prefix == Path(identity['base_prefix']).resolve() or identity['user_site_enabled'] is not False
            or not Path(identity['interpreter']).resolve().is_relative_to(prefix)
            or not Path(identity['paddle_origin']).resolve().is_relative_to(prefix)):
        raise ValueError('Python source dependencies must belong to the new isolated virtual environment')
    packages = identity['distributions']
    expected = {'paddlepaddle-gpu': '3.3.1', 'paddleocr': '3.7.0', 'paddlex': '3.7.2'}
    if cuda == '12.6':
        expected['nvidia-nvjitlink-cu12'] = '12.6.85'
    if 'paddlepaddle' in packages or any(packages.get(key) != value for key, value in expected.items()):
        raise ValueError('Wrong or mixed Paddle source dependency identities')
    qt = [packages.get(name) for name in ('pyside6', 'pyside6-essentials', 'pyside6-addons', 'shiboken6')]
    if not all(qt) or len(set(qt)) != 1:
        raise ValueError('Ordinary source pip install must retain its matching declared full PySide6 dependency set')
    if (identity['paddle_version'] != '3.3.1' or identity['compiled_with_cuda'] is not True
            or identity['compiled_cuda_version'] != cuda or identity['actual_device'] != 'cpu'):
        raise ValueError('Installed Paddle CUDA identity or explicit CPU selection differs from the recipe')


def run(cuda, pip_report, output):
    pip_report, output = Path(pip_report), Path(output)
    report = {'schema': 1, 'scope': 'python-cuda-source-environment', 'cuda': cuda,
              'result': 'failed', 'gpu_inference_verified': False,
              'native_worker_used': False, 'ocr_cases_verified': False,
              'final_package_accepted': False,
              'environment_check_scope': 'Pip-verified wheel identity, installed versions, interpreter and Paddle roots; not a complete dependency-file or loaded-GPU-module audit'}
    try:
        profile, wheels = load_profile(cuda)
        report['source_wheels'] = validate_install_report(json.loads(pip_report.read_bytes()), wheels)
        report['pip_report_sha256'] = hashlib.sha256(pip_report.read_bytes()).hexdigest()
        report['reviewed_wheel_provenance'] = profile
        identity = runtime_identity()
        validate_runtime_identity(identity, cuda)
        report.update(runtime=identity, result='passed')
    except Exception as error:
        report['error'] = type(error).__name__ + ': ' + str(error)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cuda', required=True, choices=('11.8', '12.6'))
    parser.add_argument('--pip-report', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args(argv)
    report = run(args.cuda, args.pip_report, args.report)
    print(json.dumps({key: report.get(key) for key in ('scope', 'result', 'error')}))
    return 0 if report['result'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
