"""Quick CoreML phases are genuine execution gates, not full/GPU acceptance."""
import copy
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
