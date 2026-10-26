"""Offline synthetic evidence contracts; no real video, CoreML or GPU proof.

The mocked CLI writes raw records through the actual producer/consumer paths.
Do not use these fixtures as acceptance artifacts for a released executable.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from scripts import accept_release_archive as acceptance
from scripts import e2e_smoke as smoke


WORKER_SHA256 = 'a' * 64


def raw_record(mode='fast', *, provider='coreml', request_id=1, worker_pid=123):
    tier = 'medium' if mode == 'accurate' else 'small'
    coreml = provider == 'coreml'
    actual = 'CoreMLExecutionProvider' if coreml else 'CPUExecutionProvider'
    window = {'worker_pid': worker_pid, 'utc_start_ns': 100, 'utc_end_ns': 200,
              'monotonic_start_ns': 1000, 'monotonic_end_ns': 1100}
    request = {'id': request_id, 'provider': provider, 'tier': tier, 'operation': 'ocr',
               'rec_model': f'PP-OCRv6_{tier}_rec', 'batch_size': 6, 'cpu_threads': 2,
               'timeout_ms': 120000, 'image_sha256': 'b' * 64, 'image_bytes': 100, 'image_format': 'png'}
    def session(role):
        result = {'model': f'PP-OCRv6_{tier}_{role}', 'status': 'executed',
                  'source': 'ort_first_run_profile' if coreml else 'explicit_cpu_session',
                  'capture': 'first_successful_run' if coreml else 'explicit_cpu_session',
                  'request_run_count': 1, 'session_run_count': 1, 'evidence_reused': False,
                  'coreml_ep_executed': coreml, 'cpu_ep_executed': True,
                  'coreml_kernel_events': 3 if coreml else 0, 'cpu_kernel_events': 1 if coreml else 0,
                  'observed_providers': ['CPUExecutionProvider'] + (['CoreMLExecutionProvider'] if coreml else []),
                  'gpu_execution_verified': False}
        if coreml:
            result['profile_window'] = deepcopy(window)
        return result
    evidence = {'schema_version': 1, 'requested_provider': provider, 'actual_provider': actual,
                'compute_units': 'CPUAndGPU' if coreml else 'CPUOnly', 'ane_allowed': False,
                'gpu_execution_verified': False, 'coreml_ep_executed': coreml,
                'cpu_fallback_observed': coreml, 'sessions': {role: session(role) for role in ('det', 'rec')}}
    response = {'id': request_id, 'ok': True, 'requested_provider': provider, 'actual_provider': actual,
                **{key: request[key] for key in ('rec_model', 'batch_size', 'cpu_threads')},
                'result': [[[[0, 0], [2, 0], [2, 2], [0, 2]]], [['synthetic', .9]]],
                'execution_evidence': evidence, 'execution_window': window}
    return {'schema_version': 1, 'request': request, 'response': response,
            'worker_pid': worker_pid, 'worker_sha256': WORKER_SHA256}


def write_json(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding='utf-8')


def file_record(path):
    data = path.read_bytes()
    return {'path': path.name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


@pytest.fixture
def simulated_cli(monkeypatch):
    monkeypatch.setenv('VSE_OCR_BACKEND', 'native')
    monkeypatch.delenv('VSE_NATIVE_EVIDENCE_DIR', raising=False)
    state = {'calls': [], 'behavior': 'ok'}
    monkeypatch.setattr(smoke, 'generate_video', lambda path: path.write_bytes(b'synthetic video'))
    def check_srt(path):
        assert path.read_text() == 'synthetic fresh srt'
        return {'recognized': ['HELLO WORLD', 'SECOND LINE'], 'subtitle_count': 2}
    monkeypatch.setattr(smoke, 'check_srt', check_srt)
    def run(command, **kwargs):
        state['calls'].append((list(command), kwargs))
        mode = command[command.index('--mode') + 1]
        Path(command[command.index('--output') + 1]).write_text('synthetic fresh srt')
        directory = kwargs['env'].get('VSE_NATIVE_EVIDENCE_DIR')
        if directory and state['behavior'] != 'missing':
            document = raw_record('accurate' if state['behavior'] == 'wrong_mode' else mode,
                                  provider='cpu' if state['behavior'] == 'cpu' else 'coreml')
            if state['behavior'] == 'fake_gpu':
                document['response']['execution_evidence']['gpu_execution_verified'] = True
            if state['behavior'] == 'cpu_response':
                document['response']['actual_provider'] = 'CPUExecutionProvider'
            write_json(Path(directory) / 'native-ocr-123-1.json', document)
            if state['behavior'] == 'duplicate':
                write_json(Path(directory) / 'native-ocr-123-2.json', document)
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(smoke.subprocess, 'run', run)
    return state


def test_default_cpu_still_forces_cpu_and_does_not_inherit_evidence_directory(simulated_cli, monkeypatch, tmp_path):
    monkeypatch.setenv('VSE_NATIVE_EVIDENCE_DIR', str(tmp_path / 'must not write'))
    monkeypatch.setenv('VSE_NATIVE_OCR_PROVIDER', 'coreml')
    report = smoke.main(['--output', str(tmp_path / 'default')])
    command, kwargs = simulated_cli['calls'][0]
    assert '--cpu' in command and report['requested_provider'] == 'cpu' and report['cpu_forced'] is True
    assert report['mode'] == 'fast' and report['gpu_inference_verified'] is False
    assert 'VSE_NATIVE_EVIDENCE_DIR' not in kwargs['env']
    assert not (tmp_path / 'must not write').exists() and 'native_requests' not in report


def test_coreml_all_modes_require_native_requests_without_cpu_switch(simulated_cli, tmp_path):
    report = smoke.main(['--provider', 'coreml', '--all-modes', '--output', str(tmp_path / 'matrix')])
    assert report['result'] == 'passed' and report['cpu_forced'] is False
    assert report['gpu_inference_verified'] is False
    assert [case['mode'] for case in report['cases']] == list(smoke.MODES)
    for (command, kwargs), case in zip(simulated_cli['calls'], report['cases']):
        env = kwargs['env']
        assert '--cpu' not in command and command[command.index('--mode') + 1] == case['mode']
        assert env['VSE_OCR_BACKEND'] == 'native' and env['VSE_NATIVE_OCR_PROVIDER'] == 'coreml'
        assert env['ORT_DISABLE_TELEMETRY'] == '1' and env['VSE_OFFLINE'] == '1'
        directory = Path(env['VSE_NATIVE_EVIDENCE_DIR'])
        assert directory == (tmp_path / 'matrix' / case['mode'] / 'native-requests').resolve()
        assert case['native_requests'] == [file_record(directory / 'native-ocr-123-1.json')]
        assert case['gpu_inference_verified'] is False
        preferences = json.loads(Path(env['VSE_CONFIG_FILE']).read_text())
        assert preferences['Main']['Language'] == 'korean' and preferences['Main']['Mode'] != case['mode']


@pytest.mark.parametrize('backend', ['paddle', 'directml', ''])
def test_coreml_non_native_backend_is_rejected_before_launch(simulated_cli, monkeypatch, tmp_path, backend):
    monkeypatch.setenv('VSE_OCR_BACKEND', backend)
    with pytest.raises(ValueError, match='production native'):
        smoke.run_mode(tmp_path / 'bad', 'fast', 10, provider='coreml')
    assert not simulated_cli['calls']


@pytest.mark.parametrize('behavior', ['missing', 'cpu', 'cpu_response', 'duplicate', 'wrong_mode', 'fake_gpu'])
def test_successful_cli_exit_cannot_replace_actual_valid_ep_records(simulated_cli, tmp_path, behavior):
    simulated_cli['behavior'] = behavior
    output = tmp_path / behavior
    with pytest.raises(ValueError): smoke.run_mode(output, 'fast', 10, provider='coreml')
    assert not (output / 'report.json').exists()


def test_old_native_evidence_directory_is_never_reused_or_deleted(simulated_cli, tmp_path):
    output = tmp_path / 'old'
    smoke.run_mode(output, 'fast', 10, provider='coreml')
    original = (output / 'native-requests/native-ocr-123-1.json').read_bytes()
    with pytest.raises(FileExistsError): smoke.run_mode(output, 'fast', 10, provider='coreml')
    assert (output / 'native-requests/native-ocr-123-1.json').read_bytes() == original
    assert not (output / 'report.json').exists() and len(simulated_cli['calls']) == 1


def test_missing_raw_evidence_directory_is_not_a_success(tmp_path):
    with pytest.raises(FileNotFoundError):
        smoke.collect_coreml_video_evidence(tmp_path / 'missing', {'mode': 'fast'})


@pytest.fixture
def archive_evidence(tmp_path):
    relative = 'test-results/candidate'
    paths = {}
    for mode in smoke.MODES:
        directory = tmp_path / relative / f'bundle-coreml-e2e-{mode}'
        record = directory / 'native-requests/native-ocr-123-1.json'
        write_json(record, raw_record(mode))
        report = {'result': 'passed', 'mode': mode, 'requested_provider': 'coreml', 'cpu_forced': False,
                  'gpu_inference_verified': False, 'command': ['python', '-m', 'backend.cli', 'input.mp4', '--mode', mode]}
        smoke.collect_coreml_video_evidence(record.parent, report)
        report_path = directory / 'report.json'; write_json(report_path, report)
        paths[mode] = (report_path, record)
    return tmp_path, relative, paths


def accept_fixture(fixture):
    root, relative, _ = fixture
    evidence = acceptance.Evidence()
    acceptance._coreml_video_evidence(evidence, root, relative, 'package', worker_sha256=WORKER_SHA256)
    evidence.assert_unchanged()
    return evidence


def mutate_report(fixture, transform, mode='fast'):
    path, _ = fixture[2][mode]
    document = json.loads(path.read_text()); transform(document); write_json(path, document)


def mutate_record(fixture, transform, mode='fast', *, refresh_index=True):
    _, path = fixture[2][mode]
    document = json.loads(path.read_text()); transform(document); write_json(path, document)
    if refresh_index:
        mutate_report(fixture, lambda row: row.update(native_requests=[file_record(path)]), mode)


def test_archive_acceptance_consumes_all_three_modes_and_exact_original_hashes(archive_evidence):
    evidence = accept_fixture(archive_evidence)
    assert len(evidence.index) == 6
    root, relative, paths = archive_evidence
    for mode, (_, record) in paths.items():
        assert evidence.index[f'package/{relative}/bundle-coreml-e2e-{mode}/native-requests/{record.name}'] == {
            key: value for key, value in file_record(record).items() if key != 'path'}


@pytest.mark.parametrize('change', [
    lambda report: report.update(native_requests=[]),
    lambda report: report['native_requests'].append(deepcopy(report['native_requests'][0])),
    lambda report: report.update(mode='accurate'),
    lambda report: report['command'].append('--cpu'),
    lambda report: report['command'].append('--cpu=true'),
    lambda report: report['command'].extend(['--mode', 'accurate']),
    lambda report: report.update(command=['python', '--mode']),
    lambda report: report.update(command=['python', '--mode', 'accurate']),
    lambda report: report.update(cpu_forced=True),
    lambda report: report.update(requested_provider='cpu'),
    lambda report: report.update(gpu_inference_verified=True),
    lambda report: report.update(executed_models=[]),
    lambda report: report['native_requests'][0].update(path='../outside.json'),
    lambda report: report['native_requests'][0].update(sha256='c' * 64),
    lambda report: report['native_requests'][0].update(bytes=1),
])
def test_archive_rejects_incomplete_cpu_mislabeled_or_changed_producer_report(archive_evidence, change):
    mutate_report(archive_evidence, change)
    with pytest.raises(RuntimeError): accept_fixture(archive_evidence)


@pytest.mark.parametrize('change', [
    lambda record: record.update(worker_sha256='c' * 64),
    lambda record: record.update(raw_record(provider='cpu')),
    lambda record: record.update(raw_record('accurate')),
    lambda record: record['response'].update(actual_provider='CPUExecutionProvider'),
    lambda record: record['response']['execution_window'].update(worker_pid=999),
])
def test_archive_rejects_wrong_executable_cpu_fallback_mode_or_pid_even_with_reindexed_bytes(archive_evidence, change):
    mutate_record(archive_evidence, change)
    with pytest.raises(RuntimeError): accept_fixture(archive_evidence)


def test_archive_rejects_missing_record_and_changed_bytes(archive_evidence):
    _, path = archive_evidence[2]['fast']
    original = path.read_bytes(); path.write_bytes(original + b'\n')
    with pytest.raises(RuntimeError, match='bytes differ'): accept_fixture(archive_evidence)
    path.unlink()
    with pytest.raises(RuntimeError, match='Missing'): accept_fixture(archive_evidence)


def test_archive_rejects_duplicate_requests_under_distinct_filenames(archive_evidence):
    _, first = archive_evidence[2]['fast']; second = first.with_name('native-ocr-123-2.json')
    second.write_bytes(first.read_bytes())
    mutate_report(archive_evidence, lambda report: report['native_requests'].append(file_record(second)))
    with pytest.raises(RuntimeError): accept_fixture(archive_evidence)


def test_producer_and_archive_reject_filename_not_bound_to_pid_and_request(archive_evidence):
    report_path, first = archive_evidence[2]['fast']
    renamed = first.with_name('native-ocr-999-2.json'); first.rename(renamed)
    report = json.loads(report_path.read_text())
    with pytest.raises(ValueError, match='filename'):
        smoke.collect_coreml_video_evidence(renamed.parent, report)
    mutate_report(archive_evidence, lambda row: row.update(native_requests=[file_record(renamed)]))
    with pytest.raises(RuntimeError, match='filename'): accept_fixture(archive_evidence)


def test_raw_record_symlink_is_not_accepted_by_producer_or_archive(archive_evidence):
    root, _, paths = archive_evidence
    report_path, record = paths['fast']
    target = root / 'outside-record.json'; target.write_bytes(record.read_bytes()); record.unlink()
    try:
        record.symlink_to(target)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f'Symlink unavailable: {error}')
    with pytest.raises(ValueError):
        smoke.collect_coreml_video_evidence(record.parent, json.loads(report_path.read_text()))
    with pytest.raises(RuntimeError, match='Symlink'): accept_fixture(archive_evidence)


def test_archive_detects_evidence_changed_after_validation(archive_evidence):
    evidence = accept_fixture(archive_evidence)
    _, path = archive_evidence[2]['auto']; path.write_bytes(path.read_bytes() + b'\n')
    with pytest.raises(RuntimeError, match='changed during acceptance'): evidence.assert_unchanged()


@pytest.mark.parametrize('change', [
    lambda record: record['request'].pop('image_sha256'),
    lambda record: record['request'].update(image_sha256='not-a-sha256'),
    lambda record: record['request'].update(image_bytes=True),
    lambda record: (record['request'].update(id=True), record['response'].update(id=True)),
    lambda record: [record['response']['execution_evidence']['sessions'][role].pop('profile_window')
                    for role in ('det', 'rec')],
    lambda record: [record['response']['execution_evidence']['sessions'][role].pop(key)
                    for role in ('det', 'rec') for key in ('capture', 'request_run_count', 'session_run_count', 'evidence_reused')],
], ids=['missing-png-sha', 'invalid-png-sha', 'boolean-image-size', 'boolean-request-id',
        'missing-first-profile-window', 'missing-capture-reuse-semantics'])
def test_video_proof_requires_bound_input_integer_id_and_complete_profile_capture_semantics(archive_evidence, change):
    # The video gate must not be weaker than the final all-model gate merely
    # because command-line switches and plausible EP names appear in a report.
    mutate_record(archive_evidence, change)
    with pytest.raises(RuntimeError): accept_fixture(archive_evidence)
