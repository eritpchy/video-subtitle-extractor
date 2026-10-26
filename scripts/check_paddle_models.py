#!/usr/bin/env python3
"""Offline real Paddle CPU gate using external frozen image/reference fixtures.

--fixtures points to native/ocr/tests/evidence; its sibling goldens/smoke.json
is also required. Neither fixtures nor references belong in a customer package.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import gc
import hashlib
import importlib
import importlib.util
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import sys
import time
import traceback
from types import SimpleNamespace

GOLDEN_SHA256 = 'ccb9ab2da844debe51fd010785c72c4e7ee92a1aac69e1dd67548d2b4cab3698'
FIXTURE_MANIFEST_SHA256 = '9a4e13ad5a30809b45ebaeacef6c54beb209bbf8f9678ca87bd734eec950f553'
SOURCE_PINS_SHA256 = 'db66f8583dd143a21a2949b4a4833da41ce98dcfd5e7837df7c8800f8884c015'
# Canonical 168-case inputs derived from the two fixed trust roots above,
# including each original Paddle expected result and exact fixture digest.
CASE_PROOF_SHA256 = 'a1271577943121d26ff61423693312db48cce4e5b1ddf2e9ebd3a250d77a2f8d'
CASE_PROOF_FIELDS = ('id', 'mode', 'tier', 'operation', 'rec_model', 'det_model', 'language',
                     'fixture', 'fixture_sha256', 'expected', 'baseline', 'baseline_mode')
TOLERANCE = 1e-5
MODES = ('fast', 'accurate', 'auto')
DEFAULT_FIXTURES = ('english', 'chinese', 'japanese', 'mixed', 'blank',
                    'av1_0.5', 'av1_1.5', 'av1_2.5', 'multiline_0.5', 'multiline_1.5', 'multiline_2.5')
LEGACY = {
    'arabic_PP-OCRv5_mobile_rec': 'ar', 'cyrillic_PP-OCRv5_mobile_rec': 'bg',
    'devanagari_PP-OCRv5_mobile_rec': 'hi', 'el_PP-OCRv5_mobile_rec': 'el',
    'eslav_PP-OCRv5_mobile_rec': 'ru', 'korean_PP-OCRv5_mobile_rec': 'korean',
    'latin_PP-OCRv5_mobile_rec': 'pi', 'th_PP-OCRv5_mobile_rec': 'th',
    'ta_PP-OCRv5_mobile_rec': 'ta', 'te_PP-OCRv5_mobile_rec': 'te',
    'ka_PP-OCRv3_mobile_rec': 'kn',
}
ALL_MODELS = set(LEGACY) | {f'PP-OCRv6_{tier}_{kind}' for tier in ('small', 'medium') for kind in ('det', 'rec')}
PRODUCTION_MODULES = ('backend.tools.hardware_accelerator', 'backend.tools.paddle_model_config',
                      'backend.tools.native_ocr', 'backend.tools.ocr', 'backend.tools.subtitle_detect')


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def case_specs():
    """Exact reachable production combinations; auto must execute, not alias-pass."""
    result = []
    for mode in MODES:
        tier = 'medium' if mode == 'accurate' else 'small'
        detector = f'PP-OCRv6_{tier}_det'
        entries = [('ocr', f'PP-OCRv6_{tier}_rec', 'ch', f'fixtures/{name}.png') for name in DEFAULT_FIXTURES]
        for model, language in LEGACY.items():
            folder = 'missing' if language in ('ta', 'te', 'kn') else 'legacy'
            entries += [('ocr', model, language, f'{folder}/fixtures/{model}-{sample}.png') for sample in ('script', 'blank', 'wide')]
        entries.append(('ocr', 'ka_PP-OCRv3_mobile_rec', 'ka', 'missing/fixtures/ka_PP-OCRv3_mobile_rec-script.png'))
        entries += [('detect', None, 'ch', f'fixtures/{name}.png') for name in DEFAULT_FIXTURES]
        for operation, recognizer, language, fixture in entries:
            result.append({'id': len(result), 'mode': mode, 'tier': tier, 'operation': operation,
                           'rec_model': recognizer, 'det_model': detector, 'language': language, 'fixture': fixture})
    return result


def _case_proof(cases):
    values = [{key: row[key] for key in CASE_PROOF_FIELDS} for row in sorted(cases, key=lambda row: row['id'])]
    return hashlib.sha256((json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')).hexdigest()


def _fixture_path(root, relative):
    value = PurePosixPath(relative)
    if value.is_absolute() or '..' in value.parts or '\\' in relative:
        raise ValueError('Unsafe fixture path')
    path = root.joinpath(*value.parts)
    for component in (path, *path.parents):
        if component == root:
            break
        if component.is_symlink():
            raise ValueError('Fixture links are not accepted')
    if not path.is_file() or not path.resolve().is_relative_to(root):
        raise ValueError('Fixture must be a regular file below the external fixture root')
    return path


def load_cases(fixtures):
    fixtures = Path(fixtures).resolve(strict=True)
    manifest_path = _fixture_path(fixtures, 'fixture-manifest.json')
    golden_path = fixtures.parent / 'goldens/smoke.json'
    if sha256(manifest_path) != FIXTURE_MANIFEST_SHA256 or sha256(golden_path) != GOLDEN_SHA256:
        raise ValueError('Frozen fixture/golden trust root mismatch')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    files = {}
    for item in manifest['files']:
        path = _fixture_path(fixtures, item['path'])
        if item['path'] in files or path.stat().st_size != item['bytes'] or sha256(path) != item['sha256']:
            raise ValueError('Frozen fixture/reference identity mismatch: ' + item['path'])
        files[item['path']] = item
    golden = json.loads(golden_path.read_text(encoding='utf-8'))
    if golden['confidence_tolerance'] != TOLERANCE or len(golden['cases']) != 110:
        raise ValueError('Unexpected frozen comparison contract')
    lookup = {}
    for item in golden['cases']:
        key = (item['tier'], item['operation'], item.get('rec_model'), item['fixture'])
        if key in lookup:
            raise ValueError('Duplicate frozen case')
        lookup[key] = item
    cases = []
    for spec in case_specs():
        model = spec['rec_model'] if spec['rec_model'] in LEGACY else None
        original = lookup[(spec['tier'], spec['operation'], model, spec['fixture'])]
        if files[spec['fixture']]['sha256'] != original['fixture_sha256']:
            raise ValueError('Fixture identity differs between manifest and golden')
        expected = original['expected']
        baseline = 'goldens/smoke.json'
        # Native raw Arabic is logical order. Paddle's unchanged original
        # reference is display order: compare it directly, never transform text.
        if original.get('paddle_parity_reference'):
            baseline = original['paddle_parity_reference']
            rows = json.loads(_fixture_path(fixtures, baseline).read_text(encoding='utf-8'))
            sample = Path(spec['fixture']).stem.rsplit('-', 1)[1]
            baseline_mode = 'accurate' if spec['mode'] == 'accurate' else 'fast'
            matches = [row for row in rows if row['model'] == model and row['mode'] == baseline_mode and row['case'] == sample]
            if len(matches) != 1:
                raise ValueError('Missing or duplicate original Paddle comparison')
            expected = matches[0]['paddle']
        cases.append({**spec, 'fixture_sha256': original['fixture_sha256'], 'expected': expected,
                      'baseline': baseline, 'baseline_mode': 'accurate' if spec['mode'] == 'accurate' else 'fast'})
    if _case_proof(cases) != CASE_PROOF_SHA256:
        raise ValueError('Frozen case proof differs from the committed canonical contract')
    return cases


def _polygons(value):
    return (isinstance(value, list) and all(isinstance(polygon, list) and len(polygon) == 4 and
        all(isinstance(point, list) and len(point) == 2 and all(type(number) in (int, float) and math.isfinite(number) for number in point)
            for point in polygon) for polygon in value))


def compare_ocr(actual, expected):
    if len(actual) != 2 or len(expected) != 2:
        raise ValueError('Malformed OCR pair')
    boxes, records = actual
    if (not _polygons(boxes) or not _polygons(expected[0]) or len(boxes) != len(records)
            or any(len(row) != 2 or not isinstance(row[0], str) or type(row[1]) not in (int, float) for row in records)):
        raise ValueError('Malformed OCR records')
    finite = all(math.isfinite(float(row[1])) for row in records)
    same_text = [row[0] for row in records] == [row[0] for row in expected[1]]
    delta = max((abs(float(a[1]) - float(b[1])) for a, b in zip(records, expected[1])), default=0.0)
    same_boxes = boxes == expected[0]
    return {'passed': same_boxes and same_text and finite and len(records) == len(expected[1]) and delta <= TOLERANCE,
            'same_boxes': same_boxes, 'same_text': same_text, 'finite_confidence': finite, 'confidence_delta': delta}


def validate_report(report: dict, identity: dict | None = None) -> None:
    """Stdlib-only receipt contract: verify each actual case, not summary counts."""
    required = {'schema': 1, 'result': 'passed', 'ocr_backend': 'paddle', 'cpu_forced': True,
                'gpu_inference_verified': False, 'model_count': 15, 'modes': list(MODES),
                'confidence_tolerance': TOLERANCE, 'source_pins_sha256': SOURCE_PINS_SHA256,
                'golden_sha256': GOLDEN_SHA256, 'fixture_manifest_sha256': FIXTURE_MANIFEST_SHA256,
                'models_unchanged': True, 'production_modules_unchanged': True, 'network_attempts': []}
    for name, value in required.items():
        if report.get(name) != value or (type(value) in (bool, int) and type(report.get(name)) is not type(value)):
            raise ValueError('Invalid Paddle model receipt field: ' + name)
    if report.get('scope') not in ('source', 'final-bundle'):
        raise ValueError('Invalid Paddle model receipt scope')
    if report.get('bundled_python_verified') is not (report['scope'] == 'final-bundle'):
        raise ValueError('Unverified bundled Python provenance')
    if identity is not None:
        if report.get('scope') != 'final-bundle':
            raise ValueError('Final package requires a final-bundle model receipt')
        for name in ('commit', 'version', 'build_profile'):
            if name not in identity or report.get(name) != identity[name]:
                raise ValueError('Paddle model receipt identity mismatch: ' + name)
    device = report.get('device', {})
    if device.get('paddle_device') != 'cpu' or device.get('hardware_enabled') is not False or device.get('inference_kwargs', {}).get('device') != 'cpu':
        raise ValueError('Model receipt does not prove explicitly requested CPU execution')
    cases = report.get('cases', [])
    specs = case_specs()
    if len(cases) != len(specs) or {row.get('id') for row in cases} != set(range(len(specs))):
        raise ValueError('Missing or duplicated actual Paddle model cases')
    if _case_proof(cases) != CASE_PROOF_SHA256:
        raise ValueError('Case expectations/fixtures differ from the canonical frozen proof')
    observed_models = set()
    for row in cases:
        if type(row.get('id')) is not int or row.get('passed') is not True:
            raise ValueError('Unsuccessful or invalid actual Paddle model case')
        spec = specs[row['id']]
        if any(row.get(key) != value for key, value in spec.items()):
            raise ValueError('Unexpected actual model/mode/fixture combination')
        if row['operation'] == 'ocr':
            verdict = compare_ocr(row['actual'], row['expected'])
            if not verdict['passed'] or any(row.get(key) != value for key, value in verdict.items()):
                raise ValueError('OCR outcome differs from its recorded comparison')
            observed_models.add(row['rec_model'])
        elif (row.get('same_boxes') is not True or not _polygons(row.get('actual'))
              or not _polygons(row.get('expected')) or row.get('actual') != row.get('expected')):
            raise ValueError('Detector outcome differs from its recorded comparison')
        observed_models.add(row['det_model'])
    if observed_models != ALL_MODELS or report.get('covered_models') != sorted(ALL_MODELS):
        raise ValueError('Incomplete actually executed original-model coverage')


class NetworkGuard:
    def __init__(self):
        self.active = False
        self.attempts = []

    def _audit(self, event, args):
        if self.active and event in ('socket.connect', 'socket.getaddrinfo', 'urllib.Request'):
            self.attempts.append({'event': event, 'arguments': str(args)[:500]})
            raise RuntimeError('Offline model verification forbids network access: ' + event)

    def __enter__(self):
        sys.addaudithook(self._audit)
        self.active = True
        return self

    def __exit__(self, *args):
        self.active = False


@contextmanager
def _environment(values):
    old = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _module_origin(module, app_root):
    paths = ([Path(module.__file__).resolve()] if getattr(module, '__file__', None) else
             [Path(path).resolve() for path in getattr(module, '__path__', [])])
    if not paths or any(not path.is_relative_to(app_root) for path in paths):
        raise ValueError('Production module came from outside app-root: ' + module.__name__)
    return paths


def _load_runtime(app_root):
    for name, module in tuple(sys.modules.items()):
        if module is not None and (name == 'backend' or name.startswith('backend.')):
            _module_origin(module, app_root)
    sys.path.insert(0, str(app_root))
    # Match the supported application import policy on native Windows.
    if sys.platform == 'win32':
        import PySide6  # noqa: F401
    import cv2
    import paddle
    import paddleocr
    from paddlex.utils.deps import require_extra
    require_extra('ocr', obj_name='OCR', alt='ocr-core')
    modules = {name: importlib.import_module(name) for name in PRODUCTION_MODULES}
    origins = {str(path): sha256(path) for module in modules.values() for path in _module_origin(module, app_root) if path.is_file()}
    if modules['backend.tools.native_ocr'].selected_backend() != 'paddle':
        raise ValueError('Wrong production backend')
    hardware = modules['backend.tools.hardware_accelerator'].HardwareAccelerator()
    hardware.set_enabled(False)
    paddle.set_device('cpu')
    if hardware.has_cuda() or hardware.inference_kwargs()['device'] != 'cpu' or paddle.device.get_device() != 'cpu':
        raise ValueError('Explicit CPU device contract was not honored')
    return SimpleNamespace(cv2=cv2, paddle=paddle, hardware=hardware, origins=origins,
                           OcrRecogniser=modules['backend.tools.ocr'].OcrRecogniser,
                           SubtitleDetect=modules['backend.tools.subtitle_detect'].SubtitleDetect,
                           ModelConfig=modules['backend.tools.paddle_model_config'].PaddleModelConfig,
                           versions={'paddle': paddle.__version__, 'paddleocr': paddleocr.__version__, 'opencv': cv2.__version__},
                           device={'paddle_device': paddle.device.get_device(), 'hardware_enabled': hardware.is_enabled,
                                   'inference_kwargs': hardware.inference_kwargs(), 'compiled_with_cuda': paddle.is_compiled_with_cuda()})


def _load_model_verifier(app_root):
    path = app_root / 'scripts/paddle_package_models.py'
    spec = importlib.util.spec_from_file_location('_vse_checked_paddle_models', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _module_origin(module, app_root)
    if module.PINNED_SOURCE_SHA256 != SOURCE_PINS_SHA256:
        raise ValueError('Unexpected packaged original-model trust root')
    return module.verify_models, path


def _execute(cases, fixtures, runtime, observe):
    engine, previous = None, None
    try:
        for case in cases:
            row = dict(case)
            started = time.monotonic()
            try:
                path = _fixture_path(fixtures, case['fixture'])
                if sha256(path) != case['fixture_sha256']:
                    raise ValueError('Fixture changed during inference')
                image = runtime.cv2.imread(str(path))
                if image is None:
                    raise ValueError('Cannot decode frozen fixture')
                selected = runtime.ModelConfig(runtime.hardware, case['language'], case['mode'])
                if selected.DET_MODEL_NAME != case['det_model'] or (case['operation'] == 'ocr' and selected.REC_MODEL_NAME != case['rec_model']):
                    raise ValueError('Production selection differs from required model/mode')
                key = (case['operation'], case['mode'], case['language'])
                if key != previous:
                    if engine is not None:
                        engine.close()
                    engine = None
                    gc.collect()
                    engine = (runtime.OcrRecogniser(case['language'], case['mode'], 6, runtime.hardware)
                              if case['operation'] == 'ocr' else runtime.SubtitleDetect(case['language'], case['mode'], runtime.hardware))
                    previous = key
                if case['operation'] == 'ocr':
                    row['actual'] = json.loads(json.dumps(engine.predict(image), allow_nan=False))
                    row.update(compare_ocr(row['actual'], case['expected']))
                else:
                    polygons, elapsed = engine.detect_subtitle(image)
                    row['actual'] = json.loads(json.dumps(polygons.tolist(), allow_nan=False))
                    row.update(same_boxes=row['actual'] == case['expected'], passed=row['actual'] == case['expected'], inference_seconds=elapsed)
            except Exception as error:
                row.update(passed=False, error=str(error), traceback=traceback.format_exc())
                if engine is not None:
                    engine.close()
                engine, previous = None, None
            row['elapsed_seconds'] = round(time.monotonic() - started, 4)
            observe(row)
    finally:
        if engine is not None:
            engine.close()


def _write_report(path, report):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def _check_bundle_runtime(app_root, manifest):
    relative = PurePosixPath(manifest.get('runtime_python', ''))
    if not str(relative) or str(relative) == '.' or relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Final bundle has no safe runtime_python identity')
    runtime = app_root.joinpath(*relative.parts).resolve(strict=True)
    if not runtime.is_relative_to(app_root) or Path(sys.executable).resolve() != runtime:
        raise ValueError('Final-bundle model gate must run with the actual bundled Python')


def run_check(app_root, fixtures, report_path):
    app_root, fixtures = Path(app_root).resolve(strict=True), Path(fixtures).resolve(strict=True)
    report_path = Path(report_path).resolve()
    manifest_path = app_root / 'bundle-manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.is_file() else None
    if manifest is not None and manifest.get('ocr_backend') != 'paddle':
        raise ValueError('Final bundle is not the Paddle profile')
    models_root = (app_root / 'backend/models' if manifest is not None else
                   Path(os.environ.get('VSE_MODEL_DIR', app_root / 'backend/models'))).resolve()
    if (report_path.is_relative_to(fixtures) or report_path.is_relative_to(models_root)
            or (manifest is not None and report_path.is_relative_to(app_root))):
        raise ValueError('Model report/cache must be outside immutable bundle/models/fixtures')
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {'schema': 1, 'result': 'running', 'ocr_backend': 'paddle', 'cpu_forced': True,
              'gpu_inference_verified': False, 'model_count': 15, 'modes': list(MODES),
              'scope': 'final-bundle' if manifest is not None else 'source',
              'bundled_python_verified': False,
              **{key: manifest.get(key) if manifest else None for key in ('commit', 'version', 'build_profile')},
              'source_pins_sha256': SOURCE_PINS_SHA256, 'golden_sha256': GOLDEN_SHA256,
              'fixture_manifest_sha256': FIXTURE_MANIFEST_SHA256, 'confidence_tolerance': TOLERANCE,
              'case_proof_sha256': CASE_PROOF_SHA256,
              'app_root': str(app_root), 'fixtures': str(fixtures), 'model_root': str(models_root), 'python': sys.executable,
              'platform': platform.platform(), 'cases': [], 'covered_models': [], 'network_attempts': [],
              'limits': 'CPU image inference only; not GPU hardware, GUI/video, or per-language accuracy certification.'}
    _write_report(report_path, report)
    started = time.monotonic()
    cache = report_path.parent / (report_path.stem + '-cache')
    values = {'VSE_OCR_BACKEND': 'paddle', 'VSE_MODEL_DIR': str(models_root),
              'VSE_OFFLINE': '1', 'QT_QPA_PLATFORM': 'offscreen', 'PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK': 'True',
              'PADDLE_PDX_CACHE_HOME': str(cache / 'paddlex'), 'VSE_CONFIG_FILE': str(cache / 'config.json'),
              'VSE_ENABLE_MKLDNN': '0', 'VSE_OCR_CPU_THREADS': '2', 'CUDA_VISIBLE_DEVICES': ''}
    guard = NetworkGuard()
    try:
        with _environment(values), guard:
            if manifest is not None:
                _check_bundle_runtime(app_root, manifest)
                report['bundled_python_verified'] = True
            cases = load_cases(fixtures)
            verify_models, verifier_path = _load_model_verifier(app_root)
            verified = verify_models(models_root)
            if {row['name'] for row in verified['models']} != ALL_MODELS:
                raise ValueError('Incomplete original Paddle model set')
            verifier_sha = sha256(verifier_path)
            runtime = _load_runtime(app_root)
            report.update(device=runtime.device, runtime_versions=runtime.versions,
                          production_modules=runtime.origins, model_files=verified['files'])
            def observe(row):
                report['cases'].append(row)
                _write_report(report_path, report)
                print(json.dumps({key: row.get(key) for key in ('id', 'mode', 'operation', 'rec_model', 'passed', 'error')}, ensure_ascii=False), flush=True)
            _execute(cases, fixtures, runtime, observe)
            report['models_unchanged'] = verify_models(models_root) == verified
            report['production_modules_unchanged'] = (sha256(verifier_path) == verifier_sha and
                all(sha256(path) == digest for path, digest in runtime.origins.items()))
            report['covered_models'] = sorted({name for row in report['cases'] if row.get('passed') is True
                for name in (row['rec_model'], row['det_model']) if name is not None})
            report['network_attempts'] = guard.attempts
            report['result'] = 'passed'
            validate_report(report, manifest)
    except Exception as error:
        report.update(result='failed', fatal_error=str(error), traceback=traceback.format_exc())
    finally:
        report['network_attempts'] = guard.attempts
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        report['summary'] = {'requested': len(case_specs()), 'completed': len(report['cases']),
                             'passed': sum(row.get('passed') is True for row in report['cases']),
                             'failed': sum(row.get('passed') is not True for row in report['cases']),
                             'max_confidence_delta': max((row.get('confidence_delta', 0) for row in report['cases']), default=0)}
        _write_report(report_path, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-root', type=Path, required=True)
    parser.add_argument('--fixtures', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args(argv)
    result = run_check(args.app_root, args.fixtures, args.report)
    print(json.dumps({key: result.get(key) for key in ('result', 'scope', 'summary', 'fatal_error')}, ensure_ascii=False))
    return 0 if result['result'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
