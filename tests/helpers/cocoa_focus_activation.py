"""Test-only, conditional own-process Cocoa activation around the original wait."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import threading
import time


@contextmanager
def inactive_cocoa_activation(app, window, qtcore, output):
    """Request activation once only when inactive; the caller still must wait."""
    if sys.platform != "darwin" or app.platformName() != "cocoa":
        yield
        return

    started = time.monotonic()
    report = {"scope": "test fixture only; not external AX or native inactive-path proof",
              "activation_requests": 0, "events": [], "evidence_errors": []}
    native = None
    on_gui_thread = False
    primary_error = None

    def describe(obj):
        return None if obj is None else {"class": obj.metaObject().className(),
                                        "name": obj.objectName()}

    def record(stage):
        state = {"stage": stage, "at_utc": datetime.now(timezone.utc).isoformat(),
                 "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                 "activation_requests": report["activation_requests"],
                 "native_is_active": bool(native.isActive()) if native is not None else None,
                 "qt": {"qpa": app.platformName(),
                        "application_state": app.applicationState().name,
                        "active_window": describe(app.activeWindow()),
                        "focus_window": describe(app.focusWindow()),
                        "focus_widget": describe(app.focusWidget()),
                        "window_active": window.isActiveWindow(),
                        "window_visible": window.isVisible(),
                        "window_exposed": bool(window.windowHandle() and window.windowHandle().isExposed())}}
        report["events"].append(state)
        return state

    try:
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("Cocoa test activation requires the Python main thread")
        if qtcore.QThread.currentThread() != app.thread():
            raise RuntimeError("Cocoa test activation requires the QApplication thread")
        on_gui_thread = True
        import AppKit  # Existing dependency; never imported on other platforms/QPA.
        native = AppKit.NSApplication.sharedApplication()
        before = record("before-conditional-native-request")
        if not before["native_is_active"]:
            report["activation_requests"] += 1  # Count even a bridge exception; never retry.
            native.activate()
        record("after-conditional-native-request")
        yield  # Immediate return from activate is not window activation success.
    except BaseException as error:
        primary_error = error
        report["exception"] = repr(error)
        raise
    finally:
        evidence_error = None
        try:
            if on_gui_thread:
                record("after-original-window-wait")
        except Exception as error:
            evidence_error = error
            report["evidence_errors"].append(repr(error))
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        try:
            path = Path(output)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        except Exception:
            if primary_error is None:
                raise
        if primary_error is None and evidence_error is not None:
            raise evidence_error
