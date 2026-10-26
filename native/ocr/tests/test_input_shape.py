"""Bounded real-ONNX metadata reader; standalone native target never loads ORT."""
import json
import os
from pathlib import Path
import subprocess

import pytest


def varint(value):
    out = bytearray()
    while value >= 128:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def field(number, value):
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    return varint((number << 3) | 2) + varint(len(value)) + value


def model(dimensions, dtype=1, extra_input=False):
    dims = b''.join(field(1, field(2, d.encode()) if isinstance(d, str) else field(1, d)) for d in dimensions)
    tensor = field(1, dtype) + field(2, dims)
    value = field(1, b'x') + field(2, field(1, tensor))
    graph = field(11, value) * (2 if extra_input else 1)
    return field(1, 9) + field(7, graph)


@pytest.fixture
def binary():
    value = os.environ.get('VSE_NATIVE_SHAPE_TEST_BINARY')
    if not value:
        pytest.skip('set VSE_NATIVE_SHAPE_TEST_BINARY for standalone C++ reader')
    assert Path(value).is_file()
    return value


def run(binary, path, shape):
    return subprocess.run([binary, str(path), *map(str, shape)], capture_output=True, text=True, timeout=15)


@pytest.mark.parametrize('dimensions,shape,expected', [
    (['n', 3, 'h', 'w'], [1, 3, 96, 320], {'n': 1, 'h': 96, 'w': 320}),
    (['n', 3, 48, 'w'], [6, 3, 48, 511], {'n': 6, 'w': 511}),
    (['n', 3, 48, 'w'], [1, 3, 48, 320], {'n': 1, 'w': 320}),
    (['n', 3, 'hw', 'hw'], [1, 3, 64, 64], {'n': 1, 'hw': 64}),
])
def test_exact_shapes(binary, tmp_path, dimensions, shape, expected):
    path = tmp_path / 'input.onnx'
    path.write_bytes(model(dimensions))
    result = run(binary, path, shape)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'input': 'x', 'overrides': expected, 'shape': shape}


@pytest.mark.parametrize('data,shape', [
    (model(['n', 3, 'hw', 'hw']), [1, 3, 64, 96]),
    (model(['n', 3, 48, 'w']), [1, 3, 32, 320]),
    (model(['n', 3, 48, 'w']), [0, 3, 48, 320]),
    (model(['n', 3, 48, 'w']), [1000000, 3, 48, 320]),
    (model(['n', 3, 48, 'w'], dtype=10), [1, 3, 48, 320]),
    (model(['n', 3, 48, 'w'], extra_input=True), [1, 3, 48, 320]),
    (model(['n', 3, 'w']), [1, 3, 48, 320]),
    (model(['n', 3, 48, '']), [1, 3, 48, 320]),
    (model(['n', 3, 48, 'w'])[:-1], [1, 3, 48, 320]),
    (b'\x3a\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff', [1, 3, 48, 320]),
])
def test_invalid_metadata_or_binding_is_rejected(binary, tmp_path, data, shape):
    path = tmp_path / 'invalid.onnx'
    path.write_bytes(data)
    result = run(binary, path, shape)
    assert result.returncode == 2 and result.stderr and not result.stdout


def test_all_pinned_model_inputs(binary):
    root = os.environ.get('VSE_NATIVE_SHAPE_MODEL_ROOT')
    if not root:
        pytest.skip('set VSE_NATIVE_SHAPE_MODEL_ROOT for actual 15-model metadata')
    paths = sorted(Path(root).glob('*.onnx'))
    assert len(paths) == 15
    for path in paths:
        detector = '_det_' in path.name
        shape = [1, 3, 96, 320] if detector else [2, 3, 48, 333]
        result = run(binary, path, shape)
        assert result.returncode == 0, (path, result.stderr)
        expected = {'DynamicDimension.0': shape[0], 'DynamicDimension.1': shape[2 if detector else 3]}
        if detector:
            expected['DynamicDimension.2'] = shape[3]
        assert json.loads(result.stdout)['overrides'] == expected
