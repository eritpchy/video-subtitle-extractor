import sys
from types import SimpleNamespace

import numpy as np
import pytest

from backend.tools.hardware_accelerator import HardwareAccelerator
from backend.tools.ocr import OcrRecogniser, get_coordinates, normalize_ocr_result


@pytest.fixture(autouse=True)
def isolated_model_environment(monkeypatch):
    # Tests of missing bundle selection explicitly control network policy.
    monkeypatch.delenv('VSE_OFFLINE', raising=False)
    monkeypatch.delenv('VSE_MODEL_DIR', raising=False)


def box(x, y, width=50, height=20):
    return [[x, y], [x+width, y], [x+width, y+height], [x, y+height]]


def test_filtered_recognition_keeps_its_own_polygon():
    result = {'dt_polys': [box(0, 0), box(200, 100)],
              'rec_polys': [box(200, 100)], 'rec_texts': ['kept'], 'rec_scores': [.99]}
    boxes, texts = normalize_ocr_result(result)
    assert get_coordinates(boxes) == [(200, 250, 100, 120)]
    assert texts == [('kept', .99)]


def test_reading_order_is_geometric_without_changing_pixels():
    result = {'rec_polys': np.array([box(150, 52), box(100, 112), box(10, 50)]),
              'rec_texts': ['right', 'bottom', 'left'], 'rec_scores': [.91, .92, .93]}
    boxes, texts = normalize_ocr_result(result)
    assert [text for text, _ in texts] == ['left', 'right', 'bottom']
    assert get_coordinates(boxes) == [(10, 60, 50, 70), (150, 200, 52, 72), (100, 150, 112, 132)]


def test_empty_and_legacy_results():
    assert normalize_ocr_result({'rec_polys': [], 'rec_texts': [], 'rec_scores': []}) == ([], [])
    assert normalize_ocr_result({'dt_polys': [box(0, 0)], 'rec_texts': ['ok'], 'rec_scores': [.8]})[1] == [('ok', .8)]


def test_filtered_legacy_result_cannot_silently_misassociate_text():
    with pytest.raises(ValueError, match='unaligned'):
        normalize_ocr_result({'dt_polys': [box(0, 0), box(50, 0)], 'rec_texts': ['x'], 'rec_scores': [.8]})


def test_nonfinite_results_fail():
    with pytest.raises(ValueError, match='non-finite'):
        normalize_ocr_result({'rec_polys': [box(0, 0)], 'rec_texts': ['x'], 'rec_scores': [float('nan')]})


def test_public_constructor_uses_selected_models_and_options(monkeypatch, tmp_path):
    captured = {}
    def create(**kwargs):
        captured.update(kwargs)
        return object()
    monkeypatch.setitem(sys.modules, 'paddleocr', SimpleNamespace(PaddleOCR=create))
    monkeypatch.setenv('VSE_MODEL_DIR', str(tmp_path))
    monkeypatch.setattr('backend.tools.paddle_model_config.PaddleModelConfig.ensure_models', lambda *args, **kwargs: None)
    recognizer = OcrRecogniser('japan', 'accurate', 9, HardwareAccelerator())
    recognizer.init_model()
    assert captured['text_recognition_model_name'] == 'PP-OCRv6_medium_rec'
    assert captured['text_recognition_batch_size'] == 9
    assert captured['device'] == 'cpu'
    assert captured['use_doc_unwarping'] is False
    assert captured['use_textline_orientation'] is False
    assert 'use_gpu' not in captured


def test_public_detector_uses_same_device_policy(monkeypatch, tmp_path):
    from backend.tools.subtitle_detect import SubtitleDetect
    captured = {}
    class Detector:
        def __init__(self, **kwargs):
            captured.update(kwargs)
        def predict(self, image):
            return [{'dt_polys': []}]
    monkeypatch.setitem(sys.modules, 'paddleocr', SimpleNamespace(TextDetection=Detector))
    monkeypatch.setenv('VSE_MODEL_DIR', str(tmp_path))
    monkeypatch.setattr('backend.tools.paddle_model_config.PaddleModelConfig.ensure_models', lambda *args, **kwargs: None)
    detector = SubtitleDetect('en', 'fast', HardwareAccelerator())
    assert captured['model_name'] == 'PP-OCRv6_small_det'
    assert captured['device'] == 'cpu'
    polygons, duration = detector.detect_subtitle(np.zeros((32, 32, 3)))
    assert polygons.shape == (0, 4, 2)
    assert duration >= 0
