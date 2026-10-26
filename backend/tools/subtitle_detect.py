"""Text detection through the same model and device policy as recognition."""
from time import perf_counter
import numpy as np

from backend.tools.paddle_model_config import PaddleModelConfig
from backend.tools.hardware_accelerator import HardwareAccelerator


class SubtitleDetect:
    def __init__(self, language=None, mode=None, hardware_accelerator=None):
        from backend.tools.native_ocr import NativeOcr, selected_backend
        self.hardware_accelerator = hardware_accelerator or HardwareAccelerator.instance()
        model_config = PaddleModelConfig(self.hardware_accelerator, language, mode)
        if selected_backend() == 'native':
            self.text_detector = NativeOcr(model_config, detection_only=True)
            return
        if selected_backend() == 'python-coreml':
            from backend.tools.python_coreml_ocr import PythonCoreMlOcr
            self.text_detector = PythonCoreMlOcr(model_config, detection_only=True)
            return
        if selected_backend() == 'directml':
            from backend.tools.directml_ocr import DirectMlOcr
            self.text_detector = DirectMlOcr(model_config, detection_only=True)
            return
        from paddleocr import TextDetection
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

    def close(self):
        if hasattr(self.text_detector, 'close'):
            self.text_detector.close()
