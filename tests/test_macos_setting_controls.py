"""Real Qt input tests. Offscreen results are not external macOS AX acceptance."""

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6 import QtCore, QtGui, QtTest, QtWidgets
from qfluentwidgets import ComboBox, ComboBoxSettingCard, FluentIcon, Theme, qconfig, themeColor

from backend.config import config
from test_theme import ThemeTestCase
from ui.macos_setting_controls import MacSettingComboBox, adapt_macos_setting_combo

ROOT = Path(__file__).resolve().parents[1]

COLOR_GROUPS = (QtGui.QPalette.ColorGroup.Active, QtGui.QPalette.ColorGroup.Inactive,
                QtGui.QPalette.ColorGroup.Disabled)


def palette_snapshot(palette):
    """Include inherited roles too; Qt's repr prints only explicit resolve bits."""
    return {
        "current_group": palette.currentColorGroup().name,
        "resolve_mask": palette.resolveMask(),
        "groups": {group.name: {
            role.name: {"rgba": palette.color(group, role).name(QtGui.QColor.NameFormat.HexArgb),
                        "color_spec": palette.color(group, role).spec().name,
                        "rgba_f": palette.color(group, role).getRgbF(),
                        "brush_style": palette.brush(group, role).style().name,
                        "explicit": palette.isBrushSet(group, role)}
            for role in QtGui.QPalette.ColorRole
            if role not in (QtGui.QPalette.ColorRole.NColorRoles, QtGui.QPalette.ColorRole.NoRole)
        } for group in COLOR_GROUPS},
    }


class MacSettingControlsTests(ThemeTestCase):
    def assert_theme_palette(self, palette, theme, label):
        """Check every group of every role promised by the adapter, exactly.

        QWidget resolves *other* roles from its class/ancestor platform palette.
        A combo and its item view need not inherit identical Link/Shadow/etc.
        """
        dark = theme == Theme.DARK
        for group in COLOR_GROUPS:
            foreground = ("#999999" if dark else "#777777") if group == QtGui.QPalette.ColorGroup.Disabled \
                else ("#ffffff" if dark else "#1b1b1b")
            expected = {"Text": foreground, "ButtonText": foreground, "WindowText": foreground,
                        "Base": "#323232" if dark else "#ffffff",
                        "Button": "#323232" if dark else "#ffffff",
                        "Window": "#323232" if dark else "#ffffff",
                        "Highlight": themeColor().name(), "HighlightedText": "#ffffff"}
            for role_name, color in expected.items():
                role = getattr(QtGui.QPalette.ColorRole, role_name)
                with self.subTest(widget=label, theme=theme.value, group=group.name, role=role_name):
                    brush = palette.brush(group, role)
                    details = json.dumps(palette_snapshot(palette), sort_keys=True)
                    # QColor equality also compares RGB vs HSV representation;
                    # exact display RGBA plus solid brush semantics is intended.
                    self.assertEqual(brush.color().rgba(), QtGui.QColor(color).rgba(), details)
                    self.assertEqual(brush.style(), QtCore.Qt.BrushStyle.SolidPattern, details)
                    self.assertTrue(brush.transform().isIdentity(), details)

    def card(self, item=None, adapt=True):
        item = config.mode if item is None else item
        card = ComboBoxSettingCard(item, FluentIcon.SETTING, "OCR mode / 识别模式",
                                   "Accessible setting", texts=[str(x) for x in item.options])
        self.widgets.append(card)
        if adapt:
            self.adapt(card)
        return card

    def adapt(self, card, platform="darwin"):
        # Patch only the adapter's platform view, never Qt/Fluent's platform.
        with patch("ui.macos_setting_controls.sys", SimpleNamespace(platform=platform)):
            return adapt_macos_setting_combo(card)

    def show(self, widget):
        widget.resize(500, 100)
        widget.show()
        widget.activateWindow()
        self.app.processEvents()

    def test_initialization_preserves_items_current_value_binding_and_layout(self):
        config.mode.value = "accurate"
        card = self.card(adapt=False)
        old = card.comboBox
        expected = [(old.itemText(i), old.itemData(i)) for i in range(old.count())]
        index = card.hBoxLayout.indexOf(old)
        count = card.hBoxLayout.count()
        with patch.object(qconfig, "set", wraps=qconfig.set) as setter, \
                patch.object(qconfig, "save", wraps=qconfig.save) as save:
            combo = self.adapt(card)
            self.assertIs(self.adapt(card), combo)
            setter.assert_not_called()
            save.assert_not_called()
        self.assertFalse(config.file.exists())
        self.assertEqual([(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())], expected)
        self.assertEqual(combo.currentData(), "accurate")
        self.assertIs(combo.parent(), card)
        self.assertEqual(card.hBoxLayout.indexOf(combo), index)
        self.assertEqual(card.hBoxLayout.count(), count)
        self.assertEqual(card.hBoxLayout.itemAt(index).alignment(), QtCore.Qt.AlignmentFlag.AlignRight)
        self.assertTrue(old.signalsBlocked())
        self.assertTrue(old.isHidden())
        self.assertEqual(combo.accessibleName(), card.titleLabel.text())
        self.assertIn(card.contentLabel.text(), combo.accessibleDescription())
        self.assertIn(card.titleLabel.text(), combo.accessibleDescription())
        self.assertIs(card.titleLabel.buddy(), combo)

    def test_other_platforms_keep_the_original_fluent_control(self):
        for platform in ("win32", "linux"):
            card = self.card(adapt=False)
            original = card.comboBox
            self.assertIs(self.adapt(card, platform), original)
            self.assertIsInstance(original, ComboBox)

    def test_disabled_items_and_disabled_widget_remain_disabled(self):
        card = self.card(adapt=False)
        card.comboBox.setItemEnabled(2, False)
        card.comboBox.setEnabled(False)
        combo = self.adapt(card)
        self.assertFalse(combo.isEnabled())
        self.assertFalse(combo.model().item(2).isEnabled())

    def test_standard_accessibility_role_and_menu_action_are_available(self):
        combo = self.card().comboBox
        accessible = QtGui.QAccessible.queryAccessibleInterface(combo)
        self.assertEqual(accessible.role(), QtGui.QAccessible.Role.ComboBox)
        self.assertEqual(accessible.text(QtGui.QAccessible.Text.Value), combo.currentText())
        self.assertEqual(accessible.text(QtGui.QAccessible.Text.Description), combo.accessibleDescription())
        self.assertIn(QtGui.QAccessibleActionInterface.showMenuAction(),
                      accessible.actionInterface().actionNames())

    def test_real_arrow_key_emits_one_activation_and_saves_once(self):
        config.mode.value = "auto"
        card = self.card()
        combo = card.comboBox
        self.show(card)
        combo.setFocus()
        self.app.processEvents()
        activated = QtTest.QSignalSpy(combo.activated)
        changed = QtTest.QSignalSpy(combo.currentIndexChanged)
        with patch.object(qconfig, "save", wraps=qconfig.save) as save:
            QtTest.QTest.keyClick(combo, QtCore.Qt.Key.Key_Down)
            self.app.processEvents()
            self.assertEqual(combo.currentData(), "fast")
            self.assertEqual(config.mode.value, "fast")
            self.assertEqual(activated.count(), 1)
            self.assertEqual(changed.count(), 1)
            self.assertEqual(save.call_count, 1)
        self.assertEqual(json.loads(config.file.read_text())["Main"]["Mode"], "fast")

    def test_same_value_and_programmatic_update_do_not_duplicate_changes(self):
        card = self.card()
        combo = card.comboBox
        changed = QtTest.QSignalSpy(combo.currentIndexChanged)
        activated = QtTest.QSignalSpy(combo.activated)
        with patch.object(qconfig, "save", wraps=qconfig.save) as save:
            combo.setCurrentIndex(combo.currentIndex())
            self.assertEqual(changed.count(), 0)
            self.assertEqual(save.call_count, 0)
            target = "accurate" if combo.currentData() != "accurate" else "auto"
            qconfig.set(config.mode, target)
            self.assertEqual(combo.currentData(), target)
            self.assertEqual(changed.count(), 1)
            self.assertEqual(activated.count(), 0)
            self.assertEqual(save.call_count, 1)

    def test_all_modes_are_keyboard_selectable_and_reload_from_saved_file(self):
        config.mode.value = "auto"
        card = self.card()
        combo = card.comboBox
        self.show(card)
        combo.setFocus()
        for key, expected in ((QtCore.Qt.Key.Key_Down, "fast"),
                              (QtCore.Qt.Key.Key_Down, "accurate"),
                              (QtCore.Qt.Key.Key_Home, "auto")):
            QtTest.QTest.keyClick(combo, key)
            self.app.processEvents()
            self.assertEqual(combo.currentData(), expected)
            self.assertEqual(config.mode.value, expected)
            result = subprocess.run([sys.executable, "-c",
                "from backend.config import config; print('SAVED=' + config.mode.value)"],
                cwd=ROOT, env=dict(os.environ, VSE_CONFIG_FILE=str(config.file)),
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("SAVED=" + expected, result.stdout)

    @unittest.skipIf(sys.platform == "darwin", "External Cocoa popup input is checked on the native Mac")
    def test_real_popup_keys_cancel_and_select_without_double_activation(self):
        config.mode.value = "auto"
        card = self.card()
        combo = card.comboBox
        self.show(card)
        combo.setFocus()
        activated = QtTest.QSignalSpy(combo.activated)
        # Actual standard Qt accessible ShowMenu action, not a callback invocation.
        action = QtGui.QAccessible.queryAccessibleInterface(combo).actionInterface()
        action.doAction(QtGui.QAccessibleActionInterface.showMenuAction())
        self.app.processEvents()
        self.assertTrue(combo.view().isVisible())
        self.assertIs(combo.view().window().parent(), combo)
        QtTest.QTest.keyClick(combo.view(), QtCore.Qt.Key.Key_Escape)
        self.app.processEvents()
        self.assertFalse(combo.view().isVisible())
        self.assertEqual(combo.currentData(), "auto")
        self.assertEqual(activated.count(), 0)
        self.assertFalse(config.file.exists())
        # A single mouse release opens one popup (no clicked + release double toggle).
        QtTest.QTest.mouseClick(combo, QtCore.Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertTrue(combo.view().isVisible())
        QtTest.QTest.keyClick(combo.view(), QtCore.Qt.Key.Key_Down)
        QtTest.QTest.keyClick(combo.view(), QtCore.Qt.Key.Key_Return)
        self.app.processEvents()
        self.assertFalse(combo.view().isVisible())
        self.assertEqual(combo.currentData(), "fast")
        self.assertEqual(activated.count(), 1)
        self.assertEqual(json.loads(config.file.read_text())["Main"]["Mode"], "fast")

    def test_tab_focus_includes_the_standard_combo(self):
        window = QtWidgets.QWidget()
        self.widgets.append(window)
        layout = QtWidgets.QVBoxLayout(window)
        before = QtWidgets.QLineEdit(window)
        after = QtWidgets.QLineEdit(window)
        card = self.card()
        layout.addWidget(before)
        layout.addWidget(card)
        layout.addWidget(after)
        window.setTabOrder(before, card.comboBox)
        window.setTabOrder(card.comboBox, after)
        self.show(window)
        before.setFocus()
        QtTest.QTest.keyClick(before, QtCore.Qt.Key.Key_Tab)
        self.assertTrue(card.comboBox.hasFocus())

    def test_scrolling_unfocused_combo_does_not_change_configuration(self):
        config.mode.value = "auto"
        card = self.card()
        self.show(card)
        combo = card.comboBox
        combo.clearFocus()
        self.assertFalse(combo.hasFocus())
        event = QtGui.QWheelEvent(QtCore.QPointF(5, 5), QtCore.QPointF(5, 5),
            QtCore.QPoint(), QtCore.QPoint(0, -120), QtCore.Qt.MouseButton.NoButton,
            QtCore.Qt.KeyboardModifier.NoModifier, QtCore.Qt.ScrollPhase.NoScrollPhase, False)
        self.app.sendEvent(combo, event)
        self.assertEqual(config.mode.value, "auto")
        self.assertFalse(config.file.exists())
        self.assertFalse(event.isAccepted())

    def test_theme_keys_palette_auto_and_persistence_keep_existing_semantics(self):
        listener = self.listener(Theme.LIGHT)
        card = self.card(config.themeMode)
        combo = card.comboBox
        self.show(card)
        combo.setFocus()
        for expected in (Theme.DARK, Theme.AUTO):
            QtTest.QTest.keyClick(combo, QtCore.Qt.Key.Key_Down)
            self.app.processEvents()
            self.assertEqual(config.themeMode.value, expected)
            self.assertEqual(json.loads(config.file.read_text())["QFluentWidgets"]["ThemeMode"], expected.value)
        before = config.file.read_bytes()
        records = []
        evidence = ROOT / "test-results/macos-setting-controls"
        evidence.mkdir(parents=True, exist_ok=True)
        for theme in (Theme.DARK, Theme.LIGHT, Theme.DARK):
            listener._onThemeChanged(theme)
            self.app.processEvents()
            self.assertEqual(combo.currentData(), Theme.AUTO)
            self.assertEqual(config.file.read_bytes(), before)
            palette = combo.palette()
            view_palette = combo.view().palette()
            records.append({"platform": sys.platform, "qt_version": QtCore.qVersion(),
                            "qpa": self.app.platformName(), "theme": theme.value,
                            "scope": "Qt palette API state only; not native popup rendering or external AX acceptance",
                            "combo": palette_snapshot(palette),
                            "popup_view": palette_snapshot(view_palette)})
            # Save before assertions so a native failure retains all raw colors.
            (evidence / "palette-states.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
            dark = palette.color(QtGui.QPalette.ColorRole.Base).lightness() < 128
            self.assertEqual(dark, theme == Theme.DARK)
            self.assert_theme_palette(palette, theme, "combo")
            self.assert_theme_palette(view_palette, theme, "popup_view")
        # A fresh process reads the file written by real key input, not test setup.
        result = subprocess.run([sys.executable, "-c",
            "from backend.config import config; print('SAVED=' + config.themeMode.value.value)"],
            cwd=ROOT, env=dict(os.environ, VSE_CONFIG_FILE=str(config.file)),
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SAVED=Auto", result.stdout)

    def test_unset_platform_palette_roles_can_differ_without_breaking_theme_contract(self):
        listener = self.listener(Theme.LIGHT)
        card = self.card(config.themeMode)
        for theme in (Theme.DARK, Theme.LIGHT):
            listener._applyTheme(theme, force=True)
            requested = QtGui.QPalette(card.comboBox.palette())
            # Real Qt resolution, modelling distinct native class defaults.
            combo_native, view_native = QtGui.QPalette(), QtGui.QPalette()
            combo_native.setColor(QtGui.QPalette.ColorRole.Link, QtGui.QColor("#ff0000"))
            view_native.setColor(QtGui.QPalette.ColorRole.Link, QtGui.QColor("#00ff00"))
            combo_palette = requested.resolve(combo_native)
            view_palette = requested.resolve(view_native)
            self.assertNotEqual(combo_palette, view_palette)
            self.assert_theme_palette(combo_palette, theme, "combo-with-native-defaults")
            self.assert_theme_palette(view_palette, theme, "view-with-other-native-defaults")

    def test_equivalent_rgb_and_hsv_brush_representations_keep_exact_display_colors(self):
        # Isolate the equality example from native widget palette normalization.
        original = QtGui.QPalette()
        accent = themeColor().toHsv()
        role = QtGui.QPalette.ColorRole.Highlight
        for group in COLOR_GROUPS:
            original.setColor(group, role, accent)
        converted = QtGui.QPalette(original)
        for group in COLOR_GROUPS:
            self.assertEqual(original.color(group, role).spec(), QtGui.QColor.Spec.Hsv)
            converted.setColor(group, role, original.color(group, role).toRgb())
            self.assertEqual(converted.color(group, role).spec(), QtGui.QColor.Spec.Rgb)
            self.assertEqual(converted.color(group, role).rgba(), original.color(group, role).rgba())
            self.assertEqual(converted.brush(group, role).style(), original.brush(group, role).style())
            self.assertEqual(converted.brush(group, role).transform(), original.brush(group, role).transform())
        # This illustrates the equality boundary, not proof of the native cause.
        self.assertNotEqual(original, converted)

    def test_only_mode_and_theme_call_sites_are_adapted(self):
        from ui.setting_interface import SettingInterface
        from ui.advanced_setting_interface import AdvancedSettingInterface
        parent = QtWidgets.QWidget()
        self.widgets.append(parent)
        with patch("ui.macos_setting_controls.sys", SimpleNamespace(platform="darwin")):
            settings = SettingInterface(parent)
            parent.setLayout(settings)
            advanced = AdvancedSettingInterface()
        self.widgets.append(advanced)
        self.assertIsInstance(settings.mode_combo.comboBox, MacSettingComboBox)
        self.assertIsInstance(advanced.theme_mode.comboBox, MacSettingComboBox)
        self.assertIsInstance(settings.interface_combo.comboBox, ComboBox)
        self.assertIsInstance(settings.language_combo.comboBox, ComboBox)
        self.assertIsInstance(advanced.subtitle_area.comboBox, ComboBox)

    def test_full_window_theme_keys_repaint_adapted_controls(self):
        from gui import SubtitleExtractorGUI
        from helpers.theme_wait import wait_for_widget_animations
        with patch("ui.macos_setting_controls.sys", SimpleNamespace(platform="darwin")):
            window = SubtitleExtractorGUI()
        self.widgets.append(window)
        window.resize(1280, 900)
        window.show()
        page = window.advancedSettingInterface
        done = QtTest.QSignalSpy(window.stackedWidget.view.aniFinished)
        window.switchTo(page)
        self.assertTrue(done.count() or done.wait(2000))
        combo = page.theme_mode.comboBox
        self.assertIsInstance(combo, MacSettingComboBox)
        combo.setFocus()
        evidence = ROOT / "test-results/macos-setting-controls"
        evidence.mkdir(parents=True, exist_ok=True)
        for key, mode in ((QtCore.Qt.Key.Key_Down, Theme.DARK),
                          (QtCore.Qt.Key.Key_Up, Theme.LIGHT)):
            QtTest.QTest.keyClick(combo, key)
            wait_for_widget_animations([window])
            self.assertEqual(config.themeMode.value, mode)
            self.assertEqual(qconfig.theme, mode)
            self.assertEqual(combo.currentData(), mode)
            self.assertEqual(combo.palette().color(QtGui.QPalette.ColorRole.Base).lightness() < 128,
                             mode == Theme.DARK)
            self.assertTrue(window.grab().save(str(evidence / (mode.value.lower() + ".png"))))
