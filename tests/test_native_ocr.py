"""Native adapter transport/lifecycle contracts, without native build dependencies."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import numpy as np
import pytest

from backend.tools.hardware_accelerator import HardwareAccelerator
from backend.tools.native_ocr import NativeOcr, selected_backend
from backend.tools.ocr import OcrRecogniser
from backend.tools.paddle_model_config import PaddleModelConfig

WORKER = r'''
import json, os, struct, sys, time
assert os.environ['ORT_DISABLE_TELEMETRY'] == '1'
if '--capabilities' in sys.argv:
 capability=os.environ.get('NATIVE_TEST_CAPABILITY','ok')
 if capability=='hang': time.sleep(60)
 if capability=='oversize': print('x'*70000);sys.exit(0)
 if capability=='error': sys.stderr.write('fixture CoreML unavailable');sys.exit(3)
 if capability=='bad_json': print('{');sys.exit(0)
 available=capability!='cpu_only'
 out={'schema_version':1,'ort_version':'1.31.0','default_request_provider':'cpu',
      'available_providers':['CPUExecutionProvider']+(['CoreMLExecutionProvider'] if available else []),
      'supported_request_providers':['cpu']+(['coreml'] if available else []),
      'coreml':{'available':available,'model_format':'MLProgram','compute_units':'CPUAndGPU','ane_allowed':False,'gpu_execution_verified':False}}
 if capability=='gpu_claim': out['coreml']['gpu_execution_verified']=True
 if capability=='fragmented':
  for ch in json.dumps(out): sys.stdout.write(ch);sys.stdout.flush()
 else: print(json.dumps(out))
 sys.exit(0)
while True:
 h=sys.stdin.buffer.read(4)
 if not h: break
 size,=struct.unpack('<I',h)
 req=json.loads(sys.stdin.buffer.read(size))
 if os.environ.get('NATIVE_TEST_REQUEST_LOG'):
  with open(os.environ['NATIVE_TEST_REQUEST_LOG'],'a',encoding='utf-8') as log: log.write(json.dumps(req)+'\n')
 behavior=os.environ.get('NATIVE_TEST_BEHAVIOR','ok')
 if behavior=='hang': time.sleep(60)
 if behavior=='eof':
  sys.stderr.write('missing native library example');sys.stderr.flush();sys.exit(2)
 if behavior=='flood': sys.stderr.buffer.write(b'x'*1000000);sys.stderr.buffer.flush()
 if behavior=='oversize': sys.stdout.buffer.write(struct.pack('<I',17000000));sys.stdout.buffer.flush();continue
 out={'id':req['id'],'ok':True,'result':[[[[1,2],[20,2],[20,12],[1,12]]],[['你好',.95]]],
      'dt_polys':[[[1,2],[20,3],[19,12],[1,11]]]}
 out.update({key:req[key] for key in ('rec_model','batch_size','cpu_threads')})
 provider=req['provider'];coreml=provider=='coreml';actual='CoreMLExecutionProvider' if coreml else 'CPUExecutionProvider'
 if behavior=='empty': out['result']=[[],[]]
 def session(model,run=True):
  return {'model':model,'status':'executed' if run else 'not_run','source':'ort_first_run_profile' if coreml else 'explicit_cpu_session',
   'coreml_ep_executed':coreml and run,'cpu_ep_executed':run,'coreml_kernel_events':3 if coreml and run else 0,
   'cpu_kernel_events':1 if coreml and run else 0,'observed_providers':(['CoreMLExecutionProvider','CPUExecutionProvider'] if coreml else ['CPUExecutionProvider']) if run else [],'gpu_execution_verified':False}
 sessions={'det':session('PP-OCRv6_'+req['tier']+'_det'),'rec':None if req['operation']=='detect' else session(req['rec_model'],behavior!='empty')}
 evidence={'schema_version':1,'requested_provider':provider,'actual_provider':actual,'compute_units':'CPUAndGPU' if coreml else 'CPUOnly',
  'ane_allowed':False,'gpu_execution_verified':False,'coreml_ep_executed':coreml,'cpu_fallback_observed':coreml,'sessions':sessions}
 out.update(requested_provider=provider,actual_provider=actual,execution_evidence=evidence)
 now=time.time_ns();mono=time.monotonic_ns()
 out['execution_window']={'worker_pid':os.getpid(),'utc_start_ns':now,'utc_end_ns':now,
  'monotonic_start_ns':mono,'monotonic_end_ns':mono,'monotonic_clock':'fixture'}
 if behavior=='wrong_provider': out['actual_provider']='CPUExecutionProvider' if coreml else 'CoreMLExecutionProvider'
 if behavior=='gpu_claim': evidence['gpu_execution_verified']=True
 if behavior=='no_coreml':
  sessions['det'].update(coreml_ep_executed=False,coreml_kernel_events=0,observed_providers=['CPUExecutionProvider'])
 if behavior=='wrong_model': sessions['det']['model']='wrong model'
 if behavior=='no_evidence': out.pop('execution_evidence')
 if behavior=='bad_id': out['id']=-1
 if behavior=='boolean_id': out['id']=True
 if behavior=='float_batch': out['batch_size']=float(req['batch_size'])
 if behavior=='error': out={'id':req['id'],'ok':False,'error':'bad asset'}
 payload=json.dumps(out).encode()
 sys.stdout.buffer.write(struct.pack('<I',len(payload))+payload);sys.stdout.buffer.flush()
'''

@pytest.fixture
def native_root(tmp_path, monkeypatch):
    root = tmp_path / '原生 worker'
    (root / 'models').mkdir(parents=True)
    (root / 'config').mkdir()
    executable = root / ('native-ocr.exe' if os.name == 'nt' else 'native-ocr')
    executable.write_text(WORKER, encoding='utf-8')
    entries = []
    for tier in ('small', 'medium'):
        configuration = {}
        for role in ('det', 'rec'):
            name = f'PP-OCRv6_{tier}_{role}'
            data = name.encode()
            (root / 'models' / f'{name}_infer.onnx').write_bytes(data)
            entries.append({'name': name + '_infer', 'onnx_bytes': len(data),
                            'onnx_sha256': hashlib.sha256(data).hexdigest()})
            configuration[role] = {'Global': {'model_name': name}}
        (root / 'config' / f'{tier}.json').write_text(json.dumps(configuration))
    for entry in entries:
        tier = 'small' if '_small_' in entry['name'] else 'medium'
        entry['config_file'] = f'config/{tier}.json'
        entry['config_sha256'] = hashlib.sha256((root / entry['config_file']).read_bytes()).hexdigest()
    (root / 'model-manifest.json').write_text(json.dumps(entries))
    monkeypatch.setenv('VSE_NATIVE_OCR_ROOT', str(root))
    monkeypatch.setenv('VSE_OCR_BACKEND', 'native')
    monkeypatch.delenv('VSE_NATIVE_OCR_PROVIDER', raising=False)
    monkeypatch.delenv('VSE_NATIVE_OCR_TIMEOUT', raising=False)
    popen = subprocess.Popen
    def launch(command, **kwargs):
        assert kwargs.get('shell', False) is False
        return popen([sys.executable, '-u', *command], **kwargs)
    monkeypatch.setattr('backend.tools.native_ocr.subprocess.Popen', launch)
    return root


def make_native(**kwargs):
    return NativeOcr(PaddleModelConfig(HardwareAccelerator(), 'ch', 'fast'), **kwargs)


def test_opt_in_recognizer_long_lived_unicode_and_detection(native_root):
    recognizer = OcrRecogniser('ch', 'fast', 6, HardwareAccelerator())
    try:
        first = recognizer.predict(np.zeros((32, 64, 3), dtype=np.uint8))
        process = recognizer.recogniser._process
        assert first[1] == [('你好', .95)]
        assert recognizer.predict(np.zeros((32, 64, 3), dtype=np.uint8)) == first
        assert recognizer.recogniser._process is process
        from backend.tools.subtitle_detect import SubtitleDetect
        detector = SubtitleDetect('ch', 'fast', HardwareAccelerator())
        try:
            polygons, _ = detector.detect_subtitle(np.zeros((32, 64, 3), dtype=np.uint8))
            assert polygons.tolist() == [[[1, 2], [20, 3], [19, 12], [1, 11]]]
        finally:
            detector.close()
    finally:
        recognizer.close()
    assert process.poll() is not None


@pytest.mark.parametrize('behavior,match', [('eof','transport'), ('oversize','protocol limit'),
                                         ('bad_id','unaligned'), ('boolean_id','unaligned'),
                                         ('float_batch','honor requested batch_size'), ('error','bad asset')])
def test_errors_reaped_and_next_call_recovers(native_root, monkeypatch, behavior, match):
    native = make_native()
    monkeypatch.setenv('NATIVE_TEST_BEHAVIOR', behavior)
    with pytest.raises((RuntimeError, ValueError), match=match):
        list(native.predict_iter(np.zeros((32, 64, 3), dtype=np.uint8)))
    assert native._process is None
    monkeypatch.setenv('NATIVE_TEST_BEHAVIOR', 'ok')
    try:
        assert list(native.predict_iter(np.zeros((32, 64, 3), dtype=np.uint8)))[0]['rec_texts'] == ['你好']
    finally:
        native.close()


def test_stderr_flood_is_drained_bounded(native_root, monkeypatch):
    monkeypatch.setenv('NATIVE_TEST_BEHAVIOR', 'flood')
    with make_native() as native:
        list(native.predict_iter(np.zeros((32, 64, 3), dtype=np.uint8)))
        assert sum(map(len, native._stderr)) <= 65536


def test_whole_request_deadline_reaps_worker(native_root, monkeypatch):
    monkeypatch.setenv('NATIVE_TEST_BEHAVIOR', 'hang')
    monkeypatch.setenv('VSE_NATIVE_OCR_TIMEOUT', '.15')
    native = make_native()
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        list(native.predict_iter(np.zeros((32, 64, 3), dtype=np.uint8)))
    assert time.monotonic() - started < 5
    assert native._process is None
    native.close()


def test_cancellation_reaps_worker(native_root, monkeypatch):
    monkeypatch.setenv('NATIVE_TEST_BEHAVIOR', 'hang')
    stop = threading.Event()
    native = make_native(stop_event=stop)
    timer = threading.Timer(.15, stop.set)
    timer.start()
    try:
        with pytest.raises(RuntimeError, match='cancelled'):
            list(native.predict_iter(np.zeros((32, 64, 3), dtype=np.uint8)))
        assert native._process is None
    finally:
        timer.join()
        native.close()


def test_close_during_request(native_root, monkeypatch):
    monkeypatch.setenv('NATIVE_TEST_BEHAVIOR', 'hang')
    native = make_native()
    timer = threading.Timer(.15, native.close)
    timer.start()
    try:
        with pytest.raises(RuntimeError):
            list(native.predict_iter(np.zeros((32, 64, 3), dtype=np.uint8)))
    finally:
        timer.join()
    assert native._process is None
    with pytest.raises(RuntimeError, match='closed'):
        list(native.predict_iter(np.zeros((32, 64, 3), dtype=np.uint8)))


def test_no_substitution_or_unsupported_provider(native_root, monkeypatch):
    with pytest.raises(FileNotFoundError, match='no installed config.*korean'):
        NativeOcr(PaddleModelConfig(HardwareAccelerator(), 'korean', 'fast'))
    monkeypatch.setenv('VSE_NATIVE_OCR_PROVIDER', 'cuda')
    with pytest.raises(ValueError, match='Unsupported native OCR provider'):
        selected_backend()
    monkeypatch.setenv('VSE_OCR_BACKEND', 'typo')
    with pytest.raises(ValueError, match='Unsupported VSE_OCR_BACKEND'):
        selected_backend()


def test_missing_or_modified_assets_fail(native_root):
    model = native_root / 'models/PP-OCRv6_small_rec_infer.onnx'
    model.write_bytes(b'changed')
    with pytest.raises(ValueError, match='checksum mismatch'):
        make_native()
    model.unlink()
    with pytest.raises(FileNotFoundError, match='no model substitution'):
        make_native()


@pytest.mark.parametrize('image', [np.zeros((0, 32, 3), np.uint8), np.zeros((32,32,2),np.uint8),
                                   np.zeros((32,32,3),np.float32), np.zeros((1,16385,3),np.uint8)])
def test_invalid_images_do_not_start_worker(native_root, image):
    with make_native() as native:
        with pytest.raises(ValueError):
            list(native.predict_iter(image))
        assert native._process is None


def test_native_hardware_never_imports_paddle(monkeypatch):
    import builtins
    original_import = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name.split('.')[0] in ('paddle', 'paddlex', 'paddleocr', 'onnxruntime'):
            raise AssertionError(f'Native profile imported {name}')
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guarded)
    monkeypatch.setenv('VSE_OCR_BACKEND', 'native')
    hardware = HardwareAccelerator()
    hardware.initialize()
    assert not hardware.has_cuda()
    assert hardware.accelerator_name == 'CPU'


def install_legacy_fixture(root, name):
    data = name.encode()
    (root / 'models' / f'{name}_infer.onnx').write_bytes(data)
    (root / 'config' / f'{name}.json').write_text(json.dumps({'Global': {'model_name': name}}))
    manifest_path = root / 'model-manifest.json'
    entries = json.loads(manifest_path.read_text())
    entries.append({'name': name + '_infer', 'onnx_bytes': len(data),
                    'onnx_sha256': hashlib.sha256(data).hexdigest(),
                    'config_file': f'config/{name}.json',
                    'config_sha256': hashlib.sha256((root/'config'/f'{name}.json').read_bytes()).hexdigest()})
    manifest_path.write_text(json.dumps(entries))


@pytest.mark.parametrize('order', ['logical', 'display', None])
def test_arabic_bidi_exactly_once(native_root, monkeypatch, order):
    from bidi.algorithm import get_display
    install_legacy_fixture(native_root, 'arabic_PP-OCRv5_mobile_rec')
    native = NativeOcr(PaddleModelConfig(HardwareAccelerator(), 'ar', 'fast'))
    logical = 'مرحبا 123 hello'
    display = get_display(logical)
    text = display if order == 'display' else logical
    response = {'text_order': order, 'result': [[], []]}
    response['result'] = [[[[0, 0], [20, 0], [20, 10], [0, 10]]], [[text, .9]]]
    monkeypatch.setattr(native, '_image_request', lambda image: response)
    if order is None:
        with pytest.raises(ValueError, match='must declare'):
            list(native.predict_iter(None))
    else:
        assert list(native.predict_iter(None))[0]['rec_texts'] == [display]
    native.close()


def test_detection_does_not_require_recognition_assets(native_root):
    (native_root / 'models/PP-OCRv6_small_rec_infer.onnx').unlink()
    with NativeOcr(PaddleModelConfig(HardwareAccelerator(), 'ta', 'fast'), detection_only=True) as native:
        assert native._process is None
        with pytest.raises(RuntimeError, match='Detection-only'):
            list(native.predict_iter(np.zeros((32,32,3),np.uint8)))


def test_injected_cuda_is_not_silently_used_as_cpu(native_root, monkeypatch):
    hardware = HardwareAccelerator()
    monkeypatch.setattr(hardware, 'has_cuda', lambda: True)
    with pytest.raises(ValueError, match='requested CUDA'):
        NativeOcr(PaddleModelConfig(hardware, 'ch', 'auto'))


@pytest.mark.parametrize('language', ['ta', 'te', 'kn', 'ka'])
def test_uninstalled_languages_fail_explicitly(native_root, language):
    with pytest.raises(FileNotFoundError, match='no installed config.*no language substitution'):
        NativeOcr(PaddleModelConfig(HardwareAccelerator(), language, 'fast'))


def test_close_on_consumer_failure(monkeypatch, tmp_path):
    import queue
    from types import SimpleNamespace
    from backend.tools import subtitle_ocr
    native = SimpleNamespace(stop_event=None, closed=False)
    native.close = lambda: setattr(native, 'closed', True)
    monkeypatch.setattr(subtitle_ocr, 'OcrRecogniser', lambda **kwargs: native)
    def fail(*args, **kwargs):
        raise RuntimeError('inference failed')
    monkeypatch.setattr(subtitle_ocr, 'extract_subtitles', fail)
    tasks = queue.Queue()
    tasks.put((1, None, None, None))
    options = SimpleNamespace(REC_CHAR_TYPE='ch', MODE='fast', REC_BATCH_NUMBER=6,
                              HARDWARD_ACCELERATOR=HardwareAccelerator())
    with pytest.raises(RuntimeError, match='inference failed'):
        subtitle_ocr.ocr_task_consumer(tasks, str(tmp_path/'raw.txt'), None,
                                     str(tmp_path/'input.mp4'), options, queue.Queue())
    assert native.closed
    assert native.stop_event is not None
    assert not (tmp_path/'raw.txt').exists()


@pytest.mark.parametrize('result', [None, {}, [None, []], [[], 'not records'],
    [[[[0,0],[10,0],[10,10],[0,10]]], [[None, .9]]],
    [[[[0,0],[10,0],[10,10],[0,10]]], [['text', float('nan')]]],
    [[[[0,0],[10,0],[10,10],[0,float('inf')]]], [['text', .9]]]])
def test_malformed_successful_results_are_rejected(native_root, monkeypatch, result):
    with make_native() as native:
        monkeypatch.setattr(native, '_image_request', lambda image: {'result': result})
        with pytest.raises(ValueError):
            list(native.predict_iter(None))


def test_short_writes_are_completed():
    import io
    from types import SimpleNamespace
    from backend.tools.native_ocr import _exchange
    class ShortWriter(io.BytesIO):
        def write(self, data):
            return super().write(data[:2])
    import queue, struct
    stdin = ShortWriter()
    reply = b'{"ok": true}'
    process = SimpleNamespace(stdin=stdin, stdout=io.BytesIO(struct.pack('<I',len(reply))+reply))
    answers = queue.Queue()
    _exchange(process, b'abcdef', answers)
    assert stdin.getvalue() == struct.pack('<I',6) + b'abcdef'
    assert answers.get() == {'ok': True}


@pytest.mark.parametrize('primary_failure', [False, True])
def test_all_extractor_resources_close_after_one_cleanup_error(primary_failure):
    from unittest.mock import Mock
    from backend.main import SubtitleExtractor
    extractor = SubtitleExtractor.__new__(SubtitleExtractor)
    extractor.lock = threading.RLock()
    extractor.video_cap = Mock()
    extractor.ocr = Mock()
    extractor.ocr.close.side_effect = RuntimeError('close failed')
    detector = extractor.sub_detector = Mock()
    extractor._run_impl = Mock(side_effect=ValueError('primary failure') if primary_failure else None)
    expected = ValueError if primary_failure else RuntimeError
    with pytest.raises(expected, match='primary failure' if primary_failure else 'close failed'):
        extractor.run()
    detector.close.assert_called_once()
    extractor.video_cap.release.assert_called_once()


def test_modified_character_dictionary_fails_integrity(native_root):
    path = native_root / 'config/small.json'
    data = json.loads(path.read_text())
    data['rec']['PostProcess'] = {'character_dict': ['wrong dictionary']}
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='config checksum mismatch'):
        make_native()


def test_duplicate_manifest_model_fails(native_root):
    path = native_root / 'model-manifest.json'
    data = json.loads(path.read_text())
    data.append(data[0])
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='Duplicate native model'):
        make_native()


def test_startup_failure_surfaces_library_diagnostic(native_root, monkeypatch):
    monkeypatch.setenv('NATIVE_TEST_BEHAVIOR', 'eof')
    with make_native() as native:
        with pytest.raises(RuntimeError, match='native stderr: missing native library'):
            list(native.predict_iter(np.zeros((32, 64, 3), dtype=np.uint8)))


@pytest.mark.parametrize('polygons', [[[]], {}, [[[True,0],[1,0],[1,1],[0,1]]],
                                     [[[0,0],[1,0],[1,1],[0,float('nan')]]]])
def test_malformed_detection_results_are_rejected(native_root, monkeypatch, polygons):
    with make_native() as native:
        monkeypatch.setattr(native, '_image_request', lambda *args: {'dt_polys': polygons})
        with pytest.raises(ValueError, match='Invalid native detection'):
            native.predict(None)


def test_garbage_collection_reaps_unclosed_worker(native_root):
    import gc
    native = make_native()
    list(native.predict_iter(np.zeros((32,64,3), np.uint8)))
    process = native._process
    del native
    gc.collect()
    assert process.wait(timeout=5) is not None


def test_concurrent_requests_are_serialized_and_threads_close(native_root):
    from concurrent.futures import ThreadPoolExecutor
    native = make_native()
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: list(native.predict_iter(np.zeros((32,64,3),np.uint8))), range(8)))
    assert all(result[0]['rec_texts'] == ['你好'] for result in results)
    process = native._process
    native.close()
    assert process.poll() is not None
    assert native._stderr_thread is None


@pytest.mark.parametrize('language,model', [('ta','ta_PP-OCRv5_mobile_rec'),
    ('te','te_PP-OCRv5_mobile_rec'), ('kn','ka_PP-OCRv3_mobile_rec'), ('ka','ka_PP-OCRv3_mobile_rec')])
def test_official_recovered_models_and_existing_kannada_aliases(native_root, language, model):
    install_legacy_fixture(native_root, model)
    with NativeOcr(PaddleModelConfig(HardwareAccelerator(), language, 'fast')) as native:
        assert native.model_name == model


def test_every_project_language_keeps_exact_native_model_selection():
    from backend.tools.native_ocr import VALIDATED_LEGACY_MODELS
    hardware = HardwareAccelerator()
    assert len(PaddleModelConfig.MULTI_LANG) == 117
    for language in PaddleModelConfig.MULTI_LANG:
        for mode in ('fast', 'accurate', 'auto'):
            config = PaddleModelConfig(hardware, language, mode)
            assert config.TIER == ('medium' if mode == 'accurate' else 'small')
            assert config.REC_MODEL_NAME in VALIDATED_LEGACY_MODELS | {f'PP-OCRv6_{config.TIER}_rec'}
            assert config.DET_MODEL_NAME == f'PP-OCRv6_{config.TIER}_det'


def test_verification_hook_observes_real_child_after_success_and_before_reaping(native_root, monkeypatch):
    from backend.tools import native_ocr as adapter
    observed = []
    def audit(process, executable, phase):
        assert process.poll() is None
        assert executable == native_root / ('native-ocr.exe' if os.name == 'nt' else 'native-ocr')
        observed.append((process.pid, phase))
    monkeypatch.setattr(adapter, '_record_worker_audit', audit)
    with make_native() as native:
        list(native.predict_iter(np.zeros((32, 64, 3), dtype=np.uint8)))
        process = native._process
        list(native.predict_iter(np.zeros((32, 64, 3), dtype=np.uint8)))
    assert observed == [(process.pid, phase) for phase in ('start', 'inference', 'inference', 'close')]
    assert process.poll() is not None


@pytest.mark.parametrize('phase', ['start', 'inference', 'close'])
def test_verification_failure_never_leaks_native_worker(native_root, monkeypatch, phase):
    from backend.tools import native_ocr as adapter
    processes = []
    def audit(process, executable, event):
        processes.append(process)
        if event == phase:
            raise RuntimeError('native-audit-fixture-failure')
    monkeypatch.setattr(adapter, '_record_worker_audit', audit)
    native = make_native()
    try:
        with pytest.raises(RuntimeError, match='native-audit-fixture-failure'):
            list(native.predict_iter(np.zeros((32, 64, 3), dtype=np.uint8)))
            native.close()
        assert native._process is None
        assert all(process.poll() is not None for process in processes)
    finally:
        native.close()


@pytest.mark.parametrize('platform,active,expected', [('win32', True, True), ('win32', False, False), ('darwin', True, False), ('linux', True, False)])
def test_native_audit_only_calls_already_installed_windows_verification_probe(monkeypatch, platform, active, expected):
    from types import SimpleNamespace
    from backend.tools import native_ocr as adapter
    calls = []
    hook = SimpleNamespace(_NATIVE_WORKER_AUDIT={} if active else None,
                           record_native_worker=lambda *args: calls.append(args))
    monkeypatch.setattr(adapter, 'sys', SimpleNamespace(platform=platform, modules={'standalone_runtime_probe': hook}))
    adapter._record_worker_audit(SimpleNamespace(pid=123), Path('fixture.exe'), 'inference')
    assert bool(calls) is expected
