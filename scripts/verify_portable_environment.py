#!/usr/bin/env python3
"""Accept final portable ZIPs through their real launchers under child-only envs.

No installs, downloads, system environment changes, or foreign native code.
'clean-host-path' means the invoking child PATH, not an uninstalled host Python.
Conda/venv contamination is explicitly simulated, not an installed-Conda test.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

try:
    from .bundle_bootstrap import NATIVE_SEARCH_VARIABLES, RUNTIME_SELECTOR_PREFIXES, host_platform, sha256, verify_payload
    from .package_release import extract_checked
    from .portable_python import launcher_command
    from .standalone_runtime_probe import audit_environment, summarize_reports, audit_windows_executables, audit_macos_executables
except ImportError:
    from bundle_bootstrap import NATIVE_SEARCH_VARIABLES, RUNTIME_SELECTOR_PREFIXES, host_platform, sha256, verify_payload
    from package_release import extract_checked
    from portable_python import launcher_command
    from standalone_runtime_probe import audit_environment, summarize_reports, audit_windows_executables, audit_macos_executables

SCENARIOS = ('clean-host-path', 'contaminated-host-environment')
LIMITS = [
    'Conda/venv are simulated environment/fixture directories, not genuinely installed distributions.',
    'Windows registry inventory is read-only existing-host evidence; no registry injection fixture or registry mutation is performed.',
    'clean-host-path removes host tools from the incoming child PATH; it does not uninstall host Python/Qt. Linux stock-container acceptance is a separate gate.',
    'Only child environments and temporary fixtures change; no global OS settings or installations are modified.',
    'Native path fixtures contain inert text markers, never executable foreign DLLs/shared libraries.',
    'No claim against a malicious/broken OS loader or LD_PRELOAD that runs before /bin/sh itself can sanitize its child.',
    'macOS app route executes Contents/MacOS/VSE; Finder/LaunchServices/Gatekeeper and a signed distribution require separate validation.',
]


def existing_windows_python_registry() -> dict:
    """Read only the registry branch CPython 3.12 would otherwise consult."""
    if sys.platform != 'win32':
        return {'applicable': False, 'registry_modified': False}
    import winreg
    key_name = r'SOFTWARE\Python\PythonCore\3.12\PythonPath'
    result = {'applicable': True, 'key': key_name, 'registry_modified': False,
              'injected_fixture': False, 'entries': [], 'errors': []}
    for label, hive in (('HKCU', winreg.HKEY_CURRENT_USER), ('HKLM', winreg.HKEY_LOCAL_MACHINE)):
        try:
            with winreg.OpenKeyEx(hive, key_name, 0, winreg.KEY_READ) as key:
                try:
                    value = winreg.QueryValue(key, None)
                    result['entries'].append({'hive': label, 'subkey': None, 'value': value})
                except FileNotFoundError:
                    pass
                index = 0
                while True:
                    try:
                        child = winreg.EnumKey(key, index)
                    except OSError as error:
                        if getattr(error, 'winerror', None) == 259:  # ERROR_NO_MORE_ITEMS
                            break
                        raise
                    result['entries'].append({'hive': label, 'subkey': child, 'value': winreg.QueryValue(key, child)})
                    index += 1
        except FileNotFoundError:
            continue
        except OSError as error:
            result['errors'].append({'hive': label, 'error': str(error)})
    return result


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def fixture_environment(directory: Path, contaminated: bool, inherited: dict[str, str] | None = None) -> tuple[dict[str, str], dict]:
    """Build inert hostile-looking search paths without touching host installs."""
    directory.mkdir(parents=True)
    env = dict(os.environ if inherited is None else inherited)
    for key in list(env):
        if key.upper().startswith(RUNTIME_SELECTOR_PREFIXES) or key.upper() in {*(name.upper() for name in NATIVE_SEARCH_VARIABLES), 'VIRTUAL_ENV', 'VIRTUAL_ENV_PROMPT', '_CE_CONDA', '_CE_M', '__PYVENV_LAUNCHER__'}:
            env.pop(key)
    paths = {name: directory / name for name in ('home', 'empty-path', 'foreign-bin', 'foreign-python', 'foreign-native', 'foreign-qt', 'user-base', 'config', 'cache', 'work', 'cwd', 'conda', 'venv')}
    for path in paths.values():
        path.mkdir()
    config = paths['config'] / 'preferences.json'
    config.write_text('{}\n')
    marker = directory / 'FOREIGN_CODE_EXECUTED'
    canary = ('from pathlib import Path\n' + f'Path({str(marker)!r}).write_text("foreign Python selected")\n' + 'raise RuntimeError("Foreign Python fixture must never execute")\n')
    user_sites = [paths['user-base'] / 'lib/python3.12/site-packages', paths['user-base'] / 'Python312/site-packages',
                  paths['home'] / '.local/lib/python3.12/site-packages', paths['home'] / 'Library/Python/3.12/lib/python/site-packages']
    for pythonpath in (paths['foreign-python'], paths['cwd'], *user_sites):
        pythonpath.mkdir(parents=True, exist_ok=True)
        for name in ('sitecustomize.py', 'usercustomize.py', 'cv2.py', 'numpy.py', 'PySide6.py', 'paddle.py', 'backend.py'):
            (pythonpath / name).write_text(canary)
    for name in ('Qt6Core.dll', 'python312.dll', 'libQt6Core.so.6', 'libQt6Core.6.dylib', 'libstdc++.so.6', 'foreign-opencl.dll'):
        (paths['foreign-native'] / name).write_text('INERT PATH MARKER: NOT A NATIVE LIBRARY\n')
    for name in ('python', 'python3', 'pip', 'pip3', 'conda', 'ffmpeg', 'VideoSubFinderCli'):
        path = paths['foreign-bin'] / name
        path.write_text('#!/bin/sh\nprintf "foreign command selected" > "' + str(marker) + '"\nexit 93\n')
        path.chmod(0o755)
        (paths['foreign-bin'] / (name + '.cmd')).write_text('@echo foreign command selected > "' + str(marker) + '"\r\n@exit /b 93\r\n')
    env.update(HOME=str(paths['home']), USERPROFILE=str(paths['home']),
               APPDATA=str(paths['home'] / 'AppData/Roaming'), LOCALAPPDATA=str(paths['home'] / 'AppData/Local'),
               PATH=str(paths['empty-path']), XDG_CONFIG_HOME=str(paths['config']), XDG_CACHE_HOME=str(paths['cache']),
               VSE_CONFIG_FILE=str(config), VSE_WORK_DIR=str(paths['work']), VSE_OFFLINE='1',
               PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK='True', PADDLE_PDX_CACHE_HOME=str(paths['cache'] / 'paddlex'))
    injected = {}
    if contaminated:
        injected = dict(PATH=os.pathsep.join(str(paths[name]) for name in ('foreign-bin', 'foreign-native', 'conda', 'venv')),
                        PYTHONHOME=str(paths['foreign-python']), PYTHONPATH=str(paths['foreign-python']),
                        PYTHONEXECUTABLE=str(paths['foreign-bin'] / 'python'), __PYVENV_LAUNCHER__=str(paths['foreign-bin'] / 'python'),
                        PYTHONUSERBASE=str(paths['user-base']), PYTHONSTARTUP=str(paths['foreign-python'] / 'sitecustomize.py'),
                        PYTHONWARNINGS='error', PYTHONINSPECT='1', PYTHONUTF8='0', PYTHONIOENCODING='ascii:strict', CONDA_PREFIX=str(paths['conda']), CONDA_DEFAULT_ENV='fixture-not-installed',
                        CONDA_SHLVL='1', VIRTUAL_ENV=str(paths['venv']), VIRTUAL_ENV_PROMPT='fixture-not-installed',
                        QT_PLUGIN_PATH=str(paths['foreign-qt']), QT_QPA_PLATFORM_PLUGIN_PATH=str(paths['foreign-qt']),
                        QML2_IMPORT_PATH=str(paths['foreign-qt']), QML_IMPORT_PATH=str(paths['foreign-qt']),
                        OPENCV_VIDEOIO_PLUGIN_PATH=str(paths['foreign-native']), OPENCV_CORE_PLUGIN_PATH=str(paths['foreign-native']),
                        OPENCV_DNN_PLUGIN_PATH=str(paths['foreign-native']), OPENCV_UI_PLUGIN_PATH=str(paths['foreign-native']),
                        OPENCV_OPENCL_RUNTIME=str(paths['foreign-native'] / 'foreign-opencl.dll'),
                        OPENCV_FFMPEG_DLL_DIR=str(paths['foreign-native']), OPENCV_DNN_IE_EXTRA_PLUGIN_PATH=str(paths['foreign-native']),
                        OPENCV_VIDEOIO_PLUGIN_FFMPEG=str(paths['foreign-native'] / 'foreign-ffmpeg.dll'),
                        OPENCV_CORE_PARALLEL_PLUGIN_TBB=str(paths['foreign-native'] / 'foreign-tbb.dll'),
                        VSE_VSF_BINARY=str(paths['foreign-bin'] / ('VideoSubFinderCli.cmd' if os.name == 'nt' else 'VideoSubFinderCli')),
                        VSE_VSF_DIR=str(paths['foreign-bin']),
                        IMAGEIO_FFMPEG_EXE=str(paths['foreign-bin'] / ('ffmpeg.cmd' if os.name == 'nt' else 'ffmpeg')),
                        LD_LIBRARY_PATH=str(paths['foreign-native']), DYLD_LIBRARY_PATH=str(paths['foreign-native']),
                        DYLD_FRAMEWORK_PATH=str(paths['foreign-native']), DYLD_FALLBACK_LIBRARY_PATH=str(paths['foreign-native']),
                        VSE_OCR_BACKEND='deliberately-invalid-inherited-selection', VSE_NATIVE_OCR_ROOT=str(paths['foreign-native']),
                        VSE_DIRECTML_MODEL_ROOT=str(paths['foreign-native']))
        env.update(injected)
        for key in NATIVE_SEARCH_VARIABLES:
            if key.startswith('FLAGS_') or key in ('CUDA_PATH', 'CUDA_HOME', 'CUDNN_PATH', 'CUDNN_HOME'):
                injected[key] = str(paths['foreign-native'])
                env[key] = injected[key]
    metadata = {'kind': 'simulated fixtures, no host installation changed', 'injected': injected,
                'fixture_root': str(directory), 'cwd': str(paths['cwd']), 'marker': str(marker),
                'expected_config': str(config), 'expected_work': str(paths['work']),
                'forbidden_roots': [str(paths[name]) for name in ('foreign-bin', 'foreign-python', 'foreign-native', 'foreign-qt', 'user-base', 'conda', 'venv', 'cwd')],
                'user_sites': list(map(str, user_sites))}
    return env, metadata


def verify_vendor_qt_plugin_path(root: Path, value: str) -> None:
    """Accept only an inventoried vendor's exact platform-plugin directory.

    Linux cv2 imports regenerate this variable after bootstrap has cleared it.
    This is not a general allowance for Qt environment overrides or arbitrary
    paths inside the bundle. Verify the package anchors and complete plugin
    subtree, rejecting aliases, mixed search paths and uninventoried content.
    """
    root = root.resolve(strict=True)
    manifest = json.loads((root / 'bundle-manifest.json').read_text(encoding='utf-8'))
    target = manifest['platform']
    packages = 'runtime/Lib/site-packages' if target == 'windows' else 'runtime/lib/python3.12/site-packages'
    platform_plugin = {'linux': 'libqxcb.so', 'macos': 'libqcocoa.dylib', 'windows': 'qwindows.dll'}[target]
    pyside = f'{packages}/PySide6'
    declarations = [(f'{pyside}/' + ('plugins' if target == 'windows' else 'Qt/plugins'),
                     (f'{pyside}/__init__.py',))]
    if target == 'linux':
        declarations.append((f'{packages}/cv2/qt/plugins',
                             (f'{packages}/cv2/__init__.py', f'{packages}/cv2/config-3.py')))
    path = Path(value)
    for relative, anchors in declarations:
        directory = root / relative
        if path not in (directory, directory / 'platforms'):
            continue
        if not directory.is_dir() or directory.is_symlink() or directory.resolve(strict=True) != directory:
            break
        entries = [directory, *directory.rglob('*'), *(root / name for name in anchors)]
        # resolve(strict=True) plus exact spelling rejects both an outside link
        # and an alias to another legitimate bundle directory.
        if any(not entry.exists() or entry.is_symlink() or entry.resolve(strict=True) != entry for entry in entries):
            break
        inventory = manifest['files']
        required = [*anchors, f'{relative}/platforms/{platform_plugin}']
        actual = {entry.relative_to(root).as_posix() for entry in entries if entry.is_file()}
        declared = {name for name in inventory if name.startswith(relative + '/')}
        if not set(required).issubset(inventory) or actual != declared | set(anchors):
            break
        verify_payload(root, {'files': {name: inventory[name] for name in actual}})
        return
    raise RuntimeError(f'Unverified regenerated Qt platform plugin path: {value}')


def is_packaged_native_worker(value: str | None, root: Path) -> bool:
    """Compare filesystem identity, including macOS /var -> /private/var.

    Only the actual native-ocr directory of this verified bundle is accepted;
    a missing directory or a payload link redirecting it elsewhere still fails.
    Archive/manifest hashes remain checked separately before this evidence gate.
    """
    if not value:
        return False
    try:
        canonical_root = root.resolve(strict=True)
        expected = canonical_root / 'native-ocr'
        observed = Path(value)
        return (observed.is_absolute() and expected.is_dir()
                and expected.resolve(strict=True) == expected
                and observed.resolve(strict=True) == expected
                and observed.samefile(expected))
    except (OSError, ValueError, RuntimeError):
        return False


def check_environment_evidence(evidence: Path, fixture: dict, profile: str, root: Path) -> None:
    if Path(fixture['marker']).exists():
        raise RuntimeError('Foreign fixture code/command was executed')
    report = json.loads((evidence / 'environment-report.json').read_text())
    if not all(report[key] == 1 for key in ('isolated', 'ignore_environment', 'no_user_site')) or report['user_site_enabled'] is not False:
        raise RuntimeError('Launcher did not start isolated bundled Python before bootstrap')
    if report['profile'] != profile:
        raise RuntimeError('Inherited environment changed packaged profile selection')
    process_reports = sorted((evidence / 'dependency-isolation').glob('process-*.json'))
    if not process_reports:
        raise RuntimeError('Trusted child audit produced no reports')
    verified_qt_paths = set()
    for record in [report, *(json.loads(path.read_text()) for path in process_reports)]:
        environment = record['environment']
        if report.get('platform') == 'windows':
            if record.get('isolated') != 1 or record.get('ignore_environment') != 1:
                raise RuntimeError('Windows descendant lost ._pth startup isolation')
            if not record['sys_path'] or any(not path or not Path(path).resolve().is_relative_to(root.resolve()) for path in record['sys_path']):
                raise RuntimeError('Windows descendant has a non-bundled Python search path')
        if record.get('utf8_mode') != 1 or environment.get('PYTHONUTF8') != '1' or 'PYTHONIOENCODING' in environment:
            raise RuntimeError('App-controlled UTF-8 mode was lost')
        if record['user_site_enabled'] is not False or not record['no_user_site']:
            raise RuntimeError('User-site isolation was lost in a Python child')
        # The bootstrap must preserve the documented user selections. Existing
        # GUI/video smoke workloads intentionally replace config with their own
        # temporary fixture files; that is not launcher preference loss.
        if record is report and (environment.get('VSE_CONFIG_FILE') != fixture['expected_config']
                                 or environment.get('VSE_WORK_DIR') != fixture['expected_work']):
            raise RuntimeError('Documented application config/work override was lost')
        if environment.get('VSE_OCR_BACKEND') != profile:
            raise RuntimeError('Packaged backend selection was lost in a Python child')
        if profile == 'native' and not is_packaged_native_worker(environment.get('VSE_NATIVE_OCR_ROOT'), root):
            raise RuntimeError('Inherited native-worker path replaced packaged worker')
        if profile == 'directml':
            expected_models = root.resolve() / 'directml-models'
            if not environment.get('VSE_DIRECTML_MODEL_ROOT') or Path(environment['VSE_DIRECTML_MODEL_ROOT']).resolve(strict=True) != expected_models:
                raise RuntimeError('Inherited DirectML model path replaced packaged models')
        if any(key in environment for key in NATIVE_SEARCH_VARIABLES
               if key.startswith('FLAGS_') or key in ('CUDA_PATH', 'CUDA_HOME', 'CUDNN_PATH', 'CUDNN_HOME')):
            raise RuntimeError('Inherited CUDA native-library search path survived')
        for key in ('PYTHONHOME', 'PYTHONUSERBASE', 'CONDA_PREFIX', 'VIRTUAL_ENV', 'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH', 'QML2_IMPORT_PATH',
                    'OPENCV_VIDEOIO_PLUGIN_PATH', 'OPENCV_CORE_PLUGIN_PATH', 'OPENCV_DNN_PLUGIN_PATH', 'OPENCV_UI_PLUGIN_PATH', 'OPENCV_OPENCL_RUNTIME',
                    'OPENCV_FFMPEG_DLL_DIR', 'OPENCV_DNN_IE_EXTRA_PLUGIN_PATH'):
            if key in environment:
                if key == 'QT_QPA_PLATFORM_PLUGIN_PATH' and record is not report:
                    value = environment[key]
                    if value not in verified_qt_paths:
                        verify_vendor_qt_plugin_path(root, value)
                        verified_qt_paths.add(value)
                    continue
                raise RuntimeError(f'Inherited runtime search setting survived: {key}')
        if any(key.startswith(('OPENCV_VIDEOIO_PLUGIN_', 'OPENCV_DNN_PLUGIN_', 'OPENCV_UI_PLUGIN_', 'OPENCV_CORE_PARALLEL_PLUGIN_')) for key in environment):
            raise RuntimeError('Inherited OpenCV plugin filename selector survived')
        for key in ('VSE_VSF_BINARY', 'VSE_VSF_DIR', 'IMAGEIO_FFMPEG_EXE'):
            if not environment.get(key) or not Path(environment[key]).resolve().is_relative_to(root.resolve()):
                raise RuntimeError(f'Portable media selector is not pinned inside the bundle: {key}')
        observed = '\n'.join([*environment.values(), *record['sys_path']])
        for forbidden in fixture['forbidden_roots'] + fixture['user_sites']:
            if forbidden in observed:
                raise RuntimeError(f'Foreign runtime search path survived: {forbidden}')


def verify_archive(archive: Path, output: Path) -> dict:
    archive = archive.resolve(strict=True)
    report = {'archive': str(archive), 'archive_sha256': sha256(archive), 'result': 'running', 'scenarios': [], 'limits': LIMITS}
    output.mkdir(parents=True, exist_ok=False)
    try:
        expected = archive.with_suffix('.zip.sha256').read_text().split()[0]
        if report['archive_sha256'] != expected:
            raise RuntimeError('Final archive checksum mismatch')
        with tempfile.TemporaryDirectory(prefix='vse launcher 环境 & spaces ') as temporary:
            extracted = extract_checked(archive, Path(temporary) / 'extracted')
            app_root = extracted / 'Video Subtitle Extractor.app/Contents/Resources'
            root = app_root if (app_root / 'bundle-manifest.json').is_file() else extracted
            manifest = json.loads((root / 'bundle-manifest.json').read_text())
            if manifest.get('format') != 2 or host_platform() != (manifest['platform'], manifest['arch']):
                raise RuntimeError('Expected native-host format 2 bundle; no emulation/foreign-OS claims')
            verify_payload(root, manifest)
            report.update(commit=manifest.get('commit'), platform=manifest['platform'], arch=manifest['arch'], profile=manifest.get('ocr_backend', 'paddle'))
            for scenario in SCENARIOS:
                evidence = output / scenario
                evidence.mkdir()
                case = {'scenario': scenario, 'result': 'running', 'evidence': str(evidence)}
                report['scenarios'].append(case)
                try:
                    env, fixture = fixture_environment(Path(temporary) / scenario, scenario == 'contaminated-host-environment')
                    write_json(evidence / 'fixture.json', fixture)
                    audit = evidence / 'dependency-isolation'
                    # Prepare exact trusted hooks, but deliberately do NOT add their
                    # PYTHONPATH to the launch env: real contamination must hit -I.
                    audit_environment(root, audit)
                    if manifest['platform'] == 'windows':
                        audit_windows_executables(root, root / manifest['runtime_python'], audit)
                    elif manifest['platform'] == 'macos':
                        audit_macos_executables(root, audit)
                    command = launcher_command(root, manifest['platform'], ['--verify', '--results-dir', str(evidence), '--runtime-audit-dir', str(audit)])
                    case['command'] = command
                    with (evidence / 'launcher.log').open('w', encoding='utf-8') as stream:
                        subprocess.run(command, cwd=fixture['cwd'], env=env, stdout=stream, stderr=subprocess.STDOUT, check=True, timeout=4800)
                    required = ('PySide6.QtWidgets', 'cv2', 'qfluentwidgets') + {'paddle': ('paddle',), 'directml': ('onnxruntime',)}.get(report['profile'], ())
                    forbidden = ('paddle', 'paddleocr', 'paddlex', 'onnxruntime') if report['profile'] == 'native' else ('paddle', 'paddleocr', 'paddlex') if report['profile'] == 'directml' else ()
                    case['audit'] = summarize_reports(root, audit, required, forbidden, require_isolated=manifest['platform'] == 'windows', require_native_workers=manifest['platform'] == 'windows' and manifest.get('ocr_backend') == 'native')
                    check_environment_evidence(evidence, fixture, report['profile'], root)
                    if manifest['platform'] == 'macos':
                        app_audit = evidence / 'macos-app-dependency-isolation'
                        audit_environment(root, app_audit)
                        app_command = launcher_command(root, 'macos', ['--runtime-audit-dir', str(app_audit), '--cli', '--help'], app=True)
                        with (evidence / 'macos-app-launcher.log').open('w', encoding='utf-8') as stream:
                            subprocess.run(app_command, cwd=fixture['cwd'], env=env, stdout=stream, stderr=subprocess.STDOUT, check=True, timeout=180)
                        case['macos_app_cli_audit'] = summarize_reports(root, app_audit, ('backend',))
                        if Path(fixture['marker']).exists():
                            raise RuntimeError('Foreign fixture executed through macOS app launcher')
                    case['result'] = 'passed'
                except Exception as error:
                    case.update(result='failed', error=f'{type(error).__name__}: {error}')
                finally:
                    write_json(evidence / 'scenario-report.json', case)
            if any(case['result'] != 'passed' for case in report['scenarios']):
                raise RuntimeError('Portable environment scenario failed; inspect per-scenario logs/reports')
            report['result'] = 'passed'
    except Exception as error:
        report.update(result='failed', error=f'{type(error).__name__}: {error}')
        for case in report['scenarios']:
            if case['result'] == 'running':
                case['result'] = 'failed'
        raise
    finally:
        write_json(output / 'portable-environment-report.json', report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--archive', type=Path)
    source.add_argument('--archives', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    archives = [args.archive] if args.archive else sorted(args.archives.glob('*.zip'))
    if not archives:
        parser.error('No final ZIPs found; acceptance cannot be skipped')
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error('Output must be new or empty; stale acceptance cannot pass')
    output.mkdir(parents=True, exist_ok=True)
    report = {'result': 'running', 'archives': [], 'limits': LIMITS,
              'host_interpreters_before_test': {name: shutil.which(name) for name in ('python', 'python3', 'conda')},
              'windows_existing_registry': existing_windows_python_registry()}
    try:
        for archive in archives:
            destination = output / archive.stem
            try:
                report['archives'].append(verify_archive(archive, destination))
            except Exception as error:
                detail = destination / 'portable-environment-report.json'
                report['archives'].append(json.loads(detail.read_text()) if detail.is_file() else
                                          {'archive': str(archive), 'result': 'failed', 'error': str(error)})
        if any(item['result'] != 'passed' for item in report['archives']):
            raise RuntimeError('One or more final archives failed portable environment acceptance')
        report['result'] = 'passed'
    except Exception as error:
        report.update(result='failed', error=f'{type(error).__name__}: {error}')
        raise
    finally:
        write_json(output / 'portable-environment-report.json', report)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
