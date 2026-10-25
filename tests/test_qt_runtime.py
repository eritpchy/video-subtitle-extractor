"""Run in fresh processes: cv2 mutates environment during its first import."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
NAMES = ("QT_QPA_PLATFORM_PLUGIN_PATH", "QT_QPA_FONTDIR")


@pytest.mark.parametrize("system,backend", [
    ("darwin", "cocoa"), ("linux", "xcb"), ("linux", "offscreen"),
    ("win32", "windows"), ("win32", "offscreen"),
])
def test_supported_native_window_backends(system, backend):
    from ui.qt_runtime import require_native_window_backend
    require_native_window_backend(backend, system=system)


@pytest.mark.parametrize("backend", ["offscreen", "minimal", "xcb"])
def test_macos_rejects_non_nsview_backends_before_frameless_window(backend):
    from ui.qt_runtime import require_native_window_backend
    with pytest.raises(RuntimeError, match="native Cocoa backend"):
        require_native_window_backend(backend, system="darwin")


@pytest.mark.parametrize("preimport", [False, True])
def test_opencv_qt_paths_are_removed_before_pyside_application(preimport):
    env = {key: value for key, value in os.environ.items() if key not in NAMES}
    env["QT_QPA_PLATFORM"] = "offscreen"
    script = ("import cv2\n" if preimport else "") + """
import os
from pathlib import Path
from ui.qt_runtime import prepare_qt_runtime
prepare_qt_runtime()
import cv2
qt = Path(cv2.__file__).resolve().parent / 'qt'
for name in ('QT_QPA_PLATFORM_PLUGIN_PATH', 'QT_QPA_FONTDIR'):
    assert name not in os.environ, (name, os.environ.get(name))
from PySide6.QtWidgets import QApplication
app = QApplication([])
assert app.platformName() == 'offscreen'
"""
    subprocess.run([sys.executable, "-c", script], env=env, cwd=ROOT,
                   check=True, capture_output=True, text=True, timeout=30)


def test_explicit_user_qt_overrides_survive_cv2_import(tmp_path):
    env = dict(os.environ)
    env.update({name: str(tmp_path / name.lower()) for name in NAMES})
    script = """
import os
before = {name: os.environ[name] for name in ('QT_QPA_PLATFORM_PLUGIN_PATH', 'QT_QPA_FONTDIR')}
from ui.qt_runtime import prepare_qt_runtime
prepare_qt_runtime()
prepare_qt_runtime()
assert all(os.environ[name] == value for name, value in before.items())
"""
    subprocess.run([sys.executable, "-c", script], env=env, cwd=ROOT,
                   check=True, capture_output=True, text=True, timeout=30)
