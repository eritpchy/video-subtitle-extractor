#!/usr/bin/env python3
"""Real video -> VideoSubFinder -> PP-OCRv6 -> SRT smoke regression.

Requires installed runtime dependencies, a verified VSF 6.10 runtime, and real
PP-OCRv6 small models. No network or mocks are used by the fixture itself.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = (("HELLO WORLD", 1000, 3000), ("SECOND LINE", 4000, 6000))


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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/e2e")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args(argv)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    video = output / "input video.mp4"
    subtitles = output / "recognized.srt"
    generate_video(video)
    # Deliberately conflicting GUI preferences catch leaked pre-crop settings
    # and failed language/mode propagation into spawned OCR workers.
    configuration = output / "isolated-config.json"
    saved_configuration = json.dumps({"Main": {"Language": "korean", "Mode": "accurate", "SubtitleArea": 1}})
    configuration.write_text(saved_configuration, encoding="utf-8")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", VSE_CONFIG_FILE=str(configuration),
               VSE_OFFLINE="1", PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK="True", PYTHONUNBUFFERED="1")
    command = [sys.executable, "-m", "backend.cli", str(video), "--output", str(subtitles),
               "--language", "en", "--mode", "fast", "--cpu", "--threads", "2",
               "--area", "0.6,1,0,1", "--overwrite", "--no-word-segmentation"]
    started = time.monotonic()
    with (output / "extraction.log").open("w", encoding="utf-8") as log:
        result = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=args.timeout)
    if result.returncode:
        raise RuntimeError(f"Extraction exited {result.returncode}; see {output / 'extraction.log'}")
    if configuration.read_text(encoding="utf-8") != saved_configuration:
        raise AssertionError("CLI changed saved GUI preferences")
    report = check_srt(subtitles)
    report.update({"result": "passed", "elapsed_seconds": round(time.monotonic() - started, 2),
                   "video": str(video), "srt": str(subtitles), "command": command})
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
