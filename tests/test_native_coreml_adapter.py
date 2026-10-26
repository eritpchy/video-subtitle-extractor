"""Synthetic native-protocol tests; never macOS/CoreML/GPU execution evidence."""
import builtins
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import numpy as np
import pytest

from backend.tools import native_ocr as adapter
from backend.tools.hardware_accelerator import HardwareAccelerator
from backend.tools.paddle_model_config import PaddleModelConfig
from tests.test_native_ocr import native_root


@pytest.fixture
def coreml_host(monkeypatch):
    # Do not lie about the real Python/Qt host. Only exercise adapter selection
    # against a fake worker protocol; this never loads an Apple GPU backend.
    monkeypatch.setattr(adapter, 'sys', SimpleNamespace(platform='darwin', modules=sys.modules))
    monkeypatch.setenv('VSE_OCR_BACKEND', 'native')
    monkeypatch.setenv('VSE_NATIVE_OCR_PROVIDER', 'coreml')


def evidence_response(provider='coreml', *, operation='ocr', blank=False):
    request = {'provider': provider, 'tier': 'small', 'rec_model': 'PP-OCRv6_small_rec', 'operation': operation}
    coreml = provider == 'coreml'
    actual = 'CoreMLExecutionProvider' if coreml else 'CPUExecutionProvider'
    def session(model, run=True):
        return {'model': model, 'status': 'executed' if run else 'not_run',
                'source': 'ort_first_run_profile' if coreml else 'explicit_cpu_session',
                'coreml_ep_executed': coreml and run, 'cpu_ep_executed': run,
                'coreml_kernel_events': 3 if coreml and run else 0, 'cpu_kernel_events': 1 if coreml and run else 0,
                'observed_providers': (['CoreMLExecutionProvider', 'CPUExecutionProvider'] if coreml else ['CPUExecutionProvider']) if run else [],
                'gpu_execution_verified': False}
    evidence = {'schema_version': 1, 'requested_provider': provider, 'actual_provider': actual,
                'compute_units': 'CPUAndGPU' if coreml else 'CPUOnly', 'ane_allowed': False,
                'gpu_execution_verified': False, 'coreml_ep_executed': coreml, 'cpu_fallback_observed': coreml,
                'sessions': {'det': session('PP-OCRv6_small_det'),
                             'rec': None if operation == 'detect' else session(request['rec_model'], not blank)}}
    response = {'requested_provider': provider, 'actual_provider': actual, 'execution_evidence': evidence,
                'result': [[], []] if blank else [[[[0, 0], [2, 0], [2, 2], [0, 2]]], [['x', .9]]]}
    return request, response


def test_provider_selection_is_explicit_and_cpu_override_preserves_package_default(coreml_host):
    assert adapter.selected_backend() == 'native'
    assert adapter.selected_native_provider() == 'coreml'
    assert adapter.selected_native_provider(enabled=False) == 'cpu'
    assert os.environ['VSE_NATIVE_OCR_PROVIDER'] == 'coreml'


def test_non_macos_coreml_is_an_error_but_explicit_cpu_works(monkeypatch):
    monkeypatch.setattr(adapter, 'sys', SimpleNamespace(platform='linux', modules=sys.modules))
    monkeypatch.setenv('VSE_OCR_BACKEND', 'native'); monkeypatch.setenv('VSE_NATIVE_OCR_PROVIDER', 'coreml')
    with pytest.raises(ValueError, match='requires macOS'): adapter.selected_native_provider()
    assert adapter.selected_native_provider(enabled=False) == 'cpu'
    hardware = HardwareAccelerator(); hardware.initialize(); hardware.set_enabled(False)
    assert not hardware.has_accelerator() and hardware.accelerator_name == 'CPU'


def test_native_capability_probe_is_bounded_and_disables_telemetry(native_root, coreml_host, monkeypatch):
    monkeypatch.setenv('ORT_DISABLE_TELEMETRY', '0')
    monkeypatch.setenv('NATIVE_TEST_CAPABILITY', 'fragmented')
    result = adapter.native_capabilities(native_root)
    assert result['coreml']['available'] is True
    assert result['coreml']['gpu_execution_verified'] is False
    assert result['coreml']['ane_allowed'] is False
    assert os.environ['ORT_DISABLE_TELEMETRY'] == '0', 'Only child telemetry environment changes'


@pytest.mark.parametrize('inherited', ['', '0', '1', 'unexpected'])
def test_normal_worker_and_capability_probe_remove_inherited_metal_capture(native_root, coreml_host, monkeypatch, inherited):
    monkeypatch.setenv('MTL_CAPTURE_ENABLED', inherited)
    launch = adapter.subprocess.Popen
    commands = []
    def checked_launch(command, **kwargs):
        assert 'MTL_CAPTURE_ENABLED' not in kwargs['env']
        assert kwargs['env']['ORT_DISABLE_TELEMETRY'] == '1'
        commands.append(command)
        return launch(command, **kwargs)
    monkeypatch.setattr(adapter.subprocess, 'Popen', checked_launch)
    adapter.native_capabilities(native_root)
    with adapter.NativeOcr(PaddleModelConfig(HardwareAccelerator(), 'ch', 'fast')) as native:
        list(native.predict_iter(np.zeros((32, 64, 3), np.uint8)))
    assert len(commands) == 3 and sum('--capabilities' in command for command in commands) == 2
    assert not any(any('--diagnostic' in value for value in command) for command in commands)
    assert os.environ['MTL_CAPTURE_ENABLED'] == inherited


@pytest.mark.parametrize('behavior,match', [('error', 'probe failed'), ('oversize', 'bounded JSON'), ('bad_json', None), ('gpu_claim', 'GPU execution')])
def test_bad_capability_response_fails_closed(native_root, coreml_host, monkeypatch, behavior, match):
    monkeypatch.setenv('NATIVE_TEST_CAPABILITY', behavior)
    with pytest.raises((RuntimeError, ValueError), match=match): adapter.native_capabilities(native_root)


def test_capability_timeout_reaps_process(native_root, coreml_host, monkeypatch):
    monkeypatch.setenv('NATIVE_TEST_CAPABILITY', 'hang')
    started = time.monotonic()
    with pytest.raises(TimeoutError, match='timed out'): adapter.native_capabilities(native_root, timeout=.1)
    assert time.monotonic() - started < 5


def test_missing_ep_never_becomes_cpu(native_root, coreml_host, monkeypatch):
    monkeypatch.setenv('NATIVE_TEST_CAPABILITY', 'cpu_only')
    hardware = HardwareAccelerator(); hardware.initialize()
    with pytest.raises(RuntimeError, match='automatic CPU fallback is disabled'): hardware.has_accelerator()
    with adapter.NativeOcr(PaddleModelConfig(hardware, 'ch', 'accurate')) as native:
        with pytest.raises(RuntimeError, match='automatic CPU fallback is disabled'):
            list(native.predict_iter(np.zeros((32, 64, 3), np.uint8)))
        assert native._process is None and native.execution_evidence is None


def test_native_hardware_never_imports_python_ort_or_paddle(native_root, coreml_host, monkeypatch):
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name.split('.')[0] in {'onnxruntime', 'paddle', 'paddlex', 'paddleocr'}:
            raise AssertionError('Native profile imported ' + name)
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guarded)
    hardware = HardwareAccelerator(); hardware.initialize()
    assert hardware.has_coreml() and hardware.has_accelerator()
    assert not hardware.has_cuda()
    assert hardware.native_provider == 'coreml'
    assert hardware.onnx_providers == ['CPUExecutionProvider', 'CoreMLExecutionProvider']
    assert 'requested' in hardware.accelerator_name and 'not verified' in hardware.accelerator_name
    hardware.set_enabled(False)
    assert hardware.native_provider == 'cpu' and hardware.accelerator_name == 'CPU'
    assert not hardware.has_accelerator() and hardware.onnx_providers == []


def test_real_subprocess_protocol_selects_coreml_and_retains_honest_partition_evidence(native_root, coreml_host):
    hardware = HardwareAccelerator(); hardware.initialize()
    with adapter.NativeOcr(PaddleModelConfig(hardware, 'ch', 'accurate')) as native:
        result = list(native.predict_iter(np.zeros((32, 64, 3), np.uint8)))
        assert result[0]['rec_texts'] == ['你好']
        evidence = native.execution_evidence
        assert evidence['requested_provider'] == 'coreml' and evidence['actual_provider'] == 'CoreMLExecutionProvider'
        assert evidence['cpu_fallback_observed'] is True, 'CPU graph partitions are not hidden'
        assert evidence['gpu_execution_verified'] is False
        assert evidence['sessions']['det']['model'] == 'PP-OCRv6_medium_det'
        assert evidence['sessions']['rec']['model'] == 'PP-OCRv6_medium_rec'


@pytest.mark.parametrize('provider', ['cpu', 'coreml'])
@pytest.mark.parametrize('operation', ['ocr', 'detect'])
def test_last_request_evidence_binds_exact_encoded_png_worker_and_response(native_root, coreml_host, provider, operation):
    import cv2
    hardware = HardwareAccelerator(); hardware.initialize(); hardware.set_enabled(provider == 'coreml')
    image = np.arange(32 * 64 * 3, dtype=np.uint8).reshape((32, 64, 3))
    _, png = cv2.imencode('.png', image)
    with adapter.NativeOcr(PaddleModelConfig(hardware, 'ch', 'fast'), detection_only=operation == 'detect') as native:
        def predict():
            return native.predict(image) if operation == 'detect' else list(native.predict_iter(image))
        predict()
        first = native.last_request_evidence
        assert first['request']['image_sha256'] == hashlib.sha256(png.tobytes()).hexdigest()
        assert first['request']['image_bytes'] == png.nbytes and first['request']['image_format'] == 'png'
        assert 'path' not in first['request']
        assert first['request']['id'] == first['response']['id'] == 1
        assert first['request']['operation'] == operation and first['request']['provider'] == provider
        assert first['worker_sha256'] == hashlib.sha256(native.executable.read_bytes()).hexdigest()
        assert first['worker_pid'] == native._process.pid == first['response']['execution_window']['worker_pid']
        assert first['response']['execution_window'] == native.execution_window
        assert adapter.validate_execution_evidence(first['response'], first['request'], worker_pid=first['worker_pid']) == native.execution_evidence
        # Published views cannot mutate the raw response retained for an audit.
        native.execution_evidence['sessions']['det']['model'] = 'altered caller view'
        assert first['response']['execution_evidence']['sessions']['det']['model'] == 'PP-OCRv6_small_det'
        predict()
        assert native.last_request_evidence['request']['id'] == 2 and first['request']['id'] == 1
        assert first['worker_pid'] == native.last_request_evidence['worker_pid']
        with pytest.raises(ValueError):
            native.predict(np.zeros((2, 2), np.float64))
        assert native.last_request_evidence is None and native.execution_evidence is None
    assert native.last_request_evidence is None


@pytest.mark.parametrize('behavior', ['wrong_provider', 'gpu_claim', 'no_coreml', 'wrong_model', 'no_evidence', 'error'])
def test_bad_execution_is_reaped_without_retry_on_cpu(native_root, coreml_host, monkeypatch, tmp_path, behavior):
    log = tmp_path / 'requests.jsonl'; monkeypatch.setenv('NATIVE_TEST_REQUEST_LOG', str(log))
    monkeypatch.setenv('NATIVE_TEST_BEHAVIOR', behavior)
    with adapter.NativeOcr(PaddleModelConfig(HardwareAccelerator(), 'ch', 'fast')) as native:
        with pytest.raises((ValueError, RuntimeError)):
            list(native.predict_iter(np.zeros((32, 64, 3), np.uint8)))
        assert native._process is None and native.execution_evidence is None
        assert native.last_request_evidence is None
        assert [json.loads(row)['provider'] for row in log.read_text().splitlines()] == ['coreml']


def test_explicit_cpu_uses_same_worker_when_coreml_unavailable(native_root, coreml_host, monkeypatch):
    monkeypatch.setenv('NATIVE_TEST_CAPABILITY', 'error')  # CPU must not probe CoreML.
    hardware = HardwareAccelerator(); hardware.initialize(); hardware.set_enabled(False)
    with adapter.NativeOcr(PaddleModelConfig(hardware, 'ch', 'accurate')) as native:
        assert native.provider == 'cpu'
        list(native.predict_iter(np.zeros((32, 64, 3), np.uint8)))
        assert native.execution_evidence['actual_provider'] == 'CPUExecutionProvider'
        assert native.execution_evidence['cpu_fallback_observed'] is False
        assert native.execution_evidence['sessions']['rec']['model'] == 'PP-OCRv6_medium_rec'
    assert os.environ['VSE_NATIVE_OCR_PROVIDER'] == 'coreml'


def test_empty_ocr_and_detection_only_do_not_fabricate_recognition(native_root, coreml_host, monkeypatch):
    monkeypatch.setenv('NATIVE_TEST_BEHAVIOR', 'empty')
    config = PaddleModelConfig(HardwareAccelerator(), 'ch', 'fast')
    with adapter.NativeOcr(config) as native:
        assert list(native.predict_iter(np.zeros((32, 64, 3), np.uint8)))[0]['rec_texts'] == []
        assert native.execution_evidence['sessions']['rec']['status'] == 'not_run'
    with adapter.NativeOcr(config, detection_only=True) as native:
        native.predict(np.zeros((32, 64, 3), np.uint8))
        assert native.execution_evidence['sessions']['rec'] is None


@pytest.mark.parametrize('field,value', [('schema_version', True), ('compute_units', 'ALL'), ('ane_allowed', True),
    ('gpu_execution_verified', True), ('coreml_ep_executed', False), ('cpu_fallback_observed', False), ('actual_provider', 'CPUExecutionProvider')])
def test_evidence_metadata_and_aggregate_flags_are_strict(field, value):
    request, response = evidence_response(); response['execution_evidence'][field] = value
    with pytest.raises(ValueError): adapter.validate_execution_evidence(response, request)


@pytest.mark.parametrize('field,value', [('model', 'wrong'), ('status', 'not_run'), ('source', 'configuration'),
    ('coreml_kernel_events', 0), ('coreml_kernel_events', True), ('cpu_kernel_events', -1),
    ('gpu_execution_verified', True), ('cpu_ep_executed', False), ('observed_providers', ['CoreMLExecutionProvider'])])
def test_session_evidence_cannot_substitute_config_for_execution(field, value):
    request, response = evidence_response(); response['execution_evidence']['sessions']['det'][field] = value
    with pytest.raises(ValueError): adapter.validate_execution_evidence(response, request)


def test_first_run_profile_extensions_do_not_become_current_gpu_proof():
    request, response = evidence_response()
    response['execution_evidence']['sessions']['det'].update(capture='first_successful_run', session_run_count=10, request_run_count=1, evidence_reused=True)
    response['execution_window'] = {'worker_pid': 123, 'utc_start_ns': 100, 'utc_end_ns': 200, 'monotonic_start_ns': 1000, 'monotonic_end_ns': 1100}
    result = adapter.validate_execution_evidence(response, request)
    assert result['sessions']['det']['evidence_reused'] is True and result['gpu_execution_verified'] is False


@pytest.mark.parametrize('field,value', [('worker_pid', 999), ('utc_end_ns', 99), ('monotonic_start_ns', True)])
def test_execution_window_cannot_claim_another_worker_or_invalid_time(field, value):
    request, response = evidence_response()
    response['execution_window'] = {'worker_pid': 123, 'utc_start_ns': 100, 'utc_end_ns': 200,
                                    'monotonic_start_ns': 1000, 'monotonic_end_ns': 1100}
    response['execution_window'][field] = value
    with pytest.raises(ValueError):
        adapter.validate_execution_evidence(response, request, worker_pid=123)


def test_cached_then_blank_recognition_does_not_claim_reused_execution():
    request, response = evidence_response(blank=True)
    response['execution_evidence']['sessions']['rec'].update(
        capture='first_successful_run', session_run_count=10, request_run_count=0, evidence_reused=False)
    result = adapter.validate_execution_evidence(response, request)
    assert result['sessions']['rec']['status'] == 'not_run'
    response['execution_evidence']['sessions']['rec']['evidence_reused'] = True
    with pytest.raises(ValueError, match='mislabeled'):
        adapter.validate_execution_evidence(response, request)


@pytest.mark.parametrize('operation', ['ocr', 'detect'])
def test_opt_in_evidence_files_are_actual_successful_request_records(native_root, coreml_host, monkeypatch, tmp_path, operation):
    directory = tmp_path / 'acceptance evidence'; directory.mkdir()
    monkeypatch.setenv('VSE_NATIVE_EVIDENCE_DIR', str(directory))
    with adapter.NativeOcr(PaddleModelConfig(HardwareAccelerator(), 'ch', 'fast'), detection_only=operation == 'detect') as native:
        for request_id in (1, 2):
            image = np.zeros((32, 64, 3), np.uint8)
            native.predict(image) if operation == 'detect' else list(native.predict_iter(image))
            record = directory / f'native-ocr-{native._process.pid}-{request_id}.json'
            assert json.loads(record.read_text()) == native.last_request_evidence
    assert len(list(directory.iterdir())) == 2


def test_evidence_emission_is_disabled_by_default(native_root, coreml_host, monkeypatch):
    monkeypatch.delenv('VSE_NATIVE_EVIDENCE_DIR', raising=False)
    monkeypatch.setattr(adapter, 'emit_request_evidence', lambda *args: pytest.fail('Unexpected diagnostic write'))
    with adapter.NativeOcr(PaddleModelConfig(HardwareAccelerator(), 'ch', 'fast')) as native:
        list(native.predict_iter(np.zeros((32, 64, 3), np.uint8)))


def test_evidence_creation_never_overwrites_existing_or_symlink(native_root, coreml_host, tmp_path):
    document = {'worker_pid': 10, 'request': {'id': 1}}
    first = adapter.emit_request_evidence(document, tmp_path)
    with pytest.raises(FileExistsError): adapter.emit_request_evidence(document, tmp_path)
    assert json.loads(first.read_text()) == document
    target = tmp_path / 'user-data'; target.write_text('keep')
    link = tmp_path / 'native-ocr-10-2.json'; link.symlink_to(target)
    with pytest.raises(FileExistsError): adapter.emit_request_evidence({'worker_pid': 10, 'request': {'id': 2}}, tmp_path)
    assert target.read_text() == 'keep'
    outside = tmp_path / 'directory-link'; outside.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink/reparse'):
        adapter.emit_request_evidence(document, outside)
    with pytest.raises(FileNotFoundError): adapter.emit_request_evidence(document, tmp_path / 'missing')
    with pytest.raises(ValueError): adapter.emit_request_evidence(document, '')
    with pytest.raises(ValueError): adapter.emit_request_evidence(document, tmp_path / '..')


def test_evidence_write_failure_is_fatal_not_an_unrecorded_success(native_root, coreml_host, monkeypatch, tmp_path):
    monkeypatch.setenv('VSE_NATIVE_EVIDENCE_DIR', str(tmp_path / 'missing'))
    with adapter.NativeOcr(PaddleModelConfig(HardwareAccelerator(), 'ch', 'fast')) as native:
        with pytest.raises(FileNotFoundError): list(native.predict_iter(np.zeros((32, 64, 3), np.uint8)))
        assert native._process is None and native.last_request_evidence is None


def test_evidence_document_size_and_identity_are_bounded(tmp_path):
    for document in ({}, {'worker_pid': True, 'request': {'id': 1}}, {'worker_pid': 10, 'request': {'id': -1}}):
        with pytest.raises(ValueError): adapter.emit_request_evidence(document, tmp_path)
    with pytest.raises(ValueError, match='bounded'):
        adapter.emit_request_evidence({'worker_pid': 10, 'request': {'id': 1}, 'oversize': 'x' * (17 * 1024 * 1024)}, tmp_path)
    assert not list(tmp_path.iterdir())


def test_all_language_and_mode_model_selection_is_identical_between_coreml_and_cpu(coreml_host):
    hardware = HardwareAccelerator(); hardware.initialize()
    names = set()
    for language in PaddleModelConfig.MULTI_LANG:
        for mode in ('fast', 'accurate', 'auto'):
            hardware.set_enabled(True); accelerated = PaddleModelConfig(hardware, language, mode)
            hardware.set_enabled(False); cpu = PaddleModelConfig(hardware, language, mode)
            assert (accelerated.TIER, accelerated.DET_MODEL_NAME, accelerated.REC_MODEL_NAME) == (cpu.TIER, cpu.DET_MODEL_NAME, cpu.REC_MODEL_NAME)
            names.update((accelerated.DET_MODEL_NAME, accelerated.REC_MODEL_NAME))
    assert len(names) == 15


def test_accurate_native_coreml_keeps_cpu_vsf_route(coreml_host, monkeypatch):
    from backend.main import use_detection_frame_extraction
    hardware = SimpleNamespace(has_accelerator=lambda: True)
    for mode in ('accurate', 'fast', 'auto'):
        assert not use_detection_frame_extraction(hardware, mode)
    monkeypatch.setenv('VSE_NATIVE_OCR_PROVIDER', 'cpu')
    assert not use_detection_frame_extraction(hardware, 'accurate')
    for backend in ('paddle', 'directml'):
        monkeypatch.setenv('VSE_OCR_BACKEND', backend)
        assert use_detection_frame_extraction(hardware, 'accurate')
        assert not use_detection_frame_extraction(hardware, 'fast')


def test_coreml_gui_control_is_real_switch_and_preserves_saved_choice(coreml_host, monkeypatch):
    from PySide6.QtWidgets import QApplication, QWidget
    from qfluentwidgets import qconfig
    from backend.config import config
    from ui.setting_interface import SettingInterface
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(qconfig, 'save', lambda: None)  # Never change real user preferences in a UI test.
    before = config.hardwareAcceleration.value
    parent = QWidget(); settings = SettingInterface(parent)
    try:
        assert settings.hardware_acceleration.switchButton.isEnabled()
        assert settings.hardware_acceleration.switchButton.isChecked() == before
        assert config.hardwareAcceleration.value == before
        assert 'CoreML (CPU + GPU' in settings.hardware_acceleration.contentLabel.text()
        assert 'automatic CPU fallback' in settings.hardware_acceleration.contentLabel.text()
        for enabled in (False, True, False):
            settings.hardware_acceleration.setChecked(enabled)
            assert config.hardwareAcceleration.value is enabled
            assert adapter.selected_native_provider(enabled=enabled) == ('coreml' if enabled else 'cpu')
    finally:
        settings.hardware_acceleration.setChecked(before)
        parent.deleteLater(); app.processEvents()


def test_cli_help_explains_explicit_cpu_without_changing_provider(coreml_host):
    from backend.cli import build_parser
    parser = build_parser(); args = parser.parse_args(['input.mp4', '--cpu'])
    assert args.cpu and 'CoreML' in parser.format_help()
    assert adapter.configured_native_provider() == 'coreml'


@pytest.mark.parametrize('mode', ['fast', 'auto', 'accurate'])
def test_opt_in_actual_native_cpu_adapter_evidence(mode, monkeypatch, tmp_path):
    """Real pinned ORT CPU integration when supplied; never Mac/GPU proof."""
    value = os.environ.get('VSE_NATIVE_PROVIDER_ARTIFACT')
    if not value:
        pytest.skip('set VSE_NATIVE_PROVIDER_ARTIFACT for actual native ORT integration')
    import cv2
    root = Path(value).resolve(strict=True)
    executable = root / ('native-ocr.exe' if os.name == 'nt' else 'native-ocr')
    monkeypatch.setenv('VSE_OCR_BACKEND', 'native')
    monkeypatch.setenv('VSE_NATIVE_OCR_PROVIDER', 'cpu')
    monkeypatch.setenv('VSE_NATIVE_OCR_ROOT', str(root))
    directory = tmp_path.resolve()
    monkeypatch.setenv('VSE_NATIVE_EVIDENCE_DIR', str(directory))
    fixtures = Path(__file__).resolve().parents[1] / 'native/ocr/tests/evidence/fixtures'
    image = cv2.imread(str(fixtures / 'english.png'))
    blank = cv2.imread(str(fixtures / 'blank.png'))
    hardware = HardwareAccelerator(); hardware.initialize(); hardware.set_enabled(False)
    config = PaddleModelConfig(hardware, 'en', mode)
    with adapter.NativeOcr(config) as native:
        first = list(native.predict_iter(image))
        assert first[0]['rec_texts']
        assert list(native.predict_iter(image)) == first
        assert native.execution_evidence['sessions']['det']['session_run_count'] == 2
        assert list(native.predict_iter(blank))[0]['rec_texts'] == []
        rec = native.execution_evidence['sessions']['rec']
        assert rec['status'] == 'not_run' and not rec['evidence_reused'] and rec['session_run_count'] > 0
        assert rec['request_run_count'] == 0 and rec['observed_providers'] == []
    with adapter.NativeOcr(config, detection_only=True) as native:
        assert native.predict(image)[0]['dt_polys']
        assert native.execution_evidence['sessions']['rec'] is None
    records = list(directory.glob('native-ocr-*.json'))
    assert len(records) == 4
    for path in records:
        record = json.loads(path.read_text())
        assert record['worker_sha256'] == hashlib.sha256(executable.read_bytes()).hexdigest()
        evidence = adapter.validate_execution_evidence(record['response'], record['request'], worker_pid=record['worker_pid'])
        assert evidence['actual_provider'] == 'CPUExecutionProvider' and not evidence['gpu_execution_verified']
