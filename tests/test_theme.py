"""Real Qt GUI/theme regressions; offscreen execution is not native-desktop proof."""

import configparser
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

# macOS frameless widgets require a real Cocoa NSView, even in GUI tests.
os.environ.setdefault("QT_QPA_PLATFORM", "cocoa" if sys.platform == "darwin" else "offscreen")

from PySide6 import QtCore, QtTest, QtWidgets
from qfluentwidgets import ConfigItem, Theme, qconfig

from backend.config import config
from backend.tools.theme_listener import SystemThemeListener
from helpers.theme_wait import animation_states, wait_for_widget_animations

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
        wait_for_widget_animations(self.widgets)

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
        self.assertTrue(listener._timer.isActive())
        self.assertEqual(listener._timer.interval(), 10)
        timer_ticks = QtTest.QSignalSpy(listener._timer.timeout)
        theme_changes = QtTest.QSignalSpy(listener.systemThemeChanged)
        detector_threads = []

        def detect_dark():
            detector_threads.append(QtCore.QThread.currentThread())
            return Theme.DARK

        listener._readSystemTheme = detect_dark
        # Native timer delivery is not guaranteed within a nominal 40 ms on a
        # loaded Cocoa runner. Await the actual timer-driven theme-change signal
        # while Qt processes events, without calling refresh/the callback here.
        self.assertTrue(theme_changes.count() or theme_changes.wait(2000),
                        'The active fallback timer did not apply the changed system theme')
        self.assertGreater(timer_ticks.count(), 0)
        self.assertTrue(detector_threads)
        self.assertTrue(all(thread == self.app.thread() for thread in detector_threads))
        self.assertEqual(qconfig.theme, Theme.DARK)
        self.assertEqual(listener.thread(), self.app.thread())
        self.assertEqual(config.themeMode.value, Theme.AUTO)
        self.assertTrue(listener._timer.isActive())
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

    def test_window_and_application_icons_keep_rounded_transparency(self):
        for icon in (self.window.windowIcon(), self.app.windowIcon()):
            self.assertFalse(icon.isNull())
            for size in (16, 32, 128):
                image = icon.pixmap(size, size).toImage()
                self.assertFalse(image.isNull())
                width, height = image.width(), image.height()
                for point in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)):
                    self.assertEqual(image.pixelColor(*point).alpha(), 0)
                self.assertGreaterEqual(image.pixelColor(width // 2, height // 2).alpha(), 250)

    def switch_to_settled_page(self, interface):
        # FluentWindow.switchTo starts a 300 ms position animation. Waiting
        # only for the 120 ms theme animation can capture a moving translucent
        # card, especially with Cocoa. Await navigation itself, not a guessed
        # screenshot delay, and retain exact round-trip color assertions.
        view = self.window.stackedWidget.view
        finished = QtTest.QSignalSpy(view.aniFinished)
        needs_navigation = self.window.stackedWidget.currentWidget() is not interface
        self.window.switchTo(interface)
        if needs_navigation:
            self.assertTrue(finished.count() or finished.wait(2000),
                            "Page navigation animation did not finish")
        self.app.processEvents()
        self.assertIs(self.window.stackedWidget.currentWidget(), interface)
        self.assertTrue(interface.isVisible())
        wait_for_widget_animations([self.window])

    def test_theme_wait_uses_queued_paint_without_synchronous_rendering(self):
        # Isolate the waiter from Fluent animations' own legitimate update()
        # calls so exactly one queued request is measurable.
        widget = QtWidgets.QWidget()
        self.widgets.append(widget)
        widget.show()
        self.app.processEvents()
        # Forbidden-method mocks detect the old implementation deterministically
        # without needing to reproduce Windows style-filter recursion.
        with patch.object(widget, 'repaint') as repaint, \
                patch.object(widget, 'ensurePolished') as polish, \
                patch.object(widget, 'update', wraps=widget.update) as update:
            wait_for_widget_animations([widget])
        repaint.assert_not_called()
        polish.assert_not_called()
        update.assert_called_once_with()

    def test_theme_wait_awaits_real_completion_beyond_nominal_duration(self):
        card = self.window.timelineSyncInterface.findChild(QtWidgets.QWidget, 'timelineButtons')
        self.switch_to_settled_page(self.window.timelineSyncInterface)
        # Reproduce a transition that outlives the former 160 ms sleep. Native
        # runner scheduling may likewise delay delivery/restart of animations.
        self.window.backgroundColorAni.setDuration(450)
        card.backgroundColorAni.setDuration(450)
        for mode in (Theme.DARK, Theme.LIGHT):
            self.set_mode(mode)
            for widget in (self.window, card):
                self.assertEqual(widget.backgroundColorAni.state(), QtCore.QAbstractAnimation.State.Stopped)
                self.assertEqual(widget.backgroundColor, widget.backgroundColorAni.endValue())

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
                self.switch_to_settled_page(interface)
                self.assertFalse(self.window.grab().isNull())
        self.assertFalse(hasattr(self.window, "check_update_timer"))

    def test_timeline_card_renders_dark_and_light_backgrounds(self):
        card = self.window.timelineSyncInterface.findChild(QtWidgets.QWidget, "timelineButtons")
        self.switch_to_settled_page(self.window.timelineSyncInterface)
        brightness = []
        evidence = ROOT / 'test-results/theme-roundtrip'
        evidence.mkdir(parents=True, exist_ok=True)
        samples = []
        for mode in (Theme.LIGHT, Theme.DARK, Theme.LIGHT):
            self.set_mode(mode)
            # Capture the full window: CardWidget is translucent and an isolated
            # grab composites it against Qt's default palette, not its parent.
            image = self.window.grab().toImage()
            point = card.mapTo(self.window, QtCore.QPoint(card.width() - 20, card.height() // 2))
            point *= image.devicePixelRatio()
            brightness.append(image.pixelColor(point).lightness())
            image.save(str(evidence / f'{len(brightness)}-{mode.value.lower()}.png'))
            samples.append({'mode': mode.value, 'brightness': brightness[-1],
                            'monotonic_seconds': time.monotonic(),
                            'effective_theme': qconfig.theme.value,
                            'sample_point_pixels': [point.x(), point.y()],
                            'card_geometry': [card.x(), card.y(), card.width(), card.height()],
                            'image': f'{len(brightness)}-{mode.value.lower()}.png',
                            'window_color': self.window.backgroundColor.name(),
                            'card_rgba': card.backgroundColor.getRgb(),
                            'card_hover': card.isHover,
                            'animations': animation_states([self.window])})
            (evidence / 'states.json').write_text(json.dumps(samples, indent=2))
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
