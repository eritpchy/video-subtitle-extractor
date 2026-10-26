"""Synthetic strict shape/session evidence contracts, not CoreML/GPU execution."""
from copy import deepcopy

import pytest

from backend.tools import native_ocr as adapter
from tests.native_shape_evidence import shape_aggregate
from tests.test_native_coreml_adapter import evidence_response


def child(response, role='rec'):
    return response['execution_evidence']['sessions'][role]['shape_sessions'][0]


def validate(request, response, state=None):
    return adapter.validate_execution_evidence(response, request, worker_pid=123, shape_state=state)


@pytest.mark.parametrize('field', ['shape_sessions', 'cache_capacity', 'cached_shape_session_count',
                                  'cached_shape_session_ids', 'shape_cache_scope', 'capture',
                                  'request_run_count', 'session_run_count', 'evidence_reused'])
def test_legacy_or_incomplete_aggregate_is_not_current_shape_evidence(field):
    request, response = evidence_response()
    response['execution_evidence']['sessions']['det'].pop(field)
    with pytest.raises(ValueError): validate(request, response)


@pytest.mark.parametrize('field', ['shape_session_id', 'input_shape', 'free_dimension_overrides',
                                  'require_static_input_shapes', 'profile_window', 'capture',
                                  'request_run_count', 'session_run_count', 'evidence_reused'])
def test_every_concrete_shape_needs_its_own_complete_profile_identity(field):
    request, response = evidence_response()
    child(response).pop(field)
    with pytest.raises(ValueError): validate(request, response)


@pytest.mark.parametrize('change', [
    lambda c: c.update(shape_session_id=True),
    lambda c: c.update(shape_session_id=0),
    lambda c: c.update(input_shape=[1, 3, 48, True]),
    lambda c: c.update(input_shape=[1, 3, 48]),
    lambda c: c.update(input_shape=[1, 3, 32, 320]),
    lambda c: c.update(input_shape=[7, 3, 48, 320]),
    lambda c: c.update(input_shape=[1, 3, 48, 3201]),
    lambda c: c['free_dimension_overrides'].update({'DynamicDimension.1': 319}),
    lambda c: c['free_dimension_overrides'].update({'DynamicDimension.0': True}),
    lambda c: c['free_dimension_overrides'].update({'unexpected': 320}),
    lambda c: c.update(require_static_input_shapes=False),
    lambda c: c.update(request_run_count=0),
    lambda c: c.update(capture='configuration'),
    lambda c: c.update(coreml_kernel_events=0, coreml_ep_executed=False,
                       observed_providers=['CPUExecutionProvider']),
    lambda c: c['profile_window'].update(worker_pid=999),
    lambda c: c['profile_window'].update(monotonic_end_ns=1200),
    lambda c: c['profile_window'].update(monotonic_start_ns=900),
])
def test_malformed_or_substituted_shape_profile_fails(change):
    request, response = evidence_response()
    change(child(response))
    with pytest.raises(ValueError): validate(request, response)


def test_reused_profile_must_precede_current_request_not_claim_new_run():
    request, response = evidence_response()
    aggregate = response['execution_evidence']['sessions']['rec']
    aggregate.update(session_run_count=2, evidence_reused=True)
    child(response).update(session_run_count=2, evidence_reused=True)
    with pytest.raises(ValueError, match='predate'): validate(request, response)
    child(response)['profile_window'].update(monotonic_start_ns=800, monotonic_end_ns=900)
    validate(request, response)


@pytest.mark.parametrize('field,value', [('cache_capacity', 5), ('cache_capacity', True),
    ('cached_shape_session_count', 5), ('cached_shape_session_count', True),
    ('cached_shape_session_ids', [2, 2]), ('cached_shape_session_ids', [False]),
    ('cached_shape_session_ids', []), ('shape_cache_scope', 'width_bucket'),
    ('profile_window', {}), ('coreml_kernel_events', 99), ('cpu_kernel_events', 99),
    ('request_run_count', 2), ('session_run_count', 0), ('evidence_reused', True)])
def test_aggregate_cannot_fabricate_cache_or_event_counts(field, value):
    request, response = evidence_response()
    response['execution_evidence']['sessions']['rec'][field] = value
    with pytest.raises(ValueError): validate(request, response)


def test_det_and_rec_cannot_claim_the_same_process_global_id():
    request, response = evidence_response()
    child(response)['shape_session_id'] = 1
    response['execution_evidence']['sessions']['rec']['cached_shape_session_ids'] = [1]
    with pytest.raises(ValueError, match='across models'): validate(request, response)


def test_two_rec_shapes_count_each_first_profile_once_not_each_run():
    request, response = evidence_response()
    rec = response['execution_evidence']['sessions']['rec']
    first = child(response)
    first.update(request_run_count=2, session_run_count=2)
    second = deepcopy(first)
    second.update(shape_session_id=3, input_shape=[1, 3, 48, 640], request_run_count=1, session_run_count=1)
    second['free_dimension_overrides']['DynamicDimension.1'] = 640
    rec.update(shape_sessions=[first, second], request_run_count=3, session_run_count=3,
               coreml_kernel_events=6, cpu_kernel_events=2,
               cached_shape_session_count=2, cached_shape_session_ids=[2, 3])
    result = validate(request, response)
    assert result['sessions']['rec']['coreml_kernel_events'] == 6
    assert result['sessions']['rec']['evidence_reused'] is False
    duplicate = deepcopy(response)
    duplicate['execution_evidence']['sessions']['rec']['shape_sessions'][1]['shape_session_id'] = 2
    with pytest.raises(ValueError, match='duplicated'): validate(request, duplicate)
    rec['coreml_kernel_events'] = 9
    with pytest.raises(ValueError, match='aggregate'): validate(request, response)


def next_request(request, response):
    request, response = deepcopy(request), deepcopy(response)
    for field in ('utc_start_ns', 'utc_end_ns', 'monotonic_start_ns', 'monotonic_end_ns'):
        response['execution_window'][field] += 1000
    for aggregate in response['execution_evidence']['sessions'].values():
        if aggregate is not None:
            aggregate['session_run_count'] += aggregate['request_run_count']
            aggregate['evidence_reused'] = aggregate['status'] == 'executed'
            for item in aggregate['shape_sessions']:
                item['session_run_count'] += item['request_run_count']
                item['evidence_reused'] = True
    return request, response


def test_live_continuity_reuses_only_exact_shape_profile_and_cumulative_count():
    request, first = evidence_response()
    state = adapter.CoremlShapeEvidenceState()
    validate(request, first, state)
    request, second = next_request(request, first)
    validate(request, second, state)
    assert len(state.cached['det']) == len(state.cached['rec']) == 1
    assert state.highest_id == 2
    # Mutating a previously returned response cannot change the saved identity.
    child(second)['input_shape'][3] = 640
    child(second)['free_dimension_overrides']['DynamicDimension.1'] = 640
    request, third = next_request(request, second)
    validate(request, third)  # Internally consistent but falsely relabeled.
    with pytest.raises(ValueError, match='identity'): validate(request, third, state)
    assert state.totals == {'det': 2, 'rec': 2}, 'Failure is transactional'


@pytest.mark.parametrize('change', [
    lambda c: c['profile_window'].update(utc_start_ns=99),
    lambda c: c.update(coreml_kernel_events=4),
    lambda c: c.update(session_run_count=5),
])
def test_live_history_rejects_rewritten_first_profile_or_counter(change):
    request, first = evidence_response(); state = adapter.CoremlShapeEvidenceState()
    validate(request, first, state)
    request, second = next_request(request, first)
    change(child(second))
    rec = second['execution_evidence']['sessions']['rec']
    rec['coreml_kernel_events'] = child(second)['coreml_kernel_events']
    rec['session_run_count'] = child(second)['session_run_count']
    with pytest.raises(ValueError): validate(request, second, state)


def test_evicted_shape_can_be_recompiled_with_new_id_but_never_resurrected():
    request, first = evidence_response(); state = adapter.CoremlShapeEvidenceState()
    validate(request, first, state)
    request, second = next_request(request, first)
    rec = second['execution_evidence']['sessions']['rec']
    original = deepcopy(rec)
    rec = shape_aggregate(rec['shape_sessions'][0], 'rec', second['execution_window'], shape_id=3, shape=[1, 3, 48, 640])
    # shape_aggregate accepts the old generic session payload; remove nested
    # fields only if the supplied payload came from a shape child.
    rec['session_run_count'] = 2
    rec['shape_sessions'][0]['profile_window'] = deepcopy(second['execution_window'])
    rec['shape_sessions'][0]['session_run_count'] = 1
    rec['shape_sessions'][0]['request_run_count'] = 1
    rec['shape_sessions'][0]['evidence_reused'] = False
    rec['evidence_reused'] = False
    second['execution_evidence']['sessions']['rec'] = rec
    validate(request, second, state)
    assert set(state.cached['rec']) == {3}
    request, third = next_request(request, second)
    third['execution_evidence']['sessions']['rec'] = original
    original['session_run_count'] = 3
    # old shape ID2/profile could still be self-consistent, but was evicted.
    with pytest.raises(ValueError, match='evicted'): validate(request, third, state)


def test_blank_recognition_keeps_bounded_cache_without_claiming_execution():
    request, first = evidence_response(); state = adapter.CoremlShapeEvidenceState()
    validate(request, first, state)
    request, blank = next_request(request, first)
    blank['result'] = [[], []]
    _, empty = evidence_response(blank=True)
    rec = empty['execution_evidence']['sessions']['rec']
    rec.update(session_run_count=1, cached_shape_session_count=1, cached_shape_session_ids=[2])
    blank['execution_evidence']['sessions']['rec'] = rec
    validate(request, blank, state)
    assert len(state.cached['rec']) == 1 and state.totals['rec'] == 1
    rec['shape_sessions'] = [deepcopy(child(first))]
    with pytest.raises(ValueError): validate(request, blank)


def test_cpu_old_contract_still_does_not_require_shapes():
    request, response = evidence_response('cpu')
    validate(request, response)
    assert 'shape_sessions' not in response['execution_evidence']['sessions']['det']


def document(request, response, request_id=1):
    request, response = deepcopy(request), deepcopy(response)
    request['id'] = response['id'] = request_id
    response['ok'] = True
    return {'worker_pid': response['execution_window']['worker_pid'], 'request': request, 'response': response}


def test_complete_receipt_orders_records_numerically_and_rechecks_shape_continuity():
    request, first = evidence_response()
    req2, second = next_request(request, first)
    docs = [document(req2, second, 2), document(request, first)]
    adapter.validate_coreml_record_sequence(docs)
    child(docs[0]['response'])['input_shape'][3] = 640
    child(docs[0]['response'])['free_dimension_overrides']['DynamicDimension.1'] = 640
    with pytest.raises(ValueError, match='identity'): adapter.validate_coreml_record_sequence(docs)


@pytest.mark.parametrize('change', [
    lambda docs: docs.pop(1),
    lambda docs: docs[0]['request'].update(id=3),
    lambda docs: docs[0]['response']['execution_window'].update(monotonic_start_ns=1050),
    lambda docs: docs.append(deepcopy(docs[-1])),
])
def test_complete_receipt_rejects_gaps_misalignment_overlap_or_replay(change):
    request, first = evidence_response(); req2, second = next_request(request, first)
    docs = [document(req2, second, 2), document(request, first)]
    change(docs)
    with pytest.raises(ValueError): adapter.validate_coreml_record_sequence(docs)


def test_honest_pid_reuse_requires_new_profiles_not_previous_process_cache():
    request, first = evidence_response(); _, second = evidence_response()
    for field in ('utc_start_ns', 'utc_end_ns', 'monotonic_start_ns', 'monotonic_end_ns'):
        second['execution_window'][field] += 1000
    for aggregate in second['execution_evidence']['sessions'].values():
        aggregate['shape_sessions'][0]['profile_window'] = deepcopy(second['execution_window'])
    adapter.validate_coreml_record_sequence([document(request, first), document(request, second)])
    req2, reused = next_request(request, first)
    with pytest.raises(ValueError, match='lifetime|reused'):
        adapter.validate_coreml_record_sequence([document(request, first), document(req2, reused)])


def test_batch_size_change_does_not_reset_dispatcher_history():
    request, first = evidence_response(); state = adapter.CoremlShapeEvidenceState()
    validate(request, first, state)
    request, second = next_request(request, first)
    request['batch_size'] = 2
    validate(request, second, state)
    assert state.totals == {'det': 2, 'rec': 2}
