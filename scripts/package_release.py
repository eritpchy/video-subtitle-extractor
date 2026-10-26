"""Create and verify self-contained, explicitly identified inference packages."""
from __future__ import annotations

import argparse
from functools import lru_cache
import ast
from email.parser import BytesParser
import json
import importlib.util
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile

try:
    from .bundle_bootstrap import BUNDLE_DISTRIBUTION, host_platform, sha256, verify_payload
    from .runtime_dependencies import runtime_wheels
    from .release_profiles import profile_identity, profile_requirements, archive_name
except ImportError:
    from bundle_bootstrap import BUNDLE_DISTRIBUTION, host_platform, sha256, verify_payload
    from runtime_dependencies import runtime_wheels
    from release_profiles import profile_identity, profile_requirements, archive_name

ROOT = Path(__file__).resolve().parents[1]
# Direct-script build entry points must resolve trusted application helpers,
# including DirectML staging, without depending on the caller's PYTHONPATH.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SOURCE_DIRS = ("backend", "ui", "design", "scripts", "docs")
SOURCE_FILES = ("gui.py", "requirements.txt", "requirements-inference.txt", "requirements-native-inference.txt", "requirements-directml-inference.txt", "LICENSE", "README.md", "README_en.md", "Dockerfile", ".dockerignore")
EXCLUDED_PARTS = {"__pycache__", ".pytest_cache", ".venv", ".git"}


def read_version(root: Path = ROOT) -> str:
    tree = ast.parse((root / "backend/config.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, str) and re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", value):
                return value
    raise RuntimeError("backend/config.py must define a literal semantic VERSION")


@lru_cache(maxsize=1)
def inference_model_directories() -> frozenset[str]:
    # Derive this from production selection, not a hand-maintained language list.
    # Supplying every argument avoids importing GUI config or Paddle runtime.
    spec = importlib.util.spec_from_file_location("vse_packaging_models", ROOT / "backend/tools/paddle_model_config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    PaddleModelConfig = module.PaddleModelConfig

    class Hardware:
        def has_cuda(self):
            return True  # Includes auto's largest possible tier too.

    selected = set()
    for language in PaddleModelConfig.MULTI_LANG:
        for mode in ('fast', 'accurate', 'auto'):
            model = PaddleModelConfig(Hardware(), language, mode, model_base='models')
            for path in (model.DET_MODEL_PATH, model.REC_MODEL_PATH):
                selected.add(Path(path).relative_to('models').as_posix())
    return frozenset(selected)


def include_source(relative: Path, target_platform: str, target_arch: str) -> bool:
    if any(part in EXCLUDED_PARTS for part in relative.parts) or relative.suffix in {".pyc", ".pyo", ".log"}:
        return False
    if relative.parts[:2] == ("backend", "models") and len(relative.parts) > 3:
        if "/".join(relative.parts[2:4]) not in inference_model_directories():
            return False
    if relative.parts[0] == "design" and relative.name not in {"vse.ico", "icon_1024.png"}:
        return False
    if relative.parts[:2] == ("backend", "subfinder"):
        # Only the verified new native install, never obsolete bundled binaries.
        return relative.parts[2:4] == (target_platform, target_arch)
    return True


def wheel_lock(wheelhouse: Path, selected: list[Path] | None = None) -> list[str]:
    packages: dict[str, str] = {}
    wheels = sorted(wheelhouse.glob("*.whl")) if selected is None else sorted(selected)
    if not wheels:
        raise RuntimeError("Wheelhouse is empty; run pip wheel -r requirements.txt first")
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as archive:
            names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA") and name.count("/") == 1]
            if len(names) != 1:
                raise RuntimeError(f"Invalid wheel metadata: {wheel.name}")
            metadata = BytesParser().parsebytes(archive.read(names[0]))
            name, version = metadata["Name"], metadata["Version"]
        if not name or not version or not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or not re.fullmatch(r"[A-Za-z0-9_.+!-]+", version):
            raise RuntimeError(f"Invalid package identity in {wheel.name}")
        normalized = re.sub(r"[-_.]+", "-", name).lower()
        if normalized in packages:
            raise RuntimeError(f"Duplicate wheel for {normalized}; build with a clean wheelhouse")
        packages[normalized] = version
    return [f"{name}=={version}" for name, version in sorted(packages.items())]


def finalize_portable_runtime(stage: Path, python: Path, env: dict) -> dict:
    """Drop build-only pip/ensurepip without touching runtime package licenses."""
    stage = stage.resolve(strict=True)
    python = python.resolve(strict=True)
    query = 'import json,sysconfig; print(json.dumps({k:sysconfig.get_path(k) for k in ("purelib","stdlib","scripts")}))'
    paths = json.loads(subprocess.check_output([str(python), '-c', query], text=True, env=env))
    runtime = (stage / 'runtime').resolve(strict=True)
    paths = {key: str(Path(value).resolve(strict=True)) for key, value in paths.items()}
    roots = [Path(paths['purelib']) / 'pip', Path(paths['stdlib']) / 'ensurepip']
    candidates = []
    for directory in roots:
        if directory.is_dir():
            candidates.extend(path for path in directory.rglob('*') if path.is_file())
    candidates.extend(path for path in Path(paths['scripts']).glob('pip*') if path.is_file() and re.fullmatch(r'pip(?:3(?:\.12)?)?(?:\.exe)?', path.name))
    removed = []
    for path in candidates:
        if path.is_symlink() or not path.resolve().is_relative_to(runtime.resolve()):
            raise RuntimeError(f'Unsafe interpreter build-tool path: {path}')
        if path.name.lower().startswith(('license', 'licence', 'copying', 'notice', 'copyright')):
            continue
        removed.append({'path': path.relative_to(runtime).as_posix(), 'size': path.stat().st_size, 'sha256': sha256(path), 'reason': 'build-only installer; no client package installation'})
    for entry in removed:
        (runtime / entry['path']).unlink()
    aliases = []
    if python.name == 'python3.12':
        # PBS archive aliases are materialized as files for safe ZIP extraction.
        # Tiny shell aliases avoid shipping two redundant 31 MB interpreters.
        for name in ('python', 'python3'):
            alias = python.parent / name
            if alias.is_file() and not alias.is_symlink() and sha256(alias) == sha256(python):
                aliases.append({'path': alias.relative_to(runtime).as_posix(), 'original_size': alias.stat().st_size, 'original_sha256': sha256(alias)})
                alias.write_text('#!/bin/sh\nexec "${0%/*}/python3.12" "$@"\n', encoding='utf-8')
                alias.chmod(0o755)
    report = {'removed': removed, 'interpreter_alias_wrappers': aliases, 'licenses_and_original_RECORD_retained': True}
    (runtime / 'build-tool-prune.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return {'removed_bytes': sum(entry['size'] for entry in removed), 'alias_bytes_avoided': sum(entry['original_size'] for entry in aliases)}


def build_runtime(stage: Path, wheels: list[Path], runtime_requirements: list[str], target_platform: str, target_arch: str, profile: str = "paddle", *, accelerator: str = "cpu") -> dict:
    try:
        from .portable_python import prepare_runtime, write_windows_runtime_isolation
    except ImportError:
        from portable_python import prepare_runtime, write_windows_runtime_isolation
    info = prepare_runtime(stage / 'runtime', target_platform, target_arch)
    info['source_wheels'] = [{'filename': wheel.name, 'bytes': wheel.stat().st_size,
                             'sha256': sha256(wheel), 'hash_origin': 'build-observed complete wheel bytes'}
                            for wheel in wheels]
    # Interpreter helper returns a path relative to the bundle root and pinned
    # provenance. The build host's pip drives the bundled interpreter; no pip
    # invocation or dependency installation occurs on customers' computers.
    python = Path(info["python"]).resolve()
    env = dict(__import__('os').environ, PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1', PYTHONUTF8='1')
    env.pop('PYTHONIOENCODING', None)
    for key in list(env):
        if key.upper().startswith('VSE_RUNTIME_AUDIT_'):
            env.pop(key)
    subprocess.run([sys.executable, '-m', 'pip', '--python', str(python), 'install', '--no-cache-dir', '--no-index', '--no-deps', '--no-compile', *map(str, wheels)], check=True, env=env)
    info['build_tools'] = finalize_portable_runtime(stage, python, env)
    if target_platform == 'windows':
        info['windows_isolation'] = write_windows_runtime_isolation(stage, python)
    profile_manifest = stage / 'runtime/runtime-profile.json'
    build_profile, identity = profile_identity(profile, accelerator)
    profile_manifest.write_text(json.dumps({'runtime_requirements': runtime_requirements, 'ocr_backend': profile,
                                            'accelerator': identity['accelerator'], 'build_profile': build_profile}), encoding='utf-8')
    if accelerator.startswith('cuda'):
        # Only the byte-pinned, audited static NVJitLink archive may be removed.
        # Runtime DLLs and every language/model remain intact.
        subprocess.run([str(python), '-m', 'scripts.cuda_package', '--root', str(stage),
                        '--accelerator', accelerator, '--prepare'], check=True, env=env, cwd=stage)
    prune = [str(python), str(stage / 'scripts/prune_inference_runtime.py'), '--engine', profile, '--apply', '--manifest', str(stage / 'runtime/inference-prune.json')]
    if profile == 'paddle' and (target_platform, target_arch) == ('linux', 'x64'):
        prune.append('--include-unused-openvino')
    subprocess.run(prune, check=True, env=env)
    # The Windows ._pth deliberately excludes the direct script's directory.
    # Resolve sibling helpers through the trusted application package instead.
    subprocess.run([str(python), '-m', 'scripts.runtime_dependencies', '--manifest', str(profile_manifest)], check=True, env=env, cwd=stage)
    if profile == 'directml':
        from importlib import import_module
        audit_directml_runtime = import_module('scripts.directml_package').audit_directml_runtime
        # This is metadata/byte inspection only; never import Windows DLLs on a
        # foreign host or substitute a newer DirectML runtime/license.
        info['directml_runtime'] = audit_directml_runtime(stage / 'runtime/Lib/site-packages')
    info = dict(info, python=python.relative_to(stage.resolve()).as_posix())
    return info


def portable_launchers(stage: Path, target_platform: str, target_arch: str, runtime_python: str, profile: str = "paddle") -> dict | None:
    try:
        from .portable_python import write_launchers
    except ImportError:
        from portable_python import write_launchers
    deployment = None
    options = {}
    if target_platform == 'macos':
        from scripts.macos_minimum_version import audit_minimum_system
        deployment = audit_minimum_system(stage, expected_arches=(target_arch,))
        options['minimum_macos_version'] = deployment['minimum_macos_version']
    write_launchers(stage, target_platform, target_arch, runtime_python, **options)
    return deployment


def verify_macos_deployment(root: Path, manifest: dict) -> dict:
    """Recheck actual extracted ZIP images, not wheel tags or an SDK baseline."""
    import plistlib
    from scripts.macos_minimum_version import audit_minimum_system
    recorded = manifest.get('macos_deployment')
    if not isinstance(recorded, dict) or recorded.get('schema') != 1:
        raise RuntimeError('Missing complete macOS deployment audit')
    actual = audit_minimum_system(root, expected_arches=(manifest['arch'],))
    for key in ('minimum_encoded', 'minimum_macos_version'):
        if (recorded.get(key) != actual[key]
                or type(recorded.get(key)) is not type(actual[key])):
            raise RuntimeError('macOS deployment declaration differs from the final ZIP: ' + key)
    # ZIP creation can materialize internal runtime aliases and adds non-Mach-O
    # launcher/icon/manifest files. Do not confuse those topology/count changes
    # with the deployment bound. Every originally recorded image must still
    # match; every final image, including materialized aliases, was rescanned.
    images, hashes = recorded.get('images'), recorded.get('file_sha256')
    if not isinstance(images, dict) or not images or not isinstance(hashes, dict) or set(images) != set(hashes):
        raise RuntimeError('Incomplete recorded macOS image identities')
    for path, slices in images.items():
        if actual['images'].get(path) != slices or actual['file_sha256'].get(path) != hashes[path]:
            raise RuntimeError('macOS deployment image differs from the built payload: ' + path)
    if root.name != 'Resources' or root.parent.name != 'Contents' or root.parent.parent.suffix != '.app':
        raise RuntimeError('macOS deployment audit requires the actual app bundle')
    with (root.parent / 'Info.plist').open('rb') as stream:
        info = plistlib.load(stream)
    if info.get('LSMinimumSystemVersion') != actual['minimum_macos_version']:
        raise RuntimeError('Info.plist understates or misstates the actual macOS deployment minimum')
    return actual


def create_bundle(wheelhouse: Path, output: Path, root: Path = ROOT, *, profile: str = "paddle", native_ocr: Path | None = None,
                  accelerator: str = 'cpu', onnx_models: Path | None = None, paddle_models: Path | None = None) -> Path:
    build_profile, identity = profile_identity(profile, accelerator)
    if (profile == "native") != (native_ocr is not None) or (profile == 'directml') != (onnx_models is not None):
        raise ValueError("Select native with --native-ocr, or directml with --onnx-models; Paddle remains default")
    if profile != 'paddle' and paddle_models is not None:
        raise ValueError('--paddle-models is only valid for Paddle CPU/CUDA packages')
    target_platform, target_arch = host_platform()
    version = read_version(root)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise RuntimeError("Unable to determine exact source revision")
    # Fail rather than attaching uncommitted code to the recorded commit.
    dirty = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root, text=True)
    if dirty.strip():
        raise RuntimeError("Commit tracked source changes before packaging a release")
    native = root / "backend/subfinder" / target_platform / target_arch
    if not native.is_dir() or not any(native.iterdir()):
        raise RuntimeError("Install the verified native VideoSubFinder release before packaging")
    if profile == 'paddle':
        paddle_models = paddle_models or root / 'backend/models'
        # Fail closed on missing Tamil/Telugu/Kannada (or any other model),
        # rather than relying on a customer-side PaddleX download.
        subprocess.run([sys.executable, "scripts/paddle_package_models.py", "--verify-only", str(paddle_models)], cwd=root, check=True)
    subprocess.run([sys.executable, "scripts/install_videosubfinder.py", "--verify-only"], cwd=root, check=True)
    requirements = identity['requirements']
    if accelerator.startswith('cuda'):
        requirements = profile_requirements(root, build_profile)
    wheels, runtime_requirements = runtime_wheels(wheelhouse, requirements if isinstance(requirements, list) else root / requirements)
    # Resolve the dependency graph first, then lock only the selected untouched wheels.
    lock = wheel_lock(wheelhouse, selected=wheels)
    if profile == 'native':
        try:
            from .native_package import assert_native_requirements, stage_native_artifact
        except ImportError:
            from native_package import assert_native_requirements, stage_native_artifact
        assert_native_requirements(lock)
    output.mkdir(parents=True, exist_ok=True)
    archive_path = output / archive_name(version, target_platform, target_arch, build_profile)
    name = archive_path.stem
    with tempfile.TemporaryDirectory(prefix="vse-package-") as temporary:
        archive_root = Path(temporary) / name
        stage = archive_root / 'Video Subtitle Extractor.app/Contents/Resources' if target_platform == 'macos' else archive_root
        stage.mkdir(parents=True)
        tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode("utf-8").split("\0")
        source_paths = {root / path for path in tracked if path and Path(path).parts[0] in SOURCE_DIRS}
        # These runtime additions are deliberately not committed; their own
        # upstream checksums were verified above. Never sweep arbitrary local
        # untracked files or private scratch data into a release archive.
        source_paths.update(native.rglob("*"))
        for source in sorted(source_paths):
            relative = source.relative_to(root)
            if relative.parts[:2] == ("backend", "models"):
                continue
            if not source.is_file() or not include_source(relative, target_platform, target_arch):
                continue
            if source.is_symlink():
                raise RuntimeError(f"Refusing symlink in release source: {relative}")
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        for relative in SOURCE_FILES:
            source = root / relative
            if source.is_file():
                shutil.copy2(source, stage / relative)
        shutil.copy2(root / "scripts/bundle_bootstrap.py", stage / "bootstrap.py")
        (stage / 'requirements-lock.txt').write_text('\n'.join(lock) + '\n', encoding='utf-8')
        native_info = stage_native_artifact(native_ocr, stage / 'native-ocr', target_platform) if profile == 'native' else None
        paddle_info = None
        if profile == 'paddle':
            try:
                from .paddle_package_models import stage_paddle_models
            except ImportError:
                from paddle_package_models import stage_paddle_models
            paddle_info = stage_paddle_models(paddle_models, stage / 'backend/models')
        directml_info = None
        if profile == 'directml':
            try:
                from .directml_package import stage_directml_models
            except ImportError:
                from directml_package import stage_directml_models
            directml_info = stage_directml_models(onnx_models, stage / 'directml-models')
        runtime_info = build_runtime(stage, wheels, runtime_requirements, target_platform, target_arch, profile,
                                     **({'accelerator': accelerator} if accelerator != 'cpu' else {}))
        if target_platform == 'linux':
            try:
                from .bundle_linux_runtime_libs import bundle_linux_libraries
            except ImportError:
                from bundle_linux_runtime_libs import bundle_linux_libraries
            runtime_info['native_libraries'] = bundle_linux_libraries(stage, stage / runtime_info['python'], profile=profile)
        deployment = portable_launchers(stage, target_platform, target_arch, runtime_info["python"], profile)
        app_files = {}
        if target_platform == 'macos':
            app_root = stage.parent.parent
            app_files = {path.relative_to(app_root).as_posix(): sha256(path) for path in app_root.rglob('*') if path.is_file() and not path.is_relative_to(stage)}
        manifest = {"ocr_backend": profile, "accelerator": identity['accelerator'], "build_profile": build_profile,
                    "macos_deployment": deployment if target_platform == 'macos' else None,
                    "gpu_inference_verified": False, "directml_models": directml_info, "paddle_models": paddle_info,
                    "native_ocr": native_info, "app_files": app_files, "format": 2, "version": version, "commit": revision, "platform": target_platform, "arch": target_arch, "python": "3.12", "distribution": BUNDLE_DISTRIBUTION, "runtime_python": runtime_info["python"], "runtime_provenance": runtime_info, "runtime_requirements": runtime_requirements, "files": {path.relative_to(stage).as_posix(): sha256(path) for path in sorted(stage.rglob("*")) if path.is_file()}}
        (stage / "bundle-manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in sorted(archive_root.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(archive_root.parent).as_posix())
        size_report = {"commit": revision, "platform": target_platform, "arch": target_arch,
                       "ocr_backend": profile, "accelerator": identity['accelerator'], "build_profile": build_profile,
                       "compressed_archive_bytes": archive_path.stat().st_size,
                       "extracted_payload_bytes": sum(path.stat().st_size for path in archive_root.rglob('*') if path.is_file()),
                       "build_only_wheel_bytes": sum(wheel.stat().st_size for wheel in wheels),
                       "shipped_wheelhouse_bytes": 0,
                       "note": "Payload includes interpreter and installed inference runtime; no client installation or wheel cache."}
        archive_path.with_suffix(".zip.size.json").write_text(json.dumps(size_report, indent=2) + "\n", encoding="utf-8")
    archive_path.with_suffix(".zip.sha256").write_text(f"{sha256(archive_path)}  {archive_path.name}\n", encoding="utf-8")
    return archive_path


def extract_checked(archive_path: Path, destination: Path) -> Path:
    with zipfile.ZipFile(archive_path) as archive:
        roots = set()
        for item in archive.infolist():
            # ZipInfo.filename is normalized on Windows and truncates at NUL.
            # Validate the original archive bytes' decoded name, not the
            # already-sanitized path, before extracting any archive member.
            raw_name = item.orig_filename
            path = PurePosixPath(raw_name)
            mode = item.external_attr >> 16
            if (raw_name != item.filename or "\x00" in raw_name or path.is_absolute()
                    or ".." in path.parts or "\\" in raw_name or ":" in raw_name or stat.S_ISLNK(mode)):
                raise RuntimeError(f"Unsafe release archive member: {raw_name!r}")
            if not path.parts:
                raise RuntimeError("Empty release archive member")
            roots.add(path.parts[0])
        if len(roots) != 1:
            raise RuntimeError("Release archive must have exactly one root directory")
        archive.extractall(destination)
        for item in archive.infolist():
            mode = item.external_attr >> 16
            path = destination / item.filename
            if path.is_file() and mode & 0o111:
                path.chmod(path.stat().st_mode | 0o111)
    return destination / next(iter(roots))


def verification_path(python: Path, evidence: Path, target_platform: str) -> str:
    """Control the incoming test PATH; launchers rebuild their own OS/tool PATH."""
    import os
    tools_dir = evidence / 'base-os-tools'
    tools_dir.mkdir(parents=True, exist_ok=False)
    allowed = ['dirname'] if target_platform in ('linux', 'macos') else []
    records = []
    for name in allowed:
        executable = shutil.which(name)
        if not executable:
            raise RuntimeError(f'Required base OS verification tool is missing: {name}')
        source = Path(executable).resolve()
        if not source.is_file():
            raise RuntimeError(f'Invalid base OS verification tool: {source}')
        (tools_dir / name).symlink_to(source)
        records.append({'command': name, 'path': str(source), 'sha256': sha256(source), 'purpose': 'OS shell/coreutils used by verified upstream VSF launcher; not shipped'})
    (evidence / 'base-os-tools.json').write_text(json.dumps(records, indent=2) + '\n', encoding='utf-8')
    return os.pathsep.join((str(python.parent), str(tools_dir)))


def verify_bundle(archive_path: Path, smoke: bool) -> None:
    expected = archive_path.with_suffix(".zip.sha256").read_text(encoding="utf-8").split()[0]
    if sha256(archive_path) != expected:
        raise RuntimeError(f"Archive checksum mismatch: {archive_path.name}")
    with tempfile.TemporaryDirectory(prefix="vse-verify-") as temporary:
        root = extract_checked(archive_path, Path(temporary))
        app_payload = root / 'Video Subtitle Extractor.app/Contents/Resources'
        if (app_payload / 'bundle-manifest.json').is_file():
            root = app_payload
        manifest = json.loads((root / "bundle-manifest.json").read_text(encoding="utf-8"))
        verify_payload(root, manifest)
        macos_deployment = verify_macos_deployment(root, manifest) if manifest['platform'] == 'macos' else None
        if smoke:
            evidence = Path(temporary) / "verification-evidence"
            evidence.mkdir()
            if macos_deployment is not None:
                (evidence / 'macos-deployment.json').write_text(json.dumps(macos_deployment, indent=2) + '\n', encoding='utf-8')
            verification_passed = False
            try:
                import os
                python = root / manifest['runtime_python']
                # No inherited host Python/package-manager directories on the
                # incoming PATH. The launcher supplies explicit base-OS paths;
                # loaded-runtime audits and the stock-OS gate prove isolation.
                env = dict(os.environ, PATH=verification_path(python, evidence, manifest['platform']), PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1')
                env.pop('PYTHONPATH', None)
                env.pop('PYTHONHOME', None)
                if manifest['platform'] == 'linux':
                    env['LD_LIBRARY_PATH'] = str(root / 'native-libs')
                if (root / 'wheelhouse').exists() or (root / '.venv').exists():
                    raise RuntimeError('Self-contained release must not ship installer wheels or a host venv')
                try:
                    from .standalone_runtime_probe import audit_environment, summarize_reports, audit_windows_executables, audit_macos_executables
                except ImportError:
                    from standalone_runtime_probe import audit_environment, summarize_reports, audit_windows_executables, audit_macos_executables
                isolation_reports = evidence / 'dependency-isolation'
                env.update(audit_environment(root, isolation_reports))
                if manifest['platform'] == 'windows':
                    audit_windows_executables(root, python, isolation_reports)
                elif manifest['platform'] == 'macos':
                    audit_macos_executables(root, isolation_reports)
                try:
                    from .portable_python import launcher_command
                except ImportError:
                    from portable_python import launcher_command
                arguments = ['--verify', '--results-dir', str(evidence), '--runtime-audit-dir', str(isolation_reports)]
                if manifest.get('ocr_backend', 'paddle') == 'paddle':
                    # The hash-pinned fixtures are build-side evidence, not
                    # customer payload or a source-code runtime substitute.
                    fixtures = (ROOT / 'native/ocr/tests/evidence').resolve(strict=True)
                    arguments += ['--paddle-model-fixtures', str(fixtures)]
                command = launcher_command(root, manifest['platform'], arguments)
                subprocess.run(command, check=True, cwd=root, env=env)
                required = ('PySide6.QtWidgets', 'cv2', 'qfluentwidgets') + ({'paddle': ('paddle',), 'directml': ('onnxruntime',)}.get(manifest.get('ocr_backend', 'paddle'), ()))
                forbidden = ('paddle', 'paddleocr', 'paddlex', 'onnxruntime') if manifest.get('ocr_backend') == 'native' else ('paddle', 'paddleocr', 'paddlex') if manifest.get('ocr_backend') == 'directml' else ()
                summarize_reports(root, isolation_reports, required_modules=required, forbidden_modules=forbidden, require_isolated=manifest['platform'] == 'windows', require_native_workers=manifest['platform'] == 'windows' and manifest.get('ocr_backend') == 'native')
                verification_passed = True
            finally:
                # Preserve extraction evidence before TemporaryDirectory cleanup,
                # especially when the child reports a path in a failed smoke.
                if evidence.is_dir():
                    (evidence / 'package-verification.json').write_text(json.dumps({
                        'schema': 1, 'result': 'passed' if verification_passed else 'failed',
                        'archive': archive_path.name, 'archive_sha256': expected,
                        **{key: manifest.get(key) for key in ('commit', 'version', 'platform', 'arch', 'build_profile')},
                        'payload': 'passed', 'launcher_smoke': 'passed' if verification_passed else 'failed',
                        'gpu_inference_verified': False}, indent=2) + '\n', encoding='utf-8')
                    prune_manifest = root / 'runtime/inference-prune.json'
                    if prune_manifest.is_file():
                        shutil.copy2(prune_manifest, evidence / 'inference-prune.json')
                    shutil.copytree(evidence, archive_path.parent / "test-results" / archive_path.stem, dirs_exist_ok=True, ignore=lambda directory, names: ["base-os-tools"] if Path(directory) == evidence else [])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--profile", choices=("paddle", "native", "directml"), default="paddle")
    parser.add_argument("--accelerator", choices=('cpu', 'cuda11.8', 'cuda12.6'), default='cpu')
    parser.add_argument("--onnx-models", type=Path, help="Verified complete 15-model converted artifact for DirectML")
    parser.add_argument("--paddle-models", type=Path, help="Verified complete 15-model Paddle source tree; no client downloads")
    parser.add_argument("--native-ocr", type=Path, help="Verified complete native CPU artifact (build-side only)")
    parser.add_argument("--output", type=Path, default=Path("dist"))
    parser.add_argument("--verify", type=Path, help="Verify all bundle ZIPs in this directory")
    parser.add_argument("--smoke", action="store_true", help="Test included interpreter/runtime with no client installation or host Python")
    args = parser.parse_args()
    if args.verify:
        archives = sorted(args.verify.glob("*.zip"))
        if not archives:
            parser.error("No release ZIP files found")
        for archive in archives:
            verify_bundle(archive, args.smoke)
            print(f"Verified: {archive}")
    elif args.wheelhouse:
        print(create_bundle(args.wheelhouse, args.output, profile=args.profile, native_ocr=args.native_ocr,
                            accelerator=args.accelerator, onnx_models=args.onnx_models, paddle_models=args.paddle_models))
    else:
        parser.error("Specify --wheelhouse or --verify")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
