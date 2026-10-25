"""Real model inference, opt-in to keep unit tests offline and lightweight.

VSE_RUN_OCR_INTEGRATION=1 python -m pytest -q tests/test_ocr_integration.py
Prefetch: python -m backend.tools.model_download --tiers small medium
"""
import os
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
import pytest

from backend.tools.hardware_accelerator import HardwareAccelerator
from backend.tools.ocr import OcrRecogniser
from backend.tools.subtitle_detect import SubtitleDetect

pytestmark = pytest.mark.skipif(os.environ.get('VSE_RUN_OCR_INTEGRATION') != '1',
                                reason='Set VSE_RUN_OCR_INTEGRATION=1 to run real OCR models')
FONT = Path(__file__).parents[1] / 'backend/tools/NotoSansCJK-Bold.otf'


@pytest.fixture(autouse=True)
def require_prefetched_models(monkeypatch):
    # The real inference proof must not succeed by silently fetching a model.
    monkeypatch.setenv('VSE_OFFLINE', '1')


def subtitle_image(text):
    image = Image.new('RGB', (1000, 240), 'black')
    ImageDraw.Draw(image).text((60, 70), text, font=ImageFont.truetype(str(FONT), 48), fill='white')
    return np.ascontiguousarray(np.array(image)[:, :, ::-1])


def assert_recognized(recognizer, text):
    boxes, records = recognizer.predict(subtitle_image(text))
    actual = ''.join(record[0] for record in records).replace(' ', '')
    assert text.replace(' ', '') in actual, (text, actual, records)
    assert boxes and len(boxes) == len(records)
    assert min(score for _, score in records) > .7


def test_real_v6_small_english_chinese_japanese_and_empty():
    recognizer = OcrRecogniser('ch', 'fast', 6, HardwareAccelerator())
    for text in ('Hello world', '中文字幕测试', 'こんにちは世界'):
        assert_recognized(recognizer, text)
    assert recognizer.predict(np.zeros((240, 1000, 3), dtype=np.uint8)) == ([], [])


def test_real_v6_medium():
    assert_recognized(OcrRecogniser('ch', 'accurate', 6, HardwareAccelerator()), 'Hello world 字幕测试')


def test_real_v5_korean_fallback_with_v6_detection():
    assert_recognized(OcrRecogniser('korean', 'fast', 6, HardwareAccelerator()), '안녕하세요 세계')


def test_real_v6_detection():
    detector = SubtitleDetect('en', 'fast', HardwareAccelerator())
    polygons, seconds = detector.detect_subtitle(subtitle_image('Hello world'))
    assert polygons.shape[0] >= 1
    assert seconds > 0
    empty, _ = detector.detect_subtitle(np.zeros((240, 1000, 3), dtype=np.uint8))
    assert empty.shape == (0, 4, 2)
