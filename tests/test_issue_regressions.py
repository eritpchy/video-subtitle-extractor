"""Regression coverage for upstream configuration #450 and multiline #467."""
import os
from pathlib import Path
import subprocess
import sys

import pysrt

from backend.main import SubtitleExtractor
from backend.tools import reformat
from backend.tools.subtitle_layout import join_visual_rows

ROOT = Path(__file__).resolve().parents[1]


def test_configuration_is_independent_of_launch_directory(tmp_path):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    env.pop('VSE_CONFIG_FILE', None)
    results = []
    for name in ('first', 'second'):
        directory = tmp_path / name
        directory.mkdir()
        result = subprocess.run([sys.executable, '-c',
            "from backend.config import config; print('PATH=' + str(config.file.resolve()))"],
            cwd=directory, env=env, capture_output=True, text=True, timeout=30, check=True)
        results.append(next(line for line in result.stdout.splitlines() if line.startswith('PATH=')))
        assert not (directory / 'config').exists()
    assert results == ['PATH=' + str(ROOT / 'config/config.json')] * 2


def test_configuration_explicit_override_is_preserved(tmp_path):
    target = tmp_path / 'isolated.json'
    env = dict(os.environ, PYTHONPATH=str(ROOT), VSE_CONFIG_FILE=str(target))
    result = subprocess.run([sys.executable, '-c',
        "from backend.config import config; print('PATH=' + str(config.file.resolve()))"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30, check=True)
    assert 'PATH=' + str(target) in result.stdout


def test_visual_rows_keep_fragments_together_and_order_lines():
    assert join_visual_rows([
        ((100, 180, 140, 170), 'line'), ((10, 90, 100, 130), 'First'),
        ((10, 90, 140, 170), 'Second'), ((100, 180, 102, 132), 'line'),
    ]) == 'First line\nSecond line'
    assert join_visual_rows([((10, 90, 100, 130), '中文'),
                             ((10, 90, 140, 170), '日本語')]) == '中文\n日本語'


def test_multiline_output_preserves_raw_cache_and_vsf_timing(tmp_path):
    raw = tmp_path / 'raw.txt'
    original = ('00000025\t(100, 180, 100, 130)\tline\n'
                '00000025\t(10, 90, 140, 170)\tSecond line\n'
                '00000025\t(10, 90, 100, 130)\tFirst\n')
    raw.write_text(original, encoding='utf-8')
    timing = tmp_path / 'timing.srt'
    timing.write_text('1\n00:00:01,000 --> 00:00:03,000\nplaceholder\n\n')
    extractor = SubtitleExtractor.__new__(SubtitleExtractor)
    extractor.raw_subtitle_path = str(raw)
    extractor.vsf_subtitle = str(timing)
    extractor.subtitle_output_path = str(tmp_path / 'output.srt')
    extractor.fps = 25
    extractor.use_vsf = True
    extractor.update_progress = lambda **kwargs: None
    extractor.append_output = lambda *args: None
    extractor.generate_subtitle_file_vsf()
    result = pysrt.open(extractor.subtitle_output_path, encoding='utf-8')
    assert len(result) == 1
    assert result[0].text == 'First line\nSecond line'
    assert result[0].start.ordinal == 1000 and result[0].end.ordinal == 3000
    assert raw.read_text(encoding='utf-8') == original
    # Repeat generation must not destroy/mutate cached recognition geometry.
    extractor.generate_subtitle_file_vsf()
    assert raw.read_text(encoding='utf-8') == original


def test_word_segmentation_preserves_visual_newlines(tmp_path):
    path = tmp_path / 'two-lines.srt'
    path.write_text('1\n00:00:01,000 --> 00:00:02,000\nFirst line\nSecond line\n\n')
    assert reformat.execute(str(path), 'en')
    subtitles = pysrt.open(str(path))
    assert subtitles[0].text == 'First line\nSecond line'


def test_non_vsf_srt_keeps_blank_separators_between_multiline_cues(tmp_path):
    extractor = SubtitleExtractor.__new__(SubtitleExtractor)
    extractor.subtitle_output_path = str(tmp_path / 'sampled.srt')
    extractor.fps = 25
    extractor.use_vsf = False
    extractor._remove_duplicate_subtitle = lambda: [
        ('25', '75', 'First line\nSecond line'), ('100', '150', 'Another caption')]
    extractor.append_output = lambda *args: None
    extractor.generate_subtitle_file()
    result = pysrt.open(extractor.subtitle_output_path, encoding='utf-8')
    assert len(result) == 2
    assert result[0].text == 'First line\nSecond line'
    assert result[1].text == 'Another caption'
