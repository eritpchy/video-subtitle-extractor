"""DirectML packaging identity is supported without forging GPU/native-worker coverage."""
import json
from pathlib import Path

import pytest

from scripts import windows_verification as windows


def test_directml_capture_parser_forwards_profile_without_native_worker(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(windows, 'capture', lambda path, runner, profile: called.append((path, runner, profile)))
    assert windows.main(['capture', '--runner', 'windows-2022', '--profile', 'directml', '--output', str(tmp_path)]) == 0
    assert called == [(tmp_path / 'identity.json', 'windows-2022', 'directml')]


def test_directml_cpu_report_never_inherits_native_license_or_gpu_pass(monkeypatch):
    # Theme details are covered by the existing reporter tests; isolate profile policy here.
    monkeypatch.setattr(windows, 'system_theme_status', lambda *args: 'passed')
    identity = {'validated': True, 'profile': 'directml'}
    outcomes = dict.fromkeys(windows.COMMON_STAGES, 'success')
    outcomes['license-gate'] = 'failure'  # Unrelated native SDK entitlement state.
    report = windows.coverage(identity, outcomes, 'directml')
    assert report['execution_status'] == 'passed'
    assert report['customer_package_license_gate'] == 'not-applicable'
    assert report['directml_execution'] == 'not-verified-by-explicit-cpu-acceptance'
    assert report['gpu_execution'] == 'outside-ci-only-scope'
    assert not any(stage['stage'].startswith('native-') for stage in report['stages'])
    summary = windows.markdown(report)
    assert 'not DirectML kernel execution or GPU hardware verification' in summary
    assert 'Customer-package VC license gate outcome' not in summary


def test_directml_summary_without_capture_stays_incomplete(monkeypatch, tmp_path):
    monkeypatch.setenv('VSE_WINDOWS_STEP_OUTCOMES', '{}')
    monkeypatch.delenv('GITHUB_STEP_SUMMARY', raising=False)
    monkeypatch.setattr(windows, 'dependency_versions', lambda: {'onnxruntime-directml': '1.20.1'})
    windows.main(['summarize', '--runner', 'windows-2025', '--profile', 'directml', '--output', str(tmp_path)])
    report = json.loads((tmp_path / 'coverage.json').read_text())
    assert report['execution_status'] == 'incomplete-or-failed'
    assert report['profile'] == 'directml'
    assert report['source_dependency_versions']['onnxruntime-directml'] == '1.20.1'


def test_directml_cannot_capture_fake_windows_on_linux(monkeypatch, tmp_path):
    monkeypatch.setattr(windows.sys, 'platform', 'linux')
    with pytest.raises(RuntimeError, match='not simulated'):
        windows.capture(tmp_path / 'identity.json', 'windows-2022', 'directml')
    assert json.loads((tmp_path / 'identity.json').read_text())['validated'] is False


def test_unknown_profile_fails_closed(tmp_path):
    with pytest.raises(ValueError, match='Unknown'):
        windows.capture(tmp_path / 'identity.json', 'windows-2022', 'pretend-gpu')
    with pytest.raises(ValueError, match='Unknown'):
        windows.coverage({}, {}, 'pretend-gpu')
