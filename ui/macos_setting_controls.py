"""Use Qt's accessible combo box for the two primary macOS preferences.

Fluent 1.11.3's non-editable combo is a QPushButton whose popup is opened only
by mouseReleaseEvent, not its accessible Press action or keyboard activation.
Keep the setting card and its configuration binding; use Qt's standard input,
popup ownership and accessibility implementation rather than patching Fluent
internals or installing a global accessibility factory.
"""

import sys

from PySide6 import QtCore, QtGui, QtWidgets
from qfluentwidgets import FluentIcon, Theme, isDarkTheme, qconfig, themeColor


class MacSettingComboBox(QtWidgets.QComboBox):
    """Standard Qt semantics, with the application's effective theme palette."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_MacShowFocusRect, True)
        self.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContents)
        self._sync_palette()
        # Includes Auto/system changes without overwriting the saved preference.
        qconfig.themeChangedFinished.connect(self._sync_palette)

    @QtCore.Slot()
    def _sync_palette(self):
        palette = QtGui.QPalette(self.palette())
        dark = isDarkTheme()
        foreground = QtGui.QColor("#ffffff" if dark else "#1b1b1b")
        background = QtGui.QColor("#323232" if dark else "#ffffff")
        for role in (QtGui.QPalette.ColorRole.Text, QtGui.QPalette.ColorRole.ButtonText,
                     QtGui.QPalette.ColorRole.WindowText):
            palette.setColor(role, foreground)
            palette.setColor(QtGui.QPalette.ColorGroup.Disabled, role,
                             QtGui.QColor("#999999" if dark else "#777777"))
        for role in (QtGui.QPalette.ColorRole.Base, QtGui.QPalette.ColorRole.Button,
                     QtGui.QPalette.ColorRole.Window):
            palette.setColor(role, background)
        palette.setColor(QtGui.QPalette.ColorRole.Highlight, themeColor())
        palette.setColor(QtGui.QPalette.ColorRole.HighlightedText, QtGui.QColor("#ffffff"))
        self.setPalette(palette)
        self.view().setPalette(palette)
        # The Cocoa native bezel need not honor QPalette.
        # An explicit non-native border makes QStyleSheetStyle paint the full
        # control. Keep QComboBox event/AX/model semantics and local scope.
        # Declare selection/disabled colors in QSS too: repolishing must not
        # restore stale selection colors or overwrite disabled view text.
        border = "#707070" if dark else "#888888"
        disabled = "#999999" if dark else "#777777"
        arrow = FluentIcon.ARROW_DOWN.path(Theme.DARK if dark else Theme.LIGHT)
        self.setStyleSheet(f"""
            QComboBox {{
                color: {foreground.name()}; background-color: {background.name()};
                selection-color: #ffffff; selection-background-color: {themeColor().name()};
                border: 1px solid {border}; border-radius: 4px;
                padding: 5px 28px 5px 10px;
            }}
            QComboBox:focus {{ border-color: {themeColor().name()}; }}
            QComboBox:disabled {{ color: {disabled}; }}
            QComboBox::drop-down {{
                subcontrol-origin: padding; subcontrol-position: top right;
                width: 24px; border: none;
            }}
            QComboBox::down-arrow {{ image: url({arrow}); width: 10px; height: 10px; }}
            QComboBox QAbstractItemView:disabled {{ color: {disabled}; }}
            QComboBox QAbstractItemView {{
                color: {foreground.name()}; background-color: {background.name()};
                selection-color: #ffffff; selection-background-color: {themeColor().name()};
                border: 1px solid {border};
            }}
        """)

    def wheelEvent(self, event):
        # Scrolling the settings page must not silently change a preference.
        if not self.hasFocus():
            event.ignore()
            return
        super().wheelEvent(event)


def adapt_macos_setting_combo(card):
    """Adapt an already initialized ComboBoxSettingCard, once, on macOS only.

    Call immediately after constructing the card, before adding external combo
    signal connections. Initialization never sets or saves configuration. The
    original card's option mapping, valueChanged -> setValue binding, and
    currentIndexChanged callback continue to own persistence.
    """
    if sys.platform != "darwin" or isinstance(card.comboBox, MacSettingComboBox):
        return card.comboBox

    old = card.comboBox
    combo = MacSettingComboBox(card)
    combo.setObjectName(old.objectName())
    combo.setFont(old.font())
    combo.setAccessibleName(card.titleLabel.text())
    # QAccessibleComboBox uses the current option as its Name/Value. Preserve
    # the field label through Qt's label relation and descriptive text instead
    # of overriding that standard accessibility implementation.
    combo.setAccessibleDescription(" — ".join(filter(None, (
        card.titleLabel.text(), card.contentLabel.text()))))
    card.titleLabel.setBuddy(combo)
    combo.setToolTip(old.toolTip())
    combo.setEnabled(old.isEnabled())
    combo.setMinimumWidth(max(old.minimumWidth(), old.sizeHint().width()))
    combo.setMinimumHeight(old.minimumHeight())
    for index in range(old.count()):
        combo.addItem(old.itemIcon(index), old.itemText(index), old.itemData(index))
        combo.model().item(index).setEnabled(old.items[index].isEnabled)
    combo.setCurrentIndex(old.currentIndex())

    # All setup above is intentionally before connecting the configuration slot.
    item = card.hBoxLayout.replaceWidget(old, combo)
    if item is None:
        combo.deleteLater()
        raise RuntimeError("Setting combo is not in its card layout")
    old.currentIndexChanged.disconnect(card._onCurrentIndexChanged)
    old.blockSignals(True)
    old.hide()
    card.comboBox = combo
    combo.currentIndexChanged.connect(card._onCurrentIndexChanged)
    old.deleteLater()
    return combo
