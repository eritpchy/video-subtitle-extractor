"""Keep OpenCV's bundled Qt 5 plugins out of the PySide6 (Qt 6) GUI."""

import os
from pathlib import Path


def prepare_qt_runtime():
    """Import cv2 once, restoring only Qt variables its wheel overwrites.

    Linux OpenCV wheels set these process-wide paths at import time. Their
    xcb plugin is built for Qt 5 and cannot be loaded by our Qt 6 application.
    Leave unrelated/user-configured paths alone, including explicit overrides
    that OpenCV replaced during this import. Run before creating QApplication.
    """
    names = ("QT_QPA_PLATFORM_PLUGIN_PATH", "QT_QPA_FONTDIR")
    original = {name: os.environ.get(name) for name in names}
    import cv2

    bundled_qt = Path(cv2.__file__).resolve().parent / "qt"

    def is_opencv_path(value):
        return value is not None and Path(value).resolve().is_relative_to(bundled_qt)

    for name in names:
        if is_opencv_path(os.environ.get(name)):
            previous = original[name]
            if previous is not None and not is_opencv_path(previous):
                os.environ[name] = previous
            else:
                os.environ.pop(name, None)
