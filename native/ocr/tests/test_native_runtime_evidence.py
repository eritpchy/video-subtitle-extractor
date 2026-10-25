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
