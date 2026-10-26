#!/usr/bin/env python3
"""Real video -> VideoSubFinder -> PP-OCRv6 -> SRT smoke regression.

Requires installed runtime dependencies, a verified VSF 6.10.7 runtime, and real
PP-OCRv6 small models (also medium for accurate). No network or mocks are used
by the fixture itself. CPU remains the default. Explicit native-coreml testing
uses the same fixture/VSF path and records real worker EP evidence, not GPU proof.
"""
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
EXPECTED = (("HELLO WORLD", 1000, 3000), ("SECOND LINE", 4000, 6000))
MODES = ("fast", "accurate", "auto")


def generate_video(path):
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(str(ROOT / "backend/tools/NotoSansCJK-Bold.otf"), 32)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 25, (640, 360))
    if not writer.isOpened():
        raise RuntimeError("OpenCV cannot create the MPEG-4 regression video")
    try:
        for index in range(175):
            image = Image.new("RGB", (640, 360), (28, 44, 66))
            draw = ImageDraw.Draw(image)
            timestamp = index * 40
            for text, start, end in EXPECTED:
                if start <= timestamp <= end:
                    draw.text((320, 295), text, font=font, fill="white", anchor="mt", stroke_width=2, stroke_fill="black")
            writer.write(cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR))
    finally:
        writer.release()


def check_srt(path):
    import pysrt
    subtitles = pysrt.open(str(path), encoding="utf-8")
    if not subtitles:
        raise AssertionError("OCR output has no subtitle entries")
    matched = []
    for expected, start, end in EXPECTED:
        candidates = [item for item in subtitles if expected in " ".join(item.text.upper().split())]
        if not candidates:
            raise AssertionError(f"Missing recognized text {expected!r}: {path.read_text(encoding='utf-8')}")
        if not any(abs(item.start.ordinal - start) <= 600 and abs(item.end.ordinal - end) <= 600 for item in candidates):
            raise AssertionError(f"Timing mismatch for {expected!r}: {candidates}")
        matched.append(expected)
    previous_end = -1
    for item in subtitles:
        if not (0 <= item.start.ordinal < item.end.ordinal <= 7100):
            raise AssertionError(f"Invalid subtitle interval: {item}")
        if item.start.ordinal < previous_end:
            raise AssertionError(f"Overlapping/out-of-order subtitle: {item}")
        previous_end = item.end.ordinal
    return {"recognized": matched, "subtitle_count": len(subtitles),
            "intervals_ms": [[item.start.ordinal, item.end.ordinal] for item in subtitles]}


def validate_coreml_video_evidence(report, documents):
    """Validate actual per-request worker evidence, not merely CLI switches."""
    from backend.tools.native_ocr import validate_execution_evidence, _validated_execution_window
    if (report.get('requested_provider') != 'coreml' or report.get('cpu_forced') is not False
            or report.get('gpu_inference_verified') is not False or not documents):
        raise ValueError('Missing explicit CoreML video execution evidence')
    seen, executed = set(), set()
    tier = 'medium' if report['mode'] == 'accurate' else 'small'
    for document in documents:
        if (type(document.get('schema_version')) is not int or document['schema_version'] != 1
                or type(document.get('worker_pid')) is not int or document['worker_pid'] < 1
                or not isinstance(document.get('worker_sha256'), str)
                or not re.fullmatch('[a-f0-9]{64}', document['worker_sha256'])):
            raise ValueError('Invalid CoreML video worker identity')
        request, response = document['request'], document['response']
        if (type(request.get('id')) is not int or request['id'] < 1 or type(response.get('id')) is not int
                or not isinstance(request.get('image_sha256'), str)
                or not re.fullmatch('[a-f0-9]{64}', request['image_sha256'])
                or type(request.get('image_bytes')) is not int or not 0 < request['image_bytes'] <= 32 * 1024 * 1024
                or request.get('image_format') != 'png'
                or request.get('operation') != 'ocr' or request.get('tier') != tier
                or request.get('rec_model') != f'PP-OCRv6_{tier}_rec'):
            raise ValueError('Invalid CoreML video request/input/model identity')
        identity = document['worker_pid'], request['id']
        if identity in seen or request.get('provider') != 'coreml' or response.get('id') != request.get('id') or response.get('ok') is not True:
            raise ValueError('Duplicated or unsuccessful CoreML video request')
        seen.add(identity)
        _validated_execution_window(response.get('execution_window'), worker_pid=document['worker_pid'])
        evidence = validate_execution_evidence(response, request, worker_pid=document['worker_pid'])
        for session in evidence['sessions'].values():
            if session is not None:
                if any(key not in session for key in ('capture', 'request_run_count', 'session_run_count', 'evidence_reused')):
                    raise ValueError('CoreML video session lacks precise profile capture/reuse semantics')
                if session['status'] == 'executed':
                    if 'profile_window' not in session:
                        raise ValueError('CoreML video profile has no attributable first-run window')
                    executed.add(session['model'])
    if executed != {f'PP-OCRv6_{tier}_det', f'PP-OCRv6_{tier}_rec'}:
        raise ValueError('CoreML video did not execute both expected production models')
    return sorted(executed)


def collect_coreml_video_evidence(directory, report):
    from backend.tools.native_ocr import _unique_object
    documents, records = [], []
    for path in sorted(directory.iterdir()):
        if path.is_symlink() or not path.is_file() or path.suffix != '.json' or path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError('Unexpected CoreML video request evidence')
        data = path.read_bytes()
        document = json.loads(data, object_pairs_hook=_unique_object)
        if path.name != f"native-ocr-{document.get('worker_pid')}-{document.get('request', {}).get('id')}.json":
            raise ValueError('CoreML request filename is not bound to its process/request identity')
        documents.append(document)
        records.append({'path': path.name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    report['executed_models'] = validate_coreml_video_evidence(report, documents)
    report['native_requests'] = records


def run_mode(output, mode, timeout, *, provider='cpu'):
    if mode not in MODES:
        raise ValueError(f"Unsupported smoke mode: {mode}")
    if provider not in ('cpu', 'coreml'):
        raise ValueError('Unsupported smoke provider')
    if provider == 'coreml' and os.environ.get('VSE_OCR_BACKEND') != 'native':
        raise ValueError('CoreML video acceptance requires the production native backend')
    output.mkdir(parents=True, exist_ok=True)
    video = output / "input video.mp4"
    subtitles = output / "recognized.srt"
    # A failed rerun must not leave a previous success report or reuse its SRT.
    (output / "report.json").unlink(missing_ok=True)
    subtitles.unlink(missing_ok=True)
    generate_video(video)
    # Deliberately conflicting GUI preferences catch leaked pre-crop settings
    # and failed language/mode propagation into spawned OCR workers.
    configuration = output / "isolated-config.json"
    conflicting_mode = "fast" if mode == "accurate" else "accurate"
    saved_configuration = json.dumps({"Main": {"Language": "korean", "Mode": conflicting_mode, "SubtitleArea": 1}})
    configuration.write_text(saved_configuration, encoding="utf-8")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", VSE_CONFIG_FILE=str(configuration),
               VSE_OFFLINE="1", PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK="True", PYTHONUNBUFFERED="1",
               VSE_OCR_CPU_THREADS='2')
    env.pop('VSE_NATIVE_EVIDENCE_DIR', None)
    if provider == 'coreml':
        evidence_directory = output / 'native-requests'
        # Never mix a prior proof with a new execution. Existing evidence is
        # not recursively deleted; use a new output for each acceptance run.
        evidence_directory.mkdir(exist_ok=False)
        env.update(VSE_NATIVE_OCR_PROVIDER='coreml', ORT_DISABLE_TELEMETRY='1',
                   VSE_NATIVE_EVIDENCE_DIR=str(evidence_directory.resolve()))
    command = [sys.executable, "-m", "backend.cli", str(video), "--output", str(subtitles),
               "--language", "en", "--mode", mode, *(['--cpu'] if provider == 'cpu' else []), "--threads", "2",
               "--area", "0.6,1,0,1", "--overwrite", "--no-word-segmentation"]
    started = time.monotonic()
    with (output / "extraction.log").open("w", encoding="utf-8") as log:
        started_ns = time.perf_counter_ns()
        result = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
        elapsed_ns = time.perf_counter_ns() - started_ns
    if result.returncode:
        raise RuntimeError(f"Extraction exited {result.returncode}; see {output / 'extraction.log'}")
    if configuration.read_text(encoding="utf-8") != saved_configuration:
        raise AssertionError("CLI changed saved GUI preferences")
    report = check_srt(subtitles)
    report.update({"result": "passed", "elapsed_seconds": round(time.monotonic() - started, 2),
                   # These describe the executed CLI contract, not GPU/device telemetry.
                   "mode": mode, "cpu_forced": provider == 'cpu',
                   "requested_provider": provider, "gpu_inference_verified": False,
                   "elapsed_ns": elapsed_ns,
                   "input_video_sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
                   "srt_sha256": hashlib.sha256(subtitles.read_bytes()).hexdigest(),
                   "video": str(video), "srt": str(subtitles), "command": command})
    if provider == 'coreml':
        collect_coreml_video_evidence(evidence_directory, report)
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/e2e")
    parser.add_argument("--timeout", type=int, default=900, help="Extraction timeout in seconds per mode")
    parser.add_argument('--provider', choices=('cpu', 'coreml'), default='cpu',
                        help='Explicit native CoreML requires real EP evidence; never GPU certification')
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--mode", choices=MODES, default="fast")
    selection.add_argument("--all-modes", action="store_true",
                           help="Run all CPU modes in separate output subdirectories")
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if not args.all_modes:
        report = run_mode(output, args.mode, args.timeout, provider=args.provider)
    else:
        output.mkdir(parents=True, exist_ok=True)
        report_path = output / "report.json"
        report_path.unlink(missing_ok=True)
        # Clear unvisited modes too: an interrupted matrix cannot retain old passes.
        for mode in MODES:
            for name in ("report.json", "recognized.srt"):
                (output / mode / name).unlink(missing_ok=True)
        report = {"result": "running", "modes_requested": list(MODES),
                  "cpu_forced": args.provider == 'cpu', "requested_provider": args.provider,
                  "gpu_inference_verified": False, "cases": []}
        for mode in MODES:
            try:
                report["cases"].append(run_mode(output / mode, mode, args.timeout, provider=args.provider))
            except Exception as exc:
                report.update(result="failed", failed_mode=mode, error=str(exc))
                report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
                raise
        report["result"] = "passed"
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    main()
