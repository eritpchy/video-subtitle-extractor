"""Opt-in actual worker checks. Set VSE_NATIVE_PROVIDER_ARTIFACT to a verified build.

Unlike the JSON/source unit tests these load ORT. The launcher disables telemetry
before process creation. A Linux run cannot certify the Apple CoreML branch.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from ipc_client import Worker

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def artifact():
    value = os.environ.get('VSE_NATIVE_PROVIDER_ARTIFACT')
    if not value:
        pytest.skip('set VSE_NATIVE_PROVIDER_ARTIFACT to opt in to actual pinned ORT execution')
    path = Path(value).resolve(strict=True)
    executable = path / ('native-ocr.exe' if os.name == 'nt' else 'native-ocr')
    assert executable.is_file()
    return path, executable


def test_actual_capabilities_are_model_free_and_not_gpu_proof(artifact, tmp_path):
    _, executable = artifact
    result = subprocess.run([str(executable), '--capabilities'], cwd=tmp_path,
                            env=dict(os.environ, ORT_DISABLE_TELEMETRY='1'),
                            capture_output=True, text=True, check=True, timeout=30)
    report = json.loads(result.stdout)
    assert report['ort_version'] == '1.31.0'
    assert report['gpu_execution_verified'] is False
    assert report['coreml']['ane_allowed'] is False
    assert report['coreml']['compute_units'] == 'CPUAndGPU'
    if sys.platform != 'darwin':
        assert report['supported_request_providers'] == ['cpu']
        assert report['coreml']['available'] is False


def test_actual_cpu_cache_blank_detect_and_provider_refusal(artifact):
    path, executable = artifact
    worker = Worker([str(executable), '--models', str(path / 'models'),
                     '--config', str(path / 'config')])
    request = {'id': 1, 'tier': 'small', 'provider': 'cpu',
               'path': str(ROOT / 'tests/evidence/fixtures/english.png')}
    try:
        first = worker.request(request)
        assert first['ok'], first
        evidence = first['execution_evidence']
        assert first['requested_provider'] == 'cpu'
        assert first['actual_provider'] == 'CPUExecutionProvider'
        assert evidence['gpu_execution_verified'] is False
        for name in ('det', 'rec'):
            session = evidence['sessions'][name]
            assert session['status'] == 'executed'
            assert session['source'] == 'explicit_cpu_session'
            assert session['model'] == 'PP-OCRv6_small_' + name
            assert session['cpu_ep_executed'] is True
            assert session['coreml_kernel_events'] == session['cpu_kernel_events'] == 0
            assert session['request_run_count'] > 0
        window = first['execution_window']
        assert window['worker_pid'] == worker.proc.pid
        assert window['monotonic_end_ns'] >= window['monotonic_start_ns'] > 0
        assert window['utc_end_ns'] >= window['utc_start_ns'] > 0
        repeat = worker.request({**request, 'id': 2})
        assert repeat['ok'] and repeat['result'] == first['result']
        assert repeat['execution_evidence']['sessions']['det']['session_run_count'] == 2
        blank = worker.request({**request, 'id': 3,
                                'path': str(ROOT / 'tests/evidence/fixtures/blank.png')})
        assert blank['ok'] and blank['result'] == [[], []]
        rec = blank['execution_evidence']['sessions']['rec']
        assert rec['status'] == 'not_run' and rec['request_run_count'] == 0
        assert rec['session_run_count'] > 0
        assert rec['cpu_ep_executed'] is False and rec['observed_providers'] == []
        detect = worker.request({**request, 'id': 4, 'operation': 'detect'})
        assert detect['ok'] and detect['execution_evidence']['sessions']['rec'] is None
        for provider in (['invalid', 'coreml'] if sys.platform != 'darwin' else ['invalid']):
            rejected = worker.request({**request, 'id': 5, 'provider': provider})
            assert rejected['ok'] is False
            assert 'actual_provider' not in rejected
            assert worker.request({**request, 'id': 6})['ok']
    finally:
        code, stderr = worker.close()
        assert code == 0, stderr


@pytest.mark.skipif(sys.platform != 'darwin', reason='actual Apple CoreML EP execution requires a Mac')
def test_actual_coreml_first_profile_reuse_and_blank(artifact):
    path, executable = artifact
    worker = Worker([str(executable), '--models', str(path / 'models'),
                     '--config', str(path / 'config')])
    request = {'id': 'coreml-first', 'tier': 'small', 'provider': 'coreml',
               'path': str(ROOT / 'tests/evidence/fixtures/english.png')}
    try:
        first = worker.request(request)
        assert first['ok'], first
        assert first['actual_provider'] == 'CoreMLExecutionProvider'
        evidence = first['execution_evidence']
        assert evidence['ane_allowed'] is False
        assert evidence['compute_units'] == 'CPUAndGPU'
        assert evidence['gpu_execution_verified'] is False
        for name in ('det', 'rec'):
            session = evidence['sessions'][name]
            assert session['source'] == 'ort_first_run_profile'
            assert session['capture'] == 'first_successful_run'
            assert session['status'] == 'executed'
            assert session['coreml_kernel_events'] > 0
            assert session['coreml_ep_executed'] is True
            assert session['evidence_reused'] is False
            assert session['profile_window']['worker_pid'] == worker.proc.pid
        second = worker.request({**request, 'id': 'coreml-repeated'})
        assert second['ok'], second
        for name in ('det', 'rec'):
            session = second['execution_evidence']['sessions'][name]
            assert session['evidence_reused'] is True
            assert session['profile_window'] == evidence['sessions'][name]['profile_window']
        blank = worker.request({**request, 'id': 'coreml-blank',
                                'path': str(ROOT / 'tests/evidence/fixtures/blank.png')})
        assert blank['ok'] and blank['result'] == [[], []], blank
        session = blank['execution_evidence']['sessions']['rec']
        assert session['status'] == 'not_run'
        assert session['coreml_kernel_events'] == 0
        assert session['observed_providers'] == []
        assert session['evidence_reused'] is False
    finally:
        code, stderr = worker.close()
        assert code == 0, stderr
