#!/usr/bin/env python3
"""Final native CPU/CoreML model-and-mode correctness, never GPU certification.

The external, hash-pinned fixtures stay build-side. Each selected provider must
actually execute all 168 reachable production cases. CoreML EP evidence and
physical GPU dispatch are deliberately separate; no trace is synthesized here.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import importlib
import json
import os
from pathlib import Path
import platform
import re
import sys
import time
import traceback

try:
    from . import check_paddle_models as frozen
    from .native_package import verify_native_model_assets
except ImportError:
    import check_paddle_models as frozen
    from native_package import verify_native_model_assets

PROVIDERS = ('cpu', 'coreml')
FORBIDDEN = {'paddle', 'paddleocr', 'paddlex', 'paddle2onnx', 'onnxruntime'}
PRODUCTION_FILES = tuple(name.replace('.', '/') + '.py' for name in frozen.PRODUCTION_MODULES)
VERIFICATION_FILES = ('scripts/check_native_models.py', 'scripts/check_paddle_models.py',
                      'scripts/native_package.py', 'backend/tools/directml_ocr.py')


def request_identity(document):
    """One request, not a cached EP profile; include windows to allow PID reuse."""
    window = document['response']['execution_window']
    return (document['worker_pid'], document['request']['id'],
            *(window[key] for key in ('utc_start_ns', 'utc_end_ns', 'monotonic_start_ns', 'monotonic_end_ns')))


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value) is not None


def _model_pins(report):
    raw = report.get('source_pins_utf8')
    if not isinstance(raw, str) or len(raw) > 65536 or hashlib.sha256(raw.encode('utf-8')).hexdigest() != frozen.SOURCE_PINS_SHA256:
        raise ValueError('Native model report has no fixed source-pin trust root')
    pins = json.loads(raw)
    expected = {row['name']: row['onnx_sha256'] for row in pins['models']}
    if set(expected) != frozen.ALL_MODELS:
        raise ValueError('Native model trust root differs from the frozen coverage')
    return expected


def _raw_goldens(report):
    raw = report.get('native_golden_utf8')
    if not isinstance(raw, str) or len(raw) > 1024 * 1024 or hashlib.sha256(raw.encode('utf-8')).hexdigest() != frozen.GOLDEN_SHA256:
        raise ValueError('Missing fixed native raw-result reference')
    return {(row['tier'], row['operation'], row.get('rec_model'), row['fixture']): row['expected']
            for row in json.loads(raw)['cases']}


def _request_evidence(row, provider, worker_sha256, models, goldens):
    from backend.tools.native_ocr import validate_execution_evidence, _validated_execution_window
    document = row.get('request_evidence')
    if not isinstance(document, dict) or document.get('worker_sha256') != worker_sha256:
        raise ValueError('Native case lacks actual worker/input identity')
    if (document.get('schema_version') != 1 or type(document.get('worker_pid')) is not int
            or document['worker_pid'] < 1):
        raise ValueError('Native request evidence has an invalid schema/process identity')
    request, response = document.get('request'), document.get('response')
    if not isinstance(request, dict) or not isinstance(response, dict):
        raise ValueError('Native case lacks actual IPC request/response evidence')
    if (not _digest(request.get('image_sha256')) or request.get('image_format') != 'png'
            or type(request.get('image_bytes')) is not int or not 0 < request['image_bytes'] <= 32 * 1024 * 1024):
        raise ValueError('Native request lacks the actual encoded PNG identity')
    if (not _digest(row.get('fixture_encoded_png_sha256'))
            or request['image_sha256'] != row['fixture_encoded_png_sha256']
            or type(row.get('fixture_encoded_png_bytes')) is not int
            or request['image_bytes'] != row['fixture_encoded_png_bytes']):
        raise ValueError('Native request differs from the independently encoded frozen fixture')
    expected = {'provider': provider, 'tier': row['tier'], 'operation': row['operation'],
                'rec_model': row['rec_model'] or f"PP-OCRv6_{row['tier']}_rec", 'batch_size': 6, 'cpu_threads': 2}
    if any(request.get(key) != value for key, value in expected.items()):
        raise ValueError('Native request does not match the tested production model/mode')
    if type(request.get('id')) is not int or request['id'] < 1 or type(response.get('id')) is not int or response['id'] != request['id'] or response.get('ok') is not True:
        raise ValueError('Native response is not bound to a successful request')
    for key in ('rec_model', 'batch_size', 'cpu_threads'):
        if response.get(key) != request[key]:
            raise ValueError('Native response settings mismatch')
    model = row['rec_model'] if row['rec_model'] in frozen.LEGACY else None
    reference = goldens[(row['tier'], row['operation'], model, row['fixture'])]
    if row['operation'] == 'ocr':
        if not frozen.compare_ocr(response.get('result'), reference)['passed']:
            raise ValueError('Raw native IPC result differs from the fixed native reference')
    elif response.get('dt_polys') != reference:
        raise ValueError('Raw native detection differs from the fixed native reference')
    window = _validated_execution_window(response.get('execution_window'), worker_pid=document.get('worker_pid'))
    evidence = validate_execution_evidence(response, request, worker_pid=window['worker_pid'])
    executed = set()
    for role, session in evidence['sessions'].items():
        if session is None:
            continue
        # Cached first-run EP profiles are allowed, but must be labeled honestly.
        for key in ('capture', 'request_run_count', 'session_run_count', 'evidence_reused'):
            if key not in session:
                raise ValueError('Native session lacks precise capture/reuse semantics')
        # validate_execution_evidence requires every executed CoreML shape's
        # exact binding, first-run window, counters and aggregate consistency.
        # The aggregate deliberately has no single profile_window: different
        # widths can execute distinct compiled sessions in one request.
        name = session['model']
        if row.get('model_sha256', {}).get(name) != models[name]:
            raise ValueError('Native execution is not bound to the fixed FP32 model')
        if session['status'] == 'executed':
            executed.add(name)
    if row.get('executed_models') != sorted(executed):
        raise ValueError('Native case coverage includes an unexecuted model')
    return executed


def validate_report(report, identity=None, *, required_providers=PROVIDERS):
    required = {'schema': 1, 'result': 'passed', 'ocr_backend': 'native',
                'gpu_inference_verified': False, 'model_count': 15, 'modes': list(frozen.MODES),
                'confidence_tolerance': frozen.TOLERANCE, 'case_proof_sha256': frozen.CASE_PROOF_SHA256,
                'models_unchanged': True, 'production_modules_unchanged': True, 'network_attempts': []}
    for key, value in required.items():
        if report.get(key) != value or type(report.get(key)) is not type(value):
            raise ValueError('Invalid native correctness report field: ' + key)
    if report.get('scope') not in ('source', 'final-bundle') or report.get('bundled_python_verified') is not (report['scope'] == 'final-bundle'):
        raise ValueError('Unverified native bundled Python provenance')
    if identity is not None:
        if report['scope'] != 'final-bundle':
            raise ValueError('Final native acceptance requires final-bundle evidence')
        for key in ('commit', 'version', 'build_profile'):
            if key not in identity or report.get(key) != identity[key]:
                raise ValueError('Native correctness report identity mismatch: ' + key)
    if report.get('providers_requested') != list(required_providers) or not _digest(report.get('worker_sha256')):
        raise ValueError('Native provider/worker identity mismatch')
    if not _digest(report.get('runtime_sha256')):
        raise ValueError('Missing native runtime binary identity')
    if report.get('gpu_validation') != 'not-verified-ep-profile-is-not-device-trace':
        raise ValueError('CoreML EP evidence must not become physical GPU certification')
    models = _model_pins(report)
    goldens = _raw_goldens(report)
    pins = json.loads(report['source_pins_utf8'])
    expected_files = {'model-source-pins.json': frozen.SOURCE_PINS_SHA256,
                      **pins['config_sha256'],
                      **{f"models/{row['name']}_infer.onnx": row['onnx_sha256'] for row in pins['models']}}
    model_files = report.get('model_files')
    if (not isinstance(model_files, dict) or set(model_files) != set(expected_files) | {'model-manifest.json'}
            or any(model_files.get(key) != digest for key, digest in expected_files.items())
            or not _digest(model_files.get('model-manifest.json'))):
        raise ValueError('Native model file evidence differs from fixed graph/config identities')
    modules = report.get('production_modules')
    if (not isinstance(modules, dict) or set(modules) != set(PRODUCTION_FILES + VERIFICATION_FILES)
            or any(not _digest(value) for value in modules.values())):
        raise ValueError('Missing/invalid production and verification module identities')
    groups = report.get('providers', {})
    if set(groups) != set(required_providers):
        raise ValueError('Missing explicit CPU/CoreML comparison')
    specs = frozen.case_specs()
    requests_seen = set()
    for provider in required_providers:
        group = groups[provider]
        rows = group.get('cases', [])
        if group.get('result') != 'passed' or len(rows) != len(specs) or frozen._case_proof(rows) != frozen.CASE_PROOF_SHA256:
            raise ValueError('Missing actual canonical native model/mode cases')
        if {row.get('id') for row in rows} != set(range(len(specs))):
            raise ValueError('Duplicated/missing native case')
        covered = set()
        for row in rows:
            if type(row['id']) is not int or any(row.get(key) != value for key, value in specs[row['id']].items()):
                raise ValueError('Unexpected native model/mode/input combination')
            if row.get('passed') is not True:
                raise ValueError('Native correctness case did not pass')
            if row['operation'] == 'ocr':
                comparison = frozen.compare_ocr(row['actual'], row['expected'])
                if not comparison['passed'] or any(row.get(key) != value for key, value in comparison.items()):
                    raise ValueError('Native OCR differs from its frozen original reference')
            elif not frozen._polygons(row.get('actual')) or row.get('actual') != row.get('expected') or row.get('same_boxes') is not True:
                raise ValueError('Native detection differs from its frozen reference')
            covered |= _request_evidence(row, provider, report['worker_sha256'], models, goldens)
            document = row['request_evidence']
            execution_identity = request_identity(document)
            if execution_identity in requests_seen:
                raise ValueError('A native request was replayed as a second mode/provider/input case')
            requests_seen.add(execution_identity)
        if provider == 'coreml':
            from backend.tools.native_ocr import validate_coreml_record_sequence
            validate_coreml_record_sequence([row['request_evidence'] for row in rows])
        if covered != frozen.ALL_MODELS or group.get('covered_models') != sorted(covered):
            raise ValueError('Incomplete actual native model execution coverage')
    # Both compare against precisely the same baseline, not separate tolerances.
    # No GPU speedup claim is made from these timings or EP profiles.


def _load_runtime(app_root, provider):
    for name, module in tuple(sys.modules.items()):
        if module is not None and (name == 'backend' or name.startswith('backend.')):
            frozen._module_origin(module, app_root)
    sys.path.insert(0, str(app_root))
    import cv2
    modules = {name: importlib.import_module(name) for name in frozen.PRODUCTION_MODULES}
    native = modules['backend.tools.native_ocr']
    if native.selected_backend() != 'native':
        raise ValueError('Wrong production backend')
    hardware = modules['backend.tools.hardware_accelerator'].HardwareAccelerator()
    hardware.initialize()
    hardware.set_enabled(provider == 'coreml')
    if hardware.is_enabled is not (provider == 'coreml') or hardware.has_cuda():
        raise ValueError('Explicit native provider was not honored')
    origins = {str(path): frozen.sha256(path) for module in modules.values()
               for path in frozen._module_origin(module, app_root) if path.is_file()}
    return cv2, modules, hardware, origins


def _execute(cases, fixtures, provider, runtime, models, observe):
    cv2, modules, hardware, _ = runtime
    engine, previous = None, None
    try:
        for case in cases:
            row = dict(case)
            started = time.monotonic()
            try:
                path = frozen._fixture_path(fixtures, case['fixture'])
                if frozen.sha256(path) != case['fixture_sha256']:
                    raise ValueError('Frozen input changed during native inference')
                image = cv2.imread(str(path))
                if image is None:
                    raise ValueError('Cannot decode frozen native input')
                ok, encoded = cv2.imencode('.png', image)
                if not ok:
                    raise ValueError('Cannot encode independent production PNG expectation')
                expected_image_sha = hashlib.sha256(encoded.tobytes()).hexdigest()
                row['fixture_encoded_png_sha256'] = expected_image_sha
                row['fixture_encoded_png_bytes'] = encoded.nbytes
                config = modules['backend.tools.paddle_model_config'].PaddleModelConfig(hardware, case['language'], case['mode'])
                if config.DET_MODEL_NAME != case['det_model'] or (case['operation'] == 'ocr' and config.REC_MODEL_NAME != case['rec_model']):
                    raise ValueError('Production native mode selected a different model')
                key = case['operation'], case['mode'], case['language']
                if previous != key:
                    if engine is not None:
                        engine.close()
                    engine = None
                    gc.collect()
                    engine = (modules['backend.tools.ocr'].OcrRecogniser(case['language'], case['mode'], 6, hardware)
                              if case['operation'] == 'ocr' else modules['backend.tools.subtitle_detect'].SubtitleDetect(case['language'], case['mode'], hardware))
                    previous = key
                if case['operation'] == 'ocr':
                    row['actual'] = json.loads(json.dumps(engine.predict(image), allow_nan=False))
                    row.update(frozen.compare_ocr(row['actual'], row['expected']))
                    native = engine.recogniser
                else:
                    boxes, _ = engine.detect_subtitle(image)
                    row['actual'] = boxes.tolist()
                    row.update(same_boxes=row['actual'] == row['expected'], passed=row['actual'] == row['expected'])
                    native = engine.text_detector
                row['request_evidence'] = json.loads(json.dumps(native.last_request_evidence, allow_nan=False))
                if row['request_evidence']['request']['image_sha256'] != expected_image_sha:
                    raise ValueError('Worker request image differs from the verified frozen fixture pixels')
                sessions = row['request_evidence']['response']['execution_evidence']['sessions']
                row['model_sha256'] = {session['model']: models[session['model']] for session in sessions.values() if session is not None}
                row['executed_models'] = sorted(session['model'] for session in sessions.values() if session is not None and session['status'] == 'executed')
            except Exception as error:
                row.update(passed=False, error=str(error), traceback=traceback.format_exc())
                if engine is not None:
                    engine.close()
                engine, previous = None, None
            row['elapsed_seconds'] = round(time.monotonic() - started, 6)
            observe(row)
    finally:
        if engine is not None:
            engine.close()


def run_check(app_root, fixtures, report_path, providers=PROVIDERS):
    if not providers or len(set(providers)) != len(providers) or any(value not in PROVIDERS for value in providers):
        raise ValueError('Choose explicit CPU and/or CoreML providers')
    app_root, fixtures = Path(app_root).resolve(strict=True), Path(fixtures).resolve(strict=True)
    report_path = Path(report_path).resolve()
    manifest_path = app_root / 'bundle-manifest.json'
    manifest = json.loads(manifest_path.read_bytes()) if manifest_path.is_file() else None
    model_root = (app_root / 'native-ocr' if manifest is not None else Path(os.environ.get('VSE_NATIVE_OCR_ROOT', app_root / 'build/native-ocr'))).resolve(strict=True)
    if report_path.is_relative_to(fixtures) or report_path.is_relative_to(model_root) or (manifest is not None and report_path.is_relative_to(app_root)):
        raise ValueError('Native evidence must be outside immutable bundle/models/fixtures')
    if manifest is not None and manifest.get('ocr_backend') != 'native':
        raise ValueError('Final package is not native')
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {'schema': 1, 'result': 'running', 'ocr_backend': 'native', 'model_count': 15,
              'providers_requested': list(providers), 'providers': {}, 'modes': list(frozen.MODES),
              'gpu_inference_verified': False, 'gpu_validation': 'not-verified-ep-profile-is-not-device-trace',
              'scope': 'final-bundle' if manifest else 'source', 'bundled_python_verified': False,
              **{key: manifest.get(key) if manifest else None for key in ('commit', 'version', 'build_profile')},
              'confidence_tolerance': frozen.TOLERANCE, 'case_proof_sha256': frozen.CASE_PROOF_SHA256,
              'network_attempts': [], 'platform': platform.platform(), 'python': sys.executable,
              'limits': 'Production image correctness and explicit EP evidence, recorded by the trusted CI producer; not a signed execution attestation. Frozen PNG bytes are verified, then independently reencoded before production predict and compared with the exact worker-input hash/size. Not video/GUI or physical GPU certification. First-run EP profiles may be reused and are labeled.'}
    guard = frozen.NetworkGuard()
    origins = {}
    started = time.monotonic()
    frozen._write_report(report_path, report)
    try:
        values = {'VSE_OCR_BACKEND': 'native', 'VSE_NATIVE_OCR_ROOT': str(model_root),
                  'VSE_OFFLINE': '1', 'ORT_DISABLE_TELEMETRY': '1', 'VSE_OCR_CPU_THREADS': '2',
                  'VSE_CONFIG_FILE': str(report_path.parent / 'native-check-config.json'),
                  'QT_QPA_PLATFORM': 'offscreen', 'PYTHONDONTWRITEBYTECODE': '1'}
        with frozen._environment(values), guard:
            if str(app_root) not in sys.path:
                sys.path.insert(0, str(app_root))
            if manifest is not None:
                frozen._check_bundle_runtime(app_root, manifest)
                report['bundled_python_verified'] = True
            cases = frozen.load_cases(fixtures)
            report['native_golden_utf8'] = (fixtures.parent / 'goldens/smoke.json').read_bytes().decode('utf-8')
            verified = verify_native_model_assets(model_root)
            report['model_files'] = verified['files']
            report['source_pins_utf8'] = (model_root / 'model-source-pins.json').read_bytes().decode('utf-8')
            models = _model_pins(report)
            executable = model_root / ('native-ocr.exe' if sys.platform == 'win32' else 'native-ocr')
            report['worker_sha256'] = frozen.sha256(executable)
            runtime_library = model_root / ('onnxruntime.dll' if sys.platform == 'win32' else 'libonnxruntime.1.dylib' if sys.platform == 'darwin' else 'libonnxruntime.so.1')
            report['runtime_sha256'] = frozen.sha256(runtime_library)
            origins.update({str(app_root / relative): frozen.sha256(app_root / relative)
                            for relative in VERIFICATION_FILES})
            for provider in providers:
                group = report['providers'][provider] = {'result': 'running', 'cases': [], 'covered_models': []}
                with frozen._environment({'VSE_NATIVE_OCR_PROVIDER': provider}):
                    runtime = _load_runtime(app_root, provider)
                    origins.update(runtime[3])
                    def observe(row):
                        group['cases'].append(row)
                        frozen._write_report(report_path, report)
                        print(json.dumps({'provider': provider, **{key: row.get(key) for key in ('id', 'mode', 'rec_model', 'passed', 'error')}}), flush=True)
                    _execute(cases, fixtures, provider, runtime, models, observe)
                group['covered_models'] = sorted({name for row in group['cases'] if row.get('passed') is True for name in row.get('executed_models', [])})
                group['result'] = 'passed' if all(row.get('passed') is True for row in group['cases']) else 'failed'
            if any(name.split('.')[0] in FORBIDDEN for name in sys.modules):
                raise ValueError('Native acceptance imported a forbidden Python inference framework')
            report['models_unchanged'] = (verify_native_model_assets(model_root) == verified
                and frozen.sha256(executable) == report['worker_sha256']
                and frozen.sha256(runtime_library) == report['runtime_sha256'])
            report['production_modules'] = {Path(path).relative_to(app_root).as_posix(): digest
                                            for path, digest in origins.items()}
            report['production_modules_unchanged'] = all(frozen.sha256(path) == digest for path, digest in origins.items())
            report['network_attempts'] = guard.attempts
            report['result'] = 'passed'
            validate_report(report, manifest, required_providers=providers)
    except Exception as error:
        report.update(result='failed', fatal_error=str(error), traceback=traceback.format_exc())
    finally:
        report['network_attempts'] = guard.attempts
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        report['summary'] = {provider: {'requested': len(frozen.case_specs()), 'completed': len(group['cases']),
            'passed': sum(row.get('passed') is True for row in group['cases'])} for provider, group in report['providers'].items()}
        frozen._write_report(report_path, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-root', required=True, type=Path)
    parser.add_argument('--fixtures', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--provider', choices=('cpu', 'coreml', 'both'), default='both')
    args = parser.parse_args(argv)
    providers = PROVIDERS if args.provider == 'both' else (args.provider,)
    report = run_check(args.app_root, args.fixtures, args.report, providers)
    print(json.dumps({key: report.get(key) for key in ('result', 'scope', 'summary', 'fatal_error')}))
    return 0 if report['result'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
