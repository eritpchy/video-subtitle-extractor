"""Missing Windows worker evidence is a failed smoke, never a skipped verdict."""
from types import SimpleNamespace
import sys

import pytest
import ipc_client
import smoke


@pytest.mark.parametrize('evidence,expected', [
    ({'available': False}, False), ({'available': True}, False),
    ({'available': True, 'passed': False}, False),
    ({'available': True, 'passed': True}, True),
    ({'available': 1, 'passed': 1}, False),
])
def test_windows_requires_available_successful_native_evidence(evidence, expected):
    assert smoke.runtime_evidence_passes(evidence, 'win32') is expected


@pytest.mark.parametrize('system', ['darwin', 'linux'])
def test_non_windows_preserves_existing_platform_scope(system):
    assert smoke.runtime_evidence_passes({'available': False}, system)
    assert not smoke.runtime_evidence_passes({'available': True, 'passed': False}, system)


def test_windows_smoke_delegates_real_worker_pid_to_read_only_collector(tmp_path, monkeypatch):
    from scripts import windows_native_worker_evidence as worker
    executable = tmp_path / 'native-ocr.exe'
    observed = []
    result = {'available': True, 'passed': True}
    monkeypatch.setattr(ipc_client, 'sys', SimpleNamespace(platform='win32', path=sys.path))
    monkeypatch.setattr(worker, 'collect_native_worker_evidence',
                        lambda pid, exe: observed.append((pid, exe)) or result)
    assert ipc_client.native_evidence(12345, executable) is result
    assert observed == [(12345, executable)]


@pytest.mark.parametrize('inherited', [None, '0', '1'])
def test_raw_protocol_probe_sets_ort_privacy_before_real_child_exec(monkeypatch, inherited):
    import os
    if inherited is None:
        monkeypatch.delenv('ORT_DISABLE_TELEMETRY', raising=False)
    else:
        monkeypatch.setenv('ORT_DISABLE_TELEMETRY', inherited)
    # Real subprocess, no ORT or network: this proves the flag is present at
    # child entry even when the source helper is used outside the CI workflow.
    command = [sys.executable, '-I', '-B', '-S', '-c',
               'import os,sys; sys.exit(0 if os.environ.get("ORT_DISABLE_TELEMETRY") == "1" else 9)']
    report = ipc_client.raw_case(command, 'privacy-before-init', b'', expect_zero=True)
    assert report['passed'] and report['returncode'] == 0
    assert os.environ.get('ORT_DISABLE_TELEMETRY') == inherited
