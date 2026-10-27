"""Actual Qt paint/binding regressions; not native popup or app-restart proof.

A controlled native-like style deliberately ignores QPalette for the bezel.
Tests isolate their own configuration and never alter the global application style.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'cocoa' if sys.platform == 'darwin' else 'offscreen')
from PySide6 import QtCore, QtGui, QtTest, QtWidgets
from qfluentwidgets import (ComboBoxSettingCard, FluentIcon, OptionsConfigItem,
                            OptionsValidator, QConfig, Theme, qconfig, themeColor)
from backend.tools.theme_listener import SystemThemeListener
from ui import macos_setting_controls as adapter


class PaletteIgnoringBezel(QtWidgets.QProxyStyle):
    """Controlled drawing limitation, not an emulation of the entire Cocoa style."""
    def __init__(self):
        super().__init__(QtWidgets.QStyleFactory.create('Fusion'))
        self.bezel_calls = 0

    def drawComplexControl(self, control, option, painter, widget=None):
        if control == QtWidgets.QStyle.CC_ComboBox:
            self.bezel_calls += 1
            painter.fillRect(option.rect, QtGui.QColor('white'))
            return
        super().drawComplexControl(control, option, painter, widget)


class MacSettingControlPaintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        class PrivateConfig(QConfig):
            mode = OptionsConfigItem('Main', 'Mode', 'auto', OptionsValidator(['auto', 'fast', 'accurate']))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = PrivateConfig()
        self.config.file = Path(self.temp.name) / 'config.json'
        self.saved_mode, self.saved_theme = qconfig.themeMode.value, qconfig.theme
        # Isolate the existing Fluent singleton's backing configuration only for
        # this test. Restore the exact old object, not a reload of a user's file.
        self.config_patch = patch.object(qconfig, '_cfg', self.config)
        self.config_patch.start()
        self.bridge_connected = False
        self.addCleanup(self.restore_configuration)
        self.config.themeChanged.connect(qconfig.themeChanged)
        self.bridge_connected = True
        qconfig.themeMode.value = Theme.LIGHT
        qconfig.theme = Theme.DARK
        self.styles, self.listeners = [], []
        self.window = QtWidgets.QWidget()
        self.addCleanup(self.dispose_widgets)
        self.layout = QtWidgets.QVBoxLayout(self.window)
        self.window.resize(500, 180)
        self.original_app_style = self.app.style()
        self.original_app_stylesheet = self.app.styleSheet()

    def dispose_widgets(self):
        try:
            for listener in self.listeners:
                try:
                    listener.stop()
                finally:
                    listener.deleteLater()
        finally:
            self.window.close()
            self.window.deleteLater()
            self.app.processEvents()
            QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)

    def restore_configuration(self):
        try:
            if self.bridge_connected:
                self.config.themeChanged.disconnect(qconfig.themeChanged)
                self.bridge_connected = False
        finally:
            self.config_patch.stop()
            qconfig.themeMode.value = self.saved_mode
            qconfig.theme = self.saved_theme

    def combo(self, hostile=False):
        combo = adapter.MacSettingComboBox(self.window)
        combo.addItems(['MMMM Auto', 'Fast', 'Accurate'])
        font = combo.font()
        font.setPixelSize(18)
        combo.setFont(font)
        combo.setMinimumWidth(300)
        if hostile:
            style = PaletteIgnoringBezel()
            self.styles.append(style)
            combo.setStyle(style)
        self.layout.addWidget(combo)
        self.window.show()
        self.app.processEvents()
        return combo

    def card(self, item=None):
        item = self.config.mode if item is None else item
        card = ComboBoxSettingCard(item, FluentIcon.SETTING, 'Preference', 'Accessible setting',
                                   texts=[str(v) for v in item.options], parent=self.window)
        self.layout.addWidget(card)
        return card

    @staticmethod
    def adapt(card):
        with patch.object(adapter, 'sys', SimpleNamespace(platform='darwin')):
            return adapter.adapt_macos_setting_combo(card)

    @staticmethod
    def fill(combo):
        image = combo.grab().toImage()
        return image.pixelColor(int(image.width() * .70), image.height() // 2).name()

    def test_fixture_restores_config_and_owns_cleanup_even_if_setup_fails(self):
        class InterruptedSetup(MacSettingControlPaintTests):
            def setUp(inner):
                super().setUp()
                raise RuntimeError('controlled setup interruption')
            def test_never_runs(inner):
                raise AssertionError('setup failure must prevent test body')
        original_config = qconfig._cfg
        mode, theme = qconfig.themeMode.value, qconfig.theme
        case = InterruptedSetup('test_never_runs')
        result = unittest.TestResult()
        case.run(result)
        self.assertEqual(len(result.errors), 1)
        self.assertIn('controlled setup interruption', result.errors[0][1])
        self.assertFalse(result.failures)
        self.assertIs(qconfig._cfg, original_config)
        self.assertEqual(qconfig.themeMode.value, mode)
        self.assertEqual(qconfig.theme, theme)
        self.assertFalse(Path(case.temp.name).exists())
        with self.assertRaises(RuntimeError):
            case.window.objectName()  # Its C++ widget was actually disposed.

    def test_palette_only_native_like_bezel_still_paints_white(self):
        combo = self.combo(hostile=True)
        combo.setStyleSheet('')  # Controlled old palette-only strategy.
        self.app.processEvents()
        self.assertEqual(combo.palette().color(QtGui.QPalette.Base).name(), '#323232')
        self.assertEqual(combo.palette().color(QtGui.QPalette.Text).name(), '#ffffff')
        self.assertEqual(self.fill(combo), '#ffffff')
        self.assertGreater(self.styles[-1].bezel_calls, 0)

    def test_explicit_border_paints_dark_fill_and_real_legible_ink(self):
        combo = self.combo(hostile=True)
        self.assertEqual(self.fill(combo), '#323232')
        self.assertEqual(self.styles[-1].bezel_calls, 0)
        image = combo.grab().toImage()
        # Antialiasing need not yield exact white. Exclude border/arrow and
        # require actual neutral bright glyph pixels against exact dark fill.
        ink = sum(min(image.pixelColor(x, y).getRgb()[:3]) >= 220 and
                  max(image.pixelColor(x, y).getRgb()[:3]) - min(image.pixelColor(x, y).getRgb()[:3]) <= 2
                  for x in range(12, 100) for y in range(3, image.height() - 3))
        self.assertGreater(ink, 10)

    def test_light_dark_and_transparent_ancestor_paint_without_global_style_change(self):
        self.window.setStyleSheet('QWidget { background: transparent; }')
        combo = self.combo()
        for theme, expected in ((Theme.LIGHT, '#ffffff'), (Theme.DARK, '#323232')):
            with self.subTest(theme=theme.value):
                qconfig.theme = theme
                qconfig.themeChangedFinished.emit()
                self.app.processEvents()
                self.assertEqual(self.fill(combo), expected)
                self.assertEqual(combo.view().palette().color(QtGui.QPalette.Base).name(), expected)
        self.assertIs(self.app.style(), self.original_app_style)
        self.assertEqual(self.app.styleSheet(), self.original_app_stylesheet)

    def test_all_original_24_palette_colors_remain_exact_on_combo_and_view(self):
        combo = self.combo()
        for theme in (Theme.LIGHT, Theme.DARK, Theme.LIGHT):
            qconfig.theme = theme
            qconfig.themeChangedFinished.emit()
            self.app.processEvents()
            dark = theme == Theme.DARK
            for label, widget in (('combo', combo), ('view', combo.view())):
                for group in (QtGui.QPalette.Active, QtGui.QPalette.Inactive, QtGui.QPalette.Disabled):
                    foreground = ('#999999' if dark else '#777777') if group == QtGui.QPalette.Disabled \
                        else ('#ffffff' if dark else '#1b1b1b')
                    expected = {'Text': foreground, 'ButtonText': foreground, 'WindowText': foreground,
                                'Base': '#323232' if dark else '#ffffff',
                                'Button': '#323232' if dark else '#ffffff',
                                'Window': '#323232' if dark else '#ffffff',
                                'Highlight': themeColor().name(), 'HighlightedText': '#ffffff'}
                    for name, color in expected.items():
                        with self.subTest(theme=theme.value, widget=label, group=group.name, role=name):
                            brush = widget.palette().brush(group, getattr(QtGui.QPalette, name))
                            self.assertEqual(brush.color().rgba(), QtGui.QColor(color).rgba())
                            self.assertEqual(brush.style(), QtCore.Qt.SolidPattern)
                            self.assertTrue(brush.transform().isIdentity())

    def test_arrow_resources_exist_for_both_effective_themes(self):
        combo = self.combo()
        for theme in (Theme.LIGHT, Theme.DARK):
            qconfig.theme = theme
            qconfig.themeChangedFinished.emit()
            resource = FluentIcon.ARROW_DOWN.path(theme)
            self.assertTrue(QtCore.QFile.exists(resource), resource)
            self.assertIn(resource, combo.styleSheet())

    def test_initialization_preserves_card_data_and_never_saves(self):
        card = self.card()
        old = card.comboBox
        expected = [(old.itemText(i), old.itemData(i)) for i in range(old.count())]
        with patch.object(qconfig, 'set', wraps=qconfig.set) as setter, \
                patch.object(qconfig, 'save', wraps=qconfig.save) as save:
            combo = self.adapt(card)
            self.assertIs(self.adapt(card), combo)
            setter.assert_not_called()
            save.assert_not_called()
        self.assertEqual([(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())], expected)
        self.assertIs(card.titleLabel.buddy(), combo)
        self.assertFalse(self.config.file.exists())

    def test_local_style_keeps_real_key_activation_single_and_persistent(self):
        combo = self.adapt(self.card())
        changed, activated = QtTest.QSignalSpy(combo.currentIndexChanged), QtTest.QSignalSpy(combo.activated)
        with patch.object(qconfig, 'save', wraps=qconfig.save) as save:
            # A real Qt key event to this unit-test target, not a claim about
            # native foreground focus/Tab routing (covered by existing tests).
            QtTest.QTest.keyClick(combo, QtCore.Qt.Key_End)
            self.app.processEvents()
            self.assertEqual(combo.currentData(), 'accurate')
            self.assertEqual(changed.count(), 1)
            self.assertEqual(activated.count(), 1)
            self.assertEqual(save.call_count, 1)
            QtTest.QTest.keyClick(combo, QtCore.Qt.Key_End)
            self.assertEqual(save.call_count, 1)
        self.assertEqual(json.loads(self.config.file.read_text(encoding='utf-8'))['Main']['Mode'], 'accurate')

    def test_accessible_menu_cancel_keeps_binding_and_value(self):
        combo = self.adapt(self.card())
        self.window.show()
        self.app.processEvents()
        interface = QtGui.QAccessible.queryAccessibleInterface(combo)
        self.assertEqual(interface.role(), QtGui.QAccessible.Role.ComboBox)
        with patch.object(qconfig, 'save', wraps=qconfig.save) as save:
            interface.actionInterface().doAction(QtGui.QAccessibleActionInterface.showMenuAction())
            self.app.processEvents()
            self.assertTrue(combo.view().isVisible())
            QtTest.QTest.keyClick(combo.view(), QtCore.Qt.Key_Escape)
            self.app.processEvents()
            self.assertFalse(combo.view().isVisible())
            self.assertEqual(combo.currentData(), 'auto')
            save.assert_not_called()

    def test_disabled_widget_retains_value_and_dark_fill(self):
        combo = self.adapt(self.card())
        combo.setEnabled(False)
        self.window.show()
        self.app.processEvents()
        self.assertEqual(self.fill(combo), '#323232')
        self.assertEqual(combo.palette().color(QtGui.QPalette.Disabled, QtGui.QPalette.Text).name(), '#999999')
        with patch.object(qconfig, 'save', wraps=qconfig.save) as save:
            QtTest.QTest.keyClick(self.window, QtCore.Qt.Key_End)
            self.assertEqual(combo.currentData(), 'auto')
            save.assert_not_called()

    def test_auto_signal_updates_paint_without_saving_resolved_preference(self):
        combo = self.adapt(self.card(qconfig.themeMode))
        listener = SystemThemeListener(self.window)
        self.listeners.append(listener)
        with patch.object(listener, '_readSystemTheme', return_value=Theme.LIGHT):
            listener.start()
            QtTest.QTest.keyClick(combo, QtCore.Qt.Key_End)  # Actual choice of Auto.
            self.app.processEvents()
            self.assertEqual(qconfig.themeMode.value, Theme.AUTO)
            saved = self.config.file.read_bytes()
            changed = QtTest.QSignalSpy(combo.currentIndexChanged)
            for scheme, fill in ((QtCore.Qt.ColorScheme.Dark, '#323232'),
                                 (QtCore.Qt.ColorScheme.Light, '#ffffff')):
                with self.subTest(scheme=scheme.name), patch.object(qconfig, 'save', wraps=qconfig.save) as save:
                    listener._onColorSchemeChanged(scheme)  # Controlled signal, not a real OS change.
                    self.app.processEvents()
                    self.assertEqual(self.fill(combo), fill)
                    self.assertEqual(qconfig.themeMode.value, Theme.AUTO)
                    self.assertEqual(self.config.file.read_bytes(), saved)
                    save.assert_not_called()
            self.assertEqual(changed.count(), 0)

    def test_fresh_process_reloads_saved_preference_into_real_adapted_card(self):
        combo = self.adapt(self.card())
        theme_combo = self.adapt(self.card(qconfig.themeMode))
        QtTest.QTest.keyClick(combo, QtCore.Qt.Key_End)
        QtTest.QTest.keyClick(theme_combo, QtCore.Qt.Key_End)
        self.app.processEvents()
        root = Path(__file__).resolve().parents[1]
        code = r'''
import json,sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,sys.argv[1])
from PySide6.QtWidgets import QApplication
from qfluentwidgets import QConfig,OptionsConfigItem,OptionsValidator,ComboBoxSettingCard,FluentIcon,qconfig
from ui import macos_setting_controls as adapter
class PrivateConfig(QConfig):
    mode=OptionsConfigItem('Main','Mode','auto',OptionsValidator(['auto','fast','accurate']))
app=QApplication([])
config=PrivateConfig()
qconfig.load(file=Path(sys.argv[2]),config=config)
card=ComboBoxSettingCard(config.mode,FluentIcon.SETTING,'Mode',texts=['Auto','Fast','Accurate'])
theme_card=ComboBoxSettingCard(config.themeMode,FluentIcon.SETTING,'Theme',texts=['Light','Dark','Auto'])
with patch.object(adapter,'sys',SimpleNamespace(platform='darwin')):
    combo=adapter.adapt_macos_setting_combo(card)
    theme_combo=adapter.adapt_macos_setting_combo(theme_card)
print('RELOADED='+json.dumps({'saved':config.mode.value,'selected':combo.currentData(),'styled':bool(combo.styleSheet()),'saved_theme':config.themeMode.value.value,'selected_theme':theme_combo.currentData().value}),flush=True)
card.close()
theme_card.close()
'''
        before = self.config.file.read_bytes()
        result = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', '-c', code, str(root), str(self.config.file)],
                                env=dict(os.environ, QT_QPA_PLATFORM='offscreen'), capture_output=True,
                                text=True, encoding='utf-8', timeout=30, check=True)
        actual = json.loads(next(x.removeprefix('RELOADED=') for x in result.stdout.splitlines() if x.startswith('RELOADED=')))
        self.assertEqual(actual, {'saved': 'accurate', 'selected': 'accurate', 'styled': True,
                                  'saved_theme': 'Auto', 'selected_theme': 'Auto'})
        self.assertEqual(self.config.file.read_bytes(), before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
