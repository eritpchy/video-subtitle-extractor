"""Create and verify native CPU source + offline-wheelhouse release archives."""
from __future__ import annotations

import argparse
import ast
from email.parser import BytesParser
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile

try:
    from .bundle_bootstrap import host_platform, sha256, verify_payload
except ImportError:
    from bundle_bootstrap import host_platform, sha256, verify_payload

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ("backend", "ui", "design", "scripts", "docs", "tests")
SOURCE_FILES = ("gui.py", "requirements.txt", "LICENSE", "README.md", "README_en.md", "Dockerfile", ".dockerignore")
EXCLUDED_PARTS = {"__pycache__", ".pytest_cache", ".venv", ".git"}


def read_version(root: Path = ROOT) -> str:
    tree = ast.parse((root / "backend/config.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, str) and re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", value):
                return value
    raise RuntimeError("backend/config.py must define a literal semantic VERSION")


def include_source(relative: Path, target_platform: str, target_arch: str) -> bool:
    if any(part in EXCLUDED_PARTS for part in relative.parts) or relative.suffix in {".pyc", ".pyo", ".log"}:
        return False
    if relative.parts[:2] == ("backend", "subfinder"):
        # Only the verified new native install, never obsolete bundled binaries.
        return relative.parts[2:4] == (target_platform, target_arch)
    return True


def wheel_lock(wheelhouse: Path) -> list[str]:
    packages: dict[str, str] = {}
    wheels = sorted(wheelhouse.glob("*.whl"))
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


def create_bundle(wheelhouse: Path, output: Path, root: Path = ROOT) -> Path:
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
    if not (root / "backend/models/V6").is_dir():
        raise RuntimeError("Download PP-OCRv6 small and medium models before packaging")
    subprocess.run([sys.executable, "-m", "backend.tools.model_download", "--tiers", "small", "medium", "--verify-only"], cwd=root, check=True)
    subprocess.run([sys.executable, "scripts/install_videosubfinder.py", "--verify-only"], cwd=root, check=True)
    lock = wheel_lock(wheelhouse)
    output.mkdir(parents=True, exist_ok=True)
    name = f"vse-{version}-{target_platform}-{target_arch}-cpu-python312"
    archive_path = output / f"{name}.zip"
    with tempfile.TemporaryDirectory(prefix="vse-package-") as temporary:
        stage = Path(temporary) / name
        stage.mkdir()
        tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode("utf-8").split("\0")
        source_paths = {root / path for path in tracked if path and Path(path).parts[0] in SOURCE_DIRS}
        # These runtime additions are deliberately not committed; their own
        # upstream checksums were verified above. Never sweep arbitrary local
        # untracked files or private scratch data into a release archive.
        source_paths.update(native.rglob("*"))
        source_paths.update((root / "backend/models/V6").rglob("*"))
        for source in sorted(source_paths):
            relative = source.relative_to(root)
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
        shutil.copytree(wheelhouse, stage / "wheelhouse", ignore=lambda _, names: [name for name in names if not name.endswith(".whl")])
        (stage / "requirements-lock.txt").write_text("\n".join(lock) + "\n", encoding="utf-8")
        (stage / "Start VSE.cmd").write_text('@echo off\r\ncd /d "%~dp0"\r\npy -3.12 bootstrap.py %*\r\nif errorlevel 1 pause\r\n', encoding="utf-8")
        launcher = '#!/bin/sh\nset -eu\ncd -- "$(dirname -- "$0")"\nexec python3.12 bootstrap.py "$@"\n'
        for filename in ("start-vse.sh", "Start VSE.command"):
            path = stage / filename
            path.write_text(launcher, encoding="utf-8")
            path.chmod(0o755)
        manifest = {"format": 1, "version": version, "commit": revision, "platform": target_platform, "arch": target_arch, "python": "3.12", "distribution": "CPU source and offline wheels; requires Python 3.12", "files": {path.relative_to(stage).as_posix(): sha256(path) for path in sorted(stage.rglob("*")) if path.is_file()}}
        (stage / "bundle-manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(stage.parent).as_posix())
    archive_path.with_suffix(".zip.sha256").write_text(f"{sha256(archive_path)}  {archive_path.name}\n", encoding="utf-8")
    return archive_path


def extract_checked(archive_path: Path, destination: Path) -> Path:
    with zipfile.ZipFile(archive_path) as archive:
        roots = set()
        for item in archive.infolist():
            path = PurePosixPath(item.filename)
            mode = item.external_attr >> 16
            if path.is_absolute() or ".." in path.parts or "\\" in item.filename or ":" in item.filename or stat.S_ISLNK(mode):
                raise RuntimeError(f"Unsafe release archive member: {item.filename}")
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


def verify_bundle(archive_path: Path, smoke: bool) -> None:
    expected = archive_path.with_suffix(".zip.sha256").read_text(encoding="utf-8").split()[0]
    if sha256(archive_path) != expected:
        raise RuntimeError(f"Archive checksum mismatch: {archive_path.name}")
    with tempfile.TemporaryDirectory(prefix="vse-verify-") as temporary:
        root = extract_checked(archive_path, Path(temporary))
        manifest = json.loads((root / "bundle-manifest.json").read_text(encoding="utf-8"))
        verify_payload(root, manifest)
        if smoke:
            try:
                subprocess.run([sys.executable, str(root / "bootstrap.py"), "--verify"], check=True, cwd=root)
            finally:
                # Preserve extraction evidence before TemporaryDirectory cleanup,
                # especially when the child reports a path in a failed smoke.
                evidence = root / "test-results"
                if evidence.is_dir():
                    shutil.copytree(evidence, archive_path.parent / "test-results" / archive_path.stem, dirs_exist_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--output", type=Path, default=Path("dist"))
    parser.add_argument("--verify", type=Path, help="Verify all bundle ZIPs in this directory")
    parser.add_argument("--smoke", action="store_true", help="Offline-install extracted bundle and test real GUI/CLI")
    args = parser.parse_args()
    if args.verify:
        archives = sorted(args.verify.glob("*.zip"))
        if not archives:
            parser.error("No release ZIP files found")
        for archive in archives:
            verify_bundle(archive, args.smoke)
            print(f"Verified: {archive}")
    elif args.wheelhouse:
        print(create_bundle(args.wheelhouse, args.output))
    else:
        parser.error("Specify --wheelhouse or --verify")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
