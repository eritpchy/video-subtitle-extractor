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


def verify_report(path, native, consumer_commit):
    path, native = Path(path), Path(native).resolve(strict=True)
    if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('Missing or oversized compatibility report')
    report = json.loads(path.read_bytes())
    required = {'schema': 1, 'scope': 'source-early-coreml-compatibility-only',
                'result': 'passed', 'consumer_commit': consumer_commit,
                'requested_provider': 'coreml', 'network_attempts': [],
                'gpu_inference_verified': False, 'internal_coreml_precision_verified': False,
                'timing_is_benchmark': False, 'final_package_accepted': False,
                'production_modules_unchanged': True}
    for key, expected in required.items():
        if report.get(key) != expected or type(report.get(key)) is not type(expected):
            raise ValueError('Compatibility evidence did not pass: ' + key)
    verified = gate.verify_native_model_assets(native)
    if report.get('model_files') != verified['files']:
        raise ValueError('Compatibility model inventory differs from actual fixed bytes')
    for key, name in (('worker_sha256', 'native-ocr'), ('runtime_sha256', 'libonnxruntime.1.dylib')):
        if report.get(key) != gate.frozen.sha256(native / name):
            raise ValueError('Compatibility executable/runtime identity mismatch')
    modules = {str((ROOT / name).resolve()): gate.frozen.sha256(ROOT / name) for name in gate.PRODUCTION_FILES}
    if report.get('production_modules') != modules:
        raise ValueError('Compatibility production modules changed or are missing')
    fixtures = ROOT / 'native/ocr/tests/evidence'
    cases = gate.frozen.load_cases(fixtures)
    goldens = gate._raw_goldens({'native_golden_utf8': (fixtures.parent / 'goldens/smoke.json').read_text(encoding='utf-8')})
    models = {name: value['sha256'] for name, value in verified['models'].items()}
    seen, covered = set(), set()
    for phase, ids in (('early', EARLY_IDS), ('compatibility', COMPAT_IDS)):
        rows = report.get(phase)
        if not isinstance(rows, list) or len(rows) != len(ids):
            raise ValueError('Incomplete compatibility phase')
        for row, expected in zip(rows, select_cases(cases, ids)):
            if any(row.get(key) != value for key, value in expected.items()):
                raise ValueError('Compatibility case differs from fixed input/reference')
        covered |= validate_phase(rows, ids, report['worker_sha256'], models, goldens, seen)
    if covered != gate.frozen.ALL_MODELS or report.get('covered_models') != sorted(covered):
        raise ValueError('Real successful compatibility coverage is incomplete')
    return report


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
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--output', type=Path)
    group.add_argument('--verify-report', type=Path)
    parser.add_argument('--consumer-commit')
    args = parser.parse_args()
    if args.verify_report is not None:
        if not args.consumer_commit:
            parser.error('--verify-report requires --consumer-commit')
        verify_report(args.verify_report, os.environ['VSE_NATIVE_OCR_ROOT'], args.consumer_commit)
        print('Independent compatibility evidence validation passed; not GPU or final package acceptance.')
        return 0
    report = run(args.output)
    print(json.dumps({key: report.get(key) for key in ('result', 'covered_models', 'error')}, ensure_ascii=True))
    return 0 if report['result'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
