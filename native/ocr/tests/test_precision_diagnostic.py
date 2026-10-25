"""Diagnostic isolation/validation tests; no network or Paddle import."""
import json
from pathlib import Path
import struct
import subprocess
import sys

import pytest

import precision_diagnostic as diagnostic


def trace_fixture(tmp_path):
    metadata = tmp_path / '0.json'
    data = {'schema_version': 1, 'model': 'cyrillic_PP-OCRv5_mobile_rec_infer.onnx'}
    for kind in ('input', 'output'):
        name = f'0-{kind}.f32'
        (tmp_path / name).write_bytes(struct.pack('<2f', .25, .75))
        data[kind] = {'file': name, 'shape': [1, 2], 'dtype': '<f4'}
    metadata.write_text(json.dumps(data))
    return metadata, data


def test_capture_helper_imports_without_site_packages():
    result = subprocess.run([sys.executable, '-S', str(diagnostic.ROOT / 'precision_diagnostic.py'), '--help'],
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert 'NOT that native parity passed' in ' '.join(result.stdout.split())


def test_tensor_hash_and_byte_size_verified(tmp_path):
    path, _ = trace_fixture(tmp_path)
    trace = diagnostic.validate_trace(tmp_path, path)
    path.write_text(json.dumps(trace))
    (tmp_path / '0-output.f32').write_bytes(struct.pack('<2f', .25, .74))
    with pytest.raises(ValueError, match='hash mismatch'):
        diagnostic.validate_trace(tmp_path, path)


@pytest.mark.parametrize('change', [
    {'file': '../outside.f32'}, {'file': 'dir\\outside.f32'}, {'shape': [0]},
    {'shape': [True, 2]}, {'shape': [1, 3]}, {'dtype': '<f2'},
])
def test_bad_trace_is_rejected(tmp_path, change):
    path, data = trace_fixture(tmp_path)
    data['input'].update(change)
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        diagnostic.validate_trace(tmp_path, path)


def test_environment_restored(monkeypatch, tmp_path):
    monkeypatch.setenv('VSE_OCR_DIAGNOSTIC_MODE', 'old')
    monkeypatch.delenv('VSE_OCR_DIAGNOSTIC_TRACE_DIR', raising=False)
    with diagnostic.diagnostic_environment('baseline-repeat', tmp_path):
        assert diagnostic.os.environ['VSE_OCR_DIAGNOSTIC_MODE'] == 'baseline'
    assert diagnostic.os.environ['VSE_OCR_DIAGNOSTIC_MODE'] == 'old'
    assert 'VSE_OCR_DIAGNOSTIC_TRACE_DIR' not in diagnostic.os.environ


def test_four_failures_and_same_model_controls_are_preserved():
    cases = json.loads((diagnostic.ROOT / 'tests/goldens/smoke.json').read_text())
    assert cases['confidence_tolerance'] == 1e-5
    assert {31, 33, 66, 72}.issubset(diagnostic.DEFAULT_CASES)
    for failed in (31, 33, 66, 72):
        assert any(index not in (31, 33, 66, 72) and
                   cases['cases'][index]['rec_model'] == cases['cases'][failed]['rec_model']
                   for index in diagnostic.DEFAULT_CASES)


def test_target_is_opt_in_and_separate_from_packaging():
    cmake = (diagnostic.ROOT / 'CMakeLists.txt').read_text()
    assert 'VSE_OCR_BUILD_PRECISION_DIAGNOSTIC "Build separate, never-packaged precision evidence target" OFF' in cmake
    assert 'add_executable(native-ocr-precision EXCLUDE_FROM_ALL' in cmake
    assert 'target_compile_definitions(native-ocr-precision PRIVATE VSE_OCR_PRECISION_DIAGNOSTIC=1)' in cmake
    build = (diagnostic.ROOT / 'build.py').read_text()
    assert 'native-ocr-precision' not in build


def test_paddle_reference_does_not_import_converter_or_python_ort():
    source = Path(diagnostic.__file__).read_text()
    assert 'import onnxruntime' not in source
    assert 'import paddle2onnx' not in source
    requirements = (diagnostic.ROOT / 'requirements-precision-reference.txt').read_text()
    assert '#sha256=d7827d867503b502a881c718e3743d9b91648a62d01f93bcd5ba3d7846de5ab5' in requirements
    assert 'paddle2onnx' not in requirements


def test_capture_refuses_existing_output(tmp_path):
    worker = tmp_path / 'worker'
    worker.write_bytes(b'not executed')
    with pytest.raises(FileExistsError):
        diagnostic.capture(tmp_path, worker, tmp_path)
