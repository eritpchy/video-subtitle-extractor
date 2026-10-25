"""Text detection through the same model and device policy as recognition."""
from time import perf_counter
import numpy as np

from backend.tools.paddle_model_config import PaddleModelConfig
from backend.tools.hardware_accelerator import HardwareAccelerator


class SubtitleDetect:
    def __init__(self, language=None, mode=None, hardware_accelerator=None):
        from paddleocr import TextDetection
        self.hardware_accelerator = hardware_accelerator or HardwareAccelerator.instance()
        model_config = PaddleModelConfig(self.hardware_accelerator, language, mode)
        model_config.ensure_models(detection_only=True)
        kwargs = model_config.detection_kwargs()
        kwargs.update(self.hardware_accelerator.inference_kwargs())
        self.text_detector = TextDetection(**kwargs)

    def detect_subtitle(self, img):
        started = perf_counter()
        results = list(self.text_detector.predict(img))
        if not results:
            return np.empty((0, 4, 2), dtype=np.float32), perf_counter() - started
        polygons = np.asarray(results[0].get('dt_polys', []), dtype=np.float32)
        if not polygons.size:
            polygons = np.empty((0, 4, 2), dtype=np.float32)
        return polygons, perf_counter() - started
