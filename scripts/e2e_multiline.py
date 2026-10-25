#!/usr/bin/env python3
"""Real video -> VSF -> OCR -> multiline SRT regression for upstream #467."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    import cv2
    import numpy as np
    import pysrt
    from PIL import Image, ImageDraw, ImageFont

    video = output / 'two lines.mp4'
    font = ImageFont.truetype(str(ROOT / 'backend/tools/NotoSansCJK-Bold.otf'), 32)
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*'mp4v'), 25, (640, 360))
    if not writer.isOpened():
        raise RuntimeError('Cannot create multiline video fixture')
    cues = [(1000, 3000, ('FIRST LINE', 'SECOND LINE')),
            (4000, 6000, ('THIRD LINE', 'FOURTH LINE'))]
    try:
        for index in range(175):
            image = Image.new('RGB', (640, 360), (28, 44, 66))
            draw = ImageDraw.Draw(image)
            for start, end, lines in cues:
                if start <= index * 40 <= end:
                    for y, line in zip((245, 295), lines):
                        draw.text((320, y), line, font=font, fill='white', anchor='mt',
                                  stroke_width=2, stroke_fill='black')
            writer.write(cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR))
    finally:
        writer.release()
    target = output / 'multiline.srt'
    env = dict(os.environ, VSE_OFFLINE='1', QT_QPA_PLATFORM='offscreen',
               VSE_CONFIG_FILE=str(output / 'isolated.json'), PYTHONUNBUFFERED='1',
               PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK='True')
    # Keep default word segmentation enabled: it must preserve rows too.
    command = [sys.executable, '-m', 'backend.cli', str(video), '-o', str(target),
               '--language', 'en', '--mode', 'fast', '--cpu', '--threads', '2',
               '--area', '0.6,1,0,1', '--overwrite']
    with (output / 'extraction.log').open('w', encoding='utf-8') as log:
        result = subprocess.run(command, cwd=ROOT, env=env, stdout=log,
                                stderr=subprocess.STDOUT, timeout=900)
    if result.returncode:
        raise RuntimeError(f'Extraction failed; inspect {output / "extraction.log"}')
    subtitles = pysrt.open(str(target), encoding='utf-8')
    for start, end, lines in cues:
        expected = '\n'.join(lines)
        candidates = [sub for sub in subtitles if sub.text.upper() == expected]
        if not candidates:
            raise AssertionError(f'Missing multiline text {expected!r}: {target.read_text()}')
        if not any(abs(sub.start.ordinal - start) <= 600 and abs(sub.end.ordinal - end) <= 600
                   for sub in candidates):
            raise AssertionError(f'Incorrect multiline timing: {candidates}')
    report = {'result': 'passed', 'subtitle_count': len(subtitles),
              'texts': [sub.text for sub in subtitles], 'srt': str(target)}
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
