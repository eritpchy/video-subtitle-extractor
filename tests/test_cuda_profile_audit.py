import hashlib
import json
from types import SimpleNamespace

import pytest

from scripts.cuda_profile_audit import gpu_tensor_probe, inspect_graph, validate_profile


@pytest.mark.parametrize('kwargs', [dict(precision='float16'), dict(engine='tensorrt'),
    dict(devices=(0, 1)), dict(custom_models=True), dict(training=True),
    dict(profiling=True), dict(tensor_parallel=True), dict(hpi=True)])
def test_profile_rejects_unsupported_features(kwargs):
    with pytest.raises(ValueError):
        validate_profile(**kwargs)


def test_default_profile():
    validate_profile()


def test_cpu_cannot_pass_gpu_probe():
    with pytest.raises(RuntimeError, match='CPU fallback'):
        gpu_tensor_probe(SimpleNamespace(is_compiled_with_cuda=lambda: False))


def test_cuda_build_without_gpu_cannot_pass_probe():
    paddle = SimpleNamespace(is_compiled_with_cuda=lambda: True,
                             device=SimpleNamespace(cuda=SimpleNamespace(device_count=lambda: 0)))
    with pytest.raises(RuntimeError, match='Real CUDA'):
        gpu_tensor_probe(paddle)


@pytest.mark.parametrize('operation,tensor_type,valid', [
    ('1.matmul', '0.t_f32', True), ('1.flash_attn', '0.t_f32', False),
    ('1.all_reduce', '0.t_f32', False), ('1.matmul', '0.t_f16', False)])
def test_graph_scope(tmp_path, operation, tensor_type, valid):
    p = tmp_path / 'inference.json'
    p.write_text(json.dumps({'ops': [{'#': operation}], 'type': {'#': tensor_type}}))
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    if valid:
        assert inspect_graph(p, expected_sha256=digest)['operation_count'] == 1
    else:
        with pytest.raises(ValueError):
            inspect_graph(p, expected_sha256=digest)


def test_graph_change_rejected(tmp_path):
    p = tmp_path / 'inference.json'
    p.write_text('{}')
    with pytest.raises(ValueError, match='changed'):
        inspect_graph(p, expected_sha256='0' * 64)
