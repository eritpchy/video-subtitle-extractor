"""Synthetic shape-session fixtures, never real inference/device evidence."""
from copy import deepcopy


def shape_aggregate(session, role, window, *, shape_id=None, shape=None,
                    total=None, current=None, first_window=None):
    result = deepcopy(session)
    run = result['status'] == 'executed'
    current = int(run) if current is None else current
    total = current if total is None else total
    shape_id = (1 if role == 'det' else 2) if shape_id is None else shape_id
    shape = ([1, 3, 32, 64] if role == 'det' else [1, 3, 48, 320]) if shape is None else shape
    result.update(capture='per_shape_first_successful_run', request_run_count=current,
                  session_run_count=total, evidence_reused=bool(current and total > current),
                  cache_capacity=2 if role == 'det' else 4, cached_shape_session_count=int(total > 0),
                  cached_shape_session_ids=[shape_id] if total else [],
                  shape_cache_scope='exact_input_shape_lru',
                  aggregation_scope='Synthetic unique first-run shape profile summary, not GPU evidence')
    result.pop('profile_window', None)
    result['shape_sessions'] = []
    if run:
        child = deepcopy(session)
        overrides = {'DynamicDimension.0': shape[0], 'DynamicDimension.1': shape[2] if role == 'det' else shape[3]}
        if role == 'det':
            overrides['DynamicDimension.2'] = shape[3]
        child.update(capture='first_successful_run', request_run_count=current,
                     session_run_count=total, evidence_reused=total > current,
                     profile_window=deepcopy(first_window or window), shape_session_id=shape_id,
                     input_shape=list(shape), free_dimension_overrides=overrides,
                     require_static_input_shapes=True)
        result['shape_sessions'] = [child]
    return result
