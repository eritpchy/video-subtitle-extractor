"""Run accessibility probes in children so native crashes cannot kill pytest."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.macos_accessibility_probe import AccessibilityInvariantError, check_selected_titles


ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "macos_accessibility_probe.py"
PREFIX = "VSE_MACOS_AX_PROBE "


def records(completed):
    return [json.loads(line.removeprefix(PREFIX)) for line in completed.stdout.splitlines()
            if line.startswith(PREFIX)]


def probe(mode, scenario="selected-row", *extra):
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "cocoa" if mode == "cocoa" else "offscreen"
    return subprocess.run(
        [sys.executable, str(PROBE), "--mode", mode, "--scenario", scenario, *extra],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
    )


@pytest.mark.parametrize("actual", [[], ["name"], ["name", "progress", "wrong"]])
def test_empty_or_incomplete_selection_is_not_a_success(actual):
    with pytest.raises(AccessibilityInvariantError, match="selected cells differ"):
        check_selected_titles(actual, ["name", "progress", "status"], layer="cocoa")


def test_selected_cell_order_is_not_assumed():
    check_selected_titles(["status", "name", "progress"],
                          ["name", "progress", "status"], layer="cocoa")


@pytest.mark.skipif(sys.platform == "darwin", reason="Non-macOS negative guard")
def test_cocoa_mode_rejects_non_macos_before_importing_qt():
    completed = probe("cocoa")
    assert completed.returncode == 2, completed.stdout + completed.stderr
    events = records(completed)
    assert events[-1]["failure_kind"] == "environment"
    assert not any(event["event"] == "passed" for event in events)


@pytest.mark.parametrize("scenario,checks", [("selected-row", 4), ("model-churn", 12)])
def test_public_qt_api_probe_never_claims_native_cocoa(scenario, checks):
    pytest.importorskip("PySide6")
    completed = probe("qt", scenario, "--cycles", "2")
    events = records(completed)
    if (scenario == "model-churn" and completed.returncode == 2
            and events[-1].get("message", "").startswith("Accessibility bridge did not activate")):
        pytest.skip("This Qt offscreen bridge cannot activate model-change accessibility events")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    passed = next(event for event in events if event["event"] == "passed")
    assert passed["checks"] == checks
    assert passed["native_cocoa"] is False
    assert passed["external_ax_client"] is False
    assert events[-2]["event"] == "closed"
    assert events[-1]["event"] == "result"
    assert events[-1]["status"] == "passed"
    assert not any(event["event"] == "before_native_selected_children" for event in events)


def test_exact_runtime_guard_rejects_a_different_qt():
    pytest.importorskip("PySide6")
    completed = probe("qt", "selected-row", "--expect-qt", "0.0.0")
    assert completed.returncode == 2, completed.stdout + completed.stderr
    assert records(completed)[-1]["failure_kind"] == "environment"


@pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("VSE_MACOS_AX_REGRESSION") != "1",
    reason="Opt-in native Cocoa regression; Linux/API-only runs are not native acceptance",
)
@pytest.mark.parametrize("scenario", ["selected-row", "model-churn"])
def test_native_cocoa_selected_cells_and_model_lifetime(scenario):
    completed = probe("cocoa", scenario, "--cycles", "5")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    events = records(completed)
    passed = next(event for event in events if event["event"] == "passed")
    assert passed["native_cocoa"] is True
    assert passed["external_ax_client"] is False
    assert events[-2]["event"] == "closed"
    assert events[-1]["event"] == "result"
    assert events[-1]["status"] == "passed"
