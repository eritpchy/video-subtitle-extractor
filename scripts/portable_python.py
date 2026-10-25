"""Build-time acquisition of pinned portable CPython; never used to install on clients.

Pins were verified against the publisher's release asset SHA-256 metadata:
https://github.com/astral-sh/python-build-standalone/releases/expanded_assets/20261021
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import platform
import plistlib
import posixpath
import shutil
import tarfile
import tempfile
import urllib.request

VERSION = '3.12.15'
RELEASE = '20261021'
PINS = {
    ('linux', 'x64'): ('x86_64-unknown-linux-gnu', '6d7521138fd33afddc1ccd08abb007d9a2829e38117c2576f9efdc0ec73ce772'),
    ('windows', 'x64'): ('x86_64-pc-windows-msvc', 'aac5d331fc3307343f95928fdbea4583f0d53973d08c7e1d081884fbd229a0e1'),
    ('macos', 'arm64'): ('aarch64-apple-darwin', '7b8d00b751db21cea87dc551a8f8165147c5fbc5251d5537d7e5497dde00a219'),
}


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _member_path(name: str) -> str:
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name or not path.parts or path.parts[0] != 'python':
        raise ValueError(f'Unsafe runtime archive path: {name}')
    return path.as_posix()


def extract_runtime(archive: Path, destination: Path) -> None:
    """Extract into an empty directory without creating links or special files.

    Only internal archive file aliases are materialized. Link resolution never
    consults host paths; directory links and loops fail closed. This keeps ZIP
    payloads portable to Windows extractors without symlink privileges.
    """
    if destination.is_symlink() or (destination.exists() and any(destination.iterdir())):
        raise ValueError('Runtime destination must be an empty real directory')
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, 'r:gz') as tar:
        members = {}
        for member in tar.getmembers():
            name = _member_path(member.name)
            if name in members:
                raise ValueError(f'Duplicate archive member: {name}')
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise ValueError(f'Unsupported archive member: {name}')
            members[name] = member
        def resolve(name, seen):
            if name in seen or name not in members:
                raise ValueError(f'Invalid runtime alias: {name}')
            member = members[name]
            if member.issym() or member.islnk():
                target = member.linkname
                if target.startswith('/') or '\\' in target or ':' in target:
                    raise ValueError(f'Unsafe runtime alias: {name}')
                target = posixpath.normpath(posixpath.join(posixpath.dirname(name), target) if member.issym() else target)
                _member_path(target)
                return resolve(target, seen | {name})
            if not member.isfile():
                raise ValueError(f'Runtime alias must resolve to file: {name}')
            return member
        # Validate all aliases and parent types before writing anything.
        for name, member in members.items():
            for parent in PurePosixPath(name).parents:
                if str(parent) in members and not members[str(parent)].isdir():
                    raise ValueError(f'Non-directory archive parent: {parent}')
            if member.issym() or member.islnk():
                resolve(name, set())
        for name, member in members.items():
            target = destination.joinpath(*PurePosixPath(name).relative_to('python').parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as src, target.open('xb') as dst:
                    shutil.copyfileobj(src, dst)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
        for name, member in members.items():
            if member.issym() or member.islnk():
                source = resolve(name, set())
                target = destination.joinpath(*PurePosixPath(name).relative_to('python').parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                original = destination.joinpath(*PurePosixPath(source.name).relative_to('python').parts)
                shutil.copy2(original, target)



def prepare_runtime(destination: Path, target_platform: str, target_arch: str, cache: Path | None = None) -> dict:
    triple, checksum = PINS[(target_platform, target_arch)]
    filename = f'cpython-{VERSION}+{RELEASE}-{triple}-install_only_stripped.tar.gz'
    url = f'https://github.com/astral-sh/python-build-standalone/releases/download/{RELEASE}/{filename.replace("+", "%2B")}'
    destination = Path(destination).absolute()
    cache = Path(cache) if cache is not None else Path(os.environ.get('VSE_PYTHON_CACHE', Path(tempfile.gettempdir()) / 'vse-portable-python'))
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / filename
    if archive.is_symlink():
        raise ValueError('Runtime cache archive must not be a symlink')
    if not archive.exists():
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as tmp:
            temporary = Path(tmp.name)
        try:
            with urllib.request.urlopen(url, timeout=120) as response, temporary.open('wb') as output:
                shutil.copyfileobj(response, output)
            if digest(temporary) != checksum:
                raise ValueError('Portable Python download SHA-256 mismatch')
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    if digest(archive) != checksum:
        raise ValueError('Portable Python cache SHA-256 mismatch')
    # Publish only after successful safe extraction.
    if destination.exists():
        raise ValueError('Runtime destination already exists')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
        extracted = Path(tmp) / 'runtime'
        extract_runtime(archive, extracted)
        executable = 'python.exe' if target_platform == 'windows' else 'bin/python3.12'
        if not (extracted / executable).is_file():
            raise ValueError('Pinned runtime missing interpreter')
        extracted.rename(destination)
    return {'python': str(destination / executable), 'provider': 'astral-sh/python-build-standalone',
            'version': VERSION, 'release': RELEASE, 'target': triple, 'url': url, 'sha256': checksum,
            'aliases': 'internal-file-links-materialized', 'client_install_required': False}


try:
    from .bundle_bootstrap import NATIVE_SEARCH_VARIABLES
except ImportError:
    from bundle_bootstrap import NATIVE_SEARCH_VARIABLES


WINDOWS_ISOLATED_PATHS = 'python312.zip\nLib\nDLLs\n.\n..\nLib/site-packages\nimport site\n'
WINDOWS_SITE_CUSTOMIZE = b'''"""VSE-owned Windows portable startup hook; no external code is executed."""
import os as _os
import sys as _sys

# multiprocessing forwards -I from ._pth, which would ignore PYTHONUTF8 in its
# worker. Preserve the already-active UTF-8 mode as an explicit inherited -X.
if _sys.flags.utf8_mode == 1:
    _sys._xoptions['utf8'] = '1'

if _sys.platform == 'win32':
    try:
        from pathlib import Path as _Path
        from scripts.windows_app_local_runtime import preload_runtime
        preload_runtime(_Path(__file__).resolve().parents[3])
    except BaseException as _error:
        print('App-local Windows runtime startup failed: ' + str(_error), file=_sys.stderr, flush=True)
        _os._exit(92)

if _os.environ.get('VSE_RUNTIME_AUDIT_ACTIVE') == 'portable-bootstrap-v1':
    try:
        from pathlib import Path as _Path
        _root = _Path(__file__).resolve().parents[3]
        if _Path(_os.environ['VSE_RUNTIME_AUDIT_ROOT']).resolve() != _root:
            raise RuntimeError('Runtime audit root mismatch')
        from scripts.bundle_bootstrap import enable_trusted_audit
        enable_trusted_audit(_root, _Path(_os.environ['VSE_RUNTIME_AUDIT_REPORTS']))
    except BaseException as _error:
        # site.py normally swallows sitecustomize exceptions. Verification must
        # fail closed rather than continue with a missing descendant probe.
        import sys as _sys
        print('Trusted portable startup audit failed: ' + str(_error), file=_sys.stderr, flush=True)
        _os._exit(91)
'''


def write_windows_runtime_isolation(stage: Path, python: Path) -> dict:
    """After pip, before prune inventory: isolate every Windows Python process.

    CPython's DLL-adjacent ._pth suppresses registry/PYTHONPATH expansion even
    when children start without -I. Keep import site for installed wheels and
    the bundle-owned audit hook; launcher/bootstrap control UTF-8/user-site
    preconfiguration separately. Never overwrite a publisher/third-party hook.
    """
    stage = stage.resolve(strict=True)
    runtime = stage / 'runtime'
    if python.resolve() != runtime / 'python.exe' or not python.is_file():
        raise ValueError('Unexpected pinned Windows runtime executable')
    for name in ('python312.dll', 'Lib/os.py'):
        path = runtime / name
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(runtime):
            raise ValueError(f'Pinned Windows runtime landmark missing/unsafe: {name}')
    config = runtime / 'python312._pth'
    hook = runtime / 'Lib/site-packages/sitecustomize.py'
    for path in (config, runtime / 'python._pth', hook, runtime / 'windows-app-local-runtime.json'):
        if path.exists() or path.is_symlink():
            raise ValueError(f'Refusing to replace existing Windows path configuration/startup hook: {path}')
    if not hook.parent.is_dir() or not hook.parent.resolve().is_relative_to(runtime):
        raise ValueError('Unsafe Windows site-packages directory')
    try:
        from .windows_app_local_runtime import write_runtime_metadata
    except ImportError:
        from windows_app_local_runtime import write_runtime_metadata
    app_local_runtime = write_runtime_metadata(stage)
    # Own only these new regular files; the release manifest hashes all, and
    # sitecustomize is added before the immutable site-packages prune inventory.
    with config.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(WINDOWS_ISOLATED_PATHS)
    with hook.open('xb') as stream:
        stream.write(WINDOWS_SITE_CUSTOMIZE)
    return {'mode': 'dll-adjacent ._pth + bundle-owned sitecustomize',
            'path_config': config.relative_to(stage).as_posix(),
            'path_config_sha256': digest(config),
            'startup_hook': hook.relative_to(stage).as_posix(),
            'startup_hook_sha256': digest(hook),
            'app_local_runtime': app_local_runtime,
            'registry_writes': False, 'external_hook_execution': False}


def _shell_launcher(relative_python: str, root_expression: str, linux: bool = False) -> str:
    libraries = 'export LD_LIBRARY_PATH="$ROOT/native-libs"\n' if linux else ''
    # /bin/sh has already been loaded by the OS. This cannot defend a broken or
    # malicious loader/LD_PRELOAD that prevents the shell itself from starting.
    return f'''#!/bin/sh
set -eu
ROOT={root_expression}
unset PYTHONHOME PYTHONPATH PYTHONUSERBASE PYTHONEXECUTABLE __PYVENV_LAUNCHER__
unset VSE_RUNTIME_AUDIT_ROOT VSE_RUNTIME_AUDIT_REPORTS VSE_RUNTIME_AUDIT_ACTIVE
unset {' '.join(NATIVE_SEARCH_VARIABLES)}
export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1
export PATH="$ROOT/runtime/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd -- "$ROOT"
{libraries}exec "$ROOT/{relative_python}" -I -B -X utf8 "$ROOT/bootstrap.py" "$@"
'''


def launcher_command(root: Path, target_platform: str, arguments: list[str], *, app: bool = False) -> list[str] | str:
    """Execute the user-facing launcher, never a substitute system interpreter."""
    root = Path(root).resolve()
    if target_platform == 'windows':
        # Avoid shell=True and inherited COMSPEC. Feed cmd.exe its own quoting,
        # not MSVC backslash escaping. Every path is quoted (including & and !).
        values = [str(root / 'Start-VSE.bat'), *arguments]
        if any(any(c in value for c in '\"%\r\n') for value in values):
            raise ValueError('Unsafe cmd.exe launcher argument')
        cmd = str(Path(os.environ['SystemRoot']) / 'System32/cmd.exe')
        return f'"{cmd}" /d /s /c "' + ' '.join('"' + value + '"' for value in values) + '"'
    launcher = root.parent / 'MacOS/VSE' if app else root / ('Start-VSE.command' if target_platform == 'macos' else 'Start-VSE.sh')
    return [str(launcher), *arguments]


def write_launchers(root: Path, target_platform: str | None = None, target_arch: str | None = None, runtime_python: str | None = None, *, minimum_macos_version: str = "13.0") -> None:
    root = Path(root)
    target_platform = target_platform or {'Darwin': 'macos', 'Windows': 'windows', 'Linux': 'linux'}[platform.system()]
    runtime_python = runtime_python or ('runtime/python.exe' if target_platform == 'windows' else 'runtime/bin/python3.12')
    relative = PurePosixPath(runtime_python)
    if relative.is_absolute() or '..' in relative.parts or any(c in runtime_python for c in '\\"$`\n\r'):
        raise ValueError('Launcher interpreter must be a safe relative path')
    if target_platform == 'windows':
        python = runtime_python.replace('/', '\\')
        lines = ['@echo off', 'setlocal DisableDelayedExpansion',
                 'set "PYTHONHOME="', 'set "PYTHONPATH="', 'set "PYTHONUSERBASE="', 'set "PYTHONEXECUTABLE="', 'set "__PYVENV_LAUNCHER__="']
        lines += [f'set "{name}="' for name in (*NATIVE_SEARCH_VARIABLES, 'VSE_RUNTIME_AUDIT_ROOT', 'VSE_RUNTIME_AUDIT_REPORTS', 'VSE_RUNTIME_AUDIT_ACTIVE')]
        lines += ['set "PYTHONNOUSERSITE=1"', 'set "PYTHONDONTWRITEBYTECODE=1"',
                  'set "PATH=%~dp0runtime;%~dp0runtime\\DLLs;%SystemRoot%\\System32;%SystemRoot%"',
                  'cd /d "%~dp0" || exit /b 1',
                  '"%~dp0' + python + '" -I -B -X utf8 "%~dp0bootstrap.py" %*',
                  'exit /b %errorlevel%']
        (root / 'Start-VSE.bat').write_text('\n'.join(lines) + '\n', encoding='utf-8', newline='\r\n')
        return
    launcher = root / ('Start-VSE.command' if target_platform == 'macos' else 'Start-VSE.sh')
    launcher.write_text(_shell_launcher(runtime_python, '$(CDPATH= cd -- "${0%/*}" && pwd)', target_platform == 'linux'), encoding='utf-8')
    launcher.chmod(0o755)
    if target_platform == 'macos':
        # The packager stages the entire payload inside this movable .app.
        # No signature modification or Gatekeeper bypass is performed.
        if root.name != 'Resources' or root.parent.name != 'Contents' or root.parent.parent.suffix != '.app':
            raise ValueError('macOS payload root must be <name>.app/Contents/Resources')
        app = root.parent
        (app / 'MacOS').mkdir(parents=True)
        script = app / 'MacOS' / 'VSE'
        script.write_text(_shell_launcher(runtime_python, '$(CDPATH= cd -- "${0%/*}/../Resources" && pwd)'), encoding='utf-8')
        script.chmod(0o755)
        info = {'CFBundleName': 'Video Subtitle Extractor', 'CFBundleDisplayName': 'Video Subtitle Extractor',
                'CFBundleIdentifier': 'io.vse.portable-launcher', 'CFBundleExecutable': 'VSE',
                'CFBundlePackageType': 'APPL', 'CFBundleVersion': '1', 'LSMinimumSystemVersion': minimum_macos_version,
                'NSHighResolutionCapable': True}
        png = root / 'design/icon_1024.png'
        if png.exists():
            try:
                from .build_macos_icon import export_macos_icon
            except ImportError:
                from build_macos_icon import export_macos_icon
            export_macos_icon(png, root / 'vse.icns')
            info['CFBundleIconFile'] = 'vse.icns'
        (app / 'Info.plist').write_bytes(plistlib.dumps(info))
