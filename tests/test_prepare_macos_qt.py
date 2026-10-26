"""Source preparation never silently mutates a system interpreter or claims AX QA."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import prepare_macos_qt as module


def test_read_only_is_default_and_never_downloads_or_applies(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(module.overlay, 'load_policy', lambda: {'production_integration_allowed': False})
    monkeypatch.setattr(module.overlay, '_host', lambda _: None)
    monkeypatch.setattr(module.overlay, 'inspect_current_venv', lambda: (tmp_path, tmp_path))
    monkeypatch.setattr(module.overlay, 'inspect_target', lambda *a, **k: {'plugin_sha256': 'original'})
    def forbidden(*args, **kwargs):
        pytest.fail('Read-only inspection must not download/install')
    monkeypatch.setattr(module.overlay, 'fetch_archive', forbidden)
    monkeypatch.setattr(module.overlay, 'apply_to_venv', forbidden)
    assert module.main([]) == 0
    assert 'No files changed' in capsys.readouterr().out


def test_candidate_mode_requires_explicit_local_bytes(monkeypatch, tmp_path):
    monkeypatch.setattr(module.overlay, 'load_policy', lambda: {'production_integration_allowed': False})
    monkeypatch.setattr(module.overlay, '_host', lambda _: None)
    monkeypatch.setattr(module.overlay, 'inspect_current_venv', lambda: (tmp_path, tmp_path))
    monkeypatch.setattr(module.overlay, 'inspect_target', lambda *a, **k: {})
    with pytest.raises(SystemExit):
        module.main(['--apply', '--candidate-testing'])


@pytest.mark.parametrize('wrong', [None, 'foreign-image', 'inactive-ax', 'wrong-version', 'process-failure', 'duplicate-record'])
def test_native_load_proof_is_bounded_and_strict(monkeypatch, tmp_path, wrong):
    purelib = tmp_path / 'site-packages'
    images = [str(purelib / module.overlay.PLUGIN)]
    images += [str(purelib / f'PySide6/Qt/lib/{name}.framework/Versions/A/{name}') for name in ('QtCore', 'QtGui')]
    result = {'qt': '6.12.0', 'backend': 'cocoa', 'accessibility_active': True, 'images': images}
    if wrong == 'foreign-image': result['images'][0] = '/opt/homebrew/foreign/libqcocoa.dylib'
    if wrong == 'inactive-ax': result['accessibility_active'] = False
    if wrong == 'wrong-version': result['qt'] = '6.13.0'
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        line = 'VSE_QT_OVERLAY_LOAD=' + json.dumps(result) + '\n'
        return SimpleNamespace(returncode=1 if wrong == 'process-failure' else 0,
                               stdout=line * (2 if wrong == 'duplicate-record' else 1), stderr='')
    monkeypatch.setattr(module.subprocess, 'run', run)
    monkeypatch.setenv('DYLD_LIBRARY_PATH', '/foreign')
    monkeypatch.setenv('QT_PLUGIN_PATH', '/foreign')
    monkeypatch.setenv('PYTHONPATH', '/foreign')
    if wrong:
        with pytest.raises(RuntimeError):
            module.native_load_probe(purelib, policy={'qt_version': '6.12.0'})
    else:
        observed = module.native_load_probe(purelib, policy={'qt_version': '6.12.0'})
        assert observed['external_ax_client_tested'] is False
        assert 'not external AX' in observed['scope']
    env = calls[0][1]['env']
    assert env['QT_QPA_PLATFORM'] == 'cocoa'
    assert all(name not in env for name in ('DYLD_LIBRARY_PATH', 'QT_PLUGIN_PATH', 'PYTHONPATH'))
    assert calls[0][0][1:3] == ['-I', '-B']


def test_preparer_has_no_installer_compiler_or_privilege_escalation():
    code = Path(module.__file__).read_text()
    assert 'pip install' not in code
    assert 'sudo' not in code
    assert 'codesign --force' not in code
    assert "setActive(False)" not in code
