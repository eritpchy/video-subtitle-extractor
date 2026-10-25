#!/usr/bin/env python3
"""Real AV1 video -> VSF -> PP-OCRv6 -> SRT issue #459 regression.

Uses bundled imageio-ffmpeg to transcode the deterministic subtitle fixture;
no external downloads, mocks, or already-decoded frames replace extraction.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.e2e_smoke import generate_video, check_srt


def main():
    import imageio_ffmpeg
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    original, video, subtitles = output/'source.mp4', output/'AV1 subtitle clip.mkv', output/'recognized.srt'
    generate_video(original)
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-nostdin', '-y', '-v', 'error',
                    '-i', str(original), '-c:v', 'libaom-av1', '-crf', '28', '-cpu-used', '8',
                    '-threads', '2', '-an', str(video)], check=True, timeout=180)
    command = [sys.executable, '-m', 'backend.cli', str(video), '--output', str(subtitles),
               '--language', 'en', '--mode', 'fast', '--cpu', '--threads', '2',
               '--area', '0.6,1,0,1', '--overwrite', '--no-word-segmentation']
    env = dict(os.environ, VSE_OFFLINE='1', QT_QPA_PLATFORM='offscreen',
               PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK='True', VSE_CONFIG_FILE=str(output/'isolated-config.json'))
    started = time.monotonic()
    with (output/'extraction.log').open('w') as log:
        subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                       check=True, timeout=300)
    report = check_srt(subtitles)
    report.update(result='passed', codec='AV1', elapsed_seconds=round(time.monotonic()-started, 2))
    (output/'report.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
