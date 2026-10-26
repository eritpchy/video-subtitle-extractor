#!/usr/bin/env python3
"""Isolated Qt table-accessibility regression probe, not application startup code.

Run each scenario in a fresh subprocess: an affected Cocoa plugin may SIGSEGV.
``--mode qt`` checks Qt's public accessibility API only and never certifies Cocoa.
``--mode cocoa`` additionally queries this process's QMacAccessibilityElement
directly through PyObjC. It does not inspect other apps, request Accessibility
permission, disable accessibility, or replace any Qt runtime/library.

The private Objective-C entry point is deliberately confined to this diagnostic.
External AX hierarchy reads and a real VSE GUI acceptance run remain separate.
"""

from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
import platform
import sys
import traceback


PREFIX = "VSE_MACOS_AX_PROBE "


class AccessibilityInvariantError(RuntimeError):
    """A read lost a live table, selected cells, or their accessible content."""


class ProbeEnvironmentError(RuntimeError):
    """The required runtime or diagnostic entry point is not available."""


def emit(event: str, **fields) -> None:
    print(PREFIX + json.dumps({"event": event, **fields}, sort_keys=True), flush=True)


def check_selected_titles(actual: list[str], expected: list[str], *, layer: str) -> None:
    """Do not accept an empty AX selection as a successful crash workaround."""
    if len(actual) != len(expected) or sorted(actual) != sorted(expected):
        raise AccessibilityInvariantError(
            f"{layer} selected cells differ: expected={expected!r}, actual={actual!r}"
        )


def run_probe(args: argparse.Namespace) -> dict:
    if args.mode == "cocoa" and sys.platform != "darwin":
        raise ProbeEnvironmentError("Cocoa mode requires macOS; Qt-only is not native AX proof")

    from PySide6 import QtCore, QtGui, QtTest, QtWidgets
    import PySide6
    import shiboken6

    actual_qt = QtCore.qVersion()
    if args.expect_qt and actual_qt != args.expect_qt:
        raise ProbeEnvironmentError(f"Expected Qt {args.expect_qt}, loaded {actual_qt}")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    backend = app.platformName()
    if args.mode == "cocoa" and backend != "cocoa":
        raise ProbeEnvironmentError(f"Cocoa mode requires native cocoa plugin, loaded {backend}")

    cocoa_class = None
    autorelease_pool = nullcontext
    if args.mode == "cocoa":
        # AppKit registers the NSAccessibility method metadata for PyObjC.
        import AppKit  # noqa: F401
        import objc

        try:
            cocoa_class = objc.lookUpClass("QMacAccessibilityElement")
        except objc.nosuchclass_error as exc:
            raise ProbeEnvironmentError("Loaded Qt has no QMacAccessibilityElement class") from exc
        if not hasattr(cocoa_class, "elementWithId_"):
            raise ProbeEnvironmentError("Qt's elementWithId: diagnostic entry point is unavailable")
        autorelease_pool = objc.autorelease_pool

    emit("runtime", mode=args.mode, scenario=args.scenario, qt=actual_qt,
         pyside=PySide6.__version__, shiboken=shiboken6.__version__,
         backend=backend, os=platform.platform(),
         native_cocoa=args.mode == "cocoa", external_ax_client=False)

    # Required for model-change accessibility events. Never turn this off to
    # evade the affected path. This only affects the disposable probe process.
    QtGui.QAccessible.setActive(True)
    accessibility_active = QtGui.QAccessible.isActive()
    emit("accessibility_activation", active=accessibility_active, mode=args.mode)
    if not accessibility_active and (args.mode == "cocoa" or args.scenario == "model-churn"):
        raise ProbeEnvironmentError(
            "Accessibility bridge did not activate; model-change/native coverage is unavailable"
        )
    table = QtWidgets.QTableWidget(3, 3)
    table.setWindowTitle("VSE disposable accessibility regression probe")
    table.setAccessibleName("VSE accessibility probe task table")
    table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
    table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setHorizontalHeaderLabels(["Name", "Progress", "Status"])

    def populate(prefix: str, rows: int) -> None:
        table.setRowCount(rows)
        for row in range(rows):
            for column in range(3):
                table.setItem(row, column, QtWidgets.QTableWidgetItem(f"{prefix}:{row}:{column}"))

    populate("initial", 3)
    table.show()
    app.processEvents()
    interface = QtGui.QAccessible.queryAccessibleInterface(table)
    if interface is None:
        raise ProbeEnvironmentError("Qt did not create a table accessibility interface")
    table_id = QtGui.QAccessible.uniqueId(interface)
    # Do not keep using this Python wrapper after native reads: the regression
    # deletes the C++ object. Resolve by ID and check it first on every read.
    del interface
    native_table = None
    if cocoa_class is not None:
        native_table = cocoa_class.elementWithId_(table_id)
        if native_table is None:
            raise ProbeEnvironmentError("Qt did not create its native table element")

    checks = 0

    def live_interface():
        current = QtGui.QAccessible.accessibleInterface(table_id)
        if current is None or not current.isValid():
            raise AccessibilityInvariantError("table-cache-loss: live table interface was deleted")
        if QtGui.QAccessible.uniqueId(current) != table_id:
            raise AccessibilityInvariantError("table-cache-loss: live table accessibility ID changed")
        return current

    def inspect(label: str, expected: list[str]) -> None:
        nonlocal checks
        emit("before_read", mode=args.mode, scenario=args.scenario, step=label,
             expected_selected_cells=len(expected), table_id=table_id)
        live_interface()
        if native_table is not None:
            # Do this BEFORE asking Qt for selectedItems(). It must encounter
            # native placeholder cells, not a pre-warmed/sanitized selection.
            with autorelease_pool():
                emit("before_native_selected_children", step=label, table_id=table_id)
                selected = native_table.accessibilitySelectedChildren()
                live_interface()
                children = list(selected or [])
                titles = [str(cell.accessibilityTitle() or "") for cell in children]
                roles = [str(cell.accessibilityRole() or "") for cell in children]
                check_selected_titles(titles, expected, layer="cocoa")
                if any(role != "AXCell" for role in roles):
                    raise AccessibilityInvariantError(f"Native selection omitted cell roles: {roles!r}")
                live_interface()
                del selected, children
        current = live_interface()
        selection = current.selectionInterface()
        if selection is None:
            raise AccessibilityInvariantError("Qt table lost its selection interface")
        titles = [cell.text(QtGui.QAccessible.Text.Name) for cell in selection.selectedItems()]
        check_selected_titles(titles, expected, layer="qt")
        if selection.selectedItemCount() != len(expected):
            raise AccessibilityInvariantError("Qt selectedItemCount disagrees with selectedItems")
        checks += 1
        emit("read_passed", step=label, selected_cells=len(expected), table_id=table_id,
             native_cocoa=native_table is not None)

    def select(row: int) -> list[str]:
        table.setCurrentCell(row, 0)
        table.selectRow(row)
        return [table.item(row, column).text() for column in range(3)]

    try:
        # This is the reduced VSE import route: a newly populated table with a
        # selected row, without any video decoder, OCR worker or file dialog.
        expected = select(0)
        inspect("first-selected-row", expected)
        inspect("repeated-selected-row", expected)
        table.clearSelection()
        inspect("cleared-selection", [])
        expected = select(0)
        QtTest.QTest.keyClick(table, QtCore.Qt.Key.Key_Down)
        if table.currentRow() != 1:
            raise AccessibilityInvariantError("Keyboard Down did not move the selected task row")
        expected = [table.item(1, column).text() for column in range(3)]
        inspect("keyboard-selected-row", expected)

        if args.scenario == "model-churn":
            for cycle in range(args.cycles):
                expected = select(0)
                inspect(f"cycle-{cycle}-before-remove", expected)
                # Drain native arrays released by the model-change path while
                # a valid selected first row survives, as in upstream 772484.
                with autorelease_pool():
                    table.setRowCount(2)
                    app.processEvents()
                inspect(f"cycle-{cycle}-after-remove", expected)
                with autorelease_pool():
                    table.setRowCount(0)
                    app.processEvents()
                inspect(f"cycle-{cycle}-empty-model", [])
                populate(f"reinsert-{cycle}", 3)
                expected = select(0)
                app.processEvents()
                inspect(f"cycle-{cycle}-reinsert-selected", expected)
        result = {"mode": args.mode, "scenario": args.scenario, "checks": checks,
                  "native_cocoa": args.mode == "cocoa", "external_ax_client": False,
                  "qt": actual_qt, "table_id": table_id,
                  "accessibility_active": accessibility_active}
        emit("passed", **result)
        return result
    finally:
        # Exercise normal disposal in the same subprocess. A crash here is not
        # successful acceptance even if the prior selection checks passed.
        emit("before_close", mode=args.mode, scenario=args.scenario)
        native_table = None
        table.close()
        table.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()
        emit("closed", mode=args.mode, scenario=args.scenario)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("qt", "cocoa"), required=True)
    parser.add_argument("--scenario", choices=("selected-row", "model-churn"), default="selected-row")
    parser.add_argument("--cycles", type=int, default=5)
    parser.add_argument("--expect-qt", help="Fail unless this exact Qt version is loaded")
    args = parser.parse_args(argv)
    if not 1 <= args.cycles <= 100:
        parser.error("--cycles must be between 1 and 100")
    emit("start", mode=args.mode, scenario=args.scenario)
    try:
        result = run_probe(args)
    except AccessibilityInvariantError as exc:
        emit("failed", mode=args.mode, scenario=args.scenario,
             failure_kind="accessibility_invariant", message=str(exc))
        traceback.print_exc()
        emit("result", status="failed", mode=args.mode, scenario=args.scenario,
             failure_kind="accessibility_invariant", message=str(exc))
        return 1
    except (ProbeEnvironmentError, ImportError) as exc:
        emit("failed", mode=args.mode, scenario=args.scenario,
             failure_kind="environment", message=str(exc))
        traceback.print_exc()
        emit("result", status="failed", mode=args.mode, scenario=args.scenario,
             failure_kind="environment", message=str(exc))
        return 2
    except Exception as exc:
        emit("failed", mode=args.mode, scenario=args.scenario,
             failure_kind="unexpected", message=str(exc))
        traceback.print_exc()
        emit("result", status="failed", mode=args.mode, scenario=args.scenario,
             failure_kind="unexpected", message=str(exc))
        return 3
    emit("result", status="passed", **result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
