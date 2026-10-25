"""Make the bundled CJK font available to Qt without replacing Latin UI fonts."""
from pathlib import Path

from PySide6.QtGui import QFontDatabase


def register_bundled_cjk_font(application):
    """Register once per QApplication, including minimal native Linux runners."""
    family = application.property('vseCjkFontFamily')
    if family:
        return family
    path = Path(__file__).resolve().parents[1] / 'backend/tools/NotoSansCJK-Bold.otf'
    identifier = QFontDatabase.addApplicationFont(str(path))
    families = QFontDatabase.applicationFontFamilies(identifier)
    if identifier < 0 or not families:
        raise RuntimeError(f'Cannot load the bundled CJK font: {path}')
    # Qt may select this registered family for missing CJK glyphs. Do not
    # change QApplication.font(), which would unnecessarily replace Latin UI.
    application.setProperty('vseCjkFontFamily', families[0])
    return families[0]
