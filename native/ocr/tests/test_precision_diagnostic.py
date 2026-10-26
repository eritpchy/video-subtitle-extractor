"""Diagnostic isolation/validation tests; no network or Paddle import."""
import json
from pathlib import Path
import struct
import subprocess
import sys
import builtins

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


def geometry_fixture(tmp_path):
    pixels = tmp_path / 'geometry-0-pre-resize.u8'
    pixels.write_bytes(bytes(range(18)))
    record = {'schema_version': 1, 'det_quad': [[0, 0], [3, 0], [3, 2], [0, 2]],
              'crop_min_area_rect': [[0., 0.], [3., 0.], [3., 2.], [0., 2.]],
              'warp_perspective_matrix': [[1., 0., 0.], [0., 1., 0.], [0., 0., 1.]],
              'warp_size': [2, 3], 'rotated_90_counterclockwise': False,
              'pre_resize': {'file': pixels.name, 'shape': [2, 3, 3], 'dtype': 'uint8', 'channel_order': 'BGR'}}
    metadata = tmp_path / 'geometry-0.json'
    metadata.write_text(json.dumps(record))
    return metadata, record


def test_geometry_raw_pixels_hash_and_rotation_shape(tmp_path):
    path, record = geometry_fixture(tmp_path)
    validated = diagnostic.validate_geometry(tmp_path, path)
    assert len(validated['pre_resize']['sha256']) == 64
    record['rotated_90_counterclockwise'] = True
    record['pre_resize']['shape'] = [3, 2, 3]
    path.write_text(json.dumps(record))
    diagnostic.validate_geometry(tmp_path, path)


@pytest.mark.parametrize('kind', ['noninteger-quad', 'nonfinite-matrix', 'bad-corners', 'bad-rotation',
                                 'bad-shape', 'bad-type', 'traversal', 'truncated', 'hash'])
def test_geometry_invalid_or_corrupt_evidence_rejected(tmp_path, kind):
    path, record = geometry_fixture(tmp_path)
    if kind == 'noninteger-quad':
        record['det_quad'][0][0] = .25
    elif kind == 'nonfinite-matrix':
        record['warp_perspective_matrix'][0][0] = float('nan')
    elif kind == 'bad-corners':
        record['crop_min_area_rect'].pop()
    elif kind == 'bad-rotation':
        record['rotated_90_counterclockwise'] = 1
    elif kind == 'bad-shape':
        record['pre_resize']['shape'] = [2, 2, 3]
    elif kind == 'bad-type':
        record['pre_resize']['dtype'] = '<f4'
    elif kind == 'traversal':
        record['pre_resize']['file'] = '../outside.u8'
    elif kind == 'truncated':
        (tmp_path / record['pre_resize']['file']).write_bytes(b'short')
    else:
        record['pre_resize']['sha256'] = '0' * 64
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError):
        diagnostic.validate_geometry(tmp_path, path)


def test_source_artifact_checked_before_importing_reference_frameworks(tmp_path, monkeypatch):
    source = tmp_path / 'sources'
    source.mkdir()
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        if name.split('.')[0] in ('paddle', 'numpy'):
            pytest.fail('reference framework imported before source verification')
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guarded_import)
    monkeypatch.setattr(diagnostic, 'get_archive', lambda *_a, **_k: pytest.fail('artifact path must never download'))
    with pytest.raises(ValueError, match='artifact model/file set'):
        diagnostic.reference(tmp_path / 'capture', tmp_path / 'cache', sources_dir=source)


def test_ci_reference_sources_are_separate_offline_and_not_cache_hit_conditional():
    text = (diagnostic.ROOT.parents[1] / '.github/workflows/native-ocr.yml').read_text()
    models, remaining = text.split('\n  models:\n', 1)[1].split('\n  precision-reference-sources:\n', 1)
    sources, native = remaining.split('\n  native:\n', 1)
    assert 'precision-reference' not in models
    assert 'needs:' not in sources and 'timeout-minutes: 15' in sources
    assert 'runs-on: ubuntu-24.04' in sources
    assert 'needs: [models, qt-overlay-resources]\n' in native
    assert "needs.models.result == 'success'" in native
    export = text.split('- name: Export three verified original models for build-only Mac reference', 1)[1].split('- name:', 1)[0]
    assert 'if:' not in export
    assert '--seed-cache' not in export
    assert 'name: native-ocr-precision-reference-sources-${{ github.sha }}' in text
    assert '--sources build/precision-reference-sources --offline' in text
    assert 'path: build/precision-reference-sources/' in text
    assert '--output build/native-models --cache build/model-cache' in text
    assert 'actions: read' in native
    assert "steps.precision-source-status.outputs.available == 'true'" in native
    assert "steps.precision-source-download.outcome == 'success'" in native
    assert 'precision-reference-source-status.json' in native
    assert 'continue-on-error:' not in sources
