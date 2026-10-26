"""Quick CoreML phases are genuine execution gates, not full/GPU acceptance."""
import copy
import json
from pathlib import Path

import pytest
import ci_coreml_compat as compat


def test_fixed_phases_cover_all_fifteen_models_without_blank_rec_claims():
    cases = compat.gate.frozen.load_cases(compat.ROOT / 'native/ocr/tests/evidence')
    early = compat.select_cases(cases, compat.EARLY_IDS)
    extra = compat.select_cases(cases, compat.COMPAT_IDS)
    assert len(early) == 4 and len(extra) == 11
    assert {(row['tier'], row['fixture']) for row in early} == {
        (tier, 'fixtures/' + language + '.png')
        for tier in ('small', 'medium') for language in ('english', 'chinese')}
    assert len(set(compat.EARLY_IDS + compat.COMPAT_IDS)) == 15
    assert all(row['operation'] == 'ocr' and 'blank' not in row['fixture'] for row in early + extra)
    assert {row[key] for row in early + extra for key in ('det_model', 'rec_model')} == compat.gate.frozen.ALL_MODELS


def test_missing_or_duplicate_frozen_case_fails():
    with pytest.raises(ValueError, match='Missing/duplicated'):
        compat.select_cases([{'id': 0}, {'id': 0}], [0])
    with pytest.raises(ValueError, match='Missing/duplicated'):
        compat.select_cases([{'id': 0}], [1])


def test_failed_phase_cannot_start_a_success_claim(monkeypatch):
    monkeypatch.setattr(compat.gate, '_request_evidence', lambda *args: pytest.fail('Failed rows must stop before evidence acceptance'))
    with pytest.raises(ValueError, match='did not all execute'):
        compat.validate_phase([{'id': 0, 'passed': False}], [0], 'sha', {}, {}, set())


def test_coverage_comes_from_validated_executed_responses_not_planned_models(monkeypatch):
    rows = [{'id': 0, 'passed': True, 'rec_model': 'planned-rec', 'request_evidence': {'unique': 1}}]
    monkeypatch.setattr(compat.gate, '_request_evidence', lambda *args: {'executed-det'})
    monkeypatch.setattr(compat.gate, 'request_identity', lambda doc: doc['unique'])
    seen = set()
    assert compat.validate_phase(rows, [0], 'sha', {}, {}, seen) == {'executed-det'}
    with pytest.raises(ValueError, match='Replayed'):
        compat.validate_phase(rows, [0], 'sha', {}, {}, seen)


def test_setup_failure_is_preserved_without_gpu_or_final_bundle_claim(tmp_path, monkeypatch):
    monkeypatch.delenv('VSE_NATIVE_OCR_ROOT', raising=False)
    report = compat.run(tmp_path / 'results')
    assert report['result'] == 'failed'
    assert report['early'] == report['compatibility'] == []
    for key in ('gpu_inference_verified', 'internal_coreml_precision_verified', 'timing_is_benchmark', 'final_package_accepted'):
        assert report[key] is False
    assert (tmp_path / 'results/report.json').is_file()
    with pytest.raises(FileExistsError):
        compat.run(tmp_path / 'results')


def test_evidence_cannot_be_written_inside_model_inputs(tmp_path, monkeypatch):
    models = tmp_path / 'models'
    models.mkdir()
    monkeypatch.setenv('VSE_NATIVE_OCR_ROOT', str(models))
    with pytest.raises(ValueError, match='must not modify'):
        compat.run(models / 'evidence')
    assert list(models.iterdir()) == []


def test_independent_report_gate_rejects_the_observed_four_failed_case_result(tmp_path):
    report = tmp_path / 'failed.json'
    report.write_text(json.dumps({'schema': 1, 'scope': 'source-early-coreml-compatibility-only',
                                 'result': 'failed', 'early': [{'id': index, 'passed': False} for index in compat.EARLY_IDS],
                                 'compatibility': []}), encoding='utf-8')
    with pytest.raises(ValueError, match='did not pass: result'):
        compat.verify_report(report, tmp_path, 'a' * 40)


@pytest.fixture
def successful_report(tmp_path, monkeypatch):
    native = tmp_path / 'native'
    native.mkdir()
    (native / 'native-ocr').write_bytes(b'worker')
    (native / 'libonnxruntime.1.dylib').write_bytes(b'runtime')
    assets = {'files': {'fixed-test-file': 'b' * 64},
              'models': {name: {'sha256': 'c' * 64} for name in compat.gate.frozen.ALL_MODELS}}
    monkeypatch.setattr(compat.gate, 'verify_native_model_assets', lambda _: copy.deepcopy(assets))
    monkeypatch.setattr(compat.gate, '_request_evidence', lambda row, *args: set(row['executed_models']))
    monkeypatch.setattr(compat.gate, 'request_identity', lambda doc: doc['unique'])
    cases = compat.gate.frozen.load_cases(compat.ROOT / 'native/ocr/tests/evidence')
    report = {'schema': 1, 'scope': 'source-early-coreml-compatibility-only', 'result': 'passed',
              'consumer_commit': 'a' * 40, 'requested_provider': 'coreml', 'network_attempts': [],
              'gpu_inference_verified': False, 'internal_coreml_precision_verified': False,
              'timing_is_benchmark': False, 'final_package_accepted': False, 'production_modules_unchanged': True,
              'model_files': assets['files'],
              'worker_sha256': compat.gate.frozen.sha256(native / 'native-ocr'),
              'runtime_sha256': compat.gate.frozen.sha256(native / 'libonnxruntime.1.dylib'),
              'production_modules': {str((compat.ROOT / name).resolve()): compat.gate.frozen.sha256(compat.ROOT / name)
                                     for name in compat.gate.PRODUCTION_FILES},
              'covered_models': sorted(compat.gate.frozen.ALL_MODELS)}
    for phase, ids in (('early', compat.EARLY_IDS), ('compatibility', compat.COMPAT_IDS)):
        report[phase] = [{**row, 'passed': True, 'executed_models': [row['det_model'], row['rec_model']],
                          'request_evidence': {'unique': row['id']}}
                         for row in compat.select_cases(cases, ids)]
    return tmp_path / 'report.json', native, report


def test_independent_report_gate_rechecks_actual_coverage_and_fixed_identity(successful_report):
    path, native, report = successful_report
    path.write_text(json.dumps(report), encoding='utf-8')
    assert compat.verify_report(path, native, 'a' * 40)['result'] == 'passed'
    report['compatibility'][0]['executed_models'] = []
    path.write_text(json.dumps(report), encoding='utf-8')
    with pytest.raises(ValueError, match='coverage is incomplete'):
        compat.verify_report(path, native, 'a' * 40)


@pytest.mark.parametrize('change', ('consumer', 'worker', 'modules', 'models', 'failed', 'missing', 'expected', 'gpu'))
def test_independent_report_gate_refuses_false_success(successful_report, change):
    path, native, report = successful_report
    if change == 'consumer': report['consumer_commit'] = 'd' * 40
    elif change == 'worker': (native / 'native-ocr').write_bytes(b'changed')
    elif change == 'modules': report['production_modules'] = {}
    elif change == 'models': report['model_files'] = {}
    elif change == 'failed': report['early'][0]['passed'] = False
    elif change == 'missing': report['compatibility'].pop()
    elif change == 'expected': report['early'][0]['expected'] = []
    elif change == 'gpu': report['gpu_inference_verified'] = True
    path.write_text(json.dumps(report), encoding='utf-8')
    with pytest.raises(ValueError):
        compat.verify_report(path, native, 'a' * 40)
