#!/usr/bin/env python3
"""Fail early on missing native CPU/Qt libraries without downloading models."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def import_paddle_runtime(backend):
    if backend != 'paddle':
        return None, None
    if sys.platform == 'win32':
        # The official PySide/Shiboken wheels include app-local VC libraries,
        # including the VCOMP140 required by Paddle's mkldnn.dll. Initialize
        # their DLL directories before Paddle, as the real GUI/CLI already do.
        import PySide6  # noqa: F401
    import paddle
    import paddleocr
    return paddle, paddleocr


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    os.environ.setdefault("VSE_OFFLINE", "1")

    manifest_path = ROOT / 'bundle-manifest.json'
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        os.environ['VSE_OCR_BACKEND'] = manifest.get('ocr_backend', 'paddle')
        if os.environ['VSE_OCR_BACKEND'] == 'native':
            os.environ['VSE_NATIVE_OCR_ROOT'] = str(ROOT / 'native-ocr')
    from backend.tools.native_ocr import selected_backend
    backend = selected_backend()

    # Importing config/main reaches QtGui/QtWidgets even for CLI extraction.
    # Import PaddleOCR explicitly too; --help/--version do not load this stack.
    from ui.qt_runtime import prepare_qt_runtime
    prepare_qt_runtime()
    import cv2
    paddle, paddleocr = import_paddle_runtime(backend)
    from PIL import ImageFont
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter
    from PySide6.QtWidgets import QApplication
    from backend.main import SubtitleExtractor

    app = QApplication.instance() or QApplication([])
    font_path = ROOT / "backend/tools/NotoSansCJK-Bold.otf"
    font_id = QFontDatabase.addApplicationFont(str(font_path))
    families = QFontDatabase.applicationFontFamilies(font_id)
    if font_id < 0 or not families:
        raise RuntimeError("Qt cannot load the bundled font")
    font = QFont(families[0])
    font.setPixelSize(24)
    canvas = QImage(180, 48, QImage.Format.Format_ARGB32)
    canvas.fill(QColor("black"))
    painter = QPainter(canvas)
    try:
        painter.setFont(font)
        painter.setPen(QColor("white"))
        painter.drawText(canvas.rect(), Qt.AlignmentFlag.AlignCenter, "VSE 字幕")
    finally:
        painter.end()
    if not any(canvas.pixelColor(x, y) != QColor("black") for y in range(canvas.height()) for x in range(canvas.width())):
        raise RuntimeError("Qt font rendering produced no pixels")
    if ImageFont.truetype(str(font_path), 24).getlength("VSE") <= 0:
        raise RuntimeError("Pillow FreeType font measurement failed")
    if backend == 'paddle':
        # A real CPU operation loads lazy native Paddle math dependencies.
        paddle.set_device("cpu")
        if float(paddle.to_tensor([1.0, 2.0]).sum().item()) != 3.0:
            raise RuntimeError("Paddle CPU tensor smoke failed")
        inference = {"paddle_cpu": True, "paddle": paddle.__version__, "paddleocr": paddleocr.__version__}
    else:
        from PIL import Image, ImageDraw
        import numpy as np
        from backend.tools.ocr import OcrRecogniser
        frame = Image.new('RGB', (640, 180), 'black')
        ImageDraw.Draw(frame).text((40, 50), 'VSE 字幕', font=ImageFont.truetype(str(font_path), 48), fill='white')
        recognizer = OcrRecogniser('ch', 'fast', 1)
        try:
            boxes, records = recognizer.predict(np.ascontiguousarray(np.asarray(frame)[:, :, ::-1]))
            if not boxes or 'VSE' not in ''.join(text for text, score in records).replace(' ', ''):
                raise RuntimeError('Native CPU OCR smoke failed')
        finally:
            recognizer.close()
        if any(name.split('.')[0] in {'paddle', 'paddleocr', 'paddlex', 'onnxruntime'} for name in sys.modules):
            raise RuntimeError('Native CPU profile imported a forbidden Python inference framework')
        inference = {"native_cpu": True, "recognized": [text for text, score in records]}
    app.processEvents()
    print(json.dumps({"result": "passed", "qt_font_rendering": True,
                      "pillow_freetype": True, "ocr_backend": backend, **inference,
                      "opencv": cv2.__version__, "extractor_import": SubtitleExtractor.__name__}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
