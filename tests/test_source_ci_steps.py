"""The source receipt wrapper must execute real commands and fail closed."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import source_ci_steps as steps


def state(profile='linux-ort-cpu'):
    return {'schema': 1, 'identity': {'profile': profile},
            'paths': {'checkout': str(steps.ROOT)},
            'environment': {'interpreter': sys.executable}, 'models': '/fixed/models',
            'steps': [{'name': 'venv-create', 'returncode': 0}]}


def test_named_commands_are_fixed_python_entrypoints_without_shell_or_worker(tmp_path):
    for profile in steps.PROFILES:
        value = state(profile)
        for name in steps.STEPS:
            if name == 'image-coreml' and not profile.startswith('macos'):
                with pytest.raises(ValueError):
                    steps.command(value, tmp_path, name)
                continue
            if name == 'cuda-environment' and not profile.startswith('windows'):
                with pytest.raises(ValueError):
                    steps.command(value, tmp_path, name)
                continue
            command = steps.command(value, tmp_path, name)
            assert command[0] == sys.executable
            assert not any(word in command for word in ('sh', 'bash', 'cmd.exe', '--no-deps', 'native/ocr/build.py'))
        assert steps.command(value, tmp_path, 'pip-check')[1:] == ['-m', 'pip', 'check']
        assert steps.command(value, tmp_path, 'gui-smoke')[1:] == ['gui.py', '--smoke-test']
        install = steps.command(value, tmp_path, 'pip-install')
        assert steps.PROFILES[profile][3] in install and '--report' in install
        video = steps.command(value, tmp_path, 'video')
        assert video[video.index('--provider') + 1] == ('both' if profile.startswith('macos') else 'cpu')
    with pytest.raises(ValueError):
        steps.command(state(), tmp_path, 'echo-success')


def test_real_failed_child_keeps_exit_and_original_log_hashes(tmp_path):
    command = [sys.executable, '-I', '-c', "import sys; print('real stdout'); print('real stderr',file=sys.stderr); sys.exit(7)"]
    row = steps._run(tmp_path, 'failure', command, steps.clean_environment())
    assert row['returncode'] == 7
    assert row['started_utc_ns'] <= row['ended_utc_ns']
    for stream, expected in (('stdout', b'real stdout'), ('stderr', b'real stderr')):
        path = tmp_path / row[stream]['path']
        assert expected in path.read_bytes()
        assert steps.sha(path) == row[stream]['sha256']
        assert path.stat().st_size == row[stream]['bytes']


def test_existing_log_is_never_overwritten_or_counted_as_a_new_pass(tmp_path):
    old = tmp_path / 'same.stdout.log'
    old.write_bytes(b'previous pass must remain unchanged')
    row = steps._run(tmp_path, 'same', [sys.executable, '-c', 'raise AssertionError()'], steps.clean_environment())
    assert row['returncode'] == -1 and 'FileExistsError' in row['error']
    assert old.read_bytes() == b'previous pass must remain unchanged'


def test_timeout_cannot_be_rewritten_into_success(tmp_path):
    row = steps._run(tmp_path, 'timeout', [sys.executable, '-I', '-c', 'import time; time.sleep(1)'],
                     steps.clean_environment(), timeout=.02)
    assert row['returncode'] == -1 and 'TimeoutExpired' in row['error']


def test_clean_environment_removes_inherited_loaders_but_keeps_display_and_config(monkeypatch):
    for key in ('LD_LIBRARY_PATH', 'LD_PRELOAD', 'DYLD_FALLBACK_LIBRARY_PATH', 'PYTHONPATH', 'PYTHONHOME', 'MTL_CAPTURE_ENABLED'):
        monkeypatch.setenv(key, 'not-a-runtime')
    monkeypatch.setenv('QT_QPA_PLATFORM', 'cocoa')
    monkeypatch.setenv('HOME', '/test/home')
    env = steps.clean_environment()
    assert not any(key.startswith(('LD_', 'DYLD_')) for key in env)
    assert not {'PYTHONPATH', 'PYTHONHOME', 'MTL_CAPTURE_ENABLED'} & set(env)
    assert env['HOME'] == '/test/home' and env['QT_QPA_PLATFORM'] == 'cocoa'


def test_actual_ci_identity_requires_matching_runner_and_nonzero_attempt(monkeypatch):
    values = {'GITHUB_SHA': '1' * 40, 'GITHUB_REPOSITORY': 'owner/repo', 'GITHUB_RUN_ID': '123',
              'GITHUB_RUN_ATTEMPT': '2', 'GITHUB_JOB': 'source', 'RUNNER_OS': 'Linux', 'RUNNER_ARCH': 'X64'}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    assert steps.identity('linux-ort-cpu')['run_attempt'] == 2
    with pytest.raises(ValueError, match='CI identity'):
        steps.identity('macos-ort-cpu-coreml')
    monkeypatch.setenv('GITHUB_RUN_ATTEMPT', '0')
    with pytest.raises(ValueError):
        steps.identity('linux-ort-cpu')


def test_existing_evidence_or_venv_is_rejected_before_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(steps, 'identity', lambda profile: {'commit': '1' * 40})
    monkeypatch.setattr(steps, 'checkout_identity', lambda: {'commit': '1' * 40, 'dirty': False})
    existing = tmp_path / 'existing'
    existing.mkdir()
    marker = existing / 'user-file'
    marker.write_text('preserve')
    for evidence, venv in ((existing, tmp_path / 'new-venv'), (tmp_path / 'new-evidence', existing)):
        with pytest.raises(ValueError, match='new path'):
            steps.initialize(evidence, 'linux-ort-cpu', venv, tmp_path / 'models')
    assert marker.read_text() == 'preserve'
    assert not (tmp_path / 'new-evidence').exists()


def test_real_fresh_venv_probe_is_isolated_and_binds_actual_interpreter(tmp_path, monkeypatch):
    profile = 'windows-cuda11.8' if os.name == 'nt' else 'linux-ort-cpu'
    identity = {'commit': '1' * 40, 'profile': profile}
    monkeypatch.setattr(steps, 'identity', lambda _: identity)
    monkeypatch.setattr(steps, 'checkout_identity', lambda: {'commit': '1' * 40, 'dirty': False})
    monkeypatch.setenv('LD_LIBRARY_PATH', str(tmp_path / 'foreign-libraries'))
    result = steps.initialize(tmp_path / 'evidence', profile, tmp_path / 'venv', tmp_path / 'models')
    environment = result['environment']
    assert environment['venv_created_new'] is True
    assert environment['prefix'] != environment['base_prefix']
    assert environment['loader_overrides'] == {} and environment['user_site_enabled'] is False
    assert steps.sha(environment['interpreter']) == environment['interpreter_sha256']
    assert [row['returncode'] for row in result['steps']] == [0, 0]
    stored = json.loads((tmp_path / 'evidence/steps.json').read_bytes())
    assert stored == result


def test_failed_named_step_is_recorded_and_cannot_be_resumed(tmp_path, monkeypatch):
    value = state()
    monkeypatch.setattr(steps, '_load', lambda root: value)
    monkeypatch.setattr(steps, 'command', lambda *_: [sys.executable, '-I', '-c', 'raise SystemExit(9)'])
    with pytest.raises(RuntimeError, match='failed'):
        steps.run_step(tmp_path, 'pip-check')
    saved = json.loads((tmp_path / 'steps.json').read_bytes())
    assert saved['steps'][-1]['returncode'] == 9
    with pytest.raises(ValueError, match='failed source run'):
        steps.run_step(tmp_path, 'gui-smoke')


def test_completed_named_step_cannot_be_reused(tmp_path, monkeypatch):
    value = state()
    value['steps'].append({'name': 'pip-check', 'returncode': 0})
    monkeypatch.setattr(steps, '_load', lambda root: value)
    with pytest.raises(ValueError, match='reuse'):
        steps.run_step(tmp_path, 'pip-check')


def test_direct_script_help_uses_no_inference_dependencies():
    result = subprocess.run([sys.executable, '-I', '-S', str(Path(steps.__file__)), '--help'],
                            capture_output=True, text=True)
    assert result.returncode == 0 and 'run-step' in result.stdout
