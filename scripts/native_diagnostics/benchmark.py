#!/usr/bin/env python3
"""Offline, never-packaged CPU/CoreML diagnostics. No GPU claim from provider names.

Only uses an existing verified ORT SDK, model bytes and build dependencies. Never
installs packages, compiles ORT, changes GUI/device settings, or changes releases.
"""
from __future__ import annotations

import argparse
from array import array
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
MODES = ('ort-cpu', 'coreml-cpu', 'coreml-gpu-permitted')


def offline_runtime_environment():
    """Official process-lifetime opt-out; not the later API-only event filter.

    ORT 1.31.0 docs/Privacy.md and telemetry_1ds.cc::Initialize explicitly prevent
    creation of uploader/events/device ID with this set before initialization.
    Do not fake CI flags or bypass OS/network permissions. Never log the full env.
    """
    return dict(os.environ, ORT_DISABLE_TELEMETRY='1')


def diagnostic_environment(metal_capture=False):
    env = offline_runtime_environment()
    for key in ('LD_PRELOAD', 'LD_LIBRARY_PATH', 'DYLD_INSERT_LIBRARIES', 'DYLD_LIBRARY_PATH',
                'DYLD_FRAMEWORK_PATH', 'DYLD_FALLBACK_LIBRARY_PATH', 'DYLD_FALLBACK_FRAMEWORK_PATH'):
        env.pop(key, None)
    # Even capture-enabled launch overhead must not contaminate normal timings.
    env.pop('MTL_CAPTURE_ENABLED', None)
    if metal_capture:
        env['MTL_CAPTURE_ENABLED'] = '1'
    return env


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def new_directory(path):
    path = Path(path).absolute()
    if path.exists() or path.is_symlink():
        raise FileExistsError('Refusing to overwrite diagnostic output: ' + str(path))
    path.mkdir(mode=0o700, parents=True)
    return path


def pinned_file(path, expected):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or digest(path) != expected:
        raise ValueError('Pinned input integrity mismatch: ' + str(path))
    return {'path': str(path.resolve()), 'sha256': expected, 'bytes': path.stat().st_size}


def percentile(values, fraction):
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    low, high = math.floor(index), math.ceil(index)
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def summarize(values):
    if not values or any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError('Need finite, non-negative measurements')
    middle = statistics.median(values)
    return {'count': len(values), 'median_seconds': middle,
            'p25_seconds': percentile(values, .25), 'p75_seconds': percentile(values, .75),
            'p95_seconds': percentile(values, .95), 'min_seconds': min(values), 'max_seconds': max(values),
            'mad_seconds': statistics.median(abs(value - middle) for value in values)}


def profile_evidence(directory, sessions, mode='ort-cpu'):
    durations, calls = Counter(), Counter()
    nodes = {}
    profile_files = []
    for session in sessions:
        name = session.get('profile')
        if not name:
            continue
        if Path(name).name != name:
            raise ValueError('Profile must be local to the evidence directory')
        path = directory / name
        profile_files.append({'file': name, 'sha256': digest(path), 'model': session['model']})
        for event in read_json(path):
            provider = event.get('args', {}).get('provider')
            # ORT kernel events prove EP execution, not the CoreML internal device.
            if (event.get('cat') != 'Node' or not provider or
                    not event.get('name', '').endswith('_kernel_time')):
                continue
            duration = event.get('dur', 0)
            if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration < 0:
                raise ValueError('Invalid ORT profile duration')
            calls[provider] += 1
            durations[provider] += duration
            nodes.setdefault(provider, set()).add((session['model'], event.get('name', '')))
    return {'profiles': profile_files, 'observed_kernel_calls': dict(calls),
            'unique_profile_nodes': {key: len(value) for key, value in nodes.items()},
            'kernel_duration_microseconds': dict(durations),
            'cpu_ep_execution_observed': calls['CPUExecutionProvider'] > 0,
            'cpu_ep_fallback_observed': mode != 'ort-cpu' and calls['CPUExecutionProvider'] > 0,
            'coreml_ep_execution_observed': calls['CoreMLExecutionProvider'] > 0,
            'gpu_execution_verified': False,
            'scope': 'ORT EP execution only; fused-node counts are not graph/operator coverage or GPU share; '
                     'CPU work inside CoreML is not visible here. ProfileComputePlan is a preference/estimate.'}


def pipeline_parity(actual, expected, tolerance=1e-5):
    if len(actual) != len(expected):
        return {'passed': False, 'reason': 'image count differs'}
    boxes = texts = finite = True
    maximum = 0.0
    for left, right in zip(actual, expected):
        boxes &= left[0] == right[0]
        texts &= [item[0] for item in left[1]] == [item[0] for item in right[1]]
        if len(left[1]) != len(right[1]):
            finite = False
        for lrow, rrow in zip(left[1], right[1]):
            a, b = float(lrow[1]), float(rrow[1])
            finite &= math.isfinite(a) and math.isfinite(b)
            if math.isfinite(a) and math.isfinite(b):
                maximum = max(maximum, abs(a - b))
    return {'same_boxes': boxes, 'same_text': texts, 'finite_confidences': finite,
            'max_confidence_delta': maximum, 'confidence_tolerance': tolerance,
            'passed': boxes and texts and finite and maximum <= tolerance}


def tensor_parity(left, right, atol=1e-5, rtol=1e-5):
    if left.stat().st_size != right.stat().st_size:
        return {'passed': False, 'reason': 'output size differs'}
    a, b = array('f'), array('f')
    a.frombytes(left.read_bytes())
    b.frombytes(right.read_bytes())
    if sys.byteorder != 'little':
        a.byteswap(); b.byteswap()
    maximum = total = 0.0
    outside = nonfinite = 0
    for x, y in zip(a, b):
        if not math.isfinite(x) or not math.isfinite(y):
            nonfinite += 1
            continue
        delta = abs(x - y)
        maximum = max(maximum, delta)
        total += delta
        outside += delta > atol + rtol * abs(x)
    return {'exact_sha256_equal': digest(left) == digest(right), 'elements': len(a),
            'max_absolute_delta': maximum, 'mean_absolute_delta': total / max(1, len(a)),
            'outside_tolerance': outside, 'nonfinite': nonfinite, 'atol': atol, 'rtol': rtol,
            'passed': outside == 0 and nonfinite == 0,
            'scope': 'Numerical diagnostic only, not a relaxation of frozen OCR golden tolerance'}


def verify_models(artifact):
    pins = read_json(ROOT / 'native/ocr/model-source-pins.json')
    models = {row['name'] + '_infer.onnx': pinned_file(
        artifact / 'models' / (row['name'] + '_infer.onnx'), row['onnx_sha256']) for row in pins['models']}
    configs = {name: pinned_file(artifact / name, sha) for name, sha in pins['config_sha256'].items()}
    return models, configs


def target_for_host():
    key = platform.system(), platform.machine()
    if key == ('Linux', 'x86_64'):
        return 'linux-x64'
    if key == ('Darwin', 'arm64'):
        return 'macos-arm64'
    raise ValueError('Only Linux x64 validation and native macOS arm64 are supported (no Rosetta)')


def verify_runtime(path):
    manifest = read_json(ROOT / 'native/ocr/ort-sdk-manifest.json')
    target = target_for_host()
    record = manifest['targets'][target]
    expected = next(row for row in record['files'] if row['path'] == record['library'])
    # dladdr can return the SDK symlink. Verify the final physical file's bytes.
    return pinned_file(Path(path).resolve(strict=True), expected['sha256'])


def stage_runtime(sdk, record, target, output):
    """Copy only existing pinned runtime bytes beside the relocatable frontend."""
    files = []
    for row in record['files']:
        if row['role'] != 'runtime':
            continue
        source = sdk / row['path']
        pinned_file(source, row['sha256'])
        name = Path(row['path']).name
        if row['path'] == record['library']:
            name = 'libonnxruntime.1.dylib' if target == 'macos-arm64' else 'libonnxruntime.so.1'
        destination = output / name
        shutil.copy2(source, destination)
        files.append({'file': name, **pinned_file(destination, row['sha256'])})
    return files


def stage_runtime_notices(sdk, record, output):
    directory = output / 'ort-notices'
    directory.mkdir()
    files = []
    for row in record['files']:
        if row['role'] != 'notice':
            continue
        source = sdk / row['path']
        pinned_file(source, row['sha256'])
        destination = directory / Path(row['path']).name
        shutil.copy2(source, destination)
        files.append({'file': destination.relative_to(output).as_posix(),
                      **pinned_file(destination, row['sha256'])})
    return files


def validate_macos_linkage(dependencies, commands):
    """Reject CI/build-directory linkage instead of requiring DYLD overrides."""
    libraries = [line.strip().split(' (compatibility version', 1)[0]
                 for line in dependencies.splitlines()[1:] if line.strip()]
    expected = '@rpath/libonnxruntime.1.dylib'
    if expected not in libraries or any(name != expected and not name.startswith(
            ('/System/Library/', '/usr/lib/')) for name in libraries):
        raise ValueError('Diagnostic has non-portable non-system dependencies')
    lines = commands.splitlines()
    paths = [lines[index + 2].strip().split(' (offset ', 1)[0]
             for index, line in enumerate(lines) if line.strip() == 'cmd LC_RPATH']
    if paths != ['path @loader_path']:
        raise ValueError('Diagnostic must resolve its pinned runtime only beside itself')
    return {'dependencies': libraries, 'rpaths': ['@loader_path']}


def relocated_runtime_probe(binary, runtime_files):
    """Execute a copied frontend outside its build tree, with no loader override."""
    with tempfile.TemporaryDirectory(prefix='vse-relocated-diagnostic-') as temporary:
        root = Path(temporary).resolve()
        copied = root / binary.name
        shutil.copy2(binary, copied)
        for row in runtime_files:
            shutil.copy2(binary.parent / row['file'], root / row['file'])
            pinned_file(root / row['file'], row['sha256'])
        result = subprocess.run([str(copied), '--probe'], capture_output=True, text=True,
                                timeout=60, env=diagnostic_environment())
        report = {'scope': 'Relocated capability-only process; no models, sessions, capture or GPU proof',
                  'returncode': result.returncode, 'stderr': result.stderr,
                  'loader_environment_override': False, 'passed': False}
        if result.returncode != 0 or len(result.stdout.encode('utf-8')) > 65536:
            return report
        value = json.loads(result.stdout)
        library = Path(value['loaded_ort_library']).resolve(strict=True)
        allowed = {row['file']: row['sha256'] for row in runtime_files}
        if library.parent != root or library.name not in allowed or value['ort_version'] != '1.31.0':
            raise ValueError('Relocated frontend loaded a runtime outside its pinned sidecars')
        report.update(passed=True, loaded_runtime_file=library.name,
                      runtime_sha256=pinned_file(library, allowed[library.name])['sha256'],
                      runner_sha256=digest(copied))
        return report


def build(args):
    target = target_for_host()
    sdk = args.sdk.resolve(strict=True)
    manifest = read_json(ROOT / 'native/ocr/ort-sdk-manifest.json')
    record = manifest['targets'][target]
    if manifest['version'] != '1.31.0':
        raise ValueError('This diagnostic requires reviewed ORT 1.31.0 pins')
    sdk_inputs = [pinned_file(sdk / row['path'], row['sha256']) for row in record['files']
                  if row['role'] in ('header', 'runtime')]
    sources = [pinned_file(args.deps / row['file'], row['sha256'])
               for row in read_json(ROOT / 'native/ocr/deps/source-pins.json')
               if row['file'] in ('clipper.cpp', 'clipper.hpp', 'json.hpp')]
    output = new_directory(args.output)
    command = [str(args.cmake), '-S', str(HERE), '-B', str(output), '-DCMAKE_BUILD_TYPE=Release',
               '-DORT_INCLUDE_DIR=' + str(sdk / record['include']),
               '-DORT_LIBRARY=' + str(sdk / record['link_library']),
               '-DOCR_DEPS_DIR=' + str(args.deps.resolve(strict=True)),
               '-DOpenCV_DIR=' + str(args.opencv_dir.resolve(strict=True))]
    if target == 'macos-arm64':
        command += ['-DCMAKE_OSX_ARCHITECTURES=arm64', '-DCMAKE_OSX_DEPLOYMENT_TARGET=14.0']
    commands = [command, [str(args.cmake), '--build', str(output), '--parallel', str(args.jobs)]]
    provenance = {'schema_version': 1, 'diagnostic_only': True, 'target': target,
                  'sdk_inputs': sdk_inputs, 'frontend_sources': sources,
                  'production_worker_source_sha256': digest(ROOT / 'native/ocr/worker.cpp'),
                  'production_included_source_sha256': {
                      name: digest(ROOT / 'native/ocr' / name)
                      for name in ('worker.cpp', 'provider_evidence.hpp', 'protocol_output.hpp', 'input_shape.hpp', 'metal_capture.hpp',
                                   'metal_capture.mm', 'metal_capture_stub.cpp')},
                  'diagnostic_source_sha256': {p.name: digest(p) for p in HERE.iterdir() if p.is_file()},
                  'commands': commands, 'runtime_rebuilt': False,
                  'opencv_static_archives': [{'path': str(p.resolve()), 'sha256': digest(p)}
                                              for p in sorted(args.opencv_dir.resolve().parents[1].rglob('*.a'))],
                  'opencv_dependency_scope': 'Existing caller-supplied OpenCV; actual build string in probe. '
                                             'No claim this matches a release until its build provenance is compared.'}
    write_json(output / 'build-inputs.json', provenance)
    for index, command in enumerate(commands):
        with (output / f'build-{index}.log').open('w') as stream:
            subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, check=True)
    binary = output / 'vse-provider-diagnostic'
    provenance['bundled_runtime_files'] = stage_runtime(sdk, record, target, output)
    provenance['bundled_runtime_notices'] = stage_runtime_notices(sdk, record, output)
    if target == 'macos-arm64':
        dependencies = subprocess.check_output(['otool', '-L', str(binary)], text=True)
        commands = subprocess.check_output(['otool', '-l', str(binary)], text=True)
        provenance['relocatable_linkage'] = validate_macos_linkage(dependencies, commands)
    provenance['binary_sha256'] = digest(binary)
    provenance['relocated_runtime_probe'] = relocated_runtime_probe(binary, provenance['bundled_runtime_files'])
    write_json(output / 'build-inputs.json', provenance)
    if not provenance['relocated_runtime_probe']['passed']:
        raise ValueError('Diagnostic relocation probe failed; see build-inputs.json, no loader override attempted')
    provenance['build_succeeded'] = True
    write_json(output / 'build-inputs.json', provenance)
    print(binary)


def build_probe(args):
    target = target_for_host()
    sdk = args.sdk.resolve(strict=True)
    record = read_json(ROOT / 'native/ocr/ort-sdk-manifest.json')['targets'][target]
    names = {'onnxruntime_c_api.h', 'onnxruntime_error_code.h', 'onnxruntime_ep_c_api.h'}
    inputs = [pinned_file(sdk / row['path'], row['sha256']) for row in record['files']
              if Path(row['path']).name in names]
    if len(inputs) != len(names):
        raise ValueError('Incomplete pinned C API headers')
    output = new_directory(args.output)
    binary = output / 'vse-runtime-probe'
    command = [str(args.cxx), '-std=c++17', '-O2', '-I', str(sdk / 'include'),
               str(HERE / 'runtime_probe.cpp'), '-o', str(binary)]
    if target == 'linux-x64':
        command.append('-ldl')
    with (output / 'build.log').open('w') as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
    b = {'schema_version': 1, 'inputs': inputs, 'source_sha256': digest(HERE / 'runtime_probe.cpp'),
         'command': command, 'binary_sha256': digest(binary), 'runtime_rebuilt': False,
         'dependencies': 'system C++ and dynamic-loader API only; no OpenCV or model'}
    write_json(output / 'build-inputs.json', b)
    print(binary)


def runtime_probe(args):
    library = args.library.resolve(strict=True)
    before = verify_runtime(library)
    output = new_directory(args.output)
    result = subprocess.run([str(args.runner.resolve(strict=True)), str(library)],
                            capture_output=True, text=True, timeout=30, check=True,
                            env=offline_runtime_environment())
    report = json.loads(result.stdout)
    actual = verify_runtime(report['loaded_ort_library'])
    if before['sha256'] != actual['sha256']:
        raise ValueError('Loaded runtime differs from the preverified library')
    report.update({'runtime_integrity': actual, 'runner_sha256': digest(args.runner),
                   'telemetry_optout_before_exec': True})
    write_json(output / 'runtime-probe.json', report)
    print(output / 'runtime-probe.json')


def probe(binary):
    result = subprocess.run([str(binary), '--probe'], capture_output=True, text=True, timeout=60, check=True,
                            env=diagnostic_environment())
    evidence = json.loads(result.stdout)
    if evidence['ort_version'] != '1.31.0':
        raise ValueError('Unexpected runtime version')
    evidence['runtime_integrity'] = verify_runtime(evidence['loaded_ort_library'])
    evidence.update({'host': platform.platform(), 'machine': platform.machine(),
                     'telemetry_policy': 'ORT_DISABLE_TELEMETRY=1 set before child process initialization; '
                                         'DisableTelemetryEvents additionally called before sessions',
                     'runner_sha256': digest(binary), 'gpu_execution_verified': False,
                     'xctrace': {'available': False, 'execution_authorized': 'not tested'}})
    if platform.system() == 'Darwin':
        for label, command in {
            'os': ['sw_vers'], 'cpu': ['sysctl', '-n', 'machdep.cpu.brand_string'],
            'cpu_count': ['sysctl', '-n', 'hw.ncpu'], 'memory': ['sysctl', '-n', 'hw.memsize'],
            'power': ['pmset', '-g', 'batt']}.items():
            value = subprocess.run(command, capture_output=True, text=True, timeout=30)
            evidence[label] = {'returncode': value.returncode, 'stdout': value.stdout.strip(), 'stderr': value.stderr.strip()}
        value = subprocess.run(['xcrun', '--find', 'xctrace'], capture_output=True, text=True, timeout=30)
        evidence['xctrace'].update({'available': value.returncode == 0, 'path': value.stdout.strip(),
                                    'stderr': value.stderr.strip(), 'trace_recorded': False})
    return evidence


def metal_probe(binary):
    child = subprocess.run([str(binary), '--probe-metal'], capture_output=True, text=True,
                           timeout=60, check=True, env=diagnostic_environment(True))
    result = json.loads(child.stdout)
    result['runtime_integrity'] = verify_runtime(result['loaded_ort_library'])
    result.update({'runner_sha256': digest(binary), 'gpu_execution_verified': False,
                   'capture_executed': False, 'permission_changes_attempted': False})
    return result


def execute(binary, root, label, request, timeout):
    request_path, directory = root / (label + '.request.json'), root / label
    write_json(request_path, request)
    started = time.monotonic()
    with (root / (label + '.stderr.txt')).open('w', encoding='utf-8') as log:
        try:
            child = subprocess.run([str(binary), str(request_path), str(directory)],
                                   stdout=log, stderr=subprocess.STDOUT, timeout=timeout,
                                   env=diagnostic_environment(request.get('metal_capture', False)))
            status = child.returncode
        except subprocess.TimeoutExpired:
            return {'status': 'timeout', 'process_wall_seconds': time.monotonic() - started,
                    'request': request_path.name, 'log': log.name}
    wall = time.monotonic() - started
    if status:
        return {'status': 'failed', 'returncode': status, 'process_wall_seconds': wall,
                'request': request_path.name, 'log': log.name}
    result = read_json(directory / 'result.json')
    result.update({'status': 'completed', 'process_wall_seconds': wall, 'directory': directory.name})
    result['runtime_integrity'] = verify_runtime(result['loaded_ort_library'])
    if request['evidence']:
        result['execution_evidence'] = profile_evidence(directory, result['sessions'], request['mode'])
        result['generated_coreml_files'] = [{'file': str(p.relative_to(directory)), 'sha256': digest(p)}
                                             for p in sorted(directory.glob('coreml-cache-*/**/*')) if p.is_file()]
        for trace in result['traces']:
            for kind in ('input', 'output'):
                item = trace[kind]
                file = directory / item['file']
                if file.stat().st_size != math.prod(item['shape']) * 4:
                    raise ValueError('Invalid captured FP32 tensor byte count')
                item['sha256'] = digest(file)
    elif result['samples']:
        for key in ('wall_seconds', 'ort_run_seconds', 'lazy_session_seconds'):
            result[key + '_summary'] = summarize([row[key] for row in result['samples']])
    return result


def capture_case(args):
    """One frozen OCR case; capture is separate from every performance run."""
    binary, artifact = args.runner.resolve(strict=True), args.artifact.resolve(strict=True)
    models, configs = verify_models(artifact)
    goldens, cases = selected_cases([args.case])
    index, case = cases[0]
    host = metal_probe(binary)
    if not host.get('metal_capture', {}).get('available'):
        raise ValueError('Metal capture capability unavailable; no alternative profiler attempted')
    if args.mode != 'ort-cpu' and 'CoreMLExecutionProvider' not in host['available_providers']:
        raise ValueError('CoreML EP absent; no CPU substitution')
    output = new_directory(args.output)
    request = {'schema_version': 1, 'kind': 'pipeline', 'mode': args.mode,
               'models': str(artifact / 'models'), 'config': str(artifact / 'config'),
               'tier': case['tier'], 'rec_model': case['rec_model'], 'images': [case['image']],
               'threads': args.threads, 'batch_size': args.batch_size, 'timeout_ms': 120000,
               'evidence': True, 'metal_capture': True, 'warmup': 0, 'repeats': 0}
    result = execute(binary, output, 'capture', request, args.timeout)
    report = {'schema_version': 1, 'host': host, 'case_index': index,
              'fixture_sha256': case['fixture_sha256'], 'models': models, 'configs': configs,
              'mode': args.mode, 'result': result, 'gpu_execution_verified': False,
              'timing_eligible': False, 'gpu_speedup': None,
              'scope': 'Application-local default Metal device capture. CoreML device-object match, '
                       'command attribution and actual executed GPU dispatch must be independently '
                       'verified. A trace file, commands submitted, or a CoreML EP profile alone '
                       'do not certify GPU execution; never use these timings as performance data.'}
    if result['status'] == 'completed':
        result['frozen_golden_parity'] = pipeline_parity(result['first_result'], [case['expected']],
                                                        goldens['confidence_tolerance'])
        trace = output / result['directory'] / 'metal-capture.gputrace'
        entries = sorted(trace.rglob('*')) if trace.is_dir() else [trace]
        if trace.is_symlink() or any(path.is_symlink() for path in entries):
            raise ValueError('Refusing to read links in captured trace')
        report['trace_files'] = [{'file': str(path.relative_to(output)), 'sha256': digest(path),
                                  'bytes': path.stat().st_size} for path in entries if path.is_file()]
        report['trace_files_present'] = bool(report['trace_files'])
    write_json(output / 'capture-report.json', report)
    print(output / 'capture-report.json')


def aggregate_runs(runs, input_count=1):
    completed = [run for run in runs if run['status'] == 'completed']
    if not completed:
        return {'completed_processes': 0, 'failed_processes': len(runs)}
    process_medians = [run['wall_seconds_summary']['median_seconds'] for run in completed]
    median = statistics.median(process_medians)
    return {'completed_processes': len(completed), 'failed_processes': len(runs) - len(completed),
            'per_process_median_summary': summarize(process_medians),
            'pooled_warm_samples': summarize([s['wall_seconds'] for run in completed for s in run['samples']]),
            'cold_entry_to_first_prediction': summarize([run['process_entry_to_first_prediction_seconds'] for run in completed]),
            'inputs_per_second': input_count / median if median else None,
            'max_process_rss_bytes': max(run['max_rss_bytes'] for run in completed),
            'gpu_memory_bytes': None,
            'memory_scope': 'Process peak RSS only; not GPU VRAM or whole process-tree/unified-memory total'}


def selected_cases(indices=None):
    goldens = read_json(ROOT / 'native/ocr/tests/goldens/smoke.json')
    if indices is None:
        chosen = {}
        for index, case in enumerate(goldens['cases']):
            name = case.get('rec_model', f"PP-OCRv6_{case['tier']}_rec")
            if case['operation'] == 'ocr' and case['expected'][1] and name not in chosen:
                chosen[name] = index
        indices = list(chosen.values())
    rows = []
    for index in indices:
        case = dict(goldens['cases'][index])
        if case['operation'] != 'ocr':
            raise ValueError('Pipeline benchmark requires OCR fixtures')
        case['rec_model'] = case.get('rec_model', f"PP-OCRv6_{case['tier']}_rec")
        case['image'] = str((ROOT / 'native/ocr/tests/evidence' / case['fixture']).resolve(strict=True))
        pinned_file(case['image'], case['fixture_sha256'])
        rows.append((index, case))
    return goldens, rows


def suite(args):
    binary, artifact = args.runner.resolve(strict=True), args.artifact.resolve(strict=True)
    models, configs = verify_models(artifact)
    host = probe(binary)
    output = new_directory(args.output)
    goldens, cases = selected_cases(args.cases)
    report = {'schema_version': 1, 'diagnostic_complete': False, 'release_ready': False,
              'host': host, 'models': models, 'configs': configs,
              'requested_modes': args.modes, 'unsupported_modes': [], 'cases': [], 'tensor_cases': [],
              'gpu_execution_verified': False, 'gpu_speedup': None,
              'production_video_end_to_end_seconds': None,
              'scope': 'Configuration performance, not proven GPU acceleration. Frozen OCR inputs and '
                       'FP32 tensor replay; no device-dependent frame-selection algorithm change.',
              'precision': {'onnx_dtype': 'FP32 pinned bytes', 'coreml_format': 'MLProgram',
                            'low_precision_accumulation_allowed': False, 'ane_allowed': False,
                            'internal_coreml_precision_verified': False,
                            'note': 'Inspect generated MLProgram types before claiming matched internal '
                                    'precision. FP32 I/O or accumulation option alone is not proof.'},
              'cold_scope': 'Fresh process/session, no explicit CoreML model cache; OS/filesystem caches '
                            'are not purged. First-pass initialization is separate from warmed samples.',
              'timing_policy': {'warmup_passes_after_cold': args.warmup, 'repeats': args.repeats,
                                'process_repeats': args.process_repeats, 'order_seed': args.seed,
                                'profiling_enabled_during_timing': False}}
    modes = []
    for mode in args.modes:
        if mode != 'ort-cpu' and 'CoreMLExecutionProvider' not in host['available_providers']:
            report['unsupported_modes'].append({'mode': mode, 'reason': 'CoreML EP absent; no CPU substitution'})
        else:
            modes.append(mode)
    if 'ort-cpu' not in modes:
        raise ValueError('ORT CPU baseline is required')
    common = {'schema_version': 1, 'threads': args.threads, 'batch_size': args.batch_size,
              'timeout_ms': 120000, 'warmup': args.warmup, 'repeats': args.repeats}
    frozen_tensors = {}
    randomizer = random.Random(args.seed)
    for index, case in cases:
        request = {**common, 'kind': 'pipeline', 'models': str(artifact / 'models'),
                   'config': str(artifact / 'config'), 'tier': case['tier'],
                   'rec_model': case['rec_model'], 'images': [case['image']]}
        row = {'case_index': index, 'fixture_sha256': case['fixture_sha256'],
               'rec_model': case['rec_model'], 'evidence': {}, 'timings': {mode: [] for mode in modes}}
        for mode in modes:
            evidence = execute(binary, output, f'pipeline-{index}-{mode}-evidence',
                               {**request, 'mode': mode, 'evidence': True, 'warmup': 0, 'repeats': 1}, args.timeout)
            row['evidence'][mode] = evidence
            if evidence['status'] == 'completed':
                evidence['frozen_golden_parity'] = pipeline_parity(evidence['first_result'], [case['expected']], goldens['confidence_tolerance'])
                if mode == 'ort-cpu':
                    for trace in evidence['traces']:
                        frozen_tensors.setdefault(trace['model'], (output / evidence['directory'], trace))
                else:
                    baseline = row['evidence'].get('ort-cpu', {})
                    if baseline.get('status') == 'completed':
                        evidence['same_mac_ort_cpu_parity'] = pipeline_parity(evidence['first_result'], baseline['first_result'], goldens['confidence_tolerance'])
        for iteration in range(args.process_repeats):
            order = list(modes)
            randomizer.shuffle(order)
            for mode in order:
                if row['evidence'][mode]['status'] != 'completed':
                    continue
                measured = execute(binary, output, f'pipeline-{index}-{mode}-timing-{iteration}',
                                   {**request, 'mode': mode, 'evidence': False}, args.timeout)
                row['timings'][mode].append(measured)
        report['cases'].append(row)
        row['aggregates'] = {mode: aggregate_runs(runs) for mode, runs in row['timings'].items()}
        write_json(output / 'report.json', report)
    for model, (directory, trace) in sorted(frozen_tensors.items()):
        request = {**common, 'kind': 'tensor', 'model': models[model]['path'],
                   'inputs': [{'path': str(directory / trace['input']['file']),
                               'shape': trace['input']['shape'], 'dtype': '<f4'}]}
        row = {'model': model, 'model_sha256': models[model]['sha256'],
               'input_sha256': trace['input']['sha256'], 'shape': trace['input']['shape'],
               'evidence': {}, 'timings': {mode: [] for mode in modes}}
        baseline_output = None
        for mode in modes:
            evidence = execute(binary, output, f'tensor-{model}-{mode}-evidence',
                               {**request, 'mode': mode, 'evidence': True, 'warmup': 0, 'repeats': 1}, args.timeout)
            row['evidence'][mode] = evidence
            if evidence['status'] == 'completed':
                actual = evidence['traces'][0]
                if actual['input']['sha256'] != trace['input']['sha256']:
                    raise ValueError('Tensor replay input changed')
                current_output = output / evidence['directory'] / actual['output']['file']
                if mode == 'ort-cpu':
                    baseline_output = current_output
                elif baseline_output:
                    evidence['tensor_parity'] = tensor_parity(baseline_output, current_output)
        for iteration in range(args.process_repeats):
            order = list(modes)
            randomizer.shuffle(order)
            for mode in order:
                if row['evidence'][mode]['status'] == 'completed':
                    row['timings'][mode].append(execute(binary, output, f'tensor-{model}-{mode}-timing-{iteration}',
                                                       {**request, 'mode': mode, 'evidence': False}, args.timeout))
        report['tensor_cases'].append(row)
        row['aggregates'] = {mode: aggregate_runs(runs) for mode, runs in row['timings'].items()}
        write_json(output / 'report.json', report)
    report['model_coverage'] = {'expected': sorted(models), 'captured_and_replayed': sorted(frozen_tensors),
                                'all_15_models_covered': set(models) == set(frozen_tensors)}
    report['diagnostic_complete'] = True
    report['all_requested_modes_executed'] = not report['unsupported_modes'] and all(
        value['status'] == 'completed' for row in report['cases'] + report['tensor_cases']
        for value in row['evidence'].values())
    report['all_requested_runs_succeeded'] = report['all_requested_modes_executed'] and all(
        len(runs) == args.process_repeats and all(run['status'] == 'completed' for run in runs)
        for row in report['cases'] + report['tensor_cases'] for runs in row['timings'].values())
    report['coreml_execution_coverage'] = {
        mode: {'tensor_models_with_observed_coreml_execution': [row['model'] for row in report['tensor_cases']
                    if row['evidence'].get(mode, {}).get('execution_evidence', {}).get('coreml_ep_execution_observed')],
               'models_without_observed_coreml_execution': [row['model'] for row in report['tensor_cases']
                    if not row['evidence'].get(mode, {}).get('execution_evidence', {}).get('coreml_ep_execution_observed')],
               'gpu_execution_verified': False}
        for mode in modes if mode != 'ort-cpu'}
    write_json(output / 'report.json', report)
    print(output / 'report.json')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    command = commands.add_parser('build-probe', help='Compile a tiny system-only C API probe; no OpenCV/models/runtime build')
    command.add_argument('--sdk', type=Path, required=True)
    command.add_argument('--cxx', type=Path, required=True)
    command.add_argument('--output', type=Path, required=True)
    command.set_defaults(action=build_probe)
    command = commands.add_parser('runtime-probe', help='Only enumerate providers in an existing hash-verified library')
    command.add_argument('--runner', type=Path, required=True)
    command.add_argument('--library', type=Path, required=True)
    command.add_argument('--output', type=Path, required=True)
    command.set_defaults(action=runtime_probe)
    command = commands.add_parser('build', help='Compile only the tiny diagnostic frontend from offline inputs')
    command.add_argument('--sdk', type=Path, required=True)
    command.add_argument('--deps', type=Path, required=True)
    command.add_argument('--opencv-dir', type=Path, required=True)
    command.add_argument('--cmake', type=Path, default=Path('cmake'))
    command.add_argument('--jobs', type=int, default=2)
    command.add_argument('--output', type=Path, required=True)
    command.set_defaults(action=build)
    command = commands.add_parser('probe', help='Read runtime/hardware/tool availability; no GUI/profiler launch')
    command.add_argument('--runner', type=Path, required=True)
    command.add_argument('--output', type=Path, required=True)
    command.set_defaults(action=lambda a: write_json(new_directory(a.output) / 'probe.json', probe(a.runner.resolve(strict=True))))
    command = commands.add_parser('metal-probe', help='Explicit public in-process capture capability check; does not capture')
    command.add_argument('--runner', type=Path, required=True)
    command.add_argument('--output', type=Path, required=True)
    command.set_defaults(action=lambda a: write_json(new_directory(a.output) / 'metal-probe.json', metal_probe(a.runner.resolve(strict=True))))
    command = commands.add_parser('metal-capture', help='Explicit single frozen-case evidence only; never performance data')
    command.add_argument('--runner', type=Path, required=True)
    command.add_argument('--artifact', type=Path, required=True)
    command.add_argument('--output', type=Path, required=True)
    command.add_argument('--case', type=int, required=True)
    command.add_argument('--mode', choices=MODES, required=True)
    command.add_argument('--threads', type=int, default=4)
    command.add_argument('--batch-size', type=int, default=6)
    command.add_argument('--timeout', type=int, default=240)
    command.set_defaults(action=capture_case)
    command = commands.add_parser('suite', help='Same-host frozen pipeline + tensor tests for all 15 pinned models')
    command.add_argument('--runner', type=Path, required=True)
    command.add_argument('--artifact', type=Path, required=True)
    command.add_argument('--output', type=Path, required=True)
    command.add_argument('--modes', nargs='+', choices=MODES, default=list(MODES))
    command.add_argument('--cases', type=int, nargs='+')
    command.add_argument('--threads', type=int, default=4)
    command.add_argument('--batch-size', type=int, default=6)
    command.add_argument('--warmup', type=int, default=3)
    command.add_argument('--repeats', type=int, default=10)
    command.add_argument('--process-repeats', type=int, default=3)
    command.add_argument('--seed', type=int, default=0)
    command.add_argument('--timeout', type=int, default=600)
    command.set_defaults(action=suite)
    args = parser.parse_args(argv)
    if args.command == 'suite':
        if len(set(args.modes)) != len(args.modes) or args.modes[0] != 'ort-cpu':
            parser.error('Modes must be unique and start with ort-cpu baseline')
        if args.warmup < 0 or args.repeats < 1 or args.process_repeats < 1 or args.timeout < 1:
            parser.error('Invalid repetition/timeout count')
    args.action(args)


if __name__ == '__main__':
    main()
