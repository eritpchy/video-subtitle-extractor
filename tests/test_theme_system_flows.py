"""Real widgets with controlled system input; does not toggle desktop OS settings."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from PySide6 import QtCore, QtGui, QtTest, QtWidgets
from qfluentwidgets import Theme, qconfig

from backend.config import config
from test_theme import ThemeTestCase
from helpers.theme_wait import wait_for_widget_animations

ROOT = Path(__file__).resolve().parents[1]


class SystemThemeFlowTests(ThemeTestCase):
    def test_bundled_cjk_font_is_available_without_replacing_latin_font(self):
        from ui.fonts import register_bundled_cjk_font
        original = self.app.font().toString()
        family = register_bundled_cjk_font(self.app)
        self.assertEqual(register_bundled_cjk_font(self.app), family)
        self.assertEqual(self.app.font().toString(), original)
        raw = QtGui.QRawFont.fromFont(QtGui.QFont(family, 12))
        self.assertTrue(raw.isValid())
        for character in '【完成警告】中文测试':
            self.assertTrue(raw.supportsCharacter(ord(character)), character)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Fontconfig isolation is Linux-specific')
    def test_gui_cjk_fallback_without_any_system_fonts(self):
        directory = Path(self.temp.name)
        (directory / 'empty').mkdir()
        (directory / 'cache').mkdir()
        fontconfig = directory / 'fonts.conf'
        fontconfig.write_text('<fontconfig><dir>' + str(directory / 'empty') + '</dir><cachedir>'
                              + str(directory / 'cache') + '</cachedir></fontconfig>')
        code = '''
from PySide6 import QtWidgets, QtGui
from backend.config import config
from gui import SubtitleExtractorGUI
app = QtWidgets.QApplication([])
config.set(config.checkUpdateOnStartup, False, save=False)
window = SubtitleExtractorGUI()
layout = QtGui.QTextLayout('【完成】中文测试', window.homeInterface.output_text.font())
layout.beginLayout()
line = layout.createLine()
line.setLineWidth(500)
layout.endLayout()
runs = layout.glyphRuns()
assert runs and all(all(index != 0 for index in run.glyphIndexes()) for run in runs)
print('CJK_FALLBACK_OK')
window.close()
'''
        result = subprocess.run([sys.executable, '-c', code], cwd=ROOT,
            env=dict(os.environ, FONTCONFIG_FILE=str(fontconfig), QT_QPA_PLATFORM='offscreen',
                     VSE_CONFIG_FILE=str(directory / 'isolated.json')),
            capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('CJK_FALLBACK_OK', result.stdout)

    def create_window(self):
        from gui import SubtitleExtractorGUI
        window = SubtitleExtractorGUI()
        self.widgets.append(window)
        window.resize(1280, 900)
        window.show()
        self.app.processEvents()
        wait_for_widget_animations([window])
        return window

    def settled_page(self, window, page):
        finished = QtTest.QSignalSpy(window.stackedWidget.view.aniFinished)
        moving = window.stackedWidget.currentWidget() is not page
        window.switchTo(page)
        if moving:
            self.assertTrue(finished.count() or finished.wait(2000))
        self.app.processEvents()
        wait_for_widget_animations([window])

    def test_fresh_gui_restarts_apply_saved_mode_and_current_system(self):
        for mode in (Theme.LIGHT, Theme.DARK, Theme.AUTO):
            self.set_mode(mode)
            config.save()
            before = config.file.read_bytes()
            for system in (Theme.DARK, Theme.LIGHT):
                with self.subTest(mode=mode, system=system):
                    result = subprocess.run(
                        [sys.executable, str(ROOT / 'tests/helpers/theme_startup_probe.py'), system.value],
                        cwd=ROOT, env=dict(os.environ, VSE_CONFIG_FILE=str(config.file)),
                        capture_output=True, text=True, timeout=40)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    report = json.loads(next(line.split('=', 1)[1] for line in result.stdout.splitlines()
                                             if line.startswith('THEME_PROBE=')))
                    self.assertEqual(report['preference'], mode.value)
                    self.assertEqual(report['effective'], (system if mode == Theme.AUTO else mode).value)
                    # Closing the real window intentionally persists geometry;
                    # its appearance preference must remain unchanged.
                    saved = json.loads(config.file.read_text())
                    self.assertEqual(saved['QFluentWidgets']['ThemeMode'], mode.value)

    def test_auto_real_qt_signal_repaints_all_pages_without_changing_preference(self):
        window = self.create_window()
        current_system = [Theme.LIGHT]
        window.themeListener._readSystemTheme = lambda: current_system[0]
        self.set_mode(Theme.AUTO)
        before = config.file.read_bytes()
        hints = self.app.styleHints()
        brightness = {'home': [], 'timeline': [], 'settings': []}
        pages = (
            ('home', window.homeInterface, window.homeInterface.output_text.viewport()),
            ('timeline', window.timelineSyncInterface,
             window.timelineSyncInterface.findChild(QtWidgets.QWidget, 'timelineButtons')),
            ('settings', window.advancedSettingInterface, window.advancedSettingInterface.theme_mode),
        )
        for theme in (Theme.LIGHT, Theme.DARK, Theme.LIGHT):
            current_system[0] = theme
            scheme = QtCore.Qt.ColorScheme.Dark if theme == Theme.DARK else QtCore.Qt.ColorScheme.Light
            # Emit through Qt's real signal/connection, not the listener method.
            hints.colorSchemeChanged.emit(scheme)
            wait_for_widget_animations([window])
            self.assertEqual(qconfig.theme, theme)
            self.assertEqual(config.themeMode.value, Theme.AUTO)
            self.assertEqual(config.file.read_bytes(), before)
            for name, page, card in pages:
                self.settled_page(window, page)
                image = window.grab().toImage()
                self.assertFalse(image.isNull())
                point = card.mapTo(window, QtCore.QPoint(card.width() - 8, card.height() // 2))
                point *= image.devicePixelRatio()
                brightness[name].append(image.pixelColor(point).lightness())
        for page, values in brightness.items():
            with self.subTest(page=page):
                self.assertGreater(values[0], values[1] + 80)
                self.assertEqual(values[0], values[2])

    def test_explicit_mode_and_closed_window_ignore_late_qt_system_signals(self):
        window = self.create_window()
        hints = self.app.styleHints()
        for mode in (Theme.DARK, Theme.LIGHT, Theme.DARK):
            self.set_mode(mode)
            for scheme in (QtCore.Qt.ColorScheme.Light, QtCore.Qt.ColorScheme.Dark):
                hints.colorSchemeChanged.emit(scheme)
                self.app.processEvents()
                self.assertEqual(qconfig.theme, mode)
                self.assertFalse(window.themeListener._timer.isActive())
        self.set_mode(Theme.AUTO)
        window.close()
        before = qconfig.theme
        hints.colorSchemeChanged.emit(QtCore.Qt.ColorScheme.Light)
        hints.colorSchemeChanged.emit(QtCore.Qt.ColorScheme.Dark)
        self.app.processEvents()
        self.assertEqual(qconfig.theme, before)
        self.assertFalse(window.themeListener._timer.isActive())
