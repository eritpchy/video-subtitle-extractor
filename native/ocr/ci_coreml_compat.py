#!/usr/bin/env python3
"""Early production-worker CoreML compatibility; never package/GPU certification."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import traceback

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import check_native_models as gate

EARLY_IDS = (0, 1, 56, 57)
COMPAT_IDS = (11, 14, 17, 20, 23, 26, 29, 32, 35, 38, 41)


def select_cases(cases, ids):
    by_id = {row['id']: row for row in cases}
    if len(by_id) != len(cases) or any(index not in by_id for index in ids):
        raise ValueError('Missing/duplicated frozen compatibility case')
    return [by_id[index] for index in ids]


def validate_phase(rows, ids, worker_sha256, models, goldens, seen):
    if [row.get('id') for row in rows] != list(ids) or any(row.get('passed') is not True for row in rows):
        raise ValueError('Real CoreML compatibility cases did not all execute and pass')
    executed = set()
    for row in rows:
        executed |= gate._request_evidence(row, 'coreml', worker_sha256, models, goldens)
        identity = gate.request_identity(row['request_evidence'])
        if identity in seen:
            raise ValueError('Replayed compatibility request')
        seen.add(identity)
    return executed


def run(output):
    output = Path(output).resolve()
    protected = [ROOT / 'native/ocr/tests']
    if os.environ.get('VSE_NATIVE_OCR_ROOT'):
        protected.append(Path(os.environ['VSE_NATIVE_OCR_ROOT']))
    if any(output.is_relative_to(path.resolve()) for path in protected):
        raise ValueError('Compatibility evidence must not modify models or frozen fixtures')
    if output.exists():
        raise FileExistsError('Compatibility evidence directory must be new')
    output.mkdir(parents=True)
    report_path = output / 'report.json'
    report = {'schema': 1, 'scope': 'source-early-coreml-compatibility-only',
              'result': 'running', 'consumer_commit': os.environ.get('GITHUB_SHA'),
              'gpu_inference_verified': False, 'internal_coreml_precision_verified': False,
              'timing_is_benchmark': False, 'final_package_accepted': False,
              'requested_provider': 'coreml', 'early': [], 'compatibility': [],
              'covered_models': [], 'network_attempts': [],
              'limits': 'Actual production worker and fixed image cases, not final ZIP, all 336 cases, video/GUI, internal precision, performance, or physical GPU evidence.'}
    gate.frozen._write_report(report_path, report)
    guard = gate.frozen.NetworkGuard()
    try:
        fixtures = ROOT / 'native/ocr/tests/evidence'
        cases = gate.frozen.load_cases(fixtures)
        native = Path(os.environ['VSE_NATIVE_OCR_ROOT']).resolve(strict=True)
        verified = gate.verify_native_model_assets(native)
        models = {name: value['sha256'] for name, value in verified['models'].items()}
        report['worker_sha256'] = gate.frozen.sha256(native / 'native-ocr')
        report['runtime_sha256'] = gate.frozen.sha256(native / 'libonnxruntime.1.dylib')
        report['model_files'] = verified['files']
        golden_bytes = (fixtures.parent / 'goldens/smoke.json').read_bytes()
        goldens = gate._raw_goldens({'native_golden_utf8': golden_bytes.decode('utf-8')})
        values = {'VSE_OCR_BACKEND': 'native', 'VSE_NATIVE_OCR_ROOT': str(native),
                  'VSE_NATIVE_OCR_PROVIDER': 'coreml', 'VSE_OCR_CPU_THREADS': '2',
                  'ORT_DISABLE_TELEMETRY': '1', 'VSE_OFFLINE': '1',
                  'VSE_CONFIG_FILE': str(output / 'isolated-config.json')}
        seen, covered = set(), set()
        with gate.frozen._environment(values), guard:
            runtime = gate._load_runtime(ROOT, 'coreml')
            report['production_modules'] = runtime[3]
            for phase, ids in (('early', EARLY_IDS), ('compatibility', COMPAT_IDS)):
                def observe(row):
                    report[phase].append(row)
                    gate.frozen._write_report(report_path, report)
                    print(json.dumps({'phase': phase, 'id': row['id'], 'passed': row.get('passed'),
                                      'error': row.get('error')}, ensure_ascii=True), flush=True)
                gate._execute(select_cases(cases, ids), fixtures, 'coreml', runtime, models, observe)
                covered |= validate_phase(report[phase], ids, report['worker_sha256'], models, goldens, seen)
                report['covered_models'] = sorted(covered)
                gate.frozen._write_report(report_path, report)
            if covered != gate.frozen.ALL_MODELS:
                raise ValueError('Successful responses did not actually execute all 15 fixed models')
        if guard.attempts:
            raise ValueError('Unexpected network activity during compatibility inference')
        if gate.verify_native_model_assets(native)['files'] != verified['files']:
            raise ValueError('Model bytes changed during compatibility inference')
        if (gate.frozen.sha256(native / 'native-ocr') != report['worker_sha256']
                or gate.frozen.sha256(native / 'libonnxruntime.1.dylib') != report['runtime_sha256']):
            raise ValueError('Production worker/runtime changed during compatibility inference')
        if any(gate.frozen.sha256(Path(path)) != value for path, value in report['production_modules'].items()):
            raise ValueError('Production Python modules changed during compatibility inference')
        report['production_modules_unchanged'] = True
        report['result'] = 'passed'
    except Exception as error:
        report.update(result='failed', error=str(error), traceback=traceback.format_exc())
    finally:
        report['network_attempts'] = guard.attempts
        gate.frozen._write_report(report_path, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = run(args.output)
    print(json.dumps({key: report.get(key) for key in ('result', 'covered_models', 'error')}, ensure_ascii=True))
    return 0 if report['result'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
