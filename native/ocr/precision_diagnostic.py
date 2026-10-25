#!/usr/bin/env python3
"""CI-only FP32 localization; never replaces or relaxes the frozen parity gate.

capture needs stdlib only. reference imports Paddle/numpy in an isolated venv and
replays the *actual native inputs*, not images preprocessed by a different OpenCV.
Exit zero means evidence collection completed, NOT that native parity passed.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import shutil
import sys

from ipc_client import Worker, compare
from prepare_models import digest, extract_sources, get_archive, load_pins

ROOT = Path(__file__).resolve().parent
DEFAULT_CASES = (28, 30, 31, 33, 64, 66, 67, 69, 70, 72, 73, 75)
MODES = ('baseline', 'baseline-repeat', 'bf16-off', 'extended', 'basic',
         'disabled', 'opencv-unoptimized')


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


@contextmanager
def diagnostic_environment(mode, trace_dir):
    changes = {'VSE_OCR_DIAGNOSTIC_MODE': mode.removesuffix('-repeat'),
               'VSE_OCR_DIAGNOSTIC_TRACE_DIR': str(trace_dir)}
    old = {key: os.environ.get(key) for key in changes}
    os.environ.update(changes)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def validate_trace(root, metadata):
    record = json.loads(metadata.read_text(encoding='utf-8'))
    if record.get('schema_version') != 1:
        raise ValueError('unknown trace schema')
    model = record['model']
    if Path(model).name != model or '/' in model or '\\' in model or not model.endswith('_infer.onnx'):
        raise ValueError('invalid traced model name')
    for kind in ('input', 'output'):
        tensor = record[kind]
        if tensor['dtype'] != '<f4' or not tensor['shape'] or any(type(n) is not int or n <= 0 for n in tensor['shape']):
            raise ValueError('invalid FP32 shape/dtype')
        name = tensor['file']
        if Path(name).name != name or '/' in name or '\\' in name:
            raise ValueError('invalid trace path')
        path = root / name
        if path.is_symlink() or path.stat().st_size != math.prod(tensor['shape']) * 4:
            raise ValueError('tensor byte count or path mismatch')
        observed = digest(path)
        if tensor.get('sha256', observed) != observed:
            raise ValueError('tensor hash mismatch')
        tensor['sha256'] = observed
    return record


def capture(artifact, worker, output, cases=DEFAULT_CASES, modes=MODES):
    artifact, worker, output = artifact.resolve(strict=True), worker.resolve(strict=True), output.resolve()
    if output.exists():
        raise FileExistsError('diagnostic output must not exist')
    output.mkdir(parents=True)
    runtime = output / 'diagnostic-bin'
    runtime.mkdir()
    executable = runtime / worker.name
    shutil.copy2(worker, executable)
    # Keep the opt-in binary OUTSIDE the production stage; reuse verified libs.
    for path in artifact.iterdir():
        if path.is_file() and ('.dylib' in path.name or '.so' in path.name or path.suffix.lower() == '.dll'):
            (runtime / path.name).symlink_to(path)
    goldens = json.loads((ROOT / 'tests/goldens/smoke.json').read_text(encoding='utf-8'))
    pins = load_pins(ROOT / 'model-source-pins.json')
    model_pins = {row['name'] + '_infer.onnx': row['onnx_sha256'] for row in pins['models']}
    for name, expected in pins['config_sha256'].items():
        if digest(artifact / name) != expected:
            raise ValueError('diagnostic config bytes differ from pins')
    selected = [goldens['cases'][index] for index in cases]
    for case in selected:
        if case['operation'] != 'ocr' or not case.get('rec_model'):
            raise ValueError('diagnostic requires explicit recognition model cases')
        for name in (case['rec_model'] + '_infer.onnx', f"PP-OCRv6_{case['tier']}_det_infer.onnx"):
            if digest(artifact / 'models' / name) != model_pins[name]:
                raise ValueError('diagnostic model bytes differ from pins')
    report = {'schema_version': 1, 'diagnostic_complete': False, 'release_ready': False,
              'scope': 'Evidence only: original smoke remains the hard release gate; no golden or tolerance update.',
              'host': platform.platform(), 'machine': platform.machine(), 'python': sys.version,
              'diagnostic_worker_sha256': digest(worker), 'confidence_tolerance': goldens['confidence_tolerance'],
              'goldens_sha256': digest(ROOT / 'tests/goldens/smoke.json'), 'cases': []}
    production = artifact / ('native-ocr.exe' if os.name == 'nt' else 'native-ocr')
    report['production_worker_sha256'] = digest(production)
    report['native_library_sha256'] = {path.name: digest(path) for path in artifact.iterdir()
                                      if path.is_file() and ('.dylib' in path.name or '.so' in path.name or path.suffix.lower() == '.dll')}
    report['model_sha256'] = model_pins
    report['config_sha256'] = pins['config_sha256']
    production_results = {}
    baseline_traces = {}
    for mode in ('production', *modes):
        for index, case in zip(cases, selected):
            image = ROOT / 'tests/evidence' / case['fixture']
            if digest(image) != case['fixture_sha256']:
                raise ValueError('fixture hash mismatch')
            trace_dir = output / mode / str(index)
            command = [str(production if mode == 'production' else executable),
                       '--models', str(artifact / 'models'), '--config', str(artifact / 'config')]
            request = {'id': index, 'tier': case['tier'], 'rec_model': case['rec_model'],
                       'path': str(image), 'operation': 'ocr', 'batch_size': 6, 'cpu_threads': 4}
            with diagnostic_environment(mode, trace_dir):
                process = Worker(command)
            try:
                response = process.request(request)
            finally:
                code, stderr = process.close()
            trace_dir.mkdir(parents=True, exist_ok=True)
            (trace_dir / 'stderr.txt').write_text(stderr, encoding='utf-8')
            write_json(trace_dir / 'response.json', response)
            if code != 0 or not response['ok']:
                raise RuntimeError(f'{mode}/{index}: worker failed: {response}')
            verdict = compare(response['result'], case['expected'], goldens['confidence_tolerance'])
            verdict['text_order_correct'] = response.get('text_order') == case['text_order']
            verdict['passed'] &= verdict['text_order_correct']
            if mode == 'production':
                production_results[index] = response
                traces = []
            else:
                traces = [validate_trace(trace_dir, path) for path in sorted(trace_dir.glob('[0-9]*.json'))]
                if not traces or not any(row['model'] == case['rec_model'] + '_infer.onnx' for row in traces):
                    raise ValueError('missing diagnostic recognition trace')
            row = {'mode': mode, 'id': index, 'rec_model': case['rec_model'], 'tier': case['tier'],
                   'trace_directory': str(trace_dir.relative_to(output)), 'response': response,
                   'same_as_production': response == production_results[index], 'golden_verdict': verdict, 'traces': traces}
            if mode == 'baseline':
                baseline_traces[index] = traces
            if mode not in ('production', 'baseline'):
                baseline = baseline_traces[index]
                row['same_inputs_as_baseline'] = len(traces) == len(baseline) and all(
                    a['model'] == b['model'] and a['input'] == b['input'] for a, b in zip(traces, baseline))
                row['same_outputs_as_baseline'] = len(traces) == len(baseline) and all(
                    a['model'] == b['model'] and a['output'] == b['output'] for a, b in zip(traces, baseline))
            report['cases'].append(row)
            write_json(output / 'capture.json', report)
    report['diagnostic_complete'] = True
    report['baseline_matches_production'] = all(row['same_as_production'] for row in report['cases'] if row['mode'] in ('baseline', 'baseline-repeat'))
    report['production_frozen_parity_passed'] = all(row['golden_verdict']['passed'] for row in report['cases'] if row['mode'] == 'production')
    write_json(output / 'capture.json', report)
    return report


def ctc_rows(tensor, np):
    result = []
    for row in tensor:
        ids = row.argmax(axis=-1)
        tokens, selected = [], []
        previous = -1
        total = np.float32(0)
        for step, token in enumerate(ids):
            token = int(token)
            if token and token != previous:
                tokens.append(token)
                score = row[step, token]
                selected.append(float(score))
                total = np.float32(total + score)
            previous = token
        score = float(np.float32(total / np.float32(len(tokens)))) if tokens else 0.0
        result.append({'tokens': tokens, 'selected': selected, 'confidence': score})
    return result


def compare_tensors(actual, reference, np):
    if actual.shape != reference.shape or actual.dtype != np.float32 or reference.dtype != np.float32:
        raise ValueError('reference shape/FP32 dtype mismatch')
    if not np.isfinite(actual).all() or not np.isfinite(reference).all():
        raise ValueError('non-finite reference tensor')
    delta = np.abs(actual.astype(np.float64) - reference.astype(np.float64))
    a, b = ctc_rows(actual, np), ctc_rows(reference, np)
    return {'shape': list(actual.shape), 'tensor_max_abs_delta': float(delta.max()),
            'tensor_mean_abs_delta': float(delta.mean()),
            'same_ctc_tokens': [row['tokens'] for row in a] == [row['tokens'] for row in b],
            'confidence_max_abs_delta': max(abs(x['confidence'] - y['confidence']) for x, y in zip(a, b)),
            'native_ctc': a, 'paddle_ctc': b}


def reference(output, cache, offline=False):
    # Import only in the separate build-side reference environment.
    import numpy as np
    import paddle
    from paddle.inference import Config, create_predictor
    if paddle.__version__ != '3.3.1' or np.__version__ != '2.3.5':
        raise RuntimeError('reference requires pinned Paddle 3.3.1 and numpy 2.3.5')
    output, cache = output.resolve(strict=True), cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    captured = json.loads((output / 'capture.json').read_text(encoding='utf-8'))
    if not captured['diagnostic_complete']:
        raise ValueError('capture incomplete')
    if captured['machine'] != platform.machine() or captured['host'] != platform.platform():
        raise ValueError('same-host reference cannot replay capture from a different host platform')
    pins = load_pins(ROOT / 'model-source-pins.json')
    sources = {row['name']: row for row in pins['models']}
    predictors = {}
    report = {'schema_version': 1, 'diagnostic_complete': False, 'release_ready': False,
              'host': platform.platform(), 'machine': platform.machine(), 'python': sys.version,
              'paddle_version': paddle.__version__, 'numpy_version': np.__version__,
              'installed_packages': sorted(f'{d.metadata["Name"]}=={d.version}' for d in importlib.metadata.distributions()),
              'reference_settings': {'device': 'cpu', 'threads': 4, 'onednn': False, 'ir_optim_flag': False,
                                     'pir_note': 'Paddle still runs mandatory PIR passes; see the preserved reference log.',
                                     'precision': 'FP32', 'input': 'exact traced native input bytes'},
              'scope': 'Same-host recognizer tensor replay, not an independent whole-image accuracy certification.', 'cases': []}
    for case in captured['cases']:
        for trace in case['traces']:
            name = trace['model'].removesuffix('_infer.onnx')
            if name != case['rec_model']:
                continue
            source = cache / name
            if name not in predictors:
                pin = sources[name]
                if not source.exists():
                    extract_sources(get_archive(pin, cache, offline), pin, source)
                for filename, expected in pin['files'].items():
                    if digest(source / filename) != expected:
                        raise ValueError('reference source hash mismatch')
                config = Config(str(source / 'inference.json'), str(source / 'inference.pdiparams'))
                config.disable_gpu()
                config.disable_mkldnn()
                config.switch_ir_optim(False)
                config.set_cpu_math_library_num_threads(4)
                predictors[name] = create_predictor(config)
            predictor = predictors[name]
            directory = output / case['trace_directory']
            def tensor(kind):
                data = trace[kind]
                path = directory / data['file']
                if digest(path) != data['sha256']:
                    raise ValueError('capture tensor changed before replay')
                return np.fromfile(path, dtype='<f4').reshape(data['shape'])
            x, native = tensor('input'), tensor('output')
            inputs, outputs = predictor.get_input_names(), predictor.get_output_names()
            if len(inputs) != 1 or len(outputs) != 1:
                raise ValueError('unexpected reference inputs/outputs')
            handle = predictor.get_input_handle(inputs[0])
            handle.reshape(x.shape)
            handle.copy_from_cpu(x)
            predictor.run()
            result = predictor.get_output_handle(outputs[0]).copy_to_cpu()
            verdict = compare_tensors(native, result, np)
            paddle_output = directory / ('paddle-' + trace['output']['file'])
            result.astype('<f4', copy=False).tofile(paddle_output)
            report['cases'].append({'id': case['id'], 'mode': case['mode'], 'model': name,
                                    'input_sha256': trace['input']['sha256'],
                                    'native_output_sha256': trace['output']['sha256'],
                                    'paddle_output_sha256': hashlib.sha256(result.tobytes()).hexdigest(),
                                    'paddle_output_file': str(paddle_output.relative_to(output)),
                                    'source_sha256': sources[name]['files'], **verdict})
            write_json(output / 'paddle-reference.json', report)
    if not report['cases']:
        raise ValueError('no recognition tensors replayed')
    report['diagnostic_complete'] = True
    write_json(output / 'paddle-reference.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('capture', 'reference'))
    parser.add_argument('--artifact', type=Path)
    parser.add_argument('--worker', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--cache', type=Path, default=Path('build/precision-reference-cache'))
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args()
    if args.phase == 'capture':
        if args.artifact is None or args.worker is None:
            parser.error('capture requires --artifact and --worker')
        result = capture(args.artifact, args.worker, args.output)
    else:
        result = reference(args.output, args.cache, args.offline)
    print(json.dumps({key: result[key] for key in ('diagnostic_complete', 'release_ready')}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
