"""Opt-in hosted Server CI only: real HKCU appearance -> native Qt -> GUI.

Never imported by normal customer launch. This is not Windows Settings UI
automation or Windows client certification. See the official protocol sources:
https://learn.microsoft.com/en-us/windows/apps/develop/settings/settings-common
https://learn.microsoft.com/en-us/windows/win32/winmsg/wm-settingchange
https://github.com/qt/qtbase/blob/6.11/src/plugins/platforms/windows/qwindowstheme.cpp
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import ctypes
import json
import os
from pathlib import Path
import sys
import time

PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
VALUE_NAME = "AppsUseLightTheme"
SERVER_BUILDS = {"windows-2022": 20348, "windows-2025": 26100}


class UnsupportedAppearance(RuntimeError):
    """A measured prerequisite prevents testing; never counts as a pass."""


def require_hosted_server_ci(platform_name):
    """Fail closed before importing winreg or opening a registry key."""
    required = {"VSE_CI_WINDOWS_SYSTEM_THEME": "1", "CI": "true",
                "GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted",
                "RUNNER_OS": "Windows"}
    if (sys.platform != "win32" or platform_name != "windows"
            or "--smoke-test" not in sys.argv
            or any(os.environ.get(key) != value for key, value in required.items())):
        raise PermissionError("Real system-theme mutation requires opted-in GitHub-hosted Windows smoke CI")
    runner = os.environ.get("VSE_CI_WINDOWS_RUNNER")
    version = sys.getwindowsversion()
    if version.product_type != 3 or version.build != SERVER_BUILDS.get(runner):
        raise PermissionError(f"Only measured Server 2022/2025 CI is allowed: {runner}, {version}")
    return {"runner": runner, "build": version.build, "product_type": version.product_type,
            "commit": os.environ.get("GITHUB_SHA"), "run_id": os.environ.get("GITHUB_RUN_ID"),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT")}


def high_contrast_enabled():
    from ctypes import wintypes

    class HighContrast(ctypes.Structure):
        _fields_ = [("size", wintypes.UINT), ("flags", wintypes.DWORD),
                    ("scheme", wintypes.LPWSTR)]

    state = HighContrast()
    state.size = ctypes.sizeof(state)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT,
                                            wintypes.LPVOID, wintypes.UINT]
    user32.SystemParametersInfoW.restype = wintypes.BOOL
    if not user32.SystemParametersInfoW(0x0042, state.size, ctypes.byref(state), 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return bool(state.flags & 1)


def broadcast_appearance_change():
    """Native OS broadcast, not Qt signal emission or an app listener call."""
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    send = user32.SendMessageTimeoutW
    send.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                     wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
    send.restype = wintypes.LPARAM
    area = ctypes.create_unicode_buffer("ImmersiveColorSet")
    result = ctypes.c_size_t()
    ctypes.set_last_error(0)
    # HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG; 1 s per recipient.
    if not send(0xFFFF, 0x001A, 0, ctypes.addressof(area), 0x0002, 1000,
                ctypes.byref(result)):
        raise OSError(ctypes.get_last_error(), "WM_SETTINGCHANGE broadcast failed or timed out")


def read_value(registry, key):
    try:
        value, kind = registry.QueryValueEx(key, VALUE_NAME)
        return {"exists": True, "value": value, "type": kind}
    except FileNotFoundError:
        return {"exists": False}


@contextmanager
def restored_appearance(registry, report, notify=broadcast_appearance_change):
    """Change one HKCU value; restore its type/absence and any empty key we create."""
    with ExitStack() as handles:
        access = registry.KEY_QUERY_VALUE | registry.KEY_SET_VALUE
        parent, created_key = None, False
        try:
            key = registry.OpenKey(registry.HKEY_CURRENT_USER, PERSONALIZE, 0, access)
        except FileNotFoundError:
            # Missing Personalize means defaults, not proof of unsupported OS.
            # Require the parent so we cannot accidentally create an untracked
            # chain of ancestors. Only this single child key is ever created.
            try:
                parent = handles.enter_context(registry.OpenKey(
                    registry.HKEY_CURRENT_USER, PERSONALIZE.rsplit("\\", 1)[0], 0,
                    registry.KEY_CREATE_SUB_KEY | registry.KEY_ENUMERATE_SUB_KEYS))
            except FileNotFoundError as exc:
                raise RuntimeError("HKCU Themes parent is absent; refusing untracked ancestor creation") from exc
            key = registry.CreateKeyEx(parent, "Personalize", 0, access)
            created_key = True
        key = handles.enter_context(key)
        report["original_registry_key_exists"] = not created_key
        try:
            original = read_value(registry, key)
        except BaseException:
            # Snapshot failure must not leave an empty child key we just made.
            if created_key:
                subkeys, values, _ = registry.QueryInfoKey(key)
                if not subkeys and not values:
                    registry.DeleteKey(parent, "Personalize")
                    report["restoration"] = {"status": "restored", "created_key_cleanup": "removed-empty-key"}
                else:
                    report["restoration"] = {"status": "failed", "created_key_cleanup": "retained-nonempty"}
            raise
        report["original_registry"] = original
        report["restoration"] = {"status": "pending"}
        try:
            yield key
        finally:
            # Restore even after an assertion, a broadcast error, or interruption.
            try:
                if original["exists"]:
                    registry.SetValueEx(key, VALUE_NAME, 0, original["type"], original["value"])
                else:
                    try:
                        registry.DeleteValue(key, VALUE_NAME)
                    except FileNotFoundError:
                        pass
                restored = read_value(registry, key)
                report["restoration"] = {"status": "registry-restored", "registry": restored}
                if restored != original:
                    raise AssertionError("Original appearance registry value/type/absence was not restored")
                notify()
                if created_key:
                    subkeys, values, _ = registry.QueryInfoKey(key)
                    if subkeys or values:
                        report["restoration"]["created_key_cleanup"] = "retained-nonempty"
                        raise RuntimeError("New Personalize key contains other entries; retained without deleting them")
                    registry.DeleteKey(parent, "Personalize")
                    report["restoration"]["created_key_cleanup"] = "removed-empty-key"
                report["restoration"]["status"] = "restored"
            except BaseException as exc:
                report["restoration"]["status"] = "failed"
                report["restoration"]["error"] = repr(exc)
                raise


def _json_value(value):
    # Preserve uncommon original registry types in evidence as well as in memory.
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    raise TypeError(type(value).__name__)


def _write_report(output, report):
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, default=_json_value) + "\n", encoding="utf-8")


def run_windows_system_theme_smoke(app, window, settle, screenshot_dir):
    """A bounded light -> dark -> light round trip through the unmodified listener."""
    report = {"schema": 1, "status": "blocked", "transitions": [], "qt_signals": [],
              "scope": "hosted-server-HKCU-native-message-Qt-GUI; not Settings UI/client coverage",
              "registry_target": "HKCU\\" + PERSONALIZE + "\\" + VALUE_NAME,
              "restoration": {"status": "not-needed"}}
    destination = os.environ.get("VSE_WINDOWS_SYSTEM_THEME_REPORT")
    if not destination and not screenshot_dir:
        raise PermissionError("CI appearance probe requires an external report or screenshot destination")
    output = Path(destination) if destination else Path(screenshot_dir) / "system-theme.json"
    hints = None
    on_scheme = None
    try:
        report["identity"] = require_hosted_server_ci(app.platformName())
        report["status"] = "failed"
        import winreg
        from PySide6 import QtCore, QtWidgets
        from qfluentwidgets import Theme, qconfig
        from backend.config import config

        report["qt_version"] = QtCore.qVersion()
        report["high_contrast"] = high_contrast_enabled()
        if report["high_contrast"]:
            raise UnsupportedAppearance("High contrast is enabled; it was not changed to force dark/light support")
        hints = app.styleHints()
        on_scheme = lambda scheme: report["qt_signals"].append(scheme.name)
        hints.colorSchemeChanged.connect(on_scheme)
        combo = window.advancedSettingInterface.theme_mode.comboBox
        combo.setCurrentIndex(combo.findData(Theme.AUTO))
        settle()
        saved = config.file.read_bytes()
        if json.loads(saved)["QFluentWidgets"]["ThemeMode"] != Theme.AUTO.value:
            raise AssertionError("Auto preference was not persisted before OS mutation")
        original_scheme, original_effective = hints.colorScheme(), qconfig.theme
        report["original_appearance"] = {"qt_scheme": original_scheme.name,
                                         "effective": original_effective.value}
        pages = (
            ("home", window.homeInterface, window.homeInterface.output_text.viewport()),
            ("timeline", window.timelineSyncInterface,
             window.timelineSyncInterface.findChild(QtWidgets.QWidget, "timelineButtons")),
            ("settings", window.advancedSettingInterface, window.advancedSettingInterface.theme_mode),
        )
        with restored_appearance(winreg, report) as key:
            # Preserve the original value/type before the first mutation, even
            # if a native crash or CI force-kill prevents Python's finally.
            # A pending restoration is incomplete evidence, never a pass.
            _write_report(output, report)
            for index, theme in enumerate((Theme.LIGHT, Theme.DARK, Theme.LIGHT)):
                expected = QtCore.Qt.ColorScheme.Light if theme == Theme.LIGHT else QtCore.Qt.ColorScheme.Dark
                signal_start = len(report["qt_signals"])
                step = {"requested": theme.value, "status": "pending"}
                report["transitions"].append(step)
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_DWORD, int(theme == Theme.LIGHT))
                step["registry"] = read_value(winreg, key)
                if step["registry"] != {"exists": True, "type": winreg.REG_DWORD,
                                        "value": int(theme == Theme.LIGHT)}:
                    raise AssertionError(f"Appearance registry write did not take effect: {step}")
                broadcast_appearance_change()
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    settle()  # Pump genuine native messages, listener timer and repaint events.
                    if hints.colorScheme() == expected and qconfig.theme == theme:
                        break
                step.update(qt_scheme=hints.colorScheme().name, effective=qconfig.theme.value,
                            preference=config.themeMode.value.value,
                            qt_signals=report["qt_signals"][signal_start:])
                if hints.colorScheme() != expected or qconfig.theme != theme:
                    raise AssertionError(f"Native OS round trip did not converge within 8 s: {step}")
                if index and expected.name not in step["qt_signals"]:
                    raise AssertionError(f"No genuine Qt change signal observed: {step}")
                if config.themeMode.value != Theme.AUTO or config.file.read_bytes() != saved:
                    raise AssertionError("OS change overwrote the persisted Auto preference")
                step["page_lightness"] = {}
                for name, page, card in pages:
                    window.switchTo(page)
                    settle()
                    image = window.grab().toImage()
                    if image.isNull() or card is None:
                        raise AssertionError(f"Could not render system-theme page {name}")
                    point = card.mapTo(window, QtCore.QPoint(card.width() - 8, card.height() // 2))
                    point *= image.devicePixelRatio()
                    if not image.rect().contains(point):
                        raise AssertionError(f"Theme sample is outside {name} screenshot")
                    step["page_lightness"][name] = image.pixelColor(point).lightness()
                    if screenshot_dir and not image.save(str(Path(screenshot_dir) / f"system-{index}-{theme.value.lower()}-{name}.png")):
                        raise AssertionError(f"Could not save system-theme {name}")
                step["status"] = "passed"
            for name, _, _ in pages:
                light, dark, restored = [step["page_lightness"][name] for step in report["transitions"]]
                if light <= dark + 80 or abs(light - restored) > 3:
                    raise AssertionError(f"Actual {name} pixels did not round trip: {light}, {dark}, {restored}")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            settle()
            if hints.colorScheme() == original_scheme and qconfig.theme == original_effective:
                break
        report["restoration"].update(qt_scheme=hints.colorScheme().name,
                                     effective=qconfig.theme.value)
        if hints.colorScheme() != original_scheme or qconfig.theme != original_effective:
            raise AssertionError("Registry restored but native Qt/app appearance did not return to its original state")
        if config.themeMode.value != Theme.AUTO or config.file.read_bytes() != saved:
            raise AssertionError("Restoring the OS preference overwrote Auto")
        report["status"] = "passed"
    except UnsupportedAppearance as exc:
        report.update(status="unsupported", error=str(exc))
        raise
    except BaseException as exc:
        report["error"] = repr(exc)
        raise
    finally:
        if hints is not None and on_scheme is not None:
            hints.colorSchemeChanged.disconnect(on_scheme)
        _write_report(output, report)
        print(f"WINDOWS_SYSTEM_THEME_{report['status'].upper()}: {output}")
