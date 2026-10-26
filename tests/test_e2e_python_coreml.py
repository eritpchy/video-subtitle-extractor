"""Synthetic subprocess/record contracts, never real source inference evidence."""
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import e2e_python_coreml as smoke


@pytest.fixture
def simulated(tmp_path, monkeypatch):
    models = tmp_path / 'models'; models.mkdir()
    state = {'calls': [], 'fail_at': None, 'missing': False, 'mutation': False}
    monkeypatch.setattr(smoke, 'generate_video', lambda path: path.write_bytes(b'identical video'))
    monkeypatch.setattr(smoke, 'check_srt', lambda path: {
        'recognized': ['HELLO WORLD', 'SECOND LINE'], 'subtitle_count': 2,
        'intervals_ms': [[1000, 3000], [4000, 6000]]})
    def validate(documents, *, provider):
        assert all(row['provider'] == provider for row in documents)
        return set(documents[0]['executed'])
    monkeypatch.setitem(sys.modules, 'backend.tools.python_coreml_ocr',
                        SimpleNamespace(validate_python_coreml_records=validate))
    def process(command, **kwargs):
        state['calls'].append((command, kwargs))
        if len(state['calls']) == state['fail_at']:
            return SimpleNamespace(returncode=9)
        mode = command[command.index('--mode') + 1]
        provider = 'cpu' if '--cpu' in command else 'coreml'
        Path(command[command.index('--output') + 1]).write_bytes(b'same actual SRT')
        if state['mutation']:
            Path(kwargs['env']['VSE_CONFIG_FILE']).write_text('{}')
        tier = 'medium' if mode == 'accurate' else 'small'
        if not state['missing']:
            directory = Path(kwargs['env']['VSE_PYTHON_COREML_EVIDENCE_DIR'])
            record = {'process_pid': 123, 'engine_id': 1, 'request_id': 1,
                      'provider': provider, 'executed': [f'PP-OCRv6_{tier}_det', f'PP-OCRv6_{tier}_rec']}
            (directory / 'python-ocr-123-1-1.json').write_text(json.dumps(record))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(smoke.subprocess, 'run', process)
    return models, state


def test_same_video_real_cli_commands_cover_both_providers_all_modes(simulated, tmp_path):
    models, state = simulated
    report = smoke.run(tmp_path / 'evidence', models, ('cpu', 'coreml'))
    assert report['result'] == 'passed' and len(report['cases']) == len(state['calls']) == 6
    assert [(row['mode'], row['provider']) for row in report['cases']] == [
        (mode, provider) for mode in smoke.MODES for provider in ('cpu', 'coreml')]
    assert len({row['input_video_sha256'] for row in report['cases']}) == 1
    assert all(row['elapsed_ns'] > 0 and row['python_requests'] for row in report['cases'])
    assert all(not row[key] for row in report['cases'] for key in ('timing_is_benchmark', 'gpu_execution_verified'))
    assert report['final_package_accepted'] is False
    for command, kwargs in state['calls']:
        assert command[0] == sys.executable and command[1:3] == ['-m', 'backend.cli']
        assert kwargs['env']['VSE_OCR_BACKEND'] == 'python-coreml'
        assert kwargs['env']['ORT_DISABLE_TELEMETRY'] == '1'
        assert kwargs['cwd'] == smoke.ROOT


@pytest.mark.parametrize('change', ('fail_at', 'missing', 'mutation', 'timeout'))
def test_failed_cli_missing_records_mutation_or_timeout_never_reuses_success(simulated, tmp_path, monkeypatch, change):
    models, state = simulated
    if change == 'timeout':
        def fail(*args, **kwargs): raise subprocess.TimeoutExpired(args[0], 1)
        monkeypatch.setattr(smoke.subprocess, 'run', fail)
    else:
        state[change] = 1 if change == 'fail_at' else True
    output = tmp_path / 'evidence'
    report = smoke.run(output, models, ('cpu',))
    assert report['result'] == 'failed' and report['cases'] == []
    assert json.loads((output / 'report.json').read_text())['result'] == 'failed'
    with pytest.raises(FileExistsError): smoke.run(output, models, ('cpu',))


def test_clean_selection_cannot_inherit_native_or_loader_or_capture(tmp_path, monkeypatch):
    for name in ('VSE_OCR_BACKEND', 'VSE_NATIVE_OCR_PROVIDER', 'VSE_NATIVE_EVIDENCE_DIR',
                 'PYTHONPATH', 'PYTHONHOME', 'DYLD_LIBRARY_PATH', 'DYLD_INSERT_LIBRARIES',
                 'LD_PRELOAD', 'LD_LIBRARY_PATH', 'MTL_CAPTURE_ENABLED'):
        monkeypatch.setenv(name, 'contaminated')
    values = smoke.environment(tmp_path, tmp_path / 'config', tmp_path / 'records')
    assert values['VSE_OCR_BACKEND'] == 'python-coreml'
    for name in ('VSE_NATIVE_OCR_PROVIDER', 'VSE_NATIVE_EVIDENCE_DIR', 'PYTHONPATH',
                 'PYTHONHOME', 'DYLD_LIBRARY_PATH', 'DYLD_INSERT_LIBRARIES',
                 'LD_PRELOAD', 'LD_LIBRARY_PATH', 'MTL_CAPTURE_ENABLED'):
        assert name not in values


@pytest.mark.parametrize('change', ('name', 'identity', 'duplicate', 'non-json', 'symlink', 'wrong-models'))
def test_request_collection_fails_closed(simulated, tmp_path, change):
    models, _ = simulated
    records = tmp_path / 'records'; records.mkdir()
    path = records / 'python-ocr-123-1-1.json'
    document = {'process_pid': 123, 'engine_id': 1, 'request_id': 1, 'provider': 'coreml',
                'executed': ['PP-OCRv6_small_det', 'PP-OCRv6_small_rec']}
    if change == 'identity': document['engine_id'] = 2
    if change == 'wrong-models': document['executed'] = ['PP-OCRv6_small_det']
    path.write_text(json.dumps(document))
    if change == 'name': path.rename(records / 'python-ocr-123-1-2.json')
    if change == 'non-json': (records / 'runtime.so').write_bytes(b'no')
    if change == 'duplicate': path.write_text('{"process_pid":123,"process_pid":124}')
    if change == 'symlink':
        real = tmp_path / 'real'; path.rename(real)
        try: path.symlink_to(real)
        except OSError: pytest.skip('symlink creation unavailable')
    with pytest.raises(ValueError): smoke.collect_records(records, provider='coreml', mode='fast')


def test_source_records_cannot_be_written_over_models(simulated):
    models, _ = simulated
    with pytest.raises(ValueError, match='must not modify'):
        smoke.run(models / 'evidence', models, ('cpu',))


def test_full_srt_difference_between_providers_is_not_hidden_by_expected_phrases(simulated, tmp_path, monkeypatch):
    models, state = simulated
    original = smoke.run_case
    def mismatch(*args, **kwargs):
        row = original(*args, **kwargs)
        if kwargs['provider'] == 'coreml': row['srt_sha256'] = 'f' * 64
        return row
    monkeypatch.setattr(smoke, 'run_case', mismatch)
    report = smoke.run(tmp_path / 'evidence', models, ('cpu', 'coreml'))
    assert report['result'] == 'failed' and 'subtitle text/timing differs' in report['error']
    assert len(state['calls']) == 2
