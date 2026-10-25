#!/usr/bin/env python3
"""Fail early on missing native CPU/Qt libraries without downloading models."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    os.environ.setdefault("VSE_OFFLINE", "1")

    # Importing config/main reaches QtGui/QtWidgets even for CLI extraction.
    # Import PaddleOCR explicitly too; --help/--version do not load this stack.
    from ui.qt_runtime import prepare_qt_runtime
    prepare_qt_runtime()
    import cv2
    import paddle
    import paddleocr
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
    # A real CPU operation also loads lazy native Paddle math dependencies.
    paddle.set_device("cpu")
    if float(paddle.to_tensor([1.0, 2.0]).sum().item()) != 3.0:
        raise RuntimeError("Paddle CPU tensor smoke failed")
    app.processEvents()
    print(json.dumps({"result": "passed", "qt_font_rendering": True,
                      "pillow_freetype": True, "paddle_cpu": True,
                      "paddle": paddle.__version__, "paddleocr": paddleocr.__version__,
                      "opencv": cv2.__version__, "extractor_import": SubtitleExtractor.__name__}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
