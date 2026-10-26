"""Synthetic receipt validation; these tests do not execute CoreML or a GPU."""
import copy
import json
from pathlib import Path

import pytest

from scripts import check_native_models as checker
from scripts import check_paddle_models as frozen
from tests.native_shape_evidence import shape_aggregate

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'native/ocr/tests/evidence'


def report_fixture(providers=('cpu', 'coreml')):
    cases = frozen.load_cases(FIXTURES)
    raw = (FIXTURES.parent / 'goldens/smoke.json').read_bytes().decode('utf-8')
    pins = (ROOT / 'native/ocr/model-source-pins.json').read_bytes().decode('utf-8')
    models = {row['name']: row['onnx_sha256'] for row in json.loads(pins)['models']}
    goldens = {(r['tier'], r['operation'], r.get('rec_model'), r['fixture']): r['expected'] for r in json.loads(raw)['cases']}
    report = {'schema': 1, 'result': 'passed', 'ocr_backend': 'native', 'model_count': 15,
              'modes': list(frozen.MODES), 'providers_requested': list(providers), 'providers': {},
              'confidence_tolerance': frozen.TOLERANCE, 'case_proof_sha256': frozen.CASE_PROOF_SHA256,
              'scope': 'final-bundle', 'bundled_python_verified': True,
              'commit': 'a' * 40, 'version': '2.3.0', 'build_profile': 'native-coreml',
              'gpu_inference_verified': False, 'gpu_validation': 'not-verified-ep-profile-is-not-device-trace',
              'models_unchanged': True, 'production_modules_unchanged': True, 'network_attempts': [],
              'source_pins_utf8': pins, 'native_golden_utf8': raw, 'worker_sha256': 'b' * 64, 'runtime_sha256': 'd' * 64}
    report['model_files'] = {'model-source-pins.json': frozen.SOURCE_PINS_SHA256,
        'model-manifest.json': 'e' * 64, **json.loads(pins)['config_sha256'],
        **{f'models/{name}_infer.onnx': digest for name, digest in models.items()}}
    report['production_modules'] = {name: 'f' * 64 for name in checker.PRODUCTION_FILES + checker.VERIFICATION_FILES}
    window = {'worker_pid': 7, 'utc_start_ns': 10, 'utc_end_ns': 20,
              'monotonic_start_ns': 100, 'monotonic_end_ns': 110}
    for provider in providers:
        window['worker_pid'] = 7 if provider == 'cpu' else 8
        coreml = provider == 'coreml'
        actual_provider = 'CoreMLExecutionProvider' if coreml else 'CPUExecutionProvider'
        rows = []
        for case in cases:
            row = copy.deepcopy(case)
            row['fixture_encoded_png_sha256'] = 'c' * 64
            row['fixture_encoded_png_bytes'] = 123
            row['actual'] = copy.deepcopy(case['expected'])
            row.update(frozen.compare_ocr(row['actual'], row['expected']) if case['operation'] == 'ocr' else {'same_boxes': True, 'passed': True})
            # Independent synthetic processes, never cached execution proof.
            window['worker_pid'] = (7 if provider == 'cpu' else 8) + case['id'] * 2
            request = {'id': 1, 'provider': provider, 'tier': case['tier'], 'operation': case['operation'],
                       'rec_model': case['rec_model'] or f"PP-OCRv6_{case['tier']}_rec", 'batch_size': 6, 'cpu_threads': 2,
                       'image_sha256': 'c' * 64, 'image_bytes': 123, 'image_format': 'png'}
            reference = goldens[(case['tier'], case['operation'], case['rec_model'] if case['rec_model'] in frozen.LEGACY else None, case['fixture'])]
            response = {'id': request['id'], 'ok': True, 'requested_provider': provider, 'actual_provider': actual_provider,
                        'execution_window': copy.deepcopy(window),
                        **{key: request[key] for key in ('rec_model', 'batch_size', 'cpu_threads')}}
            response['result' if case['operation'] == 'ocr' else 'dt_polys'] = copy.deepcopy(reference)
            def session(name, executed=True):
                values = {'model': name, 'status': 'executed' if executed else 'not_run',
                          'source': 'ort_first_run_profile' if coreml else 'explicit_cpu_session',
                          'coreml_ep_executed': coreml and executed, 'cpu_ep_executed': executed,
                          'coreml_kernel_events': 1 if coreml and executed else 0,
                          'cpu_kernel_events': 1 if coreml and executed else 0,
                          'observed_providers': (['CoreMLExecutionProvider', 'CPUExecutionProvider'] if coreml else ['CPUExecutionProvider']) if executed else [],
                          'gpu_execution_verified': False,
                          'capture': 'first_successful_run' if coreml else 'explicit_cpu_session',
                          'request_run_count': int(executed), 'session_run_count': int(executed), 'evidence_reused': False}
                if coreml and executed:
                    values['profile_window'] = copy.deepcopy(window)
                if coreml:
                    values = shape_aggregate(values, 'det' if name.endswith('_det') else 'rec', window)
                return values
            sessions = {'det': session(case['det_model']),
                        'rec': None if case['operation'] == 'detect' else session(case['rec_model'], bool(reference[0]))}
            response['execution_evidence'] = {'schema_version': 1, 'requested_provider': provider, 'actual_provider': actual_provider,
                'compute_units': 'CPUAndGPU' if coreml else 'CPUOnly', 'ane_allowed': False,
                'gpu_execution_verified': False, 'coreml_ep_executed': coreml, 'cpu_fallback_observed': coreml,
                'sessions': sessions}
            row['model_sha256'] = {s['model']: models[s['model']] for s in sessions.values() if s is not None}
            row['executed_models'] = sorted(s['model'] for s in sessions.values() if s is not None and s['status'] == 'executed')
            row['request_evidence'] = {'schema_version': 1, 'worker_sha256': report['worker_sha256'], 'worker_pid': window['worker_pid'],
                                      'request': request, 'response': response}
            rows.append(row)
        report['providers'][provider] = {'result': 'passed', 'cases': rows, 'covered_models': sorted(frozen.ALL_MODELS)}
    return report


def test_both_providers_and_all_modes_have_real_distinct_cases():
    report = report_fixture()
    checker.validate_report(report, {key: report[key] for key in ('commit', 'version', 'build_profile')})
    assert [len(group['cases']) for group in report['providers'].values()] == [168, 168]
    assert report['gpu_inference_verified'] is False
    for provider in checker.PROVIDERS:
        blank = next(row for row in report['providers'][provider]['cases'] if row['fixture'] == 'fixtures/blank.png' and row['operation'] == 'ocr')
        assert blank['rec_model'] not in blank['executed_models']


@pytest.mark.parametrize('change', ['gpu', 'missing-provider', 'wrong-sha', 'wrong-scope', 'missing-case',
    'expectation', 'input', 'model', 'worker', 'raw-result', 'cpu-fallback', 'pid', 'reuse', 'capture', 'unrun'])
def test_native_receipt_cannot_fake_execution_or_coverage(change):
    report = report_fixture()
    row = report['providers']['coreml']['cases'][0]
    document = row['request_evidence']; response = document['response']
    if change == 'gpu': report['gpu_inference_verified'] = True
    elif change == 'missing-provider': report['providers'].pop('coreml')
    elif change == 'wrong-sha': report['source_pins_utf8'] += ' '
    elif change == 'wrong-scope': report['scope'] = 'source'
    elif change == 'missing-case': report['providers']['coreml']['cases'].pop()
    elif change == 'expectation': row['expected'] = row['actual'] = [[], []]
    elif change == 'input': document['request']['image_sha256'] = ''
    elif change == 'model': row['model_sha256'][row['det_model']] = 'd' * 64
    elif change == 'worker': document['worker_sha256'] = 'd' * 64
    elif change == 'raw-result': response['result'] = [[], []]
    elif change == 'cpu-fallback': response['actual_provider'] = 'CPUExecutionProvider'
    elif change == 'pid': document['worker_pid'] = 999
    elif change == 'reuse': response['execution_evidence']['sessions']['det']['evidence_reused'] = True
    elif change == 'capture': response['execution_evidence']['sessions']['det'].pop('capture')
    else: row['executed_models'] = []
    with pytest.raises(ValueError): checker.validate_report(report)


def test_cpu_only_source_evidence_does_not_satisfy_both_final_providers():
    report = report_fixture(('cpu',))
    checker.validate_report(report, required_providers=('cpu',))
    with pytest.raises(ValueError, match='provider'):
        checker.validate_report(report)


def test_full_request_replay_is_not_a_second_auto_execution():
    report = report_fixture()
    rows = report['providers']['coreml']['cases']
    fast = rows[0]
    auto = next(row for row in rows if row['mode'] == 'auto' and row['fixture'] == fast['fixture'] and row['operation'] == fast['operation'])
    auto['request_evidence'] = copy.deepcopy(fast['request_evidence'])
    with pytest.raises(ValueError, match='replayed'):
        checker.validate_report(report)


def test_different_valid_input_sha_cannot_replace_the_independent_fixture_encoding():
    report = report_fixture()
    report['providers']['coreml']['cases'][0]['request_evidence']['request']['image_sha256'] = 'd' * 64
    with pytest.raises(ValueError, match='independently encoded'):
        checker.validate_report(report)


@pytest.mark.parametrize('field', ['model_files', 'production_modules', 'runtime_sha256'])
def test_missing_or_self_authored_module_model_identity_cannot_pass(field):
    report = report_fixture(); report.pop(field)
    with pytest.raises(ValueError): checker.validate_report(report)
    report = report_fixture(); report[field] = {} if field != 'runtime_sha256' else 'wrong'
    with pytest.raises(ValueError): checker.validate_report(report)


def test_complete_model_receipt_rejects_same_shape_id_relabelled_across_cases():
    from backend.tools.native_ocr import validate_execution_evidence
    report = report_fixture()
    first, second = report['providers']['coreml']['cases'][:2]
    original = first['request_evidence']
    record = second['request_evidence']
    record['worker_pid'] = original['worker_pid']
    record['request']['id'] = record['response']['id'] = 2
    window = record['response']['execution_window']
    window['worker_pid'] = original['worker_pid']
    for field in ('utc_start_ns', 'utc_end_ns', 'monotonic_start_ns', 'monotonic_end_ns'):
        window[field] += 1000
    for role, session in record['response']['execution_evidence']['sessions'].items():
        session.update(session_run_count=2, evidence_reused=True)
        shape = session['shape_sessions'][0]
        shape.update(session_run_count=2, evidence_reused=True)
        shape['profile_window'] = copy.deepcopy(original['response']['execution_window'])
        if role == 'rec':
            shape['input_shape'][3] = 640
            shape['free_dimension_overrides']['DynamicDimension.1'] = 640
    validate_execution_evidence(record['response'], record['request'], worker_pid=record['worker_pid'])
    with pytest.raises(ValueError, match='shape/profile identity'):
        checker.validate_report(report)
