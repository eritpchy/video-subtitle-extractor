import copy
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from backend.tools import directml_ocr as module
from backend.tools.hardware_accelerator import HardwareAccelerator
from backend.tools.native_ocr import selected_backend
from backend.tools.paddle_model_config import PaddleModelConfig


def fake_ort(monkeypatch, *, available=None, constructed=None, executed=None, fail_run=False):
    available = available if available is not None else [module.DML_PROVIDER, module.CPU_PROVIDER]
    constructed = constructed if constructed is not None else available
    executed = executed if executed is not None else [module.DML_PROVIDER, module.CPU_PROVIDER]
    calls = []
    class Session:
        def __init__(self, path, sess_options, providers):
            self.options, self.providers = sess_options, providers
            self.disabled = False
            calls.append(self)
        def disable_fallback(self): self.disabled = True
        def get_providers(self): return constructed
        def get_inputs(self): return [SimpleNamespace(name='x')]
        def run(self, outputs, values):
            if fail_run: raise RuntimeError('DML device lost')
            return [values['x']]
        def end_profiling(self):
            path = Path(self.options.profile_file_prefix + '.json')
            path.write_text(json.dumps([{'cat': 'Node', 'args': {'provider': item}} for item in executed]))
            return str(path)
    monkeypatch.setitem(sys.modules, 'onnxruntime', SimpleNamespace(__version__='1.20.1',
        get_version_string=lambda: '1.20.1', get_available_providers=lambda: available, SessionOptions=SimpleNamespace,
        ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL='sequential'), InferenceSession=Session))
    return calls


def test_directml_backend_is_explicit_and_imports_no_paddle(monkeypatch):
    monkeypatch.setenv('VSE_OCR_BACKEND', 'directml')
    fake_ort(monkeypatch)
    hardware = HardwareAccelerator()
    hardware.initialize()
    assert selected_backend() == 'directml'
    assert hardware.has_directml() and hardware.has_accelerator()
    assert hardware.accelerator_name == 'DirectML'
    assert not hardware.has_cuda()
    hardware.set_enabled(False)
    assert not hardware.is_enabled and not hardware.has_accelerator()
    assert hardware.accelerator_name == 'CPU'


def test_paddle_backend_does_not_advertise_unused_dml(monkeypatch):
    hardware = HardwareAccelerator()
    hardware._HardwareAccelerator__onnx_providers = [module.DML_PROVIDER]
    assert not hardware.has_accelerator()
    assert hardware.accelerator_name == 'CPU'


def test_session_is_sequential_and_requires_real_dml_kernel(monkeypatch):
    calls = fake_ort(monkeypatch)
    session = module.OnnxSession('fixture.onnx', 'directml')
    assert calls[0].disabled
    assert calls[0].providers == [(module.DML_PROVIDER, {'device_id': '0'})]
    assert calls[0].options.enable_mem_pattern is False
    assert calls[0].options.execution_mode == 'sequential'
    assert not session.evidence['verified']
    assert session.run('tensor') == 'tensor'
    assert session.evidence['verified']
    assert session.evidence['executed_nodes'][module.CPU_PROVIDER] == 1
    assert session.evidence['cpu_explicit'] is False
    session.close()


@pytest.mark.parametrize('mode', ['absent', 'constructor-fallback', 'all-cpu'])
def test_silent_cpu_fallback_is_refused(monkeypatch, mode):
    calls = fake_ort(monkeypatch, available=[module.CPU_PROVIDER] if mode == 'absent' else None,
                     constructed=[module.CPU_PROVIDER] if mode == 'constructor-fallback' else None,
                     executed=[module.CPU_PROVIDER] if mode == 'all-cpu' else None)
    with pytest.raises(RuntimeError, match='CPU|DirectML'):
        session = module.OnnxSession('fixture.onnx', 'directml')
        session.run('tensor')
    if mode == 'absent': assert not calls


def test_device_loss_does_not_retry_cpu(monkeypatch):
    calls = fake_ort(monkeypatch, fail_run=True)
    session = module.OnnxSession('fixture.onnx', 'directml')
    with pytest.raises(RuntimeError, match='device lost'):
        session.run('tensor')
    assert len(calls) == 1 and session.session is None


def test_explicit_cpu_is_not_reported_as_gpu(monkeypatch):
    calls = fake_ort(monkeypatch, available=[module.CPU_PROVIDER])
    session = module.OnnxSession('fixture.onnx', 'cpu')
    assert calls[0].providers == [module.CPU_PROVIDER]
    assert session.run('tensor') == 'tensor'
    assert session.evidence == {'requested_provider': module.CPU_PROVIDER, 'verified': True, 'cpu_explicit': True}


def test_unreviewed_ort_version_is_refused(monkeypatch):
    fake_ort(monkeypatch)
    sys.modules['onnxruntime'].__version__ = '1.24.4'
    with pytest.raises(RuntimeError, match='requires ONNX Runtime 1.20.1'):
        module.OnnxSession('fixture.onnx', 'directml')


def asset_fixture(tmp_path, monkeypatch):
    pins = json.loads((Path(__file__).parents[1] / 'native/ocr/model-source-pins.json').read_text())
    manifest = []
    for row in pins['models']:
        name = row['name']
        model = tmp_path / 'models' / (name + '_infer.onnx')
        model.parent.mkdir(exist_ok=True)
        model.write_bytes(name.encode())
        row['onnx_bytes'] = model.stat().st_size
        row['onnx_sha256'] = hashlib.sha256(model.read_bytes()).hexdigest()
        config_file = f'config/{name.split("_")[1]}.json' if name.startswith('PP-OCRv6_') else f'config/{name}.json'
        cfg = tmp_path / config_file
        cfg.parent.mkdir(exist_ok=True)
        data = json.loads(cfg.read_text()) if cfg.exists() else {}
        value = {'Global': {'model_name': name}}
        if name.startswith('PP-OCRv6_'): data[name.rsplit('_', 1)[1]] = value
        else: data = value
        cfg.write_text(json.dumps(data))
        manifest.append({'name': name + '_infer', 'onnx_bytes': row['onnx_bytes'], 'onnx_sha256': row['onnx_sha256'], 'config_file': config_file})
    for row in manifest:
        digest = hashlib.sha256((tmp_path / row['config_file']).read_bytes()).hexdigest()
        row['config_sha256'] = pins['config_sha256'][row['config_file']] = digest
    (tmp_path / 'model-manifest.json').write_text(json.dumps(manifest))
    (tmp_path / 'model-source-pins.json').write_text(json.dumps(pins))
    monkeypatch.setattr(module, 'MODEL_SOURCE_PINS_SHA256', module._sha(tmp_path / 'model-source-pins.json'))
    return tmp_path


def test_all_15_assets_authenticated_without_model_substitution(tmp_path, monkeypatch):
    root = asset_fixture(tmp_path, monkeypatch)
    result = module.validate_model_assets(root)
    assert len(result['configs']) == 15
    with pytest.raises(ValueError, match='Unvalidated'):
        module.validate_model_assets(root, ['fake_model'])


@pytest.mark.parametrize('corruption', ['model', 'config', 'pins', 'manifest', 'duplicate', 'traversal'])
def test_asset_tampering_refused(tmp_path, monkeypatch, corruption):
    root = asset_fixture(tmp_path, monkeypatch)
    manifest = json.loads((root / 'model-manifest.json').read_text())
    if corruption == 'model': (root / 'models/PP-OCRv6_small_det_infer.onnx').write_bytes(b'tampered')
    elif corruption == 'config': (root / 'config/small.json').write_text('{}')
    elif corruption == 'pins': (root / 'model-source-pins.json').write_text('{}')
    else:
        if corruption == 'manifest': manifest[0]['onnx_sha256'] = '0' * 64
        elif corruption == 'duplicate': manifest[0] = copy.deepcopy(manifest[1])
        else: manifest[0]['config_file'] = '../escape'
        (root / 'model-manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError): module.validate_model_assets(root)


def test_model_staging_contains_no_worker_or_conversion_stack(tmp_path, monkeypatch):
    from scripts.directml_package import stage_directml_models
    root = tmp_path / 'source'; root.mkdir()
    asset_fixture(root, monkeypatch)
    (root / 'native-ocr.exe').write_bytes(b'never copied')
    result = stage_directml_models(root, tmp_path / 'output')
    assert result['model_count'] == 15 and result['gpu_hardware_verified'] is False
    assert not (tmp_path / 'output/native-ocr.exe').exists()


def test_recognizer_routes_directml_before_paddle_import(tmp_path, monkeypatch):
    monkeypatch.setenv('VSE_OCR_BACKEND', 'directml')
    root = asset_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv('VSE_DIRECTML_MODEL_ROOT', str(root))
    from backend.tools.ocr import OcrRecogniser
    from backend.tools.subtitle_detect import SubtitleDetect
    hardware = HardwareAccelerator()
    recognizer = OcrRecogniser('ch', 'fast', 6, hardware).init_model()
    assert isinstance(recognizer, module.DirectMlOcr) and recognizer.provider == 'directml'
    hardware.set_enabled(False)
    detector = SubtitleDetect('ch', 'fast', hardware)
    assert isinstance(detector.text_detector, module.DirectMlOcr)
    assert detector.text_detector.provider == 'cpu'


def test_invalid_images_and_closed_engine_fail_before_inference(tmp_path, monkeypatch):
    import numpy as np
    root = asset_fixture(tmp_path, monkeypatch)
    config = PaddleModelConfig(HardwareAccelerator(), 'ch', 'fast')
    engine = module.DirectMlOcr(config, root=root)
    for image in (None, np.zeros((0, 2, 3), dtype=np.uint8), np.zeros((5, 5, 2), dtype=np.uint8), np.zeros((5, 5, 3), dtype=np.float32)):
        with pytest.raises(ValueError): list(engine.predict_iter(image))
    engine.close()
    with pytest.raises(RuntimeError, match='closed'): list(engine.predict_iter(np.zeros((5, 5, 3), dtype=np.uint8)))


@pytest.mark.parametrize('version,conflict,valid_dll', [('1.24.4', False, True), ('1.20.1', True, True), ('1.20.1', False, False)])
def test_packaging_rejects_changed_runtime_or_conflicting_stacks(tmp_path, monkeypatch, version, conflict, valid_dll):
    import importlib.metadata
    from scripts import directml_package
    dist = SimpleNamespace(metadata={'Name': 'onnxruntime-directml'}, version=version)
    packages = [dist]
    if conflict:
        packages.append(SimpleNamespace(metadata={'Name': 'onnxruntime'}, version='1.20.1'))
    monkeypatch.setattr(importlib.metadata, 'distributions', lambda **kwargs: packages)
    dll = tmp_path / 'onnxruntime/capi/DirectML.dll'
    dll.parent.mkdir(parents=True); dll.write_bytes(b'fixture')
    monkeypatch.setattr(directml_package, '_sha', lambda path: '7e64aab30990e4b772751719a42290304d7502c99e75ae8164529d75b35c42f1' if valid_dll else 'bad')
    with pytest.raises(ValueError): directml_package.audit_directml_runtime(tmp_path)


def test_staging_rechecks_pinned_hash_after_copy(tmp_path, monkeypatch):
    from scripts import directml_package
    root = tmp_path / 'source'; root.mkdir()
    asset_fixture(root, monkeypatch)
    original = directml_package.shutil.copy2
    def corrupt(source, destination):
        result = original(source, destination)
        if str(source).endswith('.onnx'):
            Path(destination).write_bytes(b'corrupted after initial validation')
        return result
    monkeypatch.setattr(directml_package.shutil, 'copy2', corrupt)
    with pytest.raises(ValueError, match='changed while staging'):
        directml_package.stage_directml_models(root, tmp_path / 'output')


def test_original_runtime_notice_files_are_pinned():
    root = Path(__file__).parents[1] / 'docs/licenses/directml-1.15.2'
    source = json.loads((root / 'source.json').read_text())
    assert source['existing_runtime']['version'] == '1.20.1'
    assert source['existing_runtime']['directml_dll_version'] == '1.15.2'
    for name, expected in source['files'].items():
        assert module._sha(root / name) == expected


def test_native_version_cannot_hide_behind_old_python_metadata(monkeypatch):
    fake_ort(monkeypatch)
    sys.modules['onnxruntime'].get_version_string = lambda: '1.24.4'
    with pytest.raises(RuntimeError, match='native 1.24.4'):
        module.OnnxSession('fixture.onnx', 'directml')
