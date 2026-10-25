"""Build-time Windows path locking and trusted, inherited startup instrumentation."""
import json
import base64
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
from types import SimpleNamespace

import pytest

from scripts import portable_python as portable
from scripts import bundle_bootstrap as bootstrap
from scripts import standalone_runtime_probe as probe
from scripts import verify_portable_environment as verifier
from scripts import windows_app_local_runtime as app_local


def stage_vcomp(root, content=b'fixture DLL bytes'):
    path = root / app_local.DLL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    metadata = path.parent.parent / 'shiboken6-6.12.0.dist-info'
    metadata.mkdir()
    (metadata / 'METADATA').write_text('Name: shiboken6\nVersion: 6.12.0\n')
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip('=')
    (metadata / 'RECORD').write_text(f'shiboken6/vcomp140.dll,sha256={digest},{len(content)}\n')
    scripts = root / 'scripts'
    scripts.mkdir(exist_ok=True)
    shutil.copy2(app_local.__file__, scripts / 'windows_app_local_runtime.py')


def windows_runtime(root):
    runtime = root / 'runtime'
    (runtime / 'Lib/site-packages').mkdir(parents=True)
    for name in ('python.exe', 'python312.dll', 'Lib/os.py'):
        (runtime / name).write_bytes(b'fixture publisher file')
    stage_vcomp(root)
    return runtime / 'python.exe'


def test_windows_path_config_and_owned_hook_are_relative_and_inventoriable(tmp_path):
    python = windows_runtime(tmp_path)
    report = portable.write_windows_runtime_isolation(tmp_path, python)
    assert (tmp_path / report['path_config']).read_text() == portable.WINDOWS_ISOLATED_PATHS
    assert (tmp_path / report['startup_hook']).read_bytes() == portable.WINDOWS_SITE_CUSTOMIZE
    assert report['path_config_sha256'] == bootstrap.sha256(tmp_path / report['path_config'])
    assert report['startup_hook_sha256'] == bootstrap.sha256(tmp_path / report['startup_hook'])
    assert report['registry_writes'] is False
    assert report['external_hook_execution'] is False
    paths = portable.WINDOWS_ISOLATED_PATHS.splitlines()
    assert '..' in paths and 'Lib/site-packages' in paths and paths[-1] == 'import site'
    assert all(not Path(path).is_absolute() for path in paths[:-1])
    assert b'from scripts.bundle_bootstrap import enable_trusted_audit' in portable.WINDOWS_SITE_CUSTOMIZE
    assert portable.WINDOWS_SITE_CUSTOMIZE.index(b'preload_runtime(') < portable.WINDOWS_SITE_CUSTOMIZE.index(b'enable_trusted_audit(')
    assert b'_os._exit(92)' in portable.WINDOWS_SITE_CUSTOMIZE
    assert b'exec(' not in portable.WINDOWS_SITE_CUSTOMIZE


@pytest.mark.parametrize('occupied', ['python312._pth', 'python._pth', 'Lib/site-packages/sitecustomize.py', 'windows-app-local-runtime.json'])
def test_refuses_to_overwrite_foreign_path_config_or_hook(tmp_path, occupied):
    python = windows_runtime(tmp_path)
    path = tmp_path / 'runtime' / occupied
    path.write_text('publisher or third-party file')
    with pytest.raises(ValueError, match='Refusing to replace'):
        portable.write_windows_runtime_isolation(tmp_path, python)
    assert path.read_text() == 'publisher or third-party file'


def test_no_windows_configuration_before_pip_or_after_prune_inventory(tmp_path, monkeypatch):
    from scripts import package_release
    events = []
    def prepare(destination, target_platform, target_arch):
        python = windows_runtime(destination.parent)
        return {'python': str(python), 'version': '3.12.15'}
    def run(command, **kwargs):
        is_pip = 'pip' in command
        configured = (tmp_path / 'runtime/python312._pth').exists()
        if is_pip:
            assert not configured
            events.append('pip')
        elif '--apply' in command:
            assert configured
            assert (tmp_path / 'runtime/Lib/site-packages/sitecustomize.py').is_file()
            events.append('prune')
        else:
            assert configured
            assert command[1:3] == ['-m', 'scripts.runtime_dependencies']
            assert kwargs['cwd'] == tmp_path
            events.append('dependencies')
    monkeypatch.setattr(portable, 'prepare_runtime', prepare)
    monkeypatch.setattr(package_release, 'finalize_portable_runtime', lambda *args: {})
    monkeypatch.setattr(package_release.subprocess, 'run', run)
    result = package_release.build_runtime(tmp_path, [], [], 'windows', 'x64', 'native')
    assert events == ['pip', 'prune', 'dependencies']
    assert result['windows_isolation']['registry_writes'] is False


def _copy_for_startup_fixture(root):
    """Use existing interpreter files only; no registry/global install changes.

    Standard library paths intentionally refer to the source installation; this
    is a startup mechanism test, NOT self-contained runtime acceptance.
    """
    runtime = root / 'runtime'
    hookdir = runtime / 'Lib/site-packages'
    hookdir.mkdir(parents=True)
    (root / 'scripts').mkdir()
    for module in (bootstrap, probe):
        shutil.copy2(module.__file__, root / 'scripts' / Path(module.__file__).name)
    (hookdir / 'sitecustomize.py').write_bytes(portable.WINDOWS_SITE_CUSTOMIZE)
    original = Path(getattr(sys, '_base_executable', sys.executable)).resolve()
    if os.name == 'nt':
        from importlib.metadata import distribution
        original_vcomp = Path(distribution('shiboken6').locate_file('shiboken6/vcomp140.dll'))
        stage_vcomp(root, original_vcomp.read_bytes())
        app_local.write_runtime_metadata(root)
        python = runtime / 'python.exe'
        shutil.copy2(original, python)
        for pattern in ('python*.dll', 'vcruntime*.dll'):
            for path in original.parent.glob(pattern):
                shutil.copy2(path, runtime / path.name)
        pth = runtime / 'python312._pth'
        extensions = Path(sys.base_prefix) / 'DLLs'
    else:
        python = runtime / 'python'
        python.symlink_to(original)
        pth = runtime / 'python._pth'
        extensions = Path(sysconfig.get_path('stdlib')) / 'lib-dynload'
    pth.write_text('\n'.join(map(str, (Path(sysconfig.get_path('stdlib')), extensions, hookdir, root))) + '\nimport site\n')
    return python


def test_native_pth_plain_children_and_spawn_keep_trusted_audit_and_utf8(tmp_path):
    root = tmp_path / 'bundle 空间 & bang!'
    python = _copy_for_startup_fixture(root)
    evidence = tmp_path / 'evidence'
    prepared = probe.audit_environment(root, evidence)
    foreign = tmp_path / 'foreign'
    foreign.mkdir()
    marker = tmp_path / 'FOREIGN_EXECUTED'
    (foreign / 'sitecustomize.py').write_text('from pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\n')
    env = bootstrap.sanitize_environment(root, dict(os.environ, PYTHONUTF8='0', PYTHONIOENCODING='ascii:strict'), 'windows' if os.name == 'nt' else 'linux')
    env.update(prepared, PYTHONHOME=str(foreign), PYTHONPATH=str(foreign), VSE_RUNTIME_AUDIT_ACTIVE='portable-bootstrap-v1')
    application = root / 'application.py'
    application.write_text('''import json, multiprocessing, subprocess, sys
from scripts.bundle_bootstrap import environment_report

def spawned():
    print('spawn worker 字幕', flush=True)

if __name__ == '__main__':
    print('parent 字幕 café 日本語', flush=True)
    subprocess.run([sys.executable, '-c', 'import ctypes; print("plain child 字幕")'], check=True)
    process = multiprocessing.get_context('spawn').Process(target=spawned)
    process.start(); process.join(15)
    assert process.exitcode == 0, process.exitcode
''', encoding='utf-8')
    result = subprocess.run([str(python), str(application)], cwd=tmp_path, env=env, capture_output=True, timeout=30, check=True)
    assert 'parent 字幕 café 日本語' in result.stdout.decode('utf-8')
    assert 'plain child 字幕' in result.stdout.decode('utf-8')
    assert 'spawn worker 字幕' in result.stdout.decode('utf-8')
    assert not marker.exists()
    reports = [json.loads(path.read_text()) for path in evidence.glob('process-*.json')]
    assert len(reports) >= 3
    for report in reports:
        assert report['isolated'] == report['ignore_environment'] == report['no_user_site'] == report['utf8_mode'] == 1
        assert report['user_site_enabled'] is False
        assert str(foreign) not in report['sys_path']
        assert report['modules']['sitecustomize'] == str(root / 'runtime/Lib/site-packages/sitecustomize.py')
        assert report['modules']['scripts.standalone_runtime_probe'] == str(root / 'scripts/standalone_runtime_probe.py')
    # This source-interpreter bridge must still fail strict self-contained
    # acceptance: its standard library paths are deliberately outside root.
    with pytest.raises(RuntimeError, match='external Python search path'):
        probe.summarize_reports(root, evidence, require_isolated=True)


def test_owned_hook_fails_closed_instead_of_site_swallowing_audit_error(tmp_path):
    root = tmp_path / 'bundle'
    python = _copy_for_startup_fixture(root)
    env = bootstrap.sanitize_environment(root, dict(os.environ), 'windows' if os.name == 'nt' else 'linux')
    env.update(VSE_RUNTIME_AUDIT_ACTIVE='portable-bootstrap-v1', VSE_RUNTIME_AUDIT_ROOT=str(tmp_path / 'wrong-root'), VSE_RUNTIME_AUDIT_REPORTS=str(tmp_path / 'reports'))
    result = subprocess.run([str(python), '-c', 'print("MUST_NOT_RUN")'], cwd=tmp_path, env=env, capture_output=True, timeout=15)
    assert result.returncode == 91
    assert b'MUST_NOT_RUN' not in result.stdout
    assert b'Trusted portable startup audit failed' in result.stderr


def test_isolated_native_dependency_audit_uses_trusted_package_not_script_search_path(tmp_path):
    """Real ._pth entrypoint/import regression; inference packages are inert fixtures."""
    import packaging
    from scripts import native_package, runtime_dependencies
    root = tmp_path / 'bundle 字幕 & spaces'
    python = _copy_for_startup_fixture(root)
    packages = root / 'runtime/Lib/site-packages'
    shutil.copytree(Path(packaging.__file__).parent, packages / 'packaging',
                    ignore=shutil.ignore_patterns('__pycache__'))
    for module in (native_package, runtime_dependencies):
        shutil.copy2(module.__file__, root / 'scripts' / Path(module.__file__).name)
    for name in ('pyside6_essentials', 'shiboken6'):
        metadata = packages / f'{name}-6.12.0.dist-info'
        metadata.mkdir(exist_ok=True)
        (metadata / 'METADATA').write_text(f'Name: {name}\nVersion: 6.12.0\n', encoding='utf-8')
    (packages / 'cv2.py').write_text('\n'.join(f'{name} = object()' for name in (
        'VideoCapture', 'VideoWriter', 'cvtColor', 'findContours', 'minAreaRect')), encoding='utf-8')
    (packages / 'PySide6').mkdir()
    for name in ('__init__', 'QtCore', 'QtGui', 'QtWidgets', 'QtSvg', 'QtSvgWidgets', 'QtXml'):
        (packages / 'PySide6' / f'{name}.py').write_text('# inert API-import fixture\n', encoding='utf-8')
    if sys.platform == 'darwin':
        for name in ('Cocoa', 'objc'):
            (packages / f'{name}.py').write_text('# inert platform-import fixture\n', encoding='utf-8')
        (packages / 'Quartz').mkdir()
        for name in ('__init__', 'CoreGraphics'):
            (packages / 'Quartz' / f'{name}.py').write_text('# inert platform-import fixture\n', encoding='utf-8')
    manifest = root / 'runtime/runtime-profile.json'
    manifest.write_text(json.dumps({'ocr_backend': 'native', 'runtime_requirements': [
        'pyside6-essentials==6.12.0', 'shiboken6==6.12.0']}), encoding='utf-8')
    foreign = tmp_path / 'unrelated-cwd'
    foreign.mkdir()
    marker = tmp_path / 'FOREIGN_HELPER_EXECUTED'
    (foreign / 'native_package.py').write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\n', encoding='utf-8')
    audit = tmp_path / 'audit'
    env = bootstrap.sanitize_environment(root, dict(os.environ), 'windows' if os.name == 'nt' else 'linux')
    env.update(probe.audit_environment(root, audit), VSE_RUNTIME_AUDIT_ACTIVE='portable-bootstrap-v1', PYTHONPATH=str(foreign))
    arguments = ['--manifest', str(manifest)]
    # Preserve the exact failure mode: direct scripts get neither __package__
    # nor an implicit scripts/ sys.path entry under the locked runtime.
    direct = subprocess.run([str(python), str(root / 'scripts/runtime_dependencies.py'), *arguments],
                            cwd=foreign, env=env, capture_output=True, timeout=30)
    assert direct.returncode != 0 and b"No module named 'native_package'" in direct.stderr
    result = subprocess.run([str(python), '-m', 'scripts.runtime_dependencies', *arguments],
                            cwd=foreign, env=env, capture_output=True, timeout=30, check=True)
    assert b'Inference dependencies verified' in result.stdout
    assert not marker.exists()
    reports = [json.loads(path.read_text(encoding='utf-8')) for path in audit.glob('process-*.json')]
    assert len(reports) == 2
    assert all(report['isolated'] == report['ignore_environment'] == 1 for report in reports)
    assert all(str(foreign) not in report['sys_path'] and str(root / 'scripts') not in report['sys_path'] for report in reports)
    assert any(report['modules'].get('scripts.native_package') == str(root / 'scripts/native_package.py') for report in reports)
    # Source-mode direct invocation remains supported without modifying its
    # documented CLI or importing a host dependency into the isolated runtime.
    source_env = bootstrap.sanitize_environment(root, dict(os.environ), 'windows' if os.name == 'nt' else 'linux')
    source_env['PYTHONPATH'] = str(packages)
    source = subprocess.run([sys.executable, '-S', str(root / 'scripts/runtime_dependencies.py'), *arguments],
                            cwd=foreign, env=source_env, capture_output=True, timeout=30, check=True)
    assert b'Inference dependencies verified' in source.stdout
    forbidden = packages / 'paddlepaddle-3.3.1.dist-info'
    forbidden.mkdir()
    (forbidden / 'METADATA').write_text('Name: paddlepaddle\nVersion: 3.3.1\n', encoding='utf-8')
    rejected = subprocess.run([str(python), '-m', 'scripts.runtime_dependencies', *arguments],
                              cwd=foreign, env=env, capture_output=True, timeout=30)
    assert rejected.returncode != 0
    assert b'Native GUI runtime contains forbidden inference stacks' in rejected.stderr


@pytest.mark.parametrize('entry', [
    'prune_inference_runtime.py', 'windows_verification.py', 'install_videosubfinder.py',
    'e2e_smoke.py', 'e2e_issues.py', 'e2e_multiline.py', 'e2e_av1.py', 'e2e_timeline.py',
])
def test_other_isolated_direct_script_children_have_resolvable_entry_imports(tmp_path, entry):
    """Cover remaining build/bootstrap/E2E script imports, not native workloads."""
    root = tmp_path / 'bundle'
    python = _copy_for_startup_fixture(root)
    repository = Path(portable.__file__).resolve().parents[1]
    shutil.copytree(repository / 'scripts', root / 'scripts', dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__'))
    for relative in ('backend/__init__.py', 'backend/tools/__init__.py', 'backend/tools/videosubfinder.py'):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repository / relative, destination)
    # AV1 imports this optional workload dependency before parsing --help;
    # no encoding is exercised by this entrypoint/import-only fixture.
    (root / 'runtime/Lib/site-packages/imageio_ffmpeg.py').write_text('# inert import fixture\n', encoding='utf-8')
    env = bootstrap.sanitize_environment(root, dict(os.environ), 'windows' if os.name == 'nt' else 'linux')
    result = subprocess.run([str(python), str(root / 'scripts' / entry), '--help'],
                            cwd=tmp_path, env=env, capture_output=True, timeout=30, check=True)
    assert b'usage:' in result.stdout.lower()


def test_bootstrap_native_dependency_child_keeps_package_module_entry():
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(bootstrap.main))
    commands = [node.args[0] for node in ast.walk(tree) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute) and node.func.attr == 'run'
                and node.args and isinstance(node.args[0], ast.List)]
    values = [[item.value if isinstance(item, ast.Constant) else None for item in command.elts]
              for command in commands]
    assert sum(command[1:3] == ['-m', 'scripts.runtime_dependencies'] for command in values) == 1
    assert all('scripts/runtime_dependencies.py' not in command for command in values)


def test_registry_inventory_is_read_only_and_distinguishes_missing_from_denied(monkeypatch):
    calls = []
    class Key:
        def __enter__(self): return self
        def __exit__(self, *args): pass
    class End(OSError):
        winerror = 259
    def open_key(hive, name, reserved, access):
        calls.append((hive, name, reserved, access))
        if hive == 'LM': raise PermissionError('read denied fixture')
        return Key()
    def enumerate_key(key, index):
        if index == 0: return 'InstalledExtension'
        raise End('done')
    fake = SimpleNamespace(HKEY_CURRENT_USER='CU', HKEY_LOCAL_MACHINE='LM', KEY_READ=0x20019,
                           OpenKeyEx=open_key, EnumKey=enumerate_key, QueryValue=lambda key, name: 'C:\\ExistingPython\\' + (name or 'base'))
    monkeypatch.setitem(sys.modules, 'winreg', fake)
    monkeypatch.setattr(verifier.sys, 'platform', 'win32')
    result = verifier.existing_windows_python_registry()
    assert result['registry_modified'] is False and result['injected_fixture'] is False
    assert len(result['entries']) == 2
    assert result['errors'] == [{'hive': 'HKLM', 'error': 'read denied fixture'}]
    assert all(call[-1] == fake.KEY_READ for call in calls)
    assert len(calls) == 2


def test_windows_bootstrap_rejects_missing_or_modified_isolation_files(tmp_path):
    python = windows_runtime(tmp_path)
    metadata = portable.write_windows_runtime_isolation(tmp_path, python)
    inventory = {metadata['path_config']: metadata['path_config_sha256'], metadata['startup_hook']: metadata['startup_hook_sha256'],
                 app_local.METADATA: bootstrap.sha256(tmp_path / app_local.METADATA),
                 'scripts/windows_app_local_runtime.py': bootstrap.sha256(tmp_path / 'scripts/windows_app_local_runtime.py')}
    bootstrap.verify_windows_runtime_configuration(tmp_path, {'files': inventory})
    with pytest.raises(RuntimeError, match='lacks its isolated runtime configuration'):
        bootstrap.verify_windows_runtime_configuration(tmp_path, {'files': {}})
    (tmp_path / metadata['path_config']).write_text('C:/ForeignPython\nimport site\n')
    with pytest.raises(RuntimeError, match='checksum mismatch'):
        bootstrap.verify_windows_runtime_configuration(tmp_path, {'files': inventory})


@pytest.mark.parametrize('regression', ['isolated', 'ignore-environment', 'user-site', 'foreign-path', 'missing-paths'])
def test_required_isolation_report_gate_never_accepts_a_missing_control(tmp_path, regression):
    bundle = tmp_path / 'bundle'
    (bundle / 'scripts').mkdir(parents=True)
    shutil.copy2(probe.__file__, bundle / 'scripts/standalone_runtime_probe.py')
    evidence = tmp_path / 'reports'
    probe.audit_environment(bundle, evidence)
    report = {'pid': 1, 'system': 'Windows', 'windows_root': r'C:\Windows', 'executable': str(bundle / 'runtime/python.exe'),
              'prefix': str(bundle / 'runtime'), 'probe_errors': [], 'images': [str(bundle / 'runtime/python312.dll')],
              'snapshots': 1, 'modules': {}, 'isolated': 1, 'ignore_environment': 1, 'no_user_site': 1,
              'sys_path': [str(bundle), str(bundle / 'runtime/Lib')]}
    (evidence / 'process-1.json').write_text(json.dumps(report))
    assert probe.summarize_reports(bundle, evidence, require_isolated=True)['passed']
    if regression == 'isolated': report['isolated'] = 0
    elif regression == 'ignore-environment': report['ignore_environment'] = 0
    elif regression == 'user-site': report['no_user_site'] = 0
    elif regression == 'foreign-path': report['sys_path'].append(str(tmp_path / 'existing-system-python'))
    else: report.pop('sys_path')
    (evidence / 'process-1.json').write_text(json.dumps(report))
    with pytest.raises(RuntimeError):
        probe.summarize_reports(bundle, evidence, require_isolated=True)


@pytest.mark.parametrize('regression', ['no-evidence', 'wrong-hash', 'foreign-path', 'unheld-handle', 'missing-image'])
def test_packaged_windows_processes_require_authenticated_vcomp_evidence(tmp_path, regression):
    bundle = tmp_path / 'bundle'
    windows_runtime(bundle)
    metadata = app_local.write_runtime_metadata(bundle)
    shutil.copy2(probe.__file__, bundle / 'scripts/standalone_runtime_probe.py')
    evidence = tmp_path / 'reports'
    probe.audit_environment(bundle, evidence)
    dll = str(bundle / app_local.DLL)
    preload = {'path': dll, 'sha256': metadata['sha256'], 'loaded_paths': [dll], 'retained_handle': True}
    report = {'pid': 1, 'system': 'Windows', 'windows_root': r'C:\Windows', 'executable': str(bundle / 'runtime/python.exe'),
              'prefix': str(bundle / 'runtime'), 'probe_errors': [], 'images': [dll], 'snapshots': 1, 'modules': {},
              'isolated': 1, 'ignore_environment': 1, 'no_user_site': 1, 'sys_path': [str(bundle)],
              'windows_app_local_runtime': preload}
    file = evidence / 'process-1.json'
    file.write_text(json.dumps(report))
    assert probe.summarize_reports(bundle, evidence, require_isolated=True)['passed']
    if regression == 'no-evidence': report.pop('windows_app_local_runtime')
    elif regression == 'wrong-hash': preload['sha256'] = '0' * 64
    elif regression == 'foreign-path': preload['loaded_paths'] = [r'C:\Windows\System32\vcomp140.dll']
    elif regression == 'unheld-handle': preload['retained_handle'] = False
    else: report['images'] = [str(bundle / 'runtime/python312.dll')]
    file.write_text(json.dumps(report))
    with pytest.raises(RuntimeError, match='missing authenticated app-local VCOMP startup evidence'):
        probe.summarize_reports(bundle, evidence, require_isolated=True)
