"""Launch/audit the included CPU inference runtime; never install on the client."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import subprocess
import sys


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
                                   'model_bytes': size(root / 'backend/models') + size(root / 'native-ocr/models'),
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify', action='store_true', help='Audit the included runtime and run real GUI/OCR/video regressions; no installation')
    parser.add_argument('--results-dir', type=Path, help='Writable verification evidence directory outside the application')
    parser.add_argument('--cli', action='store_true', help='Pass remaining arguments to the subtitle CLI instead of the GUI')
    args, forwarded = parser.parse_known_args(argv)
    root = Path(__file__).resolve().parent
    manifest_path = root / 'bundle-manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('format') != 2 or manifest.get('distribution') != 'self-contained CPU inference runtime':
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
    os.chdir(root)
    configure_user_paths()
    backend = manifest.get('ocr_backend', 'paddle')
    if backend not in ('paddle', 'native'):
        raise RuntimeError('Unknown bundled OCR backend')
    os.environ['VSE_OCR_BACKEND'] = backend
    if backend == 'native':
        os.environ['VSE_NATIVE_OCR_ROOT'] = str(root / 'native-ocr')
        # Do not override a requested unsupported GPU provider: adapter rejects it.
        os.environ['ORT_DISABLE_TELEMETRY'] = '1'
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    os.environ['PYTHONNOUSERSITE'] = '1'
    os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK'] = 'True'
    if (root / 'wheelhouse').exists() or (root / '.venv').exists():
        raise RuntimeError('Unexpected installer cache/venv in self-contained release')
    if args.verify:
        results = (args.results_dir or Path(os.environ['VSE_WORK_DIR']).parent / 'verification').expanduser().resolve()
        if results.is_relative_to(root.resolve()):
            raise RuntimeError('Verification results must be outside the application directory')
        results.mkdir(parents=True, exist_ok=True)
        verify_payload(root, manifest)
        subprocess.run([str(python), 'scripts/prune_inference_runtime.py', '--verify', '--manifest', str(root / 'runtime/inference-prune.json')], check=True)
        subprocess.run([str(python), 'scripts/runtime_dependencies.py', '--manifest', str(manifest_path)], check=True)
        subprocess.run([str(python), 'scripts/install_videosubfinder.py', '--verify-only'], check=True)
        subprocess.run([str(python), '-m', 'backend.cli', '--help'], check=True)
        qt_platform = 'cocoa' if platform.system() == 'Darwin' else 'windows' if os.name == 'nt' else 'xcb' if os.environ.get('DISPLAY') else 'offscreen'
        env = dict(os.environ, QT_QPA_PLATFORM=qt_platform, VSE_OFFLINE='1', VSE_SMOKE_SCREENSHOT_DIR=str(results / 'gui'))
        subprocess.run([str(python), 'gui.py', '--smoke-test'], check=True, env=env, timeout=120)
        subprocess.run([str(python), 'scripts/e2e_smoke.py', '--output', str(results / 'bundle-e2e')], check=True, env=env, timeout=960)
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
