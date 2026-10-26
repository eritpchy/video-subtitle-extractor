#!/usr/bin/env python3
"""Opt-in headless fixed-path video diagnostic, NOT production extraction timing.

Measures VSF + deterministic FFmpeg frame/ROI decode + the unchanged OCR Engine.
No GUI, CUDA VSF, hardware decoder, SRT text merging, install or model conversion.
Every configuration repeats the same stages; frame hashes must remain identical.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import random
import subprocess
import time

from backend.tools.videosubfinder import build_command, parse_events, verify_binary
from scripts.native_diagnostics import benchmark as b


def crop_values(text):
    try:
        top, bottom, left, right = map(float, text.split(','))
    except ValueError as error:
        raise ValueError('ROI requires top,bottom,left,right normalized coordinates') from error
    if not all(math.isfinite(v) for v in (top, bottom, left, right)) or not (0 <= top < bottom <= 1 and 0 <= left < right <= 1):
        raise ValueError('Invalid normalized ROI')
    return top, bottom, left, right


def parse_vsf_result(result):
    timestamps, finished = set(), False
    for line in result.stdout.splitlines():
        for event, value in parse_events(line):
            if event == 'frame':
                timestamps.add(value)
            elif event == 'finish':
                finished = True
        if 'Cannot decode the first frame' in line or 'Get current frame error' in line:
            raise ValueError('VSF decoder failed; no automatic transcode/fallback')
    if result.returncode or not finished:
        raise ValueError('VSF failed or did not emit FINISH')
    if not timestamps:
        raise ValueError('No subtitle frames selected; cannot measure OCR throughput')
    return sorted(timestamps)


def prepare_frames(args, root):
    top, bottom, left, right = crop_values(args.area)
    vsf_root = root / 'vsf'
    vsf_root.mkdir()
    timing = vsf_root / 'timing.srt'
    command = build_command(args.vsf, args.video, vsf_root, timing,
                            crop=(1 - top, 1 - bottom, left, right),
                            threads=args.vsf_threads, decoder=args.decoder, use_cuda=False)
    started = time.monotonic()
    process = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, errors='replace', timeout=args.timeout,
                             env=b.offline_runtime_environment())
    vsf_seconds = time.monotonic() - started
    (root / 'vsf.log').write_text(process.stdout, encoding='utf-8')
    timestamps = parse_vsf_result(process)
    if not timing.is_file():
        raise ValueError('VSF timing file is missing')
    if len(timestamps) > args.max_frames:
        raise ValueError('Subtitle-frame count exceeds explicit diagnostic limit; no silent subsampling')
    frames = root / 'frames'
    frames.mkdir()
    records = []
    started = time.monotonic()
    for index, timestamp in enumerate(timestamps):
        path = frames / f'{index:06d}.ppm'
        command = [str(args.ffmpeg), '-nostdin', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                   '-ss', f'{timestamp / 1000:.6f}',
                   '-i', str(args.video), '-frames:v', '1', '-vf',
                   f'crop=iw*{right-left:.12g}:ih*{bottom-top:.12g}:iw*{left:.12g}:ih*{top:.12g}',
                   '-pix_fmt', 'rgb24', '-c:v', 'ppm', str(path)]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=args.timeout, env=b.offline_runtime_environment())
        if result.returncode or not path.is_file():
            raise ValueError('Fixed decoder failed: ' + result.stderr.decode(errors='replace')[-1000:])
        records.append({'timestamp_ms': timestamp, 'path': str(path), 'sha256': b.digest(path)})
    decode_seconds = time.monotonic() - started
    return records, {'vsf_decode_and_selection_seconds': vsf_seconds,
                     'selected_frame_decode_and_image_io_seconds': decode_seconds,
                     'note': 'VSF includes its own video decoding; its internal decode cost cannot be '
                             'separated without VSF instrumentation. Second stage includes per-frame '
                             'FFmpeg startup and PPM I/O. Neither is called pure decoder/GPU time.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('runner', 'artifact', 'output', 'video', 'vsf', 'ffmpeg', 'ffprobe'):
        parser.add_argument('--' + key, type=Path, required=True)
    parser.add_argument('--area', default='0.6,1,0,1')
    parser.add_argument('--tier', choices=('small', 'medium'), default='small')
    parser.add_argument('--rec-model', default='PP-OCRv6_small_rec')
    parser.add_argument('--modes', nargs='+', choices=b.MODES, default=list(b.MODES))
    parser.add_argument('--threads', type=int, default=4)
    parser.add_argument('--batch-size', type=int, default=6)
    parser.add_argument('--vsf-threads', type=int, default=4)
    parser.add_argument('--decoder', choices=('opencv', 'ffmpeg'), default='opencv')
    parser.add_argument('--process-repeats', type=int, default=3)
    parser.add_argument('--warmup', type=int, default=3)
    parser.add_argument('--repeats', type=int, default=10)
    parser.add_argument('--max-frames', type=int, default=500)
    parser.add_argument('--timeout', type=int, default=900)
    parser.add_argument('--seed', type=int, default=0)
    args = parser.parse_args(argv)
    crop_values(args.area)
    if args.modes[0] != 'ort-cpu' or len(set(args.modes)) != len(args.modes):
        parser.error('Unique modes must start with ort-cpu baseline')
    if args.process_repeats < 1 or args.repeats < 1 or args.warmup < 0 or args.max_frames < 1:
        parser.error('Invalid repetition/frame limits')
    for name in ('runner', 'artifact', 'video', 'vsf', 'ffmpeg', 'ffprobe'):
        setattr(args, name, getattr(args, name).resolve(strict=True))
    models, configs = b.verify_models(args.artifact)
    if args.rec_model + '_infer.onnx' not in models:
        parser.error('Recognizer is not one of the 15 pinned models')
    host = b.probe(args.runner)
    vsf_version = verify_binary(args.vsf, expected_version='6.10.7')
    probe = subprocess.run([str(args.ffprobe), '-v', 'error', '-protocol_whitelist', 'file,pipe', '-select_streams', 'v:0',
                            '-show_entries', 'stream=avg_frame_rate,nb_frames:format=duration',
                            '-of', 'json', str(args.video)], capture_output=True, text=True,
                           timeout=30, check=True, env=b.offline_runtime_environment())
    metadata = json.loads(probe.stdout)
    output = b.new_directory(args.output)
    report = {'schema_version': 1, 'diagnostic_complete': False, 'release_ready': False,
              'host': host, 'video': {'path': str(args.video), 'sha256': b.digest(args.video), 'metadata': metadata},
              'tools': {name: {'path': str(getattr(args, name)), 'sha256': b.digest(getattr(args, name))}
                        for name in ('vsf', 'ffmpeg', 'ffprobe')},
              'vsf_version': vsf_version, 'models': models, 'configs': configs,
              'mode': args.tier, 'area': args.area, 'rec_model': args.rec_model,
              'gpu_execution_verified': False, 'gpu_speedup': None,
              'production_video_end_to_end_seconds': None,
              'scope': 'Actual sequential video→VSF→fixed frame decode→OCR JSON diagnostic wall clock. '
                       'Not production E2E: excludes SRT text merging/post-processing, uses explicit '
                       'FFmpeg frame extraction, holds VSF algorithm fixed even for accurate/GPU settings.',
              'runs': [], 'warm_ocr_runs': {}, 'unsupported_modes': []}
    modes = []
    for mode in args.modes:
        if mode != 'ort-cpu' and 'CoreMLExecutionProvider' not in host['available_providers']:
            report['unsupported_modes'].append(mode)
        else:
            modes.append(mode)
    randomizer = random.Random(args.seed)
    frame_identity = baseline_result = frozen_request = None
    for iteration in range(args.process_repeats):
        order = list(modes)
        randomizer.shuffle(order)
        for mode in order:
            root = output / f'{mode}-{iteration}'
            root.mkdir()
            started = time.monotonic()
            frames, stages = prepare_frames(args, root)
            identity = [(row['timestamp_ms'], row['sha256']) for row in frames]
            if frame_identity is None:
                frame_identity = identity
            if identity != frame_identity:
                raise ValueError('VSF timestamps or decoded ROI bytes changed; comparison refused')
            request = {'schema_version': 1, 'mode': mode, 'kind': 'pipeline', 'evidence': False,
                       'models': str(args.artifact / 'models'), 'config': str(args.artifact / 'config'),
                       'tier': args.tier, 'rec_model': args.rec_model, 'threads': args.threads,
                       'batch_size': args.batch_size, 'warmup': 0, 'repeats': 0, 'timeout_ms': 120000,
                       'images': [row['path'] for row in frames]}
            result = b.execute(args.runner, root, 'ocr-once', request, args.timeout)
            total = time.monotonic() - started
            if result['status'] != 'completed':
                raise ValueError('Video OCR failed; see preserved isolated logs')
            if mode == 'ort-cpu' and baseline_result is None:
                baseline_result = result['first_result']
                frozen_request = request
            run = {'mode': mode, 'iteration': iteration, 'frames': frames, 'stages': stages,
                   'diagnostic_video_to_ocr_json_seconds': total,
                   'ocr_subprocess_seconds': result['process_wall_seconds'],
                   'ocr_prediction_seconds': result['cold_first_pass']['wall_seconds'],
                   'ocr_ort_run_seconds': result['cold_first_pass']['ort_run_seconds'],
                   'result': result}
            duration = float(metadata.get('format', {}).get('duration', 0))
            run['video_realtime_factor'] = duration / total if duration > 0 else None
            frames_count = metadata['streams'][0].get('nb_frames', '')
            run['effective_source_video_fps'] = int(frames_count) / total if frames_count.isdigit() else None
            report['runs'].append(run)
            b.write_json(output / 'video-report.json', report)
    for run in report['runs']:
        run['same_input_ort_cpu_parity'] = b.pipeline_parity(run['result']['first_result'], baseline_result)
    for mode in modes:
        report['warm_ocr_runs'][mode] = b.execute(args.runner, output, f'warm-ocr-{mode}',
            {**frozen_request, 'mode': mode, 'warmup': args.warmup, 'repeats': args.repeats}, args.timeout)
    report['cold_video_summary_by_configuration'] = {
        mode: b.summarize([run['diagnostic_video_to_ocr_json_seconds'] for run in report['runs'] if run['mode'] == mode])
        for mode in modes}
    report['diagnostic_complete'] = True
    b.write_json(output / 'video-report.json', report)
    print(output / 'video-report.json')


if __name__ == '__main__':
    main()
