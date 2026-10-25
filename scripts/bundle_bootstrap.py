"""Install a CPU release's offline wheels and launch its GUI or CLI.

The release intentionally requires native Python 3.12; it is not a frozen binary.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
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


def verify_payload(root: Path, manifest: dict) -> None:
    for relative, expected in manifest["files"].items():
        path = root / relative
        if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
            raise RuntimeError(f"Missing or invalid bundle file: {relative}")
        if sha256(path) != expected:
            raise RuntimeError(f"Bundle checksum mismatch: {relative}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="Verify files, offline installation, dependencies and GUI startup")
    parser.add_argument("--install-only", action="store_true", help="Install offline dependencies without opening the GUI")
    parser.add_argument("--cli", action="store_true", help="Pass remaining arguments to backend.cli instead of the GUI")
    args, forwarded = parser.parse_known_args(argv)
    root = Path(__file__).resolve().parent
    manifest_path = root / "bundle-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("This bundle requires Python 3.12. Install it from https://www.python.org/downloads/.")
    if host_platform() != (manifest["platform"], manifest["arch"]):
        raise RuntimeError(f"Wrong CPU bundle: requires {manifest['platform']}/{manifest['arch']}, got {host_platform()}")
    os.chdir(root)
    venv = root / ".venv"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    marker = venv / ".bundle-sha256"
    fingerprint = sha256(manifest_path)
    install_needed = not python.is_file() or not marker.is_file() or marker.read_text(encoding="utf-8").strip() != fingerprint
    if args.verify or install_needed:
        verify_payload(root, manifest)
    if install_needed:
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        subprocess.run([str(python), "-m", "pip", "install", "--no-index", "--find-links", str(root / "wheelhouse"), "-r", "requirements-lock.txt"], check=True)
        subprocess.run([str(python), "-m", "pip", "check"], check=True)
        marker.write_text(fingerprint + "\n", encoding="utf-8")
    if args.verify:
        subprocess.run([str(python), "-m", "pip", "check"], check=True)
        subprocess.run([str(python), "scripts/install_videosubfinder.py", "--verify-only"], check=True)
        subprocess.run([str(python), "-m", "backend.cli", "--help"], check=True)
        # The macOS frameless window needs a real Cocoa NSView. Offscreen
        # creates a different native handle and can hang inside Objective-C.
        qt_platform = "cocoa" if platform.system() == "Darwin" else "offscreen"
        env = dict(os.environ, QT_QPA_PLATFORM=qt_platform, PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK="True", VSE_OFFLINE="1")
        subprocess.run([str(python), "gui.py", "--smoke-test"], check=True, env=env, timeout=120)
        subprocess.run([str(python), "scripts/e2e_smoke.py", "--output", "test-results/bundle-e2e"], check=True, env=env, timeout=960)
        subprocess.run([str(python), "scripts/e2e_issues.py", "--output", "test-results/bundle-issues"], check=True, env=env, timeout=3660)
        return 0
    if args.install_only:
        return 0
    command = [str(python), "-m", "backend.cli"] if args.cli else [str(python), "gui.py"]
    return subprocess.call(command + forwarded)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Bundle startup failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
