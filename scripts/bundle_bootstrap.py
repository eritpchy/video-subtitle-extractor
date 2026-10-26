"""Launch/audit the included inference runtime; never install on the client."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import site
import subprocess
import sys


# Language and native *search* settings only. Do not erase application options,
# themes, display sessions, locale, HOME/XDG/LOCALAPPDATA or explicit VSE paths.
NATIVE_SEARCH_VARIABLES = (
    'LD_LIBRARY_PATH', 'LD_PRELOAD', 'LD_AUDIT',
    'DYLD_LIBRARY_PATH', 'DYLD_FALLBACK_LIBRARY_PATH', 'DYLD_FRAMEWORK_PATH',
    'DYLD_FALLBACK_FRAMEWORK_PATH', 'DYLD_INSERT_LIBRARIES', 'DYLD_ROOT_PATH',
    'DYLD_VERSIONED_LIBRARY_PATH', 'DYLD_VERSIONED_FRAMEWORK_PATH',
    'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH', 'QML_IMPORT_PATH', 'QML2_IMPORT_PATH',
    'OPENCV_VIDEOIO_PLUGIN_PATH', 'OPENCV_CORE_PLUGIN_PATH', 'OPENCV_DNN_PLUGIN_PATH',
    'OPENCV_UI_PLUGIN_PATH', 'OPENCV_OPENCL_RUNTIME', 'OPENCV_FFMPEG_DLL_DIR',
    'OPENCV_DNN_IE_EXTRA_PLUGIN_PATH',
    # Paddle's dynamic loader consults explicit library-directory flags before
    # vendor package paths. Do not let a final CUDA package borrow host DLLs.
    # Keep compute options (CUDA_VISIBLE_DEVICES, memory limits, etc.) intact.
    'FLAGS_cuda_dir', 'FLAGS_cudnn_dir', 'FLAGS_cublas_dir', 'FLAGS_cupti_dir',
    'FLAGS_curand_dir', 'FLAGS_cusparse_dir', 'FLAGS_cusparselt_dir',
    'FLAGS_nccl_dir', 'FLAGS_tensorrt_dir', 'FLAGS_win_cuda_bin_dir',
    'FLAGS_nvidia_package_dir', 'CUDA_PATH', 'CUDA_HOME', 'CUDNN_PATH', 'CUDNN_HOME',
)


RUNTIME_SELECTOR_PREFIXES = ('PYTHON', 'CONDA_', '_CONDA_', 'VSE_RUNTIME_AUDIT_',
                             'OPENCV_VIDEOIO_PLUGIN_', 'OPENCV_DNN_PLUGIN_',
                             'OPENCV_UI_PLUGIN_', 'OPENCV_CORE_PARALLEL_PLUGIN_')


def sanitize_environment(root: Path, environment: dict[str, str], target_platform: str) -> dict[str, str]:
    """Return a child-only environment; never alter the caller's OS/installation."""
    env = {key: value for key, value in environment.items()
           if not key.upper().startswith(RUNTIME_SELECTOR_PREFIXES)
           and key.upper() not in {*(name.upper() for name in NATIVE_SEARCH_VARIABLES), 'VIRTUAL_ENV', 'VIRTUAL_ENV_PROMPT', '_CE_CONDA', '_CE_M', '__PYVENV_LAUNCHER__'}}
    root = root.resolve()
    if target_platform == 'windows':
        windows = Path(env.get('SystemRoot') or env['SYSTEMROOT'])
        paths = (root / 'runtime', root / 'runtime/DLLs', windows / 'System32', windows)
        separator = ';'
    else:
        paths = (root / 'runtime/bin', '/usr/bin', '/bin', '/usr/sbin', '/sbin')
        separator = ':'
    env['PATH'] = separator.join(map(str, paths))
    if target_platform == 'linux':
        env['LD_LIBRARY_PATH'] = str(root / 'native-libs')
    env.update(PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1')
    return env


def enable_trusted_audit(root: Path, directory: Path) -> None:
    """Restore only byte-validated verification hooks AFTER isolated startup.

    -I deliberately ignores inherited PYTHONPATH/sitecustomize. The initial
    process imports its probe from the checksum-verified bundle; descendants
    inherit the exact verified copy, never arbitrary original PYTHONPATH.
    """
    directory = directory.resolve()
    if directory.is_relative_to(root):
        raise RuntimeError('Runtime audit evidence must be outside the application')
    from scripts.standalone_runtime_probe import _trusted_hooks, install_probe
    for path, expected in _trusted_hooks(root, directory).values():
        if not path.is_file() or path.is_symlink() or path.read_bytes() != expected:
            raise RuntimeError(f'Missing or modified trusted runtime audit hook: {path}')
    os.environ.update(PYTHONPATH=str(directory / 'hook'), VSE_RUNTIME_AUDIT_ROOT=str(root),
                      VSE_RUNTIME_AUDIT_REPORTS=str(directory), VSE_RUNTIME_AUDIT_ACTIVE='portable-bootstrap-v1')
    install_probe()


def configure_runtime_tools(root: Path, manifest: dict) -> None:
    """Pin portable media executables; source-mode manual overrides are unchanged.

    Do not call imageio's finder: it can fall back to Conda/system FFmpeg. Only
    explicit inventory entries qualify, and ambiguous/missing binaries fail.
    """
    files = manifest['files']
    directory = f"backend/subfinder/{manifest['platform']}/{manifest['arch']}"
    names = ('VideoSubFinderCli.exe',) if manifest['platform'] == 'windows' else ('VideoSubFinderCli.run', 'VideoSubFinderCli')
    # .run is the reviewed upstream sibling-library wrapper, not ambiguity with
    # its underlying binary. Never fall back to another architecture/install.
    vsf = next((f'{directory}/{name}' for name in names if f'{directory}/{name}' in files), None)
    ffmpeg = [name for name in files
              if PurePosixPath(name).parts[:1] == ('runtime',)
              and PurePosixPath(name).parts[-3:-1] == ('imageio_ffmpeg', 'binaries')
              and PurePosixPath(name).name.startswith('ffmpeg-')]
    if not vsf or len(ffmpeg) != 1:
        raise RuntimeError('Bundle requires its manifest VideoSubFinder and exactly one bundled FFmpeg executable')

    def checked(relative: str) -> Path:
        parts = PurePosixPath(relative)
        path = root / relative
        if (relative not in files or parts.is_absolute() or '..' in parts.parts or '\\' in relative or ':' in relative
                or str(parts) != relative or not path.is_file() or path.is_symlink()
                or not path.resolve().is_relative_to(root.resolve()) or sha256(path) != files[relative]):
            raise RuntimeError(f'Missing or invalid bundled media executable: {relative}')
        return path.resolve()

    binary, encoder = checked(vsf), checked(ffmpeg[0])
    # The wrapper must not refer to a missing/uninventoried underlying binary.
    if binary.name.endswith('.run'):
        checked(f'{directory}/VideoSubFinderCli')
    os.environ.update(VSE_VSF_BINARY=str(binary), VSE_VSF_DIR=str(binary.parent), IMAGEIO_FFMPEG_EXE=str(encoder))


def verify_windows_runtime_configuration(root: Path, manifest: dict) -> None:
    """Refuse an incomplete path-isolation payload before launching children."""
    required = ('runtime/python312._pth', 'runtime/Lib/site-packages/sitecustomize.py',
                'runtime/windows-app-local-runtime.json', 'scripts/windows_app_local_runtime.py')
    if any(name not in manifest['files'] for name in required):
        raise RuntimeError('Windows bundle lacks its isolated runtime configuration; re-extract/rebuild the package')
    verify_payload(root, {'files': {name: manifest['files'][name] for name in required}})


def environment_report(root: Path, manifest: dict) -> dict:
    selected = ('PATH', *NATIVE_SEARCH_VARIABLES, 'PYTHONHOME', 'PYTHONPATH', 'PYTHONUSERBASE',
                'PYTHONNOUSERSITE', 'PYTHONUTF8', 'PYTHONIOENCODING', 'CONDA_PREFIX', 'CONDA_DEFAULT_ENV', 'VIRTUAL_ENV',
                'VSE_CONFIG_FILE', 'VSE_WORK_DIR', 'VSE_OCR_BACKEND', 'VSE_NATIVE_OCR_ROOT', 'VSE_DIRECTML_MODEL_ROOT',
                'VSE_VSF_BINARY', 'VSE_VSF_DIR', 'IMAGEIO_FFMPEG_EXE')
    return {'schema': 1, 'bundle_root': str(root), 'platform': manifest['platform'], 'executable': sys.executable,
            'prefix': sys.prefix, 'base_prefix': sys.base_prefix, 'sys_path': sys.path,
            'isolated': sys.flags.isolated, 'ignore_environment': sys.flags.ignore_environment, 'utf8_mode': sys.flags.utf8_mode,
            'no_user_site': sys.flags.no_user_site, 'user_site_enabled': site.ENABLE_USER_SITE,
            'profile': manifest.get('ocr_backend', 'paddle'),
            'environment': {key: os.environ[key] for key in selected if key in os.environ}}


def host_platform() -> tuple[str, str]:
    system = {"Linux": "linux", "Windows": "windows", "Darwin": "macos"}.get(platform.system())
    machine = platform.machine().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(machine)
    if not system or not arch:
        raise RuntimeError(f"Unsupported CPU platform: {platform.system()} {machine}")
    return system, arch


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def installer_wheel_entry(relative: str) -> bool:
    parts = PurePosixPath(relative).parts
    return len(parts) == 2 and parts[0] == 'wheelhouse' and parts[1].endswith('.whl') and '\\' not in relative and ':' not in relative and str(PurePosixPath(relative)) == relative


def verify_payload(root: Path, manifest: dict, *, compact: bool = False) -> None:
    for relative, expected in manifest["files"].items():
        normalized = PurePosixPath(relative)
        if normalized.is_absolute() or '..' in normalized.parts or '\\' in relative or ':' in relative or str(normalized) != relative:
            raise RuntimeError(f'Missing or invalid bundle file: {relative}')
        if normalized.parts[0] == 'wheelhouse' and not installer_wheel_entry(relative):
            raise RuntimeError(f'Invalid installer wheel entry: {relative}')
        path = root / relative
        if compact and installer_wheel_entry(relative) and not path.exists() and path.resolve().is_relative_to(root.resolve()):
            continue
        if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
            raise RuntimeError(f"Missing or invalid bundle file: {relative}")
        if sha256(path) != expected:
            raise RuntimeError(f"Bundle checksum mismatch: {relative}")

    if manifest.get('app_files'):
        if root.name != 'Resources' or root.parent.name != 'Contents' or root.parent.parent.suffix != '.app':
            raise RuntimeError('Invalid macOS application layout')
        verify_payload(root.parent.parent, {'files': manifest['app_files']})


def write_size_report(root: Path, manifest: dict, phase: str, results_dir: Path | None = None) -> None:
    def size(path):
        return sum(p.stat().st_size for p in path.rglob('*') if p.is_file() and not p.is_symlink()) if path.is_dir() else 0

    total = sum(p.stat().st_size for p in root.rglob('*') if p.is_file() and not p.is_symlink()
                and p.suffix != '.zip' and 'test-results' not in p.relative_to(root).parts)
    directory = results_dir if results_dir is not None else root / 'test-results'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'runtime-size.json'
    report = json.loads(path.read_text()) if path.is_file() else {'measurements': []}
    report.update({'commit': manifest.get('commit'), 'platform': manifest['platform'],
                   'arch': manifest['arch'], 'profile': manifest.get('ocr_backend', 'paddle'),
                   'byte_count_method': 'sum of regular file sizes; symlinks, original ZIPs and test-results excluded'})
    report['measurements'].append({'phase': phase, 'runtime_bytes': size(root / 'runtime'),
                                   'model_bytes': size(root / 'backend/models') + size(root / 'native-ocr/models') + size(root / 'directml-models/models'),
                                   'native_worker_bytes': size(root / 'native-ocr'),
                                   'wheelhouse_bytes': size(root / 'wheelhouse'),
                                   'total_installed_bytes': total})
    path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')


def configure_user_paths() -> None:
    """Packaged applications may live in read-only signed/program directories."""
    if not os.environ.get('VSE_CONFIG_FILE'):
        if sys.platform == 'win32':
            base = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local'))
        elif sys.platform == 'darwin':
            base = Path.home() / 'Library/Application Support'
        else:
            base = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
        directory = base / 'video-subtitle-extractor'
        directory.mkdir(parents=True, exist_ok=True)
        os.environ['VSE_CONFIG_FILE'] = str(directory / 'config.json')

    if not os.environ.get('VSE_WORK_DIR'):
        if sys.platform == 'win32':
            cache = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'video-subtitle-extractor/cache'
        elif sys.platform == 'darwin':
            cache = Path.home() / 'Library/Caches/video-subtitle-extractor'
        else:
            cache = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'video-subtitle-extractor'
        work = cache / 'work'
        work.mkdir(parents=True, exist_ok=True)
        os.environ['VSE_WORK_DIR'] = str(work)


BUNDLE_DISTRIBUTION = 'self-contained inference runtime'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify', action='store_true', help='Audit the included runtime and run real GUI/OCR/video regressions; no installation')
    parser.add_argument('--results-dir', type=Path, help='Writable verification evidence directory outside the application')
    parser.add_argument('--runtime-audit-dir', type=Path, help='Prepared trusted verification hooks/evidence outside application')
    parser.add_argument('--cli', action='store_true', help='Pass remaining arguments to the subtitle CLI instead of the GUI')
    arguments = list(sys.argv[1:] if argv is None else argv)
    # Once --cli is selected, even --help belongs to the application CLI. Do
    # not let argparse exit before launching/auditing the requested backend.
    if '--cli' in arguments:
        index = arguments.index('--cli')
        args = parser.parse_args(arguments[:index + 1])
        forwarded = arguments[index + 1:]
    else:
        args, forwarded = parser.parse_known_args(arguments)
    root = Path(__file__).resolve().parent
    manifest_path = root / 'bundle-manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('format') != 2 or manifest.get('distribution') not in (BUNDLE_DISTRIBUTION, 'self-contained CPU inference runtime'):
        raise RuntimeError('This launcher requires a self-contained release bundle')
    if host_platform() != (manifest['platform'], manifest['arch']):
        raise RuntimeError(f"Wrong native bundle: requires {manifest['platform']}/{manifest['arch']}")
    python = root / manifest['runtime_python']
    if not python.resolve().is_relative_to(root.resolve()) or not python.is_file():
        raise RuntimeError('Bundled Python runtime is missing; re-extract the original release archive')
    if Path(sys.executable).resolve() != python.resolve():
        raise RuntimeError('Use the included Start VSE launcher; no system Python installation is needed')
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError('Unexpected bundled Python version; re-extract the original release archive')
    if manifest['platform'] == 'windows':
        verify_windows_runtime_configuration(root, manifest)
    os.chdir(root)
    # -I omits the application directory. Add only the known bundle, not cwd.
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    clean_environment = sanitize_environment(root, dict(os.environ), manifest['platform'])
    os.environ.clear()
    os.environ.update(clean_environment)
    configure_user_paths()
    configure_runtime_tools(root, manifest)
    backend = manifest.get('ocr_backend', 'paddle')
    if backend not in ('paddle', 'native', 'directml'):
        raise RuntimeError('Unknown bundled OCR backend')
    os.environ['VSE_OCR_BACKEND'] = backend
    os.environ.pop('VSE_DIRECTML_MODEL_ROOT', None)
    if backend == 'native':
        os.environ['VSE_NATIVE_OCR_ROOT'] = str(root / 'native-ocr')
        # Do not override a requested unsupported GPU provider: adapter rejects it.
        os.environ['ORT_DISABLE_TELEMETRY'] = '1'
    else:
        # A fixed Paddle profile must not retain an unrelated foreign worker.
        os.environ.pop('VSE_NATIVE_OCR_ROOT', None)
        if backend == 'directml':
            os.environ['VSE_DIRECTML_MODEL_ROOT'] = str(root / 'directml-models')
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    os.environ['PYTHONNOUSERSITE'] = '1'
    os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK'] = 'True'
    if (root / 'wheelhouse').exists() or (root / '.venv').exists():
        raise RuntimeError('Unexpected installer cache/venv in self-contained release')
    if args.runtime_audit_dir:
        verify_payload(root, manifest)
        enable_trusted_audit(root, args.runtime_audit_dir)
    if args.verify:
        results = (args.results_dir or Path(os.environ['VSE_WORK_DIR']).parent / 'verification').expanduser().resolve()
        if results.is_relative_to(root.resolve()):
            raise RuntimeError('Verification results must be outside the application directory')
        results.mkdir(parents=True, exist_ok=True)
        verify_payload(root, manifest)
        (results / 'environment-report.json').write_text(json.dumps(environment_report(root, manifest), indent=2) + '\n', encoding='utf-8')
        if manifest['platform'] == 'windows' and os.environ.get('VSE_WINDOWS_RUNNER_LABEL'):
            subprocess.run([str(python), 'scripts/windows_verification.py', 'capture', '--runner', os.environ['VSE_WINDOWS_RUNNER_LABEL'], '--profile', backend, '--output', str(results / 'bundled-windows-identity')], check=True)
        subprocess.run([str(python), 'scripts/prune_inference_runtime.py', '--verify', '--manifest', str(root / 'runtime/inference-prune.json')], check=True)
        subprocess.run([str(python), '-m', 'scripts.runtime_dependencies', '--manifest', str(manifest_path)], check=True)
        if backend == 'paddle':
            subprocess.run([str(python), '-I', '-S', 'scripts/paddle_package_models.py',
                            '--verify-only', str(root / 'backend/models')], check=True)
        if manifest.get('accelerator', '').startswith('cuda'):
            subprocess.run([str(python), '-m', 'scripts.cuda_package', '--root', str(root),
                            '--accelerator', manifest['accelerator']], check=True)
        subprocess.run([str(python), 'scripts/install_videosubfinder.py', '--verify-only'], check=True)
        subprocess.run([str(python), '-m', 'backend.cli', '--help'], check=True)
        qt_platform = 'cocoa' if platform.system() == 'Darwin' else 'windows' if os.name == 'nt' else 'xcb' if os.environ.get('DISPLAY') else 'offscreen'
        env = dict(os.environ, QT_QPA_PLATFORM=qt_platform, VSE_OFFLINE='1', VSE_SMOKE_SCREENSHOT_DIR=str(results / 'gui'))
        subprocess.run([str(python), 'gui.py', '--smoke-test'], check=True, env=env, timeout=120)
        subprocess.run([str(python), 'scripts/e2e_smoke.py', '--output', str(results / 'bundle-e2e')], check=True, env=env, timeout=960)
        for mode in ('accurate', 'auto'):
            subprocess.run([str(python), 'scripts/e2e_smoke.py', '--mode', mode,
                            '--output', str(results / f'bundle-e2e-{mode}')], check=True, env=env, timeout=960)
        subprocess.run([str(python), 'scripts/e2e_issues.py', '--output', str(results / 'bundle-issues')], check=True, env=env, timeout=3660)
        write_size_report(root, manifest, 'verified-self-contained', results)
        return 0
    command = [str(python), '-m', 'backend.cli'] if args.cli else [str(python), 'gui.py']
    return subprocess.call(command + forwarded)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f'Bundle startup failed: {exc}', file=sys.stderr)
        raise SystemExit(1)
