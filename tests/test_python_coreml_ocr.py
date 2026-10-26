"""Synthetic Python-provider contracts. These are not Mac/GPU acceptance."""
import copy
import importlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from backend.tools import python_coreml_ocr as impl


def clock(value):
    return {'process_pid': 123, 'utc_ns': value + 1000, 'monotonic_ns': value}


def record(provider='coreml'):
    pins = json.loads(Path(impl.__file__).with_name('python_coreml_runtime.json').read_bytes())
    wheel = pins['wheels']['darwin-arm64-cp312' if provider == 'coreml' else 'linux-x86_64-cp312']
    model_pins = json.loads((Path(impl.__file__).resolve().parents[2] / 'native/ocr/model-source-pins.json').read_bytes())
    hashes = {row['name']: row['onnx_sha256'] for row in model_pins['models']}
    sessions = {}
    for role, shape, sid in [('det', [1, 3, 64, 320], 1), ('rec', [1, 3, 48, 320], 2)]:
        coreml = provider == 'coreml'
        child = {'shape_session_id': sid, 'input_shape': shape,
            'free_dimension_overrides': impl._shape_bindings(role, shape, 6), 'require_static_input_shapes': True,
            'request_run_count': 1, 'session_run_count': 1, 'evidence_reused': False,
            'profile_started': clock(11 + sid), 'profile_ended': clock(20 + sid),
            'source': 'ort_first_run_profile' if coreml else 'explicit_cpu_session',
            'profile_sha256': '1' * 64 if coreml else None,
            'kernel_events': {impl.COREML: int(coreml), impl.CPU: int(coreml)},
            'coreml_ep_executed': coreml, 'cpu_partition_observed': coreml, 'gpu_execution_verified': False}
        sessions[role] = {'model': f'PP-OCRv6_small_{role}', 'status': 'executed',
            'capture': 'per_shape_first_successful_run', 'request_run_count': 1, 'session_run_count': 1,
            'cache_capacity': 2 if role == 'det' else 4, 'cached_shape_session_ids': [sid],
            'shape_sessions': [child], 'gpu_execution_verified': False}
    return {'schema': 1, 'scope': 'python-source-inference', 'backend': 'python-coreml', 'provider': provider,
        'process_pid': 123, 'engine_id': 1, 'request_id': 1, 'interpreter': '/synthetic/python',
        'interpreter_sha256': 'a' * 64, 'source_files': impl._source_identity(),
        'runtime': {'version': impl.ORT_VERSION, 'wheel_filename': wheel['filename'], 'wheel_sha256': wheel['sha256'],
            'native_files': wheel['native_files'], 'telemetry_disabled_before_import': True,
            'available_providers': [impl.COREML, impl.CPU] if provider == 'coreml' else [impl.CPU]},
        'started': clock(10), 'ended': clock(30),
        'request': {'tier': 'small', 'rec_model': 'PP-OCRv6_small_rec', 'batch_size': 6, 'cpu_threads': 2, 'operation': 'ocr'},
        'result_empty': False, 'model_sha256': {name: hashes[name] for name in ('PP-OCRv6_small_det', 'PP-OCRv6_small_rec')},
        'input_encoded_png_sha256': 'b' * 64, 'input_encoded_png_bytes': 1234,
        'sessions': sessions, 'gpu_execution_verified': False, 'internal_coreml_precision_verified': False}


def reused(first):
    second = copy.deepcopy(first)
    second.update(request_id=2, started=clock(40), ended=clock(60))
    for aggregate in second['sessions'].values():
        aggregate['session_run_count'] = 2
        aggregate['shape_sessions'][0].update(session_run_count=2, evidence_reused=True)
    return second


@pytest.mark.parametrize('provider', ['cpu', 'coreml'])
def test_source_records_validate_actual_shapes_and_bounded_reuse(provider):
    first = record(provider); second = reused(first)
    assert impl.validate_python_coreml_records([first, second], provider=provider) == {'PP-OCRv6_small_det', 'PP-OCRv6_small_rec'}
    assert all(row['gpu_execution_verified'] is False for row in (first, second))


@pytest.mark.parametrize('provider', ['cpu', 'coreml'])
@pytest.mark.parametrize('mutation', ['source', 'wheel', 'native', 'gpu', 'precision', 'model', 'pid', 'duplicate',
    'shape', 'override', 'booloverride', 'flag', 'count', 'emptyshape', 'profiletime', 'booltime', 'sid', 'cache',
    'reuse', 'threads', 'kernelkey', 'sum', 'profilehash'])
def test_source_records_reject_mislabeled_identity_execution_and_shape(provider, mutation):
    value = record(provider); child = value['sessions']['det']['shape_sessions'][0]
    if mutation == 'source': value['source_files'] = {}
    elif mutation == 'wheel': value['runtime']['wheel_sha256'] = 'f' * 64
    elif mutation == 'native': value['scope'] = 'final-native-package'
    elif mutation == 'gpu': value['gpu_execution_verified'] = True
    elif mutation == 'precision': value['internal_coreml_precision_verified'] = True
    elif mutation == 'model': value['model_sha256']['PP-OCRv6_small_det'] = 'f' * 64
    elif mutation == 'pid': child['profile_started']['process_pid'] = 999
    elif mutation == 'duplicate': value['sessions']['rec']['shape_sessions'][0]['shape_session_id'] = 1
    elif mutation == 'shape': child['input_shape'][-1] = 321
    elif mutation == 'override': child['free_dimension_overrides']['DynamicDimension.2'] = 640
    elif mutation == 'booloverride': child['free_dimension_overrides']['DynamicDimension.0'] = True
    elif mutation == 'flag': child['coreml_ep_executed'] = not child['coreml_ep_executed']
    elif mutation == 'count': child['request_run_count'] = True
    elif mutation == 'emptyshape': value['sessions']['det']['shape_sessions'] = []
    elif mutation == 'profiletime': child['profile_ended'] = clock(99)
    elif mutation == 'booltime': child['profile_ended']['monotonic_ns'] = True
    elif mutation == 'sid': child['shape_session_id'] = False
    elif mutation == 'cache': value['sessions']['det']['cached_shape_session_ids'] = [1, 1]
    elif mutation == 'reuse': child['evidence_reused'] = True
    elif mutation == 'threads': value['request']['cpu_threads'] = 0
    elif mutation == 'kernelkey': child['kernel_events']['MadeUpProvider'] = 1
    elif mutation == 'sum': value['sessions']['det']['request_run_count'] = 2
    elif mutation == 'profilehash': child['profile_sha256'] = None if provider == 'coreml' else 'f' * 64
    with pytest.raises((ValueError, RuntimeError)):
        impl.validate_python_coreml_record(value, provider=provider)


def test_linux_wheel_cannot_claim_coreml_even_with_synthetic_profile():
    value = record('coreml'); value['runtime'] = record('cpu')['runtime']
    value['runtime']['available_providers'].append(impl.COREML)
    with pytest.raises(ValueError, match='platform wheel'): impl.validate_python_coreml_record(value)


@pytest.mark.parametrize('provider', ['cpu', 'coreml'])
@pytest.mark.parametrize('mutation', ['oldwidth', 'oldprofile', 'unknown', 'missing', 'duplicate', 'overlap', 'resetcount', 'newengineoldproof'])
def test_source_sequence_rejects_forged_cached_execution(provider, mutation):
    first = record(provider); second = reused(first)
    child = second['sessions']['rec']['shape_sessions'][0]
    if mutation == 'oldwidth':
        child['input_shape'][-1] = 640; child['free_dimension_overrides']['DynamicDimension.1'] = 640
    elif mutation == 'oldprofile': child['profile_started']['monotonic_ns'] += 1
    elif mutation == 'unknown':
        child['shape_session_id'] = 10; second['sessions']['rec']['cached_shape_session_ids'] = [10]
    elif mutation == 'missing': second['request_id'] = 3
    elif mutation == 'duplicate': second = first
    elif mutation == 'overlap': second['started'] = clock(25)
    elif mutation == 'resetcount': second['sessions']['rec']['session_run_count'] = 1
    elif mutation == 'newengineoldproof': second.update(engine_id=2, request_id=1)
    with pytest.raises(ValueError): impl.validate_python_coreml_records([first, second])


@pytest.mark.parametrize('provider', ['cpu', 'coreml'])
def test_blank_recognition_does_not_invent_execution(provider):
    first = record(provider)
    first['result_empty'] = True; first['sessions']['rec'] = None
    assert impl.validate_python_coreml_records([first]) == {'PP-OCRv6_small_det'}
    # Later blank keeps a known existing rec cache, with zero Runs this request.
    first = record(provider); second = reused(first); second['result_empty'] = True
    second['sessions']['rec'].update(status='not_run', request_run_count=0, session_run_count=1, shape_sessions=[])
    assert impl.validate_python_coreml_records([first, second]) == {'PP-OCRv6_small_det', 'PP-OCRv6_small_rec'}


@pytest.fixture
def fake_runtime(monkeypatch):
    state = {'sessions': [], 'providers': [impl.COREML, impl.CPU], 'fallback': False, 'zero': False}
    class Options:
        def __init__(self): self.overrides = {}; self.enable_profiling = False
        def add_free_dimension_override_by_name(self, name, value): self.overrides[name] = value
    class Session:
        def __init__(self, path, *, sess_options, providers):
            self.options, self.providers, self.path = sess_options, providers, path
            self.disabled = False; self.calls = 0; state['sessions'].append(self)
        def disable_fallback(self): self.disabled = True
        def get_providers(self): return [impl.CPU] if state['fallback'] else state['providers']
        def get_inputs(self):
            v = self.options.overrides
            shape = [v['DynamicDimension.0'], 3, v['DynamicDimension.1'], v['DynamicDimension.2']] if len(v) == 3 else [v['DynamicDimension.0'], 3, 48, v['DynamicDimension.1']]
            return [SimpleNamespace(name='x', type='tensor(float)', shape=shape)]
        def run(self, _, inputs):
            self.calls += 1
            return [np.zeros((1, 1), dtype=np.float32)]
        def end_profiling(self):
            path = Path(self.options.profile_file_prefix + '.json')
            events = [{'cat': 'Node', 'name': 'partition_kernel_time', 'args': {'provider': impl.CPU if state['zero'] else impl.COREML}}]
            path.write_text(json.dumps(events)); return str(path)
    runtime = SimpleNamespace(SessionOptions=Options, InferenceSession=Session,
        ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL=1), GraphOptimizationLevel=SimpleNamespace(ORT_ENABLE_ALL=99),
        get_available_providers=lambda: state['providers'])
    monkeypatch.setattr(impl, '_load_runtime', lambda: (runtime, {}))
    return state


def test_python_session_exact_bindings_provider_precision_and_no_extra_run(fake_runtime):
    session = impl._ShapeSession(Path('model.onnx'), 'rec', 'coreml', [2, 3, 48, 640], 6, 2)
    underlying = fake_runtime['sessions'][0]
    assert underlying.disabled and underlying.providers == [(impl.COREML, impl.PROVIDER_OPTIONS)]
    assert underlying.options.overrides == {'DynamicDimension.0': 2, 'DynamicDimension.1': 640}
    data = np.zeros((2, 3, 48, 640), np.float32)
    session.run(data); session.run(data)
    assert underlying.calls == 2 and session.evidence()['request_run_count'] == 2
    assert session.evidence()['evidence_reused'] is False
    session.request_runs = 0; session.run(data)
    assert session.evidence()['evidence_reused'] is True
    session.close()


@pytest.mark.parametrize('failure', ['unavailable', 'fallback', 'zero', 'dtype', 'shape'])
def test_coreml_cannot_silently_run_cpu_or_change_input(fake_runtime, failure):
    if failure == 'unavailable': fake_runtime['providers'] = [impl.CPU]
    if failure == 'fallback': fake_runtime['fallback'] = True
    if failure == 'zero': fake_runtime['zero'] = True
    with pytest.raises((ValueError, RuntimeError)):
        session = impl._ShapeSession(Path('model.onnx'), 'det', 'coreml', [1, 3, 64, 320], 6, 2)
        data = np.zeros((1, 3, 64, 320 if failure != 'shape' else 640), np.float64 if failure == 'dtype' else np.float32)
        session.run(data)


@pytest.mark.parametrize('role,capacity', [('det', 2), ('rec', 4)])
def test_dispatcher_bounded_lru_cannot_revive_evicted_identity(fake_runtime, role, capacity):
    dispatcher = impl._ShapeDispatcher(Path(f'PP-OCRv6_small_{role}_infer.onnx'), role, 'cpu', 6, 2)
    shape = lambda width: np.zeros((1, 3, 64 if role == 'det' else 48, width), np.float32)
    for n in range(capacity + 1): dispatcher.run(shape(320 + n * 32))
    old = dispatcher.evidence()['shape_sessions'][0]['shape_session_id']
    assert len(dispatcher.cache) == capacity
    dispatcher.begin(); dispatcher.run(shape(320))
    new = dispatcher.evidence()['shape_sessions'][0]['shape_session_id']
    assert new > old and not dispatcher.evidence()['shape_sessions'][0]['evidence_reused']
    dispatcher.close()


def test_safe_emitter_default_external_new_name_only(tmp_path):
    data = record('cpu'); directory = tmp_path / '中文 & !'; directory.mkdir()
    if os.name != 'posix':
        # Windows is not a Python-CoreML runtime target. Exercise its real
        # refusal rather than skip this safety test or invent an unsafe writer.
        marker = directory / 'existing-user-file'; marker.write_bytes(b'preserve me')
        before = {path.name: path.read_bytes() for path in directory.iterdir()}
        with pytest.raises(RuntimeError, match='secure POSIX directory-relative I/O'):
            impl._emit_source_record(data, directory)
        assert {path.name: path.read_bytes() for path in directory.iterdir()} == before
        assert not list(directory.glob('python-ocr-*.json'))
        return
    path = impl._emit_source_record(data, directory)
    assert path.name == 'python-ocr-123-1-1.json' and json.loads(path.read_bytes()) == data
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError): impl._emit_source_record(data, directory)
    alias = tmp_path / 'alias'; alias.symlink_to(directory, target_is_directory=True)
    with pytest.raises(ValueError): impl._emit_source_record(data, alias)
    data['engine_id'] = '../evil'
    with pytest.raises(ValueError): impl._emit_source_record(data, directory)


@pytest.mark.parametrize('missing', ['platform', 'O_DIRECTORY', 'O_NOFOLLOW', 'dir_fd'])
def test_source_evidence_missing_secure_io_refuses_before_any_filesystem_write(tmp_path, monkeypatch, missing):
    data = record('cpu')
    directory = tmp_path / 'untouched'; directory.mkdir()
    (directory / 'existing').write_bytes(b'keep exact bytes')
    def no_open(*args, **kwargs):
        pytest.fail('Unsupported platform must not open a directory/file')
    capabilities = dict(name='posix', O_DIRECTORY=1, O_NOFOLLOW=2, open=no_open, supports_dir_fd={no_open})
    if missing == 'platform': capabilities['name'] = 'nt'
    elif missing == 'dir_fd': capabilities['supports_dir_fd'] = set()
    else: del capabilities[missing]
    with monkeypatch.context() as patch:
        patch.setattr(impl, 'os', SimpleNamespace(**capabilities))
        with pytest.raises(RuntimeError, match='no unsafe platform fallback'):
            impl._emit_source_record(data, directory)
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == {'existing': b'keep exact bytes'}


def test_python_coreml_runtime_support_does_not_advertise_windows():
    # Other Windows source backends remain supported; only this optional
    # Python ORT/CoreML backend and its diagnostic emitter are scoped here.
    pins = json.loads(Path(impl.__file__).with_name('python_coreml_runtime.json').read_bytes())
    assert set(pins['wheels']) == {'darwin-arm64-cp312', 'linux-x86_64-cp312'}


def test_telemetry_must_precede_import_and_conflicting_packages_fail(monkeypatch):
    monkeypatch.setattr(impl, '_RUNTIME', None)
    monkeypatch.setitem(sys.modules, 'onnxruntime', SimpleNamespace())
    monkeypatch.delenv('ORT_DISABLE_TELEMETRY', raising=False)
    with pytest.raises(RuntimeError, match='before telemetry'): impl._load_runtime()
    monkeypatch.delitem(sys.modules, 'onnxruntime')
    monkeypatch.setattr(impl.importlib.metadata, 'distributions', lambda: [
        SimpleNamespace(metadata={'Name': 'onnxruntime-directml'}, version='1.20.1')])
    with pytest.raises(RuntimeError, match='separate environment'): impl._load_runtime()
    assert os.environ['ORT_DISABLE_TELEMETRY'] == '1'


def test_source_backend_routes_are_lazy_explicit_and_keep_same_vsf(monkeypatch):
    from backend.tools.native_ocr import selected_backend
    from backend.tools.hardware_accelerator import HardwareAccelerator
    from backend.tools.ocr import OcrRecogniser
    from backend.tools.subtitle_detect import SubtitleDetect
    from backend.main import use_detection_frame_extraction
    monkeypatch.delenv('VSE_OCR_BACKEND', raising=False)
    assert selected_backend() == 'paddle'
    monkeypatch.setenv('VSE_OCR_BACKEND', 'python-coreml')
    def forbidden(*args): raise AssertionError('Must not import/probe Paddle/native or CoreML for explicit CPU')
    monkeypatch.setattr(HardwareAccelerator, 'check_paddle', forbidden)
    monkeypatch.setattr(impl, 'python_coreml_capabilities', forbidden)
    hardware = HardwareAccelerator(); hardware.initialize(); hardware.set_enabled(False)
    assert not hardware.has_accelerator() and hardware.accelerator_name == 'CPU'
    assert all(not use_detection_frame_extraction(hardware, mode) for mode in ('fast', 'accurate', 'auto'))
    calls = []
    def implementation(config, *args, **kwargs):
        calls.append((config, args, kwargs)); return object()
    monkeypatch.setattr(impl, 'PythonCoreMlOcr', implementation)
    OcrRecogniser('en', 'fast', 6, hardware).init_model(); SubtitleDetect('en', 'accurate', hardware)
    assert len(calls) == 2 and calls[0][0].TIER == 'small' and calls[1][0].TIER == 'medium'
    assert calls[1][2]['detection_only'] is True
    hardware.set_enabled(True)
    # Native/Python CoreML use the same VSF algorithm without probing hardware.
    assert not use_detection_frame_extraction(hardware, 'accurate')
    with pytest.raises(AssertionError): hardware.has_coreml()


def test_source_requirements_pin_complete_official_wheels_before_import():
    from packaging.requirements import Requirement
    from urllib.parse import urlsplit
    root = Path(impl.__file__).resolve().parents[2]
    requirements = [Requirement(line) for line in (root / 'requirements-coreml-inference.txt').read_text().splitlines()
                    if line and not line.startswith('#')]
    rows = [row for row in requirements if row.name == 'onnxruntime']
    pins = json.loads(Path(impl.__file__).with_name('python_coreml_runtime.json').read_bytes())['wheels']
    assert len(rows) == 2
    for key, pin in pins.items():
        row = next(row for row in rows if pin['filename'] in row.url)
        parsed = urlsplit(row.url)
        assert parsed.scheme == 'https' and parsed.netloc == 'files.pythonhosted.org'
        assert parsed.fragment == 'sha256=' + pin['sha256']
        assert 'python_version == "3.12"' in str(row.marker)
    assert {'pyclipper', 'pyside6-essentials', 'numpy'} <= {row.name for row in requirements}
