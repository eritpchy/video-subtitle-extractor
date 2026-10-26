#!/usr/bin/env python3
"""Real Python-source CLI/video acceptance; never native ZIP or GPU certification."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.e2e_smoke import MODES, check_srt, generate_video


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(data)
    return digest.hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate Python source evidence field')
        result[key] = value
    return result


def collect_records(directory, *, provider, mode):
    from backend.tools.python_coreml_ocr import validate_python_coreml_records
    documents, index = [], []
    paths = sorted(directory.iterdir())
    if not 0 < len(paths) <= 10000:
        raise ValueError('Missing or excessive Python source request records')
    for path in paths:
        if (path.is_symlink() or not path.is_file() or
                not re.fullmatch(r'python-ocr-[1-9][0-9]*-[1-9][0-9]*-[1-9][0-9]*\.json', path.name)
                or path.stat().st_size > 16 * 1024 * 1024):
            raise ValueError('Unexpected Python source request evidence file')
        data = path.read_bytes()
        document = json.loads(data, object_pairs_hook=unique_object)
        expected = 'python-ocr-{}-{}-{}.json'.format(
            document.get('process_pid'), document.get('engine_id'), document.get('request_id'))
        if path.name != expected:
            raise ValueError('Python source filename does not bind the actual process/engine/request')
        documents.append(document)
        index.append({'path': path.name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    executed = validate_python_coreml_records(documents, provider=provider)
    tier = 'medium' if mode == 'accurate' else 'small'
    if executed != {f'PP-OCRv6_{tier}_det', f'PP-OCRv6_{tier}_rec'}:
        raise ValueError('Python CLI did not actually execute both requested models')
    return {'executed_models': sorted(executed), 'python_requests': index}


def environment(models, configuration, evidence):
    # Clean source selection cannot be inherited from a packaged/native profile,
    # Python path injection, or an unrelated loader/capture environment.
    values = {key: value for key, value in os.environ.items()
              if not key.startswith(('VSE_', 'DYLD_')) and key not in
              ('PYTHONPATH', 'PYTHONHOME', 'LD_PRELOAD', 'LD_LIBRARY_PATH', 'MTL_CAPTURE_ENABLED')}
    values.update(VSE_OCR_BACKEND='python-coreml', VSE_PYTHON_COREML_MODEL_ROOT=str(models),
                  VSE_CONFIG_FILE=str(configuration), VSE_PYTHON_COREML_EVIDENCE_DIR=str(evidence),
                  VSE_NATIVE_OCR_ROOT=str(evidence / 'absent-native-worker'),
                  VSE_OFFLINE='1', VSE_OCR_CPU_THREADS='2', ORT_DISABLE_TELEMETRY='1',
                  PYTHONUNBUFFERED='1', PYTHONDONTWRITEBYTECODE='1', QT_QPA_PLATFORM='offscreen',
                  PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK='True')
    return values


def run_case(output, models, video, *, provider, mode, timeout):
    if provider not in ('cpu', 'coreml') or mode not in MODES:
        raise ValueError('Choose an explicit Python provider and production mode')
    output.mkdir(exist_ok=False)
    evidence = output / 'python-requests'
    evidence.mkdir()
    configuration = output / 'isolated-config.json'
    saved = json.dumps({'Main': {'Language': 'korean', 'Mode': 'fast' if mode == 'accurate' else 'accurate', 'SubtitleArea': 1}})
    configuration.write_text(saved, encoding='utf-8')
    subtitles = output / 'recognized.srt'
    command = [sys.executable, '-m', 'backend.cli', str(video), '--output', str(subtitles),
               '--language', 'en', '--mode', mode, *(['--cpu'] if provider == 'cpu' else []),
               '--threads', '2', '--area', '0.6,1,0,1', '--overwrite', '--no-word-segmentation']
    with (output / 'extraction.log').open('w', encoding='utf-8') as log:
        started = time.perf_counter_ns()
        process = subprocess.run(command, cwd=ROOT, env=environment(models, configuration, evidence),
                                 stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
        elapsed = time.perf_counter_ns() - started
    if process.returncode:
        raise RuntimeError(f'Python CLI exited {process.returncode}; see {output / "extraction.log"}')
    if configuration.read_text(encoding='utf-8') != saved:
        raise ValueError('Python CLI modified saved GUI settings')
    result = {'result': 'passed', 'provider': provider, 'mode': mode,
              'cpu_forced': provider == 'cpu', 'command': command,
              'elapsed_ns': elapsed, 'timing_is_benchmark': False,
              'input_video_sha256': sha(video), 'srt_sha256': sha(subtitles),
              'gpu_execution_verified': False, **check_srt(subtitles),
              **collect_records(evidence, provider=provider, mode=mode)}
    (output / 'report.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    return result


def run(output, models, providers, timeout=900):
    output, models = Path(output).resolve(), Path(models).resolve(strict=True)
    if (not providers or len(set(providers)) != len(providers) or
            any(value not in ('cpu', 'coreml') for value in providers) or
            type(timeout) not in (int, float) or not 0 < timeout <= 1800):
        raise ValueError('Invalid explicit providers/timeout')
    if output.is_relative_to(models) or output.is_relative_to(ROOT / 'native/ocr/tests'):
        raise ValueError('Evidence must not modify models or frozen fixtures')
    output.mkdir(parents=True, exist_ok=False)
    report = {'schema': 1, 'scope': 'python-source-video-acceptance', 'backend': 'python-coreml',
              'result': 'running', 'providers': list(providers), 'modes': list(MODES), 'cases': [],
              'gpu_execution_verified': False, 'final_package_accepted': False,
              'timing_is_benchmark': False, 'commit': os.environ.get('GITHUB_SHA'),
              'interpreter': sys.executable, 'interpreter_sha256': sha(Path(sys.executable).resolve())}
    path = output / 'report.json'
    try:
        video = output / 'same input video.mp4'
        generate_video(video)
        original = sha(video)
        for mode in MODES:
            matched = []
            for provider in providers:
                row = run_case(output / f'{mode}-{provider}', models, video,
                               provider=provider, mode=mode, timeout=timeout)
                if row['input_video_sha256'] != original:
                    raise ValueError('Provider did not use the identical video bytes')
                report['cases'].append(row)
                matched.append({key: row[key] for key in ('recognized', 'subtitle_count', 'intervals_ms', 'srt_sha256')})
                path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
            if any(row != matched[0] for row in matched[1:]):
                raise ValueError('CPU/CoreML Python CLI subtitle text/timing differs')
        report.update(result='passed', input_video_sha256=original)
    except Exception as error:
        report.update(result='failed', error=f'{type(error).__name__}: {error}')
    finally:
        path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--models', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--provider', choices=('cpu', 'coreml', 'both'), required=True)
    parser.add_argument('--timeout', type=int, default=900)
    args = parser.parse_args()
    result = run(args.output, args.models, ('cpu', 'coreml') if args.provider == 'both' else (args.provider,), args.timeout)
    print(json.dumps({key: result.get(key) for key in ('result', 'scope', 'error')}, ensure_ascii=True))
    return 0 if result['result'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
