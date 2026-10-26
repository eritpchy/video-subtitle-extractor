"""Normal source CUDA installation is not native-package or GPU certification."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts import check_source_cuda_environment as check


def pip_document(wheels):
    return {'install': [{'metadata': {'name': name, 'version': '3.3.1' if name == 'paddlepaddle-gpu' else '12.6.85'},
                         'is_direct': True, 'requested': True,
                         'download_info': {'url': row['url'], 'archive_info': {'hashes': {'sha256': row['sha256']}}}}
                        for name, row in wheels.items()]}


def runtime(tmp_path, cuda):
    prefix = tmp_path / 'new-venv'
    return {'platform': 'win32', 'architecture': 'amd64', 'implementation': 'CPython',
            'python': [3, 12], 'prefix': str(prefix), 'base_prefix': str(tmp_path / 'base'),
            'interpreter': str(prefix / 'Scripts/python.exe'), 'user_site_enabled': False,
            'paddle_origin': str(prefix / 'Lib/site-packages/paddle/__init__.py'),
            'paddle_version': '3.3.1', 'compiled_with_cuda': True, 'compiled_cuda_version': cuda,
            'actual_device': 'cpu', 'distributions': {'paddlepaddle-gpu': '3.3.1', 'paddleocr': '3.7.0',
                'paddlex': '3.7.2', 'nvidia-nvjitlink-cu12': '12.6.85',
                **{name: '6.12.0' for name in ('pyside6', 'pyside6-essentials', 'pyside6-addons', 'shiboken6')}}}


@pytest.mark.parametrize('cuda', ['11.8', '12.6'])
def test_exact_profile_pip_report_and_explicit_cpu_identity(tmp_path, cuda):
    profile, wheels = check.load_profile(cuda)
    assert profile['hash_origin'] == 'build-observed complete wheel bytes'
    assert check.validate_install_report(pip_document(wheels), wheels)
    check.validate_runtime_identity(runtime(tmp_path, cuda), cuda)


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'cpu', 'hash', 'url', 'version', 'indirect', 'not-requested'])
def test_pip_report_cannot_substitute_same_name_wrong_cuda_or_unpinned_package(mutation):
    _, wheels = check.load_profile('12.6')
    report = pip_document(wheels)
    row = report['install'][0]
    if mutation == 'missing': report['install'].pop()
    elif mutation == 'duplicate': report['install'].append(deepcopy(row))
    elif mutation == 'cpu': report['install'].append({'metadata': {'name': 'paddlepaddle', 'version': '3.3.1'}})
    elif mutation == 'hash': row['download_info']['archive_info']['hashes']['sha256'] = '0' * 64
    elif mutation == 'url': row['download_info']['url'] = check.load_profile('11.8')[1]['paddlepaddle-gpu']['url']
    elif mutation == 'version': row['metadata']['version'] = '3.3.0'
    elif mutation == 'indirect': row['is_direct'] = False
    elif mutation == 'not-requested': row['requested'] = False
    with pytest.raises(ValueError): check.validate_install_report(report, wheels)


@pytest.mark.parametrize(('key', 'value'), [('platform', 'linux'), ('architecture', 'arm64'),
    ('implementation', 'PyPy'), ('python', [3, 13]), ('user_site_enabled', True),
    ('compiled_with_cuda', False), ('compiled_cuda_version', '11.8'), ('actual_device', 'gpu:0'),
    ('paddle_version', '3.3.0')])
def test_installed_runtime_wrong_identity_is_rejected(tmp_path, key, value):
    identity = runtime(tmp_path, '12.6'); identity[key] = value
    with pytest.raises(ValueError): check.validate_runtime_identity(identity, '12.6')


@pytest.mark.parametrize('mutation', ['same-prefix', 'foreign-python', 'foreign-paddle', 'cpu', 'version', 'qt-missing', 'qt-mismatch'])
def test_host_dependencies_mixed_paddle_and_fake_qt_substitution_fail(tmp_path, mutation):
    identity = runtime(tmp_path, '12.6')
    if mutation == 'same-prefix': identity['base_prefix'] = identity['prefix']
    elif mutation == 'foreign-python': identity['interpreter'] = str(tmp_path / 'host/python.exe')
    elif mutation == 'foreign-paddle': identity['paddle_origin'] = str(tmp_path / 'host/paddle/__init__.py')
    elif mutation == 'cpu': identity['distributions']['paddlepaddle'] = '3.3.1'
    elif mutation == 'version': identity['distributions']['nvidia-nvjitlink-cu12'] = '12.9.0'
    elif mutation == 'qt-missing': del identity['distributions']['pyside6']
    elif mutation == 'qt-mismatch': identity['distributions']['shiboken6'] = '6.11.0'
    with pytest.raises(ValueError): check.validate_runtime_identity(identity, '12.6')


def test_failed_pin_report_never_imports_paddle_or_turns_green(tmp_path, monkeypatch):
    source = tmp_path / 'pip.json'; source.write_text('{"install":[]}')
    monkeypatch.setattr(check, 'runtime_identity', lambda: pytest.fail('Unverified runtime must not be imported'))
    output = tmp_path / 'report.json'
    assert check.main(['--cuda', '11.8', '--pip-report', str(source), '--report', str(output)]) == 1
    report = json.loads(output.read_bytes())
    assert report['result'] == 'failed'
    assert report['gpu_inference_verified'] is False
    assert report['ocr_cases_verified'] is False
    assert report['final_package_accepted'] is False


def test_environment_pass_does_not_claim_ocr_or_gpu_test(tmp_path, monkeypatch):
    _, wheels = check.load_profile('12.6')
    source = tmp_path / 'pip.json'; source.write_text(json.dumps(pip_document(wheels)))
    monkeypatch.setattr(check, 'runtime_identity', lambda: runtime(tmp_path, '12.6'))
    result = check.run('12.6', source, tmp_path / 'report.json')
    assert result['result'] == 'passed'
    assert result['scope'] == 'python-cuda-source-environment'
    assert result['runtime']['actual_device'] == 'cpu'
    assert result['gpu_inference_verified'] is result['ocr_cases_verified'] is result['final_package_accepted'] is False
