#!/usr/bin/env python3
"""Validate fixed ONNX assets and frozen OCR parity with an explicit provider.

CPU evidence is NOT DirectML GPU certification. DirectML requires actual kernel
execution evidence for every loaded model. This verifier never chooses a fallback.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run(models, provider):
    import cv2
    import numpy as np
    import onnxruntime as ort
    from types import SimpleNamespace
    from backend.tools.directml_ocr import DirectMlOcr, validate_model_assets, ORT_VERSION
    from backend.tools.ocr import normalize_ocr_result
    from native.ocr.ipc_client import compare
    validate_model_assets(models)
    if ort.__version__ != ORT_VERSION:
        raise RuntimeError(f'Expected ORT {ORT_VERSION}; got {ort.__version__}')
    goldens = json.loads((ROOT / 'native/ocr/tests/goldens/smoke.json').read_text(encoding='utf-8'))
    report = {'schema_version': 1, 'provider': provider, 'onnxruntime': ort.__version__,
              'scope': ('Explicit CPU model/adapter parity only; NOT DirectML hardware certification.' if provider == 'cpu'
                        else 'DirectML model/adapter parity with actual execution evidence; not a GPU performance benchmark.'),
              'gpu_hardware_verified': False, 'cases': [], 'execution_evidence': {}}
    engines = {}
    try:
        cases = sorted(enumerate(goldens['cases']), key=lambda item: (item[1]['tier'], item[1].get('rec_model') or '', item[1]['operation']))
        for index, case in cases:
            detection_only = case['operation'] == 'detect'
            rec_name = case.get('rec_model') or f'PP-OCRv6_{case["tier"]}_rec'
            key = (case['tier'], rec_name, detection_only)
            if key not in engines:
                # Never retain all 15 graph sessions/arenas simultaneously on CI.
                for previous_key, previous in engines.items():
                    report['execution_evidence']['/'.join(map(str, previous_key))] = previous.execution_evidence
                    previous.close()
                engines.clear()
                config = SimpleNamespace(DET_MODEL_NAME=f'PP-OCRv6_{case["tier"]}_det', REC_MODEL_NAME=rec_name,
                                         hardware_accelerator=SimpleNamespace(is_enabled=provider == 'directml'))
                engines[key] = DirectMlOcr(config, 6, root=models, detection_only=detection_only, provider=provider)
            engine = engines[key]
            image_path = ROOT / 'native/ocr/tests/evidence' / case['fixture']
            data = image_path.read_bytes()
            if hashlib.sha256(data).hexdigest() != case['fixture_sha256']:
                raise ValueError('Frozen fixture hash mismatch')
            image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
            result = list(engine.predict(image))[0]
            expected = copy.deepcopy(case['expected'])
            if detection_only:
                verdict = {'same_boxes': result['dt_polys'] == expected, 'passed': result['dt_polys'] == expected}
            else:
                if case.get('text_order') == 'logical':
                    from bidi.algorithm import get_display
                    for record in expected[1]:
                        record[0] = get_display(record[0])
                boxes, records = normalize_ocr_result(result)
                actual = json.loads(json.dumps([boxes, records]))
                verdict = compare(actual, expected, goldens['confidence_tolerance'])
            report['cases'].append({'id': index, 'fixture': case['fixture'], 'rec_model': rec_name,
                                    'tier': case['tier'], 'operation': case['operation'], **verdict})
        for key, engine in engines.items():
            report['execution_evidence']['/'.join(map(str, key))] = engine.execution_evidence
        imported = sorted(name for name in ('paddle', 'paddleocr', 'paddlex', 'paddle2onnx') if name in sys.modules)
        if imported:
            raise RuntimeError(f'DirectML imported forbidden runtime stacks: {imported}')
        report['cases'].sort(key=lambda case: case['id'])
        report['passed'] = all(case['passed'] for case in report['cases'])
        report['summary'] = {'parity_cases': len(report['cases']), 'failed': sum(not case['passed'] for case in report['cases']),
                             'max_confidence_delta': max(case.get('confidence_delta', 0) for case in report['cases'])}
        return report
    finally:
        for engine in engines.values():
            engine.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--models', type=Path, required=True)
    parser.add_argument('--provider', choices=['cpu', 'directml'], required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    try:
        report = run(args.models, args.provider)
    except Exception as error:
        report = {'passed': False, 'provider': args.provider, 'gpu_hardware_verified': False,
                  'fatal_error': type(error).__name__ + ': ' + str(error)}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key in ('passed', 'provider', 'summary', 'fatal_error')}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
