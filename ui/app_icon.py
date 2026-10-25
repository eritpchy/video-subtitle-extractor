"""Keep Finder and the running Mac application's optical icon size consistent."""
from pathlib import Path
import sys


def load_app_icon(root: Path, target_platform: str | None = None):
    """Use the Mac-specific export, without changing Windows/Linux branding."""
    from PySide6.QtGui import QIcon, QImage, QPixmap

    root = Path(root)
    if (target_platform or sys.platform) != "darwin":
        return QIcon(str(root / "design/vse.ico"))
    packaged = root / "vse.icns"
    if packaged.is_file():
        icon = QIcon(str(packaged))
        if icon.isNull():
            raise RuntimeError("Packaged Mac app icon cannot be decoded")
        return icon

    # A source checkout has no generated .icns. Build identical representations
    # in memory, not in the checkout or the user's system icon/Dock caches.
    from scripts.build_macos_icon import render_macos_frames

    icon = QIcon()
    for size, frame in render_macos_frames(root / "design/icon_1024.png").items():
        data = frame.tobytes()
        image = QImage(data, size, size, size * 4, QImage.Format.Format_RGBA8888).copy()
        icon.addPixmap(QPixmap.fromImage(image))
    return icon
