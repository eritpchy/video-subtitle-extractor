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
from ipc_client import Worker, frame

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
        assert 'diagnostic_metal_capture' not in first and 'timing_eligible' not in first
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


@pytest.mark.parametrize('tier', ['small', 'medium'])
def test_actual_cpu_multiline_full_partial_batches_and_shape_changes(artifact, tier):
    path, executable = artifact
    worker = Worker([str(executable), '--models', str(path / 'models'),
                     '--config', str(path / 'config')])
    request = {'tier': tier, 'provider': 'cpu',
               'path': str(ROOT / 'tests/evidence/fixtures/multiline_2.5.png')}
    try:
        # The frozen fixture has two recognized lines: batch=2 is full, batch=3
        # has an actual n=2 partial batch, batch=1 makes two separate Runs.
        full = worker.request({**request, 'id': 70, 'batch_size': 2})
        partial = worker.request({**request, 'id': 71, 'batch_size': 3})
        single = worker.request({**request, 'id': 72, 'batch_size': 1})
        assert full['ok'] and partial['ok'] and single['ok']
        assert len(full['result'][1]) == 2
        assert partial['result'] == full['result']
        assert full['execution_evidence']['sessions']['rec']['request_run_count'] == 1
        assert partial['execution_evidence']['sessions']['rec']['request_run_count'] == 1
        assert single['execution_evidence']['sessions']['rec']['request_run_count'] == 2
        assert single['result'][0] == full['result'][0]
        assert [r[0] for r in single['result'][1]] == [r[0] for r in full['result'][1]]
        # Different batch sizes can change per-batch padded width and therefore
        # tensor bytes. Do not call their confidence difference a same-input
        # parity failure; repeat each exact batching configuration instead.
        repeated_single = worker.request({**request, 'id': 76, 'batch_size': 1})
        assert repeated_single['ok'] and repeated_single['result'] == single['result']
        for index, fixture in enumerate(('english.png', 'blank.png', 'multiline_2.5.png')):
            result = worker.request({**request, 'id': 73 + index,
                                     'path': str(ROOT / 'tests/evidence/fixtures' / fixture)})
            assert result['ok']
            if fixture == 'blank.png':
                assert result['execution_evidence']['sessions']['rec']['status'] == 'not_run'
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


def test_actual_metal_entrypoints_are_explicit_and_nonmac_is_unavailable(artifact, tmp_path):
    path, executable = artifact
    normal_env = dict(os.environ, ORT_DISABLE_TELEMETRY='1')
    normal_env.pop('MTL_CAPTURE_ENABLED', None)
    capture_env = dict(normal_env, MTL_CAPTURE_ENABLED='1')
    rejected = subprocess.run([str(executable), '--capabilities'], env=capture_env,
                              capture_output=True, text=True, timeout=30)
    assert rejected.returncode == 2 and 'forbidden during normal worker' in rejected.stderr
    missing = subprocess.run([str(executable), '--diagnostic-metal-capabilities'], env=normal_env,
                             capture_output=True, text=True, timeout=30)
    assert missing.returncode == 2 and 'require MTL_CAPTURE_ENABLED=1' in missing.stderr
    probe = subprocess.run([str(executable), '--diagnostic-metal-capabilities'], env=capture_env,
                           capture_output=True, text=True, check=True, timeout=30)
    capability = json.loads(probe.stdout)
    assert capability['diagnostic_only'] is True and capability['timing_eligible'] is False
    assert capability['gpu_execution_verified'] is False
    if sys.platform != 'darwin':
        assert capability['metal_capture']['available'] is False


def test_actual_metal_capture_rejects_extra_requests_and_existing_output(artifact, tmp_path):
    path, executable = artifact
    env = dict(os.environ, ORT_DISABLE_TELEMETRY='1', MTL_CAPTURE_ENABLED='1')
    request = {'id': 'one-fixed-fixture', 'tier': 'small', 'provider': 'cpu',
               'path': str(ROOT / 'tests/evidence/fixtures/english.png')}
    output = tmp_path / 'new-capture'
    command = [str(executable), '--diagnostic-metal-capture', str(output),
               '--models', str(path / 'models'), '--config', str(path / 'config')]
    empty = subprocess.run(command, input=b'', env=env, capture_output=True, timeout=30)
    assert empty.returncode == 2 and b'exactly one framed request' in empty.stderr
    assert not output.exists()
    extra = subprocess.run(command, input=frame(request) * 2, env=env, capture_output=True, timeout=30)
    assert extra.returncode == 2
    length = int.from_bytes(extra.stdout[:4], 'little')
    response = json.loads(extra.stdout[4:4 + length])
    assert response['ok'] is False and 'exactly one framed request' in response['error']
    assert response['timing_eligible'] is False
    assert not output.exists()
    missing_provider = {key: value for key, value in request.items() if key != 'provider'}
    explicit = subprocess.run(command, input=frame(missing_provider), env=env,
                              capture_output=True, timeout=30)
    assert explicit.returncode == 2
    length = int.from_bytes(explicit.stdout[:4], 'little')
    response = json.loads(explicit.stdout[4:4 + length])
    assert response['ok'] is False and 'explicit cpu or coreml provider' in response['error']
    assert not output.exists()
    output.mkdir()
    sentinel = output / 'keep.txt'
    sentinel.write_text('never overwrite')
    existing = subprocess.run(command, input=frame(request), env=env, capture_output=True, timeout=30)
    assert existing.returncode == 2 and b'existing output' in existing.stderr
    assert sentinel.read_text() == 'never overwrite'
    if sys.platform != 'darwin':
        new_output = tmp_path / 'nonmac-capture'
        command[2] = str(new_output)
        unavailable = subprocess.run(command, input=frame(request), env=env,
                                     capture_output=True, timeout=30)
        assert unavailable.returncode == 2
        length = int.from_bytes(unavailable.stdout[:4], 'little')
        response = json.loads(unavailable.stdout[4:4 + length])
        assert response['ok'] is False and 'Metal capture is unavailable' in response['error']
        assert response['timing_eligible'] is False
        assert not new_output.exists()
