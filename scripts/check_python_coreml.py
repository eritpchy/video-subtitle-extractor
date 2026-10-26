#!/usr/bin/env python3
"""Actual Python source OCR parity, never native-package/GPU certification."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import check_paddle_models as frozen

EARLY = (0, 1, 56, 57)
MODELS = EARLY + (11, 14, 17, 20, 23, 26, 29, 32, 35, 38, 41)


def verify_runtime_files(runtime):
    """Independent post-inference disk rehash, not a cached ORT import verdict.

    This attests inspected installed files, not an OS loaded-image/GPU trace.
    """
    root = Path(runtime['package_root']).resolve(strict=True)
    pin_rows = json.loads((ROOT / 'backend/tools/python_coreml_runtime.json').read_bytes())['wheels'].values()
    matching = [row for row in pin_rows if row['filename'] == runtime.get('wheel_filename')]
    if len(matching) != 1 or runtime.get('native_files') != matching[0]['native_files']:
        raise ValueError('Runtime inventory differs from the exact official wheel')
    expected = matching[0]['native_files']
    actual = {}
    discovered = {path.relative_to(root).as_posix() for path in root.rglob('*')
                  if path.is_file() and (path.suffix in ('.so', '.dylib', '.pyd', '.dll') or '.so.' in path.name)}
    if discovered != set(expected):
        raise ValueError('Unexpected/missing Python runtime native file')
    for relative, digest in expected.items():
        path = root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(root) or frozen.sha256(path) != digest:
            raise ValueError('Python runtime bytes changed: ' + relative)
        actual[relative] = digest
    return actual


def run(models, fixtures, provider, case_set, report_path):
    models, fixtures, report_path = Path(models).resolve(), Path(fixtures).resolve(), Path(report_path).resolve()
    if report_path.is_relative_to(models) or report_path.is_relative_to(fixtures):
        raise ValueError('Source evidence must be outside immutable models/fixtures')
    report = {'schema': 1, 'scope': 'python-source-correctness', 'backend': 'python-coreml',
        'provider': provider, 'case_set': case_set, 'commit': os.environ.get('GITHUB_SHA'),
        'result': 'running', 'cases': [], 'covered_models': [], 'network_attempts': [],
        'native_worker_used': False, 'gpu_execution_verified': False,
        'internal_coreml_precision_verified': False, 'final_package_accepted': False,
        'confidence_tolerance': frozen.TOLERANCE}
    started = time.monotonic()
    guard = frozen.NetworkGuard()
    subprocess_attempts = []
    audit_active = True
    def no_worker(event, args):
        if audit_active and event == 'subprocess.Popen':
            subprocess_attempts.append(Path(os.fsdecode(args[0])).name)
            raise RuntimeError('Python image verification forbids any subprocess/native worker')
    sys.addaudithook(no_worker)
    engine = None
    try:
        if provider not in ('cpu', 'coreml') or case_set not in ('early', 'models', 'all'):
            raise ValueError('Invalid Python source provider/case selection')
        loader_names = ('LD_LIBRARY_PATH', 'LD_PRELOAD', 'DYLD_LIBRARY_PATH', 'DYLD_INSERT_LIBRARIES',
                        'DYLD_FRAMEWORK_PATH', 'DYLD_FALLBACK_LIBRARY_PATH', 'DYLD_FALLBACK_FRAMEWORK_PATH')
        contaminated = [name for name in loader_names if os.environ.get(name)]
        if contaminated:
            raise ValueError('Start a fresh Python source process without loader overrides: ' + ', '.join(contaminated))
        report['loader_environment_clean'] = True
        report['inherited_metal_capture_removed'] = 'MTL_CAPTURE_ENABLED' in os.environ
        os.environ.pop('MTL_CAPTURE_ENABLED', None)
        cases = frozen.load_cases(fixtures)
        if case_set != 'all':
            ids = EARLY if case_set == 'early' else MODELS
            cases = [next(row for row in cases if row['id'] == value) for value in ids]
        with frozen._environment({'VSE_OCR_BACKEND': 'python-coreml',
                'VSE_PYTHON_COREML_MODEL_ROOT': str(models), 'VSE_OFFLINE': '1',
                'ORT_DISABLE_TELEMETRY': '1', 'VSE_OCR_CPU_THREADS': '2'}), guard:
            import cv2
            from backend.tools.hardware_accelerator import HardwareAccelerator
            from backend.tools.ocr import OcrRecogniser
            from backend.tools.subtitle_detect import SubtitleDetect
            from backend.tools.python_coreml_ocr import (validate_model_assets, _load_runtime,
                _source_identity, validate_python_coreml_record, validate_python_coreml_records)
            initial = validate_model_assets(models)
            report['model_files'] = initial['hashes']
            report['source_files'] = _source_identity()
            report['runtime'] = _load_runtime()[1]
            report['runtime_native_files_before'] = verify_runtime_files(report['runtime'])
            hardware = HardwareAccelerator(); hardware.initialize(); hardware.set_enabled(provider == 'coreml')
            previous = None
            for case in cases:
                row = dict(case)
                try:
                    key = case['operation'], case['mode'], case['language']
                    if key != previous:
                        if engine is not None:
                            engine.close()
                        engine = (OcrRecogniser(case['language'], case['mode'], 6, hardware) if case['operation'] == 'ocr'
                                  else SubtitleDetect(case['language'], case['mode'], hardware))
                        previous = key
                    path = frozen._fixture_path(fixtures, case['fixture'])
                    if frozen.sha256(path) != case['fixture_sha256']:
                        raise ValueError('Frozen Python source input changed')
                    image = cv2.imread(str(path))
                    if case['operation'] == 'ocr':
                        actual = json.loads(json.dumps(engine.predict(image)))
                        row.update(actual=actual, **frozen.compare_ocr(actual, case['expected']))
                        implementation = engine.recogniser
                    else:
                        boxes, _ = engine.detect_subtitle(image)
                        row.update(actual=boxes.tolist(), passed=boxes.tolist() == case['expected'])
                        implementation = engine.text_detector
                    record = implementation.last_request_evidence
                    ok, encoded = cv2.imencode('.png', image)
                    if not ok or record['input_encoded_png_sha256'] != hashlib.sha256(encoded.tobytes()).hexdigest():
                        raise ValueError('Actual Python input differs from frozen fixture pixels')
                    row['request_evidence'] = record
                    row['executed_models'] = sorted(validate_python_coreml_record(record, provider=provider))
                except Exception as error:
                    row.update(passed=False, error=str(error), traceback=traceback.format_exc())
                    if engine is not None:
                        engine.close()
                    engine, previous = None, None
                report['cases'].append(row)
                frozen._write_report(report_path, report)
                print(json.dumps({'id': row['id'], 'passed': row['passed'], 'error': row.get('error')}, ensure_ascii=True), flush=True)
            records = [row['request_evidence'] for row in report['cases'] if row.get('passed')]
            covered = validate_python_coreml_records(records, provider=provider)
            report['covered_models'] = sorted(covered)
            if case_set != 'early' and covered != frozen.ALL_MODELS:
                raise ValueError('Python source did not actually execute all 15 fixed graphs')
            if len(report['cases']) != len(cases) or any(row['passed'] is not True for row in report['cases']):
                raise ValueError('Python source parity did not pass every requested case')
            if validate_model_assets(models)['hashes'] != report['model_files'] or _source_identity() != report['source_files']:
                raise ValueError('Python source/model identity changed during inference')
            report['runtime_native_files_after'] = verify_runtime_files(report['runtime'])
            if report['runtime_native_files_before'] != report['runtime_native_files_after']:
                raise ValueError('Python runtime files changed during inference')
            if any(name in sys.modules for name in ('paddle', 'paddleocr', 'paddlex')):
                raise ValueError('Python ONNX source unexpectedly imported Paddle')
            report.update(result='passed', models_unchanged=True, source_unchanged=True,
                          paddle_imported=False, interpreter=sys.executable, runtime_files_unchanged=True)
    except Exception as error:
        report.update(result='failed', error=str(error), traceback=traceback.format_exc())
    finally:
        audit_active = False
        report['subprocess_attempts'] = subprocess_attempts
        if subprocess_attempts:
            report.update(result='failed', error='Subprocess activity during pure Python image inference')
        if engine is not None:
            engine.close()
        report['network_attempts'] = guard.attempts
        report['elapsed_seconds'] = round(time.monotonic() - started, 6)
        if guard.attempts:
            report.update(result='failed', error='Network activity during offline source inference')
        frozen._write_report(report_path, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--models', type=Path, required=True)
    parser.add_argument('--fixtures', type=Path, required=True)
    parser.add_argument('--provider', choices=('cpu', 'coreml'), required=True)
    parser.add_argument('--case-set', choices=('early', 'models', 'all'), default='all')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args(argv)
    report = run(args.models, args.fixtures, args.provider, args.case_set, args.report)
    print(json.dumps({key: report.get(key) for key in ('result', 'covered_models', 'error')}, ensure_ascii=True))
    return 0 if report['result'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
