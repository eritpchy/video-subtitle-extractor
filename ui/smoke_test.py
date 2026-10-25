"""Deterministic GUI smoke check used by source and packaged-app CI."""

import json
import os
from pathlib import Path
import traceback

from PySide6 import QtCore
from qfluentwidgets import Theme, qconfig

from backend.config import config


def run_gui_smoke(app, window):
    """Render every page, switch appearances, exercise a video and close cleanly.

    Set VSE_SMOKE_SCREENSHOT_DIR to retain screenshots. This verifies actual Qt
    widgets, but an offscreen run does not prove native window-manager behavior.
    """
    screenshot_dir = os.environ.get("VSE_SMOKE_SCREENSHOT_DIR")
    if screenshot_dir:
        screenshot_dir = Path(screenshot_dir)
        screenshot_dir.mkdir(parents=True, exist_ok=True)

    def settle():
        loop = QtCore.QEventLoop()
        # Include navigation-indicator and platform resize animations.
        QtCore.QTimer.singleShot(700, loop.quit)
        loop.exec()

    try:
        window.resize(1280, 900)
        window.show()
        settle()
        from ui.component.task_list_component import TaskStatus
        home = window.homeInterface
        # Source checkouts include the fixture; packaged applications need not.
        video = Path(__file__).resolve().parents[1] / "test" / "test_en.mp4"
        if video.is_file():
            home.load_video(str(video))
            if home.video_cap is None or not home.video_cap.isOpened():
                raise AssertionError("GUI video preview did not open the fixture")
        home.task_list_component.add_task(str(video))
        home.task_list_component.update_task_status(0, TaskStatus.COMPLETED)
        home.append_output("【完成】GUI smoke check")
        home.append_output("【警告】Theme contrast check")
        pages = (
            ("home", home),
            ("timeline", window.timelineSyncInterface),
            ("settings", window.advancedSettingInterface),
        )
        combo = window.advancedSettingInterface.theme_mode.comboBox
        for theme in (Theme.LIGHT, Theme.DARK, Theme.LIGHT):
            combo.setCurrentIndex(combo.findData(theme))
            settle()
            if qconfig.theme != theme:
                raise AssertionError(f"Theme did not switch to {theme.value}")
            for name, page in pages:
                window.switchTo(page)
                settle()
                screenshot = window.grab()
                if screenshot.isNull():
                    raise AssertionError(f"Could not render {name}")
                if screenshot_dir and not screenshot.save(str(screenshot_dir / f"{theme.value.lower()}-{name}.png")):
                    raise AssertionError(f"Could not save {name} screenshot")

        combo.setCurrentIndex(combo.findData(Theme.AUTO))
        settle()
        for theme in (Theme.DARK, Theme.LIGHT):
            window.themeListener._onThemeChanged(theme)
            settle()
            if qconfig.theme != theme or config.themeMode.value != Theme.AUTO:
                raise AssertionError("System appearance changed the saved theme preference")
        saved = json.loads(config.file.read_text(encoding="utf-8"))
        if saved["QFluentWidgets"]["ThemeMode"] != Theme.AUTO.value:
            raise AssertionError("System theme preference was not persisted")
        window.close()
        settle()
        if window.themeListener._timer.isActive():
            raise AssertionError("Theme listener is still running after close")
        print("GUI_SMOKE_OK: all pages, light/dark/system, persistence and shutdown")
        return 0
    except Exception:
        traceback.print_exc()
        window.close()
        return 1
    finally:
        if window.homeInterface.video_cap is not None:
            window.homeInterface.video_cap.release()
