"""Fresh GUI-process theme probe; optional detector input is simulated, not an OS toggle."""
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('QT_QPA_PLATFORM', 'cocoa' if sys.platform == 'darwin' else 'offscreen')

from PySide6 import QtCore, QtTest, QtWidgets
from qfluentwidgets import Theme, qconfig
from backend.config import config
from backend.tools.theme_listener import SystemThemeListener
from gui import SubtitleExtractorGUI


def main():
    # A deterministic detector isolates application restart semantics from the
    # runner's appearance. Separate tests retain the real Qt signal connection.
    simulated = Theme(sys.argv[1])
    SystemThemeListener._readSystemTheme = lambda self: simulated
    app = QtWidgets.QApplication([])
    config.set(config.checkUpdateOnStartup, False, save=False)
    window = SubtitleExtractorGUI()
    window.show()
    QtTest.QTest.qWait(200)
    expected = simulated if config.themeMode.value == Theme.AUTO else config.themeMode.value
    assert qconfig.theme == expected
    combo = window.advancedSettingInterface.theme_mode.comboBox
    assert combo.currentData() == config.themeMode.value
    result = {'preference': config.themeMode.value.value,
              'effective': qconfig.theme.value, 'detector': simulated.value,
              'input_kind': 'controlled detector on real Qt GUI'}
    window.close()
    assert not window.themeListener._timer.isActive()
    window.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    print('THEME_PROBE=' + json.dumps(result))


if __name__ == '__main__':
    main()
