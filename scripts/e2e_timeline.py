#!/usr/bin/env python3
"""Real video audio -> Sushi -> known shifted SRT, from an unrelated CWD."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args(argv)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    inputs = output / "媒体 with spaces"
    inputs.mkdir(exist_ok=True)
    import imageio_ffmpeg
    import numpy as np
    import pysrt
    from backend.tools.python_runner import AsyncPythonRunner

    sample_rate = 12000
    samples = (np.random.default_rng(42).normal(0, 0.15, sample_rate * 15) * 30000).astype("<i2")
    shifted = np.concatenate([np.zeros(sample_rate * 2, dtype="<i2"), samples])
    for name, audio in (("source", samples), ("destination", shifted)):
        wav = inputs / f"{name}.wav"
        with wave.open(str(wav), "wb") as stream:
            stream.setparams((1, 2, sample_rate, 0, "NONE", "not compressed"))
            stream.writeframes(audio.tobytes())
        # PCM in MKV keeps the known shift exact while exercising real demux.
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-y",
                        "-f", "lavfi", "-i", "color=c=black:s=320x240:r=10",
                        "-i", str(wav), "-shortest", "-c:v", "mpeg4", "-c:a", "pcm_s16le",
                        str(inputs / f"{name}.mkv")], check=True, timeout=args.timeout)
    subtitles = inputs / "input 字幕.srt"
    original = "1\n00:00:03,000 --> 00:00:04,000\nHELLO WORLD\n\n2\n00:00:06,000 --> 00:00:07,000\nSECOND LINE\n\n"
    subtitles.write_text(original, encoding="utf-8")
    destination = output / "synced.srt"
    launch = output / "unrelated launch 字幕"
    launch.mkdir(exist_ok=True)
    old_cwd = Path.cwd()
    runner = AsyncPythonRunner()
    log_lines = []
    runner.register_callback(stdout=log_lines.append, stderr=log_lines.append)
    started = time.monotonic()
    try:
        os.chdir(launch)
        runner.start(str(ROOT / "backend/sushi"), cwd=str(ROOT / "backend"),
                     python_args=["-u"], args=["--src", str(inputs / "source.mkv"),
                     "--dst", str(inputs / "destination.mkv"), "--script", str(subtitles),
                     "--output", str(destination), "--temp-dir", str(output / "demux")])
        if not runner.wait(args.timeout):
            raise TimeoutError("Timeline synchronization exceeded its timeout")
        if runner.returncode:
            raise RuntimeError(f"Sushi exited {runner.returncode}")
    finally:
        runner.stop()
        os.chdir(old_cwd)
        (output / "timeline.log").write_text("".join(log_lines), encoding="utf-8")
    actual = pysrt.open(str(destination), encoding="utf-8-sig")
    assert len(actual) == 2
    expected = [(5000, 6000, "HELLO WORLD"), (8000, 9000, "SECOND LINE")]
    for item, (start, end, text) in zip(actual, expected):
        assert item.text == text
        assert abs(item.start.ordinal - start) <= 20
        assert abs(item.end.ordinal - end) <= 20
    assert subtitles.read_text(encoding="utf-8") == original
    report = {"result": "passed", "expected_shift_ms": 2000,
              "intervals_ms": [[s.start.ordinal, s.end.ordinal] for s in actual],
              "subtitle_count": len(actual), "video_format": "MKV/PCM", "srt": str(destination),
              "elapsed_seconds": round(time.monotonic() - started, 3)}
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
