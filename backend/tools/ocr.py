"""Subtitle-oriented adapter for the public PaddleOCR 3.7 API."""
import math

from backend.tools.hardware_accelerator import HardwareAccelerator
from backend.tools.paddle_model_config import PaddleModelConfig


def normalize_ocr_result(result):
    """Return geometrically ordered boxes/text with their association intact.

    ``dt_polys`` includes detections rejected by the recognition stage. Only
    ``rec_polys`` is aligned with ``rec_texts`` and ``rec_scores`` in PaddleX.
    """
    texts = result.get('rec_texts', [])
    scores = result.get('rec_scores', [])
    polygons = result.get('rec_polys')
    if polygons is None:
        # Some early 3.x result objects omitted rec_polys. Only accept their
        # detector boxes if no filtering could have changed the association.
        polygons = result.get('dt_polys', [])
    if not (len(polygons) == len(texts) == len(scores)):
        raise ValueError('PaddleOCR returned unaligned recognition polygons, texts and scores')
    entries = []
    for polygon, text, score in zip(polygons, texts, scores):
        if len(polygon) < 4 or any(len(point) != 2 for point in polygon):
            raise ValueError('PaddleOCR returned an invalid text polygon')
        xs, ys = zip(*polygon)
        if not all(math.isfinite(float(value)) for value in (*xs, *ys, score)):
            raise ValueError('PaddleOCR returned non-finite geometry or confidence')
        xmin, xmax = int(min(xs)), int(max(xs))
        ymin, ymax = int(min(ys)), int(max(ys))
        if xmax <= xmin or ymax <= ymin:
            continue
        entries.append(((xmin, xmax, ymin, ymax), (str(text), float(score))))
    # Stable row grouping does not depend on Paddle's initial polygon order.
    # Do not round actual coordinates: subtitle-region clipping needs pixels.
    rows = []
    for entry in sorted(entries, key=lambda item: (item[0][2], item[0][0])):
        bounds = entry[0]
        center = (bounds[2] + bounds[3]) / 2
        height = bounds[3] - bounds[2]
        row = next((r for r in rows if abs(center - r['center']) <= max(5, min(height, r['height']) * .5)), None)
        if row is None:
            row = {'center': center, 'height': height, 'entries': []}
            rows.append(row)
        row['entries'].append(entry)
    boxes, recognition = [], []
    for row in sorted(rows, key=lambda item: item['center']):
        for (xmin, xmax, ymin, ymax), text_score in sorted(row['entries'], key=lambda item: item[0][0]):
            boxes.append([(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax)])
            recognition.append(text_score)
    return boxes, recognition


class OcrRecogniser:
    def __init__(self, language=None, mode=None, rec_batch_number=None, hardware_accelerator=None):
        self.recogniser = None
        self.hardware_accelerator = hardware_accelerator or HardwareAccelerator.instance()
        self.language = language
        self.mode = mode
        self.rec_batch_number = rec_batch_number
        self.stop_event = None

    @staticmethod
    def y_round(y):
        # Kept for integrations using the old adapter's rounding helper.
        return int((y + 4) // 10 * 10)

    def predict(self, image):
        if self.recogniser is None:
            self.recogniser = self.init_model()
        results = list(self.recogniser.predict_iter(image))
        if not results:
            return [], []
        if len(results) != 1:
            raise ValueError('Expected one OCR result for a single subtitle frame')
        return normalize_ocr_result(results[0])

    def init_model(self):
        from backend.tools.native_ocr import NativeOcr, selected_backend
        model_config = PaddleModelConfig(self.hardware_accelerator, self.language, self.mode)
        batch_number = self.rec_batch_number
        if batch_number is None:
            from backend.config import config
            batch_number = config.recBatchNumber.value
        if selected_backend() == 'native':
            return NativeOcr(model_config, batch_number, stop_event=self.stop_event)
        from paddleocr import PaddleOCR
        model_config.ensure_models()
        kwargs = model_config.ocr_kwargs()
        kwargs.update(
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            text_rec_score_thresh=0,
            text_recognition_batch_size=batch_number,
            **self.hardware_accelerator.inference_kwargs(),
        )
        return PaddleOCR(**kwargs)

    def close(self):
        if self.recogniser is not None and hasattr(self.recogniser, 'close'):
            self.recogniser.close()


def get_coordinates(dt_box):
    """Get axis-aligned bounds from list or NumPy polygon results."""
    result = []
    for polygon in dt_box:
        if len(polygon) < 4:
            raise ValueError('A subtitle polygon must contain at least four points')
        xs, ys = zip(*polygon)
        result.append((int(min(xs)), int(max(xs)), int(min(ys)), int(max(ys))))
    return result
