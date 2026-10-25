#!/usr/bin/env python3
"""Inspect native Linux Qt loader dependencies without opening a display."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    if platform.system() != "Linux":
        raise RuntimeError("This loader diagnostic is Linux-only")
    from ui.qt_runtime import prepare_qt_runtime
    prepare_qt_runtime()
    from PySide6.QtCore import QLibraryInfo

    plugin_dir = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath))
    library_dir = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.LibrariesPath))
    print(json.dumps({"platform": platform.platform(), "python": sys.version,
                      "qt_plugins": str(plugin_dir), "qt_libraries": str(library_dir),
                      "environment": {name: os.environ.get(name) for name in (
                          "QT_QPA_PLATFORM", "QT_QPA_PLATFORM_PLUGIN_PATH", "QT_PLUGIN_PATH",
                          "QT_QPA_FONTDIR", "DISPLAY", "XDG_RUNTIME_DIR", "LD_LIBRARY_PATH")}}, indent=2), flush=True)
    missing = False
    targets = (plugin_dir / "platforms/libqxcb.so", library_dir / "libQt6XcbQpa.so.6",
               library_dir / "libQt6Gui.so.6", plugin_dir / "xcbglintegrations/libqxcb-glx-integration.so")
    for path in targets:
        print(f"\n=== ldd {path} ===", flush=True)
        if not path.is_file():
            raise FileNotFoundError(path)
        result = subprocess.run(["ldd", str(path)], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        print(result.stdout, end="", flush=True)
        missing = missing or result.returncode != 0 or "not found" in result.stdout
    if missing:
        raise RuntimeError("Qt has unresolved native dependencies; see ldd output above")
    # RTLD_NOW catches unresolved symbols as well as missing SONAMEs without
    # creating QApplication, an X11 socket, or any display connection.
    ctypes.CDLL(str(targets[0]), mode=os.RTLD_NOW | os.RTLD_LOCAL)
    print("QT_XCB_LOADER_OK: plugin resolves without a display; X11 rendering still requires its separate smoke test", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
