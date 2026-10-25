"""Apply the saved appearance and follow OS changes without changing the preference."""

import darkdetect
from PySide6 import QtCore, QtGui
from qfluentwidgets import Theme, qconfig
from qfluentwidgets.common.style_sheet import updateStyleSheet


class SystemThemeListener(QtCore.QObject):
    """GUI-thread theme controller with a stoppable fallback timer.

    Qt reports live appearance changes on supported desktops. The timer also
    checks darkdetect when the platform plugin cannot report a color scheme.
    Neither path writes a resolved Light/Dark value over the saved Auto choice.
    """

    systemThemeChanged = QtCore.Signal()

    def __init__(self, parent=None, poll_interval=2000):
        super().__init__(parent)
        self._running = False
        self._style_hints = None
        self._system_theme = None
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(poll_interval)
        self._timer.setTimerType(QtCore.Qt.TimerType.CoarseTimer)
        self._timer.timeout.connect(self.refresh)

    def start(self):
        """Apply the persisted preference before showing the window."""
        if self._running:
            return
        self._running = True
        app = QtGui.QGuiApplication.instance()
        self._style_hints = app.styleHints() if app else None
        if self._style_hints is not None and hasattr(self._style_hints, "colorSchemeChanged"):
            self._style_hints.colorSchemeChanged.connect(self._onColorSchemeChanged)
        # QConfig assigns its effective theme *after* valueChanged is emitted.
        # Queue our update so that its assignment cannot undo our OS detection.
        qconfig.themeMode.valueChanged.connect(
            self._onThemeModeChanged, QtCore.Qt.ConnectionType.QueuedConnection
        )
        self._onThemeModeChanged()

    def stop(self):
        """Disconnect cleanly; no blocking listener thread needs terminating."""
        if not self._running:
            return
        self._running = False
        self._timer.stop()
        qconfig.themeMode.valueChanged.disconnect(self._onThemeModeChanged)
        if self._style_hints is not None and hasattr(self._style_hints, "colorSchemeChanged"):
            self._style_hints.colorSchemeChanged.disconnect(self._onColorSchemeChanged)

    @staticmethod
    def _qtTheme(scheme):
        if scheme == QtCore.Qt.ColorScheme.Dark:
            return Theme.DARK
        if scheme == QtCore.Qt.ColorScheme.Light:
            return Theme.LIGHT
        return None

    def _readSystemTheme(self):
        if self._style_hints is not None and hasattr(self._style_hints, "colorScheme"):
            theme = self._qtTheme(self._style_hints.colorScheme())
            if theme is not None:
                return theme
        try:
            theme = darkdetect.theme()
        except Exception:
            # Headless Linux and desktops without a settings service can fail
            # detection. Keep the last known appearance instead of flickering.
            return None
        return {"dark": Theme.DARK, "light": Theme.LIGHT}.get(str(theme).lower())

    @QtCore.Slot()
    def _onThemeModeChanged(self, *_):
        if not self._running:
            return
        mode = qconfig.themeMode.value
        if mode == Theme.AUTO:
            self._system_theme = self._readSystemTheme() or self._system_theme or Theme.LIGHT
            self._timer.start()
            self._applyTheme(self._system_theme, force=True)
        else:
            self._timer.stop()
            self._applyTheme(mode, force=True)

    @QtCore.Slot()
    def refresh(self):
        if self._running and qconfig.themeMode.value == Theme.AUTO:
            self._onThemeChanged(self._readSystemTheme())

    def _onColorSchemeChanged(self, scheme):
        theme = self._qtTheme(scheme)
        if theme is None:
            self.refresh()
        else:
            self._onThemeChanged(theme)

    def _onThemeChanged(self, theme):
        if not self._running:
            return
        if isinstance(theme, str):
            theme = {"dark": Theme.DARK, "light": Theme.LIGHT}.get(theme.lower())
        if theme not in (Theme.LIGHT, Theme.DARK):
            return
        self._system_theme = theme
        if qconfig.themeMode.value == Theme.AUTO:
            self._applyTheme(theme)

    def _applyTheme(self, theme, force=False):
        changed = qconfig.theme != theme
        if not changed and not force:
            return
        # Setting the effective theme does not alter or save themeMode.
        qconfig.theme = theme
        if changed:
            qconfig.themeChanged.emit(qconfig.themeMode.value)
        updateStyleSheet()
        qconfig.themeChangedFinished.emit()
        if changed:
            self.systemThemeChanged.emit()
