"""Real launcher startup regressions, without changing the host installation."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from scripts import bundle_bootstrap as bootstrap
from scripts.portable_python import launcher_command, write_launchers
from scripts.standalone_runtime_probe import audit_environment, summarize_reports
from scripts.verify_portable_environment import fixture_environment, check_environment_evidence, main


def media_inventory(root, platform='linux', wrapper=True):
    folder = f'backend/subfinder/{platform}/x64'
    names = [f'{folder}/VideoSubFinderCli.exe'] if platform == 'windows' else [f'{folder}/VideoSubFinderCli'] + ([f'{folder}/VideoSubFinderCli.run'] if wrapper else [])
    names += [f'runtime/lib/python3.12/site-packages/imageio_ffmpeg/binaries/ffmpeg-{platform}-fixture']
    inventory = {}
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'fixture executable bytes')
        inventory[name] = bootstrap.sha256(path)
    return inventory


def staged_fixture(tmp_path, platform):
    root = tmp_path / 'bundle 空间 & bang!'
    if platform == 'macos':
        root /= 'Video Subtitle Extractor.app/Contents/Resources'
    (root / 'runtime/bin').mkdir(parents=True)
    if platform == 'windows':
        interpreter = root / 'runtime/python.cmd'
        interpreter.write_text(f'@"{sys.executable}" %*\r\n', encoding='utf-8')
        relative = 'runtime/python.cmd'
    else:
        interpreter = root / 'runtime/bin/python3.12'
        # Explicit host-interpreter bridge tests flags before Python startup,
        # not a claim that the fixture is a self-contained release/runtime.
        interpreter.write_text('#!/bin/sh\nexec "' + sys.executable + '" "$@"\n')
        interpreter.chmod(0o755)
        relative = 'runtime/bin/python3.12'
    write_launchers(root, platform, runtime_python=relative)
    (root / 'scripts').mkdir()
    shutil.copy2(bootstrap.__file__, root / 'scripts/bundle_bootstrap.py')
    shutil.copy2(Path(bootstrap.__file__).with_name('standalone_runtime_probe.py'), root / 'scripts/standalone_runtime_probe.py')
    # Record the entry state BEFORE any Python cleanup to detect pre-bootstrap
    # contamination, then exercise the exact production sanitizer/hook loader.
    (root / 'bootstrap.py').write_text('''import json, os, pathlib, site, sys
root = pathlib.Path(__file__).resolve().parent
before = dict(isolated=sys.flags.isolated, ignore_environment=sys.flags.ignore_environment, utf8_mode=sys.flags.utf8_mode,
              dont_write_bytecode=sys.flags.dont_write_bytecode, bytecode_writes_disabled=sys.dont_write_bytecode,
              user_site_enabled=site.ENABLE_USER_SITE, args=sys.argv, path=sys.path,
              native={key:os.environ.get(key) for key in ('LD_LIBRARY_PATH','DYLD_LIBRARY_PATH','QT_PLUGIN_PATH','QT_QPA_PLATFORM_PLUGIN_PATH','PYTHONHOME','PYTHONPATH','PYTHONEXECUTABLE','__PYVENV_LAUNCHER__')})
sys.path.insert(0,str(root))
from scripts.bundle_bootstrap import sanitize_environment, enable_trusted_audit
clean = sanitize_environment(root,dict(os.environ),os.environ['FIXTURE_PLATFORM'])
os.environ.clear(); os.environ.update(clean)
if '--audit' in sys.argv:
    enable_trusted_audit(root,pathlib.Path(sys.argv[sys.argv.index('--audit')+1]))
    import subprocess
    subprocess.run([sys.executable,'-c','import ctypes'],check=True)
print('启动成功：字幕 café 日本語')
pathlib.Path(os.environ['PROBE_OUTPUT']).write_text(json.dumps(dict(before=before,after=dict(os.environ)),ensure_ascii=False),encoding='utf-8')
''')
    return root


@pytest.mark.parametrize('platform,app', [('linux', False), ('macos', False), ('macos', True), ('windows', False)])
@pytest.mark.parametrize('contaminated', [False, True])
def test_real_launcher_isolates_python_before_bootstrap(tmp_path, platform, app, contaminated):
    if (platform == 'windows') != (os.name == 'nt'):
        pytest.skip('Requires native cmd.exe or POSIX shell; no emulation claim')
    root = staged_fixture(tmp_path, platform)
    env, fixture = fixture_environment(tmp_path / 'fixture', contaminated)
    output = tmp_path / 'probe.json'
    env.update(PROBE_OUTPUT=str(output), FIXTURE_PLATFORM=platform, __PYVENV_LAUNCHER__='/foreign/python')
    original = dict(os.environ)
    command = launcher_command(root, platform, ['参数 with space'], app=app)
    result = subprocess.run(command, env=env, cwd=fixture['cwd'], check=True, timeout=30, capture_output=True)
    assert '启动成功：字幕 café 日本語' in result.stdout.decode('utf-8')
    actual = json.loads(output.read_text(encoding='utf-8'))
    before, after = actual['before'], actual['after']
    assert before['isolated'] == before['ignore_environment'] == before['utf8_mode'] == 1
    assert before['dont_write_bytecode'] == 1 and before['bytecode_writes_disabled'] is True
    assert after['PYTHONUTF8'] == '1' and 'PYTHONIOENCODING' not in after
    assert before['user_site_enabled'] is False
    assert before['args'][1:] == ['参数 with space']
    assert all(value is None for key, value in before['native'].items() if key != 'LD_LIBRARY_PATH')
    assert before['native']['LD_LIBRARY_PATH'] == (str(root / 'native-libs') if platform == 'linux' else None)
    assert after['VSE_CONFIG_FILE'] == fixture['expected_config']
    assert after['VSE_WORK_DIR'] == fixture['expected_work']
    assert not Path(fixture['marker']).exists()
    for key in ('PYTHONHOME', 'PYTHONUSERBASE', 'CONDA_PREFIX', 'VIRTUAL_ENV', 'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH'):
        assert key not in after
    for key in bootstrap.NATIVE_SEARCH_VARIABLES:
        if key.startswith('FLAGS_') or key in ('CUDA_PATH', 'CUDA_HOME', 'CUDNN_PATH', 'CUDNN_HOME'):
            assert key not in after
    assert os.environ == original  # The parent OS/process environment is untouched.


def test_isolated_launcher_retains_trusted_descendant_audit(tmp_path):
    platform = 'windows' if os.name == 'nt' else 'linux'
    root = staged_fixture(tmp_path, platform)
    env, fixture = fixture_environment(tmp_path / 'fixture', True)
    reports = tmp_path / 'evidence'
    audit_environment(root, reports)
    env.update(PROBE_OUTPUT=str(tmp_path / 'probe.json'), FIXTURE_PLATFORM=platform)
    # Keep the contaminated PYTHONPATH; never replace it with audit_environment
    # before launching. Bootstrap must enable only the validated copy after -I.
    subprocess.run(launcher_command(root, platform, ['--audit', str(reports)]),
                   env=env, cwd=fixture['cwd'], check=True, timeout=30, capture_output=True)
    processes = [json.loads(path.read_text()) for path in reports.glob('process-*.json')]
    assert len(processes) == 2
    assert all(record['user_site_enabled'] is False for record in processes)
    assert all(record['dont_write_bytecode'] == 1 and record['bytecode_writes_disabled'] is True for record in processes)
    assert not Path(fixture['marker']).exists()
    # The bridge intentionally uses HOST Python: prove the strict auditor still
    # rejects it. A flag change must not turn a missing audit into a false pass.
    with pytest.raises(RuntimeError, match='external executable'):
        summarize_reports(root, reports, ('ctypes',))


def test_changed_audit_hook_is_rejected_before_execution(tmp_path, monkeypatch):
    root = staged_fixture(tmp_path, 'windows' if os.name == 'nt' else 'linux')
    evidence = tmp_path / 'evidence'
    audit_environment(root, evidence)
    (evidence / 'hook/sitecustomize.py').write_text('raise RuntimeError("untrusted")')
    with pytest.raises(RuntimeError, match='modified trusted runtime audit hook'):
        bootstrap.enable_trusted_audit(root, evidence)


def test_sanitizer_preserves_preferences_and_clears_only_runtime_selectors(tmp_path):
    source = {'PYTHONHOME': '/foreign', 'PYTHONWARNINGS': 'error', 'PYTHONPATH': '/foreign',
              'CONDA_PREFIX_7': '/foreign', '_CONDA_EXE': '/foreign', '_CE_M': '-m',
              'VIRTUAL_ENV': '/foreign', '__PYVENV_LAUNCHER__': '/foreign', 'LD_PRELOAD': '/foreign',
              'DYLD_INSERT_LIBRARIES': '/foreign', 'QT_PLUGIN_PATH': '/foreign', 'QT_QPA_PLATFORM_PLUGIN_PATH': '/foreign',
              'HOME': '/user', 'XDG_CONFIG_HOME': '/config', 'VSE_CONFIG_FILE': '/explicit/config.json',
              'VSE_WORK_DIR': '/work', 'QT_QPA_PLATFORM': 'offscreen', 'QT_SCALE_FACTOR': '1.5',
              'VSE_NATIVE_OCR_PROVIDER': 'cuda', 'DISPLAY': ':77', 'LANG': 'zh_CN.UTF-8',
              'SYSTEMROOT': r'C:\Windows', 'PATH': '/foreign', 'VSE_RUNTIME_AUDIT_REPORTS': '/foreign'}
    actual = bootstrap.sanitize_environment(tmp_path, source, 'linux')
    for key in ('HOME', 'XDG_CONFIG_HOME', 'VSE_CONFIG_FILE', 'VSE_WORK_DIR', 'QT_QPA_PLATFORM', 'QT_SCALE_FACTOR', 'VSE_NATIVE_OCR_PROVIDER', 'DISPLAY', 'LANG'):
        assert actual[key] == source[key]
    assert actual['LD_LIBRARY_PATH'] == str(tmp_path / 'native-libs')
    assert '/foreign' not in actual.values()
    assert source['PYTHONHOME'] == '/foreign'
    windows = bootstrap.sanitize_environment(tmp_path, source, 'windows')
    assert '/foreign' not in windows['PATH']
    assert 'LD_LIBRARY_PATH' not in windows


def valid_environment_report(tmp_path):
    root = tmp_path / 'bundle'
    (root / 'native-ocr').mkdir(parents=True)
    fixture = {'marker': str(tmp_path / 'marker'), 'expected_config': '/preferences', 'expected_work': '/work',
               'forbidden_roots': ['/foreign'], 'user_sites': ['/usersite']}
    report = {'isolated': 1, 'ignore_environment': 1, 'no_user_site': 1, 'user_site_enabled': False,
              'profile': 'native', 'sys_path': [str(root)], 'utf8_mode': 1,
              'dont_write_bytecode': 1, 'bytecode_writes_disabled': True,
              'environment': {'VSE_CONFIG_FILE': '/preferences', 'VSE_WORK_DIR': '/work',
                              'VSE_OCR_BACKEND': 'native', 'VSE_NATIVE_OCR_ROOT': str(root / 'native-ocr'), 'PYTHONUTF8': '1',
                              'VSE_VSF_BINARY': str(root / 'vsf'), 'VSE_VSF_DIR': str(root), 'IMAGEIO_FFMPEG_EXE': str(root / 'ffmpeg')}}
    (tmp_path / 'dependency-isolation').mkdir()
    (tmp_path / 'environment-report.json').write_text(json.dumps(report))
    (tmp_path / 'dependency-isolation/process-1.json').write_text(json.dumps(report))
    return root, fixture, report


@pytest.mark.parametrize('regression', ['isolation', 'usersite', 'config', 'profile', 'native-path', 'pythonpath', 'qtpath', 'marker', 'missing-probe', 'bytecode-startup', 'bytecode-effective'])
def test_evidence_gate_fails_closed(tmp_path, regression):
    root, fixture, report = valid_environment_report(tmp_path)
    check_environment_evidence(tmp_path, fixture, 'native', root)
    if regression == 'isolation':
        report['isolated'] = 0
    elif regression == 'usersite':
        report['user_site_enabled'] = True
    elif regression == 'config':
        report['environment']['VSE_CONFIG_FILE'] = '/other'
    elif regression == 'profile':
        report['environment']['VSE_OCR_BACKEND'] = 'paddle'
    elif regression == 'native-path':
        report['environment']['VSE_NATIVE_OCR_ROOT'] = '/foreign'
    elif regression == 'pythonpath':
        report['sys_path'].append('/foreign')
    elif regression == 'qtpath':
        report['environment']['QT_PLUGIN_PATH'] = '/foreign'
    elif regression == 'marker':
        Path(fixture['marker']).touch()
    elif regression == 'bytecode-startup':
        report['dont_write_bytecode'] = 0
    elif regression == 'bytecode-effective':
        report['bytecode_writes_disabled'] = False
    else:
        (tmp_path / 'dependency-isolation/process-1.json').unlink()
    (tmp_path / 'environment-report.json').write_text(json.dumps(report))
    with pytest.raises(RuntimeError):
        check_environment_evidence(tmp_path, fixture, 'native', root)


@pytest.mark.parametrize('field,value', [('dont_write_bytecode', 0), ('bytecode_writes_disabled', False),
                                        ('dont_write_bytecode', None), ('bytecode_writes_disabled', None)])
def test_descendant_bytecode_protection_is_required_not_just_launcher(tmp_path, field, value):
    root, fixture, report = valid_environment_report(tmp_path)
    if value is None:
        del report[field]
    else:
        report[field] = value
    (tmp_path / 'dependency-isolation/process-1.json').write_text(json.dumps(report))
    with pytest.raises(RuntimeError, match='bytecode write protection was lost'):
        check_environment_evidence(tmp_path, fixture, 'native', root)


@pytest.mark.parametrize('mutate', [False, True])
def test_each_scenario_rechecks_inventoried_bytecode_after_launcher(tmp_path, monkeypatch, mutate):
    from scripts import verify_portable_environment as verifier
    root = tmp_path / 'bundle'
    bytecode = root / 'runtime/Lib/__pycache__/__future__.cpython-312.pyc'
    bytecode.parent.mkdir(parents=True)
    bytecode.write_bytes(b'inventoried bytecode fixture')
    (root / 'bundle-manifest.json').write_text(json.dumps({
        'format': 2, 'platform': 'linux', 'arch': 'x64', 'ocr_backend': 'native',
        'files': {bytecode.relative_to(root).as_posix(): bootstrap.sha256(bytecode)}}))
    archive = tmp_path / 'candidate.zip'
    archive.write_bytes(b'archive fixture; extraction mocked, payload hashes real')
    archive.with_suffix('.zip.sha256').write_text(bootstrap.sha256(archive))
    monkeypatch.setattr(verifier, 'extract_checked', lambda *args: root)
    monkeypatch.setattr(verifier, 'host_platform', lambda: ('linux', 'x64'))
    monkeypatch.setattr(verifier, 'SCENARIOS', ('clean-host-path',))
    monkeypatch.setattr(verifier, 'audit_environment', lambda *args: {})
    monkeypatch.setattr(verifier, 'summarize_reports', lambda *args, **kwargs: {})
    monkeypatch.setattr(verifier, 'check_environment_evidence', lambda *args: None)
    def launch(*args, **kwargs):
        if mutate:
            bytecode.write_bytes(b'rewritten bytecode fixture')
    monkeypatch.setattr(verifier.subprocess, 'run', launch)
    output = tmp_path / 'evidence'
    if mutate:
        with pytest.raises(RuntimeError, match='Portable environment scenario failed'):
            verifier.verify_archive(archive, output)
        case = json.loads((output / 'clean-host-path/scenario-report.json').read_text())
        assert case['result'] == 'failed'
        assert 'Bundle checksum mismatch: runtime/Lib/__pycache__/__future__.cpython-312.pyc' in case['error']
    else:
        assert verifier.verify_archive(archive, output)['result'] == 'passed'


@pytest.mark.parametrize('reverse', [False, True])
def test_native_worker_root_accepts_filesystem_alias_not_only_equal_spelling(tmp_path, reverse):
    root, fixture, report = valid_environment_report(tmp_path)
    alias = tmp_path / 'var-alias'
    try:
        alias.symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip('Host does not permit the temporary directory-alias fixture')
    report['environment']['VSE_NATIVE_OCR_ROOT'] = str((alias if reverse else root) / 'native-ocr')
    for path in (tmp_path / 'environment-report.json', tmp_path / 'dependency-isolation/process-1.json'):
        path.write_text(json.dumps(report))
    check_environment_evidence(tmp_path, fixture, 'native', root if reverse else alias)


@pytest.mark.skipif(sys.platform != 'darwin', reason='Actual /var -> /private/var identity requires macOS')
def test_native_worker_root_accepts_actual_macos_var_alias(tmp_path):
    root, fixture, report = valid_environment_report(tmp_path)
    canonical = root.resolve(strict=True)
    if not canonical.is_relative_to('/private/var'):
        pytest.skip('This macOS runner temporary directory is not below /private/var')
    alias = Path('/var') / canonical.relative_to('/private/var')
    assert alias.samefile(canonical)
    report['environment']['VSE_NATIVE_OCR_ROOT'] = str(canonical / 'native-ocr')
    for path in (tmp_path / 'environment-report.json', tmp_path / 'dependency-isolation/process-1.json'):
        path.write_text(json.dumps(report))
    check_environment_evidence(tmp_path, fixture, 'native', alias)


@pytest.mark.parametrize('replacement', ['foreign-directory', 'missing', 'relative', 'redirected-payload', 'foreign-symlink'])
def test_native_worker_alias_check_never_accepts_a_foreign_or_missing_worker(tmp_path, replacement):
    root, fixture, report = valid_environment_report(tmp_path)
    foreign = tmp_path / 'unrelated-worker'
    foreign.mkdir()
    value = str(foreign)
    if replacement == 'missing':
        value = str(tmp_path / 'missing-worker')
    elif replacement == 'relative':
        value = 'native-ocr'
    elif replacement in ('redirected-payload', 'foreign-symlink'):
        link = root / 'native-ocr' if replacement == 'redirected-payload' else tmp_path / 'worker-alias'
        if link.exists():
            link.rmdir()
        try:
            link.symlink_to(foreign, target_is_directory=True)
        except OSError:
            pytest.skip('Host does not permit the temporary directory-alias fixture')
        value = str(link)
    report['environment']['VSE_NATIVE_OCR_ROOT'] = value
    (tmp_path / 'dependency-isolation/process-1.json').write_text(json.dumps(report))
    with pytest.raises(RuntimeError, match='native-worker path replaced'):
        check_environment_evidence(tmp_path, fixture, 'native', root)


def test_empty_archive_directory_is_not_a_pass(tmp_path):
    with pytest.raises(SystemExit):
        main(['--archives', str(tmp_path), '--output', str(tmp_path / 'results')])


def test_refuses_stale_evidence(tmp_path):
    (tmp_path / 'candidate.zip').touch()
    output = tmp_path / 'results'
    output.mkdir()
    (output / 'stale.json').touch()
    with pytest.raises(SystemExit):
        main(['--archives', str(tmp_path), '--output', str(output)])


@pytest.mark.parametrize('profile', ['paddle', 'native', 'directml'])
def test_existing_package_smoke_uses_actual_launcher_and_explicit_trusted_audit(tmp_path, monkeypatch, profile):
    from scripts import package_release, standalone_runtime_probe
    root = tmp_path / 'app'
    (root / 'runtime/bin').mkdir(parents=True)
    (root / 'scripts').mkdir()
    (root / 'runtime/bin/python3.12').touch()
    shutil.copy2(standalone_runtime_probe.__file__, root / 'scripts/standalone_runtime_probe.py')
    (root / 'bundle-manifest.json').write_text(json.dumps({'format': 2, 'platform': 'linux', 'arch': 'x64',
                                                         'runtime_python': 'runtime/bin/python3.12', 'ocr_backend': profile, 'files': {}}))
    archive = tmp_path / 'candidate.zip'
    archive.write_bytes(b'fixture archive')
    archive.with_suffix('.zip.sha256').write_text(bootstrap.sha256(archive))
    monkeypatch.setattr(package_release, 'extract_checked', lambda *args: root)
    monkeypatch.setattr(package_release, 'verification_path', lambda *args: '/no-host-python')
    calls = []
    monkeypatch.setattr(package_release.subprocess, 'run', lambda command, **kwargs: calls.append((command, kwargs)))
    summaries = []
    monkeypatch.setattr(standalone_runtime_probe, 'summarize_reports', lambda *args, **kwargs: summaries.append((args, kwargs)))
    package_release.verify_bundle(archive, True)
    assert calls[0][0][0] == str(root / 'Start-VSE.sh')
    assert '--runtime-audit-dir' in calls[0][0]
    assert ('--paddle-model-fixtures' in calls[0][0]) == (profile == 'paddle')
    if profile == 'paddle':
        assert calls[0][0][calls[0][0].index('--paddle-model-fixtures') + 1] == str((package_release.ROOT / 'native/ocr/tests/evidence').resolve())
    assert len(summaries) == 1
    assert 'PySide6.QtWidgets' in summaries[0][1]['required_modules']


@pytest.mark.parametrize('profile', ['native', 'paddle'])
def test_bootstrap_manifest_wins_over_inherited_profile_without_discarding_preferences(tmp_path, monkeypatch, profile):
    root = tmp_path / 'app'
    (root / 'runtime').mkdir(parents=True)
    python = root / 'runtime/python'
    python.touch()
    (root / 'bundle-manifest.json').write_text(json.dumps({'format': 2, 'distribution': 'self-contained CPU inference runtime',
        'platform': 'linux', 'arch': 'x64', 'runtime_python': 'runtime/python', 'files': media_inventory(root), 'ocr_backend': profile}))
    monkeypatch.setattr(bootstrap, '__file__', str(root / 'bootstrap.py'))
    monkeypatch.setattr(bootstrap, 'host_platform', lambda: ('linux', 'x64'))
    monkeypatch.setattr(bootstrap.sys, 'executable', str(python))
    monkeypatch.setattr(bootstrap.sys, 'version_info', (3, 12, 0))
    monkeypatch.setattr(bootstrap.os, 'chdir', lambda _: None)
    monkeypatch.setattr(bootstrap.sys, 'path', list(sys.path))
    # Replace the mapping, not the real process environment: bootstrap correctly
    # mutates only its already-created child process's environment in production.
    environment = {'VSE_CONFIG_FILE': '/preferences', 'VSE_WORK_DIR': '/work', 'VSE_OCR_BACKEND': 'wrong',
                   'VSE_NATIVE_OCR_ROOT': '/foreign/worker', 'VSE_MODEL_DIR': '/foreign/models', 'VSE_NATIVE_OCR_PROVIDER': 'cuda', 'PYTHONHOME': '/foreign'}
    monkeypatch.setattr(bootstrap.os, 'environ', environment)
    monkeypatch.setattr(bootstrap.subprocess, 'call', lambda *args: 0)
    assert bootstrap.main(['--cli', '--help']) == 0
    assert environment['VSE_OCR_BACKEND'] == profile
    if profile == 'native':
        assert environment['VSE_NATIVE_OCR_ROOT'] == str(root / 'native-ocr')
        assert 'VSE_MODEL_DIR' not in environment
    else:
        assert 'VSE_NATIVE_OCR_ROOT' not in environment
        assert environment['VSE_MODEL_DIR'] == str(root / 'backend/models')
    assert environment['VSE_NATIVE_OCR_PROVIDER'] == 'cuda'  # Preserve explicit GPU refusal, do not hide it.
    assert environment['VSE_CONFIG_FILE'] == '/preferences'
    assert environment['VSE_WORK_DIR'] == '/work'
    assert 'PYTHONHOME' not in environment


def test_stock_linux_no_python_acceptance_uses_actual_launcher(tmp_path, monkeypatch):
    from scripts import verify_clean_linux_bundle
    from scripts.verify_clean_linux_bundle import container_command
    monkeypatch.setattr(verify_clean_linux_bundle, 'linux_identity', lambda: (1000, 1000))
    command = container_command(tmp_path, tmp_path, 'sha256:fixture', 'runtime/bin/python3.12',
                                ['bootstrap.py', '--verify', '--results-dir', '/results/acceptance'], 'offline-e2e')
    assert command[-4:] == ['/bundle/Start-VSE.sh', '--verify', '--results-dir', '/results/acceptance']


def test_existing_smoke_workloads_may_use_their_own_config_after_bootstrap(tmp_path):
    root, fixture, report = valid_environment_report(tmp_path)
    report['environment']['VSE_CONFIG_FILE'] = str(tmp_path / 'bundle-e2e/isolated-config.json')
    (tmp_path / 'dependency-isolation/process-1.json').write_text(json.dumps(report))
    check_environment_evidence(tmp_path, fixture, 'native', root)


def vendor_qt_fixture(root, vendor='cv2', platform='linux'):
    packages = 'runtime/Lib/site-packages' if platform == 'windows' else 'runtime/lib/python3.12/site-packages'
    package = f'{packages}/{vendor}'
    plugins = f'{package}/' + ('qt/plugins' if vendor == 'cv2' else 'plugins' if platform == 'windows' else 'Qt/plugins')
    library = {'linux': 'libqxcb.so', 'macos': 'libqcocoa.dylib', 'windows': 'qwindows.dll'}[platform]
    contents = {f'{package}/__init__.py': b'# vendor package fixture\n',
                f'{plugins}/platforms/{library}': b'inert plugin fixture, never loaded'}
    if vendor == 'cv2':
        contents[f'{package}/config-3.py'] = b'# vendor import-time Qt path configuration fixture\n'
    inventory = {}
    for name, content in contents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        inventory[name] = bootstrap.sha256(path)
    manifest = {'platform': platform, 'files': inventory}
    (root / 'bundle-manifest.json').write_text(json.dumps(manifest))
    return root / plugins, manifest


@pytest.mark.parametrize('vendor,platform,subdirectory', [
    ('cv2', 'linux', ''), ('PySide6', 'linux', ''), ('PySide6', 'macos', 'platforms'), ('PySide6', 'windows', 'platforms'),
])
def test_child_vendor_may_regenerate_only_manifest_owned_qt_plugin_root(tmp_path, vendor, platform, subdirectory):
    root, fixture, report = valid_environment_report(tmp_path)
    plugins, _ = vendor_qt_fixture(root, vendor, platform)
    report['environment']['QT_QPA_PLATFORM_PLUGIN_PATH'] = str(plugins / subdirectory)
    (tmp_path / 'dependency-isolation/process-1.json').write_text(json.dumps(report))
    check_environment_evidence(tmp_path, fixture, 'native', root)


def test_packaged_cv2_import_fixture_can_repopulate_a_cleared_qt_path(tmp_path):
    root, fixture, report = valid_environment_report(tmp_path)
    plugins, manifest = vendor_qt_fixture(root)
    package = plugins.parents[1]
    initializer = package / '__init__.py'
    # A tiny owned package models the real wheel's config-3.py assignment; no
    # fixture native library is executed, and no installed package is modified.
    initializer.write_text('import os\nfrom pathlib import Path\nos.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(Path(__file__).parent / "qt/plugins")\n')
    manifest['files'][initializer.relative_to(root).as_posix()] = bootstrap.sha256(initializer)
    (root / 'bundle-manifest.json').write_text(json.dumps(manifest))
    code = ('import os, sys; os.environ.pop("QT_QPA_PLATFORM_PLUGIN_PATH", None); '
            f'sys.path.insert(0, {str(package.parent)!r}); import cv2; '
            'print(os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"])')
    value = subprocess.check_output([sys.executable, '-I', '-B', '-X', 'utf8', '-c', code], timeout=15).decode('utf-8').strip()
    assert value == str(plugins)
    report['environment']['QT_QPA_PLATFORM_PLUGIN_PATH'] = value
    (tmp_path / 'dependency-isolation/process-1.json').write_text(json.dumps(report))
    check_environment_evidence(tmp_path, fixture, 'native', root)


@pytest.mark.parametrize('regression', [
    'initial-bootstrap', 'other-qt-variable', 'foreign', 'arbitrary-bundle-directory', 'mixed-foreign', 'mixed-bundled',
    'symlink-external', 'symlink-internal', 'symlink-alias', 'unlisted-plugin', 'missing-plugin',
    'missing-package-anchor', 'changed-plugin', 'changed-package-anchor',
])
def test_regenerated_qt_path_exception_does_not_admit_unverified_search_roots(tmp_path, regression):
    root, fixture, report = valid_environment_report(tmp_path)
    plugins, manifest = vendor_qt_fixture(root)
    value = str(plugins)
    plugin = plugins / 'platforms/libqxcb.so'
    anchor = plugins.parents[1] / '__init__.py'
    if regression == 'foreign':
        value = str(tmp_path / 'unrelated-host-qt')
    elif regression == 'arbitrary-bundle-directory':
        value = str(root / 'user-plugins')
        Path(value).mkdir()
    elif regression == 'mixed-foreign':
        value += os.pathsep + str(tmp_path / 'unrelated-host-qt')
    elif regression == 'mixed-bundled':
        value += os.pathsep + str(plugins / 'platforms')
    elif regression.startswith('symlink-'):
        target = root / 'another-plugin-tree' if regression == 'symlink-internal' else tmp_path / 'outside-plugin-tree'
        try:
            if regression == 'symlink-alias':
                target.symlink_to(plugins, target_is_directory=True)
                value = str(target)
            else:
                plugins.rename(target)
                plugins.symlink_to(target, target_is_directory=True)
        except OSError:
            pytest.skip('Native host does not permit creating the temporary symlink fixture')
    elif regression == 'unlisted-plugin':
        (plugins / 'platforms/extra-plugin.bin').write_bytes(b'uninventoried fixture')
    elif regression == 'missing-plugin':
        plugin.unlink()
    elif regression == 'missing-package-anchor':
        manifest['files'].pop(anchor.relative_to(root).as_posix())
        (root / 'bundle-manifest.json').write_text(json.dumps(manifest))
    elif regression == 'changed-plugin':
        plugin.write_bytes(b'changed plugin bytes')
    elif regression == 'changed-package-anchor':
        anchor.write_bytes(b'changed vendor code')
    key = 'QT_PLUGIN_PATH' if regression == 'other-qt-variable' else 'QT_QPA_PLATFORM_PLUGIN_PATH'
    report['environment'][key] = value
    destination = tmp_path / 'environment-report.json' if regression == 'initial-bootstrap' else tmp_path / 'dependency-isolation/process-1.json'
    destination.write_text(json.dumps(report))
    with pytest.raises(RuntimeError):
        check_environment_evidence(tmp_path, fixture, 'native', root)


def test_failed_clean_scenario_does_not_skip_contaminated_scenario(tmp_path, monkeypatch):
    from scripts import verify_portable_environment as verifier
    root = tmp_path / 'bundle'
    (root / 'scripts').mkdir(parents=True)
    shutil.copy2(Path(bootstrap.__file__).with_name('standalone_runtime_probe.py'), root / 'scripts/standalone_runtime_probe.py')
    (root / 'bundle-manifest.json').write_text(json.dumps({'format': 2, 'platform': 'linux', 'arch': 'x64', 'files': {}}))
    archive = tmp_path / 'candidate.zip'
    archive.write_bytes(b'fixture archive')
    archive.with_suffix('.zip.sha256').write_text(bootstrap.sha256(archive))
    monkeypatch.setattr(verifier, 'extract_checked', lambda *args: root)
    monkeypatch.setattr(verifier, 'host_platform', lambda: ('linux', 'x64'))
    calls = []
    def fail(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(91, command)
    monkeypatch.setattr(verifier.subprocess, 'run', fail)
    with pytest.raises(RuntimeError, match='scenario failed'):
        verifier.verify_archive(archive, tmp_path / 'results')
    assert len(calls) == 2
    report = json.loads((tmp_path / 'results/portable-environment-report.json').read_text())
    assert report['result'] == 'failed'
    assert [case['scenario'] for case in report['scenarios']] == list(verifier.SCENARIOS)
    assert all(case['result'] == 'failed' for case in report['scenarios'])


@pytest.mark.parametrize('platform,wrapper', [('linux', True), ('linux', False), ('macos', False), ('windows', False)])
def test_manifest_media_tools_replace_external_selectors(tmp_path, monkeypatch, platform, wrapper):
    inventory = media_inventory(tmp_path, platform, wrapper)
    env = {'VSE_VSF_BINARY': '/foreign/vsf', 'VSE_VSF_DIR': '/foreign', 'IMAGEIO_FFMPEG_EXE': '/foreign/ffmpeg'}
    monkeypatch.setattr(bootstrap.os, 'environ', env)
    bootstrap.configure_runtime_tools(tmp_path, {'platform': platform, 'arch': 'x64', 'files': inventory})
    expected = 'VideoSubFinderCli.exe' if platform == 'windows' else 'VideoSubFinderCli.run' if wrapper else 'VideoSubFinderCli'
    assert Path(env['VSE_VSF_BINARY']).name == expected
    assert Path(env['VSE_VSF_BINARY']).is_relative_to(tmp_path)
    assert Path(env['IMAGEIO_FFMPEG_EXE']).is_relative_to(tmp_path)
    assert Path(env['VSE_VSF_DIR']) == Path(env['VSE_VSF_BINARY']).parent


@pytest.mark.parametrize('problem', ['missing-vsf', 'missing-ffmpeg', 'ambiguous-ffmpeg', 'tampered', 'wrapper-without-inventory-binary', 'escape'])
def test_manifest_media_selection_fails_closed(tmp_path, monkeypatch, problem):
    inventory = media_inventory(tmp_path)
    ffmpeg = next(name for name in inventory if '/binaries/' in name)
    if problem == 'missing-vsf':
        inventory = {name: value for name, value in inventory.items() if 'VideoSubFinder' not in name}
    elif problem == 'missing-ffmpeg':
        inventory.pop(ffmpeg)
    elif problem == 'ambiguous-ffmpeg':
        inventory[ffmpeg + '-other'] = inventory[ffmpeg]
    elif problem == 'tampered':
        (tmp_path / ffmpeg).write_bytes(b'wrong executable')
    elif problem == 'wrapper-without-inventory-binary':
        inventory.pop('backend/subfinder/linux/x64/VideoSubFinderCli')
    else:
        inventory['runtime/../escape/imageio_ffmpeg/binaries/ffmpeg-other'] = inventory.pop(ffmpeg)
    env = {'IMAGEIO_FFMPEG_EXE': '/foreign/fallback'}
    monkeypatch.setattr(bootstrap.os, 'environ', env)
    with pytest.raises(RuntimeError):
        bootstrap.configure_runtime_tools(tmp_path, {'platform': 'linux', 'arch': 'x64', 'files': inventory})
    assert env['IMAGEIO_FFMPEG_EXE'] == '/foreign/fallback'  # No finder/fallback or partial configuration runs.


def test_opencv_search_selectors_removed_without_erasing_cache_or_data(tmp_path):
    source = {key: '/foreign/plugins' for key in ('OPENCV_VIDEOIO_PLUGIN_PATH', 'OPENCV_CORE_PLUGIN_PATH', 'OPENCV_DNN_PLUGIN_PATH', 'OPENCV_UI_PLUGIN_PATH', 'OPENCV_OPENCL_RUNTIME', 'OPENCV_FFMPEG_DLL_DIR', 'OPENCV_DNN_IE_EXTRA_PLUGIN_PATH',
                                                           'OPENCV_VIDEOIO_PLUGIN_FFMPEG', 'OPENCV_DNN_PLUGIN_INFERENCE_ENGINE', 'OPENCV_UI_PLUGIN_GTK', 'OPENCV_CORE_PARALLEL_PLUGIN_TBB')}
    source.update(OPENCV_TEMP_PATH='/user/cache', OPENCV_OPENCL_CACHE_DIR='/user/opencl-cache', OPENCV_DATA_PATH='/user/data')
    env = bootstrap.sanitize_environment(tmp_path, source, 'linux')
    assert '/foreign/plugins' not in env.values()
    assert env['OPENCV_TEMP_PATH'] == '/user/cache'
    assert env['OPENCV_OPENCL_CACHE_DIR'] == '/user/opencl-cache'
    assert env['OPENCV_DATA_PATH'] == '/user/data'


@pytest.mark.parametrize('target,profile,required', [
    ('windows', 'native', True), ('windows', 'paddle', False),
    ('linux', 'native', False), ('macos', 'native', False),
])
def test_final_archive_smoke_requires_worker_evidence_only_for_windows_native(tmp_path, monkeypatch, target, profile, required):
    from scripts import package_release, portable_python, standalone_runtime_probe, windows_os_evidence
    root = tmp_path / 'app'
    (root / 'scripts').mkdir(parents=True)
    shutil.copy2(standalone_runtime_probe.__file__, root / 'scripts/standalone_runtime_probe.py')
    (root / 'bundle-manifest.json').write_text(json.dumps({'format': 2, 'platform': target, 'arch': 'x64',
        'runtime_python': 'runtime/python', 'files': {}, 'ocr_backend': profile}))
    archive = tmp_path / 'candidate.zip'
    archive.write_bytes(b'fixture archive')
    archive.with_suffix('.zip.sha256').write_text(bootstrap.sha256(archive))
    monkeypatch.setattr(package_release, 'extract_checked', lambda *args: root)
    monkeypatch.setattr(package_release, 'verification_path', lambda *args: str(tmp_path / 'os-tools'))
    monkeypatch.setattr(package_release.subprocess, 'run', lambda *args, **kwargs: None)
    monkeypatch.setattr(portable_python, 'launcher_command', lambda *args: ['fixture-launcher'])
    monkeypatch.setattr(standalone_runtime_probe, 'audit_windows_executables', lambda *args: None)
    monkeypatch.setattr(standalone_runtime_probe, 'audit_macos_executables', lambda *args: None)
    # Exercise Windows routing on every test host. This is an inert archive and
    # launcher fixture; it must not start the real OS evidence subprocess.
    monkeypatch.setattr(standalone_runtime_probe.platform, 'system',
                        lambda: {'windows': 'Windows', 'linux': 'Linux', 'macos': 'Darwin'}[target])
    baselines = []
    def baseline(paths=()):
        assert not paths
        baselines.append(True)
        return {'schema': 1, 'registry_modified': False, 'files': [], 'errors': ['unit routing fixture only']}
    monkeypatch.setattr(windows_os_evidence, 'collect_windows_security_snapshot', baseline)
    deployment_calls = []
    # This fixture has no real executable/ZIP. Actual Mach-O/plist consistency
    # is exercised separately by test_native_package and the parser tests.
    monkeypatch.setattr(package_release, 'verify_macos_deployment',
                        lambda *args: deployment_calls.append(args) or {'scope': 'unit-routing-only'})
    calls = []
    monkeypatch.setattr(standalone_runtime_probe, 'summarize_reports', lambda *args, **kwargs: calls.append(kwargs))
    package_release.verify_bundle(archive, True)
    assert len(deployment_calls) == int(target == 'macos')
    assert len(calls) == 1
    assert calls[0]['require_native_workers'] is required
    assert calls[0]['require_isolated'] is (target == 'windows')
    assert len(baselines) == int(target == 'windows')


@pytest.mark.parametrize('profile,required', [('native', True), ('paddle', False)])
def test_every_windows_launcher_scenario_requires_native_worker_records(tmp_path, monkeypatch, profile, required):
    from scripts import verify_portable_environment as verifier, standalone_runtime_probe, windows_os_evidence
    root = tmp_path / 'app'
    (root / 'scripts').mkdir(parents=True)
    shutil.copy2(standalone_runtime_probe.__file__, root / 'scripts/standalone_runtime_probe.py')
    (root / 'bundle-manifest.json').write_text(json.dumps({'format': 2, 'platform': 'windows', 'arch': 'x64',
        'runtime_python': 'runtime/python.exe', 'files': {}, 'ocr_backend': profile}))
    archive = tmp_path / 'candidate.zip'
    archive.write_bytes(b'fixture archive')
    archive.with_suffix('.zip.sha256').write_text(bootstrap.sha256(archive))
    monkeypatch.setattr(verifier, 'extract_checked', lambda *args: root)
    monkeypatch.setattr(verifier, 'host_platform', lambda: ('windows', 'x64'))
    monkeypatch.setattr(verifier, 'fixture_environment', lambda *args: ({}, {'cwd': str(tmp_path)}))
    monkeypatch.setattr(verifier, 'launcher_command', lambda *args: ['fixture-launcher'])
    monkeypatch.setattr(verifier, 'audit_windows_executables', lambda *args: None)
    monkeypatch.setattr(verifier, 'check_environment_evidence', lambda *args: None)
    monkeypatch.setattr(verifier.subprocess, 'run', lambda *args, **kwargs: None)
    monkeypatch.setattr(standalone_runtime_probe.platform, 'system', lambda: 'Windows')
    baselines = []
    def baseline(paths=()):
        assert not paths
        baselines.append(True)
        return {'schema': 1, 'registry_modified': False, 'files': [], 'errors': ['unit routing fixture only']}
    monkeypatch.setattr(windows_os_evidence, 'collect_windows_security_snapshot', baseline)
    calls = []
    def summarize(*args, **kwargs):
        calls.append(kwargs)
        return {'passed': True}
    monkeypatch.setattr(verifier, 'summarize_reports', summarize)
    report = verifier.verify_archive(archive, tmp_path / 'results')
    assert report['result'] == 'passed'
    assert len(calls) == len(verifier.SCENARIOS)
    assert all(call['require_native_workers'] is required and call['require_isolated'] for call in calls)
    assert len(baselines) == len(verifier.SCENARIOS)


@pytest.mark.parametrize('foreign', [False, True])
def test_paddle_child_model_root_cannot_escape_final_bundle(tmp_path, foreign):
    root, fixture, report = valid_environment_report(tmp_path)
    (root / 'backend/models').mkdir(parents=True)
    report['profile'] = report['environment']['VSE_OCR_BACKEND'] = 'paddle'
    report['environment']['VSE_MODEL_DIR'] = str(root / 'backend/models')
    (tmp_path / 'environment-report.json').write_text(json.dumps(report))
    if foreign:
        report['environment']['VSE_MODEL_DIR'] = str(tmp_path)
    (tmp_path / 'dependency-isolation/process-1.json').write_text(json.dumps(report))
    if foreign:
        with pytest.raises(RuntimeError, match='Paddle model path'):
            check_environment_evidence(tmp_path, fixture, 'paddle', root)
    else:
        check_environment_evidence(tmp_path, fixture, 'paddle', root)
