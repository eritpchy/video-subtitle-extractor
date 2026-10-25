"""Real Qt GUI/theme regressions; offscreen execution is not native-desktop proof."""

import configparser
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

# macOS frameless widgets require a real Cocoa NSView, even in GUI tests.
os.environ.setdefault("QT_QPA_PLATFORM", "cocoa" if sys.platform == "darwin" else "offscreen")

from PySide6 import QtCore, QtTest, QtWidgets
from qfluentwidgets import ConfigItem, Theme, qconfig

from backend.config import config
from backend.tools.theme_listener import SystemThemeListener

ROOT = Path(__file__).resolve().parents[1]


class ThemeTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saved_file = config.file
        self.saved_values = [
            (item, item.value)
            for name in dir(type(config))
            if isinstance(item := getattr(type(config), name), ConfigItem)
        ]
        self.saved_theme = qconfig.theme
        config.file = Path(self.temp.name) / "config.json"
        config.set(config.checkUpdateOnStartup, False, save=False)
        qconfig.set(config.themeMode, Theme.LIGHT, save=False)
        self.widgets = []
        self.listeners = []

    def tearDown(self):
        for listener in self.listeners:
            listener.stop()
            listener.deleteLater()
        for widget in self.widgets:
            widget.close()
            widget.deleteLater()
        self.app.processEvents()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        for item, value in self.saved_values:
            item.value = value
        qconfig.theme = self.saved_theme
        config.file = self.saved_file
        self.temp.cleanup()

    def set_mode(self, mode):
        qconfig.set(config.themeMode, mode)
        self.app.processEvents()
        QtTest.QTest.qWait(160)  # Fluent background animations last 120 ms.

    def listener(self, system=Theme.LIGHT):
        listener = SystemThemeListener(poll_interval=60000)
        listener._readSystemTheme = lambda: system
        listener.start()
        self.listeners.append(listener)
        return listener


class ThemeControllerTests(ThemeTestCase):
    def test_explicit_preference_ignores_system_changes(self):
        listener = self.listener()
        for mode, system in [(Theme.LIGHT, Theme.DARK), (Theme.DARK, Theme.LIGHT)]:
            self.set_mode(mode)
            listener._onThemeChanged(system)
            self.assertEqual(config.themeMode.value, mode)
            self.assertEqual(qconfig.theme, mode)
            self.assertFalse(listener._timer.isActive())

    def test_auto_follows_changes_without_overwriting_persisted_choice(self):
        listener = self.listener()
        self.set_mode(Theme.AUTO)
        saved = config.file.read_bytes()
        for theme in (Theme.DARK, Theme.LIGHT, Theme.DARK):
            listener._onThemeChanged(theme)
            self.assertEqual(qconfig.theme, theme)
            self.assertEqual(config.themeMode.value, Theme.AUTO)
            self.assertEqual(config.file.read_bytes(), saved)
        self.assertTrue(listener._timer.isActive())

    def test_returning_to_auto_uses_current_system_appearance(self):
        listener = self.listener(Theme.DARK)
        self.set_mode(Theme.LIGHT)
        self.set_mode(Theme.AUTO)
        self.assertEqual(qconfig.theme, Theme.DARK)
        self.assertEqual(config.themeMode.value, Theme.AUTO)

    def test_unavailable_detection_keeps_last_known_appearance(self):
        listener = self.listener(Theme.DARK)
        self.set_mode(Theme.AUTO)
        listener._readSystemTheme = lambda: None
        listener.refresh()
        listener._onThemeChanged("unknown")
        self.assertEqual(qconfig.theme, Theme.DARK)

    def test_qt_signal_and_fallback_detector(self):
        listener = SystemThemeListener()
        listener._style_hints = None
        with patch("backend.tools.theme_listener.darkdetect.theme", return_value="Dark"):
            self.assertEqual(listener._readSystemTheme(), Theme.DARK)
        with patch("backend.tools.theme_listener.darkdetect.theme", side_effect=OSError("No desktop")):
            self.assertIsNone(listener._readSystemTheme())
        listener.deleteLater()
        listener = self.listener()
        self.set_mode(Theme.AUTO)
        listener._onColorSchemeChanged(QtCore.Qt.ColorScheme.Dark)
        self.assertEqual(qconfig.theme, Theme.DARK)

    def test_fallback_timer_updates_on_the_gui_thread(self):
        listener = self.listener()
        listener._timer.setInterval(10)
        self.set_mode(Theme.AUTO)
        listener._readSystemTheme = lambda: Theme.DARK
        QtTest.QTest.qWait(40)
        self.assertEqual(qconfig.theme, Theme.DARK)
        self.assertEqual(listener.thread(), self.app.thread())
        self.assertEqual(config.themeMode.value, Theme.AUTO)
        self.set_mode(Theme.LIGHT)
        self.assertFalse(listener._timer.isActive())

    def test_stop_is_idempotent_and_queued_callbacks_do_nothing(self):
        listener = self.listener()
        self.set_mode(Theme.AUTO)
        listener.start()
        self.assertTrue(listener._timer.isActive())
        qconfig.set(config.themeMode, Theme.DARK)
        listener.stop()
        listener.stop()
        self.app.processEvents()
        listener._onThemeChanged(Theme.LIGHT)
        self.assertEqual(qconfig.theme, Theme.DARK)
        self.assertFalse(listener._timer.isActive())

    def test_all_preferences_survive_a_fresh_process(self):
        for mode in Theme:
            with self.subTest(mode=mode):
                self.set_mode(mode)
                config.save()
                self.assertEqual(json.loads(config.file.read_text())["QFluentWidgets"]["ThemeMode"], mode.value)
                env = dict(os.environ, VSE_CONFIG_FILE=str(config.file))
                result = subprocess.run(
                    [sys.executable, "-c", "from backend.config import config; print('SAVED_THEME=' + config.themeMode.value.value)"],
                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("SAVED_THEME=" + mode.value, result.stdout)

    def test_all_interface_languages_have_theme_labels(self):
        for path in (ROOT / "backend/interface").glob("*.ini"):
            translations = configparser.ConfigParser()
            translations.read(path, encoding="utf-8")
            for key in ("Appearance", "Theme", "ThemeDesc", "ThemeLight", "ThemeDark", "ThemeSystem"):
                self.assertTrue(translations["Setting"].get(key), f"{path.name}: {key}")


class WindowThemeTests(ThemeTestCase):
    def setUp(self):
        super().setUp()
        from gui import SubtitleExtractorGUI
        self.window = SubtitleExtractorGUI()
        self.widgets.append(self.window)
        self.window.resize(1280, 900)
        self.window.show()
        self.app.processEvents()

    def test_open_window_selector_updates_all_pages_and_persists(self):
        combo = self.window.advancedSettingInterface.theme_mode.comboBox
        self.assertEqual(combo.count(), 3)
        for mode in (Theme.DARK, Theme.LIGHT, Theme.DARK, Theme.AUTO):
            combo.setCurrentIndex(combo.findData(mode))
            self.app.processEvents()
            self.assertEqual(config.themeMode.value, mode)
            self.assertEqual(json.loads(config.file.read_text())["QFluentWidgets"]["ThemeMode"], mode.value)
            if mode != Theme.AUTO:
                self.assertEqual(qconfig.theme, mode)
            for interface in (self.window.homeInterface, self.window.timelineSyncInterface,
                              self.window.advancedSettingInterface):
                self.window.switchTo(interface)
                self.app.processEvents()
                self.assertFalse(self.window.grab().isNull())
        self.assertFalse(hasattr(self.window, "check_update_timer"))

    def test_timeline_card_renders_dark_and_light_backgrounds(self):
        card = self.window.timelineSyncInterface.findChild(QtWidgets.QWidget, "timelineButtons")
        self.window.switchTo(self.window.timelineSyncInterface)
        brightness = []
        for mode in (Theme.LIGHT, Theme.DARK, Theme.LIGHT):
            self.set_mode(mode)
            # Capture the full window: CardWidget is translucent and an isolated
            # grab composites it against Qt's default palette, not its parent.
            image = self.window.grab().toImage()
            point = card.mapTo(self.window, QtCore.QPoint(card.width() - 20, card.height() // 2))
            point *= image.devicePixelRatio()
            brightness.append(image.pixelColor(point).lightness())
        self.assertGreater(brightness[0], brightness[1] + 80)
        self.assertEqual(brightness[0], brightness[2])

    def test_existing_status_and_log_text_recolors_without_losing_content(self):
        from ui.component.task_list_component import TaskStatus
        home = self.window.homeInterface
        tasks = home.task_list_component
        tasks.add_task("theme-test.mp4")
        tasks.update_task_status(0, TaskStatus.COMPLETED)
        home.append_output("【完成】Theme test")
        self.app.processEvents()
        before = home.output_text.toPlainText()
        status_colors, log_colors = [], []
        for mode in (Theme.LIGHT, Theme.DARK, Theme.LIGHT):
            self.set_mode(mode)
            status_colors.append(tasks.table.item(0, 2).foreground().color().name())
            formats = home.output_text.document().firstBlock().layout().formats()
            self.assertTrue(formats)
            log_colors.append(formats[0].format.foreground().color().name())
            self.assertEqual(home.output_text.toPlainText(), before)
        self.assertNotEqual(status_colors[0], status_colors[1])
        self.assertEqual(status_colors[0], status_colors[2])
        self.assertNotEqual(log_colors[0], log_colors[1])
        self.assertEqual(log_colors[0], log_colors[2])
        tasks.update_task_status(0, TaskStatus.PENDING)
        self.assertEqual(tasks.table.item(0, 2).foreground().style(), QtCore.Qt.BrushStyle.NoBrush)

    def test_auto_changes_on_open_window_then_stops_on_close(self):
        self.set_mode(Theme.AUTO)
        listener = self.window.themeListener
        listener._onThemeChanged(Theme.DARK)
        self.assertEqual(qconfig.theme, Theme.DARK)
        self.assertEqual(config.themeMode.value, Theme.AUTO)
        self.window.close()
        self.assertFalse(listener._timer.isActive())
        listener._onThemeChanged(Theme.LIGHT)
        self.assertEqual(qconfig.theme, Theme.DARK)

    def test_delayed_settings_workarounds_run_for_live_widget(self):
        import shiboken6
        settings = self.window.advancedSettingInterface
        callbacks = []
        settings.macos_scrollarea_issue_workaround = lambda: callbacks.append(shiboken6.isValid(settings))
        self.window.switchTo(settings)
        QtTest.QTest.qWait(900)
        self.assertEqual(callbacks, [True, True])

    def test_delayed_settings_workarounds_cancel_when_widget_is_deleted(self):
        import shiboken6
        settings = self.window.advancedSettingInterface
        callbacks = []
        settings.macos_scrollarea_issue_workaround = lambda: callbacks.append(shiboken6.isValid(settings))
        self.window.switchTo(settings)
        self.window.close()
        self.window.deleteLater()
        self.widgets.remove(self.window)  # Already deleted, not a teardown target.
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.assertFalse(shiboken6.isValid(self.window))
        self.assertFalse(shiboken6.isValid(settings))
        QtTest.QTest.qWait(900)
        self.assertEqual(callbacks, [], "Delayed callback reached a destroyed settings widget")

    def test_stop_icon_paths_are_independent_of_working_directory(self):
        from ui.icon.my_fluent_icon import MyFluentIcon
        for theme in (Theme.LIGHT, Theme.DARK):
            path = Path(MyFluentIcon.Stop.path(theme))
            self.assertTrue(path.is_absolute())
            self.assertTrue(path.is_file())


if __name__ == "__main__":
    unittest.main()
