#!/usr/bin/env python3
"""Install the official checksummed portable videosubfinder-cli release.

No GitHub API/authentication or host multimedia libraries are needed. For an
existing offline download, pass --archive FILE --checksums SHA256SUMS.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.tools.videosubfinder import (VSF_RELEASE_BASE, VSF_VERSION, VideoSubFinderError,
    default_install_dir, find_binary, platform_target, release_asset, verify_binary, run_command)

RELEASE_LOCK_PATH = ROOT / "backend" / "tools" / "videosubfinder_release.json"


def checksum_for(manifest, filename):
    matches = []
    for line in manifest.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2 and parts[1].lstrip("*") == filename:
            if not re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
                raise ValueError(f"Invalid checksum for {filename}")
            matches.append(parts[0].lower())
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one SHA256SUMS entry for {filename}")
    return matches[0]


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_release_manifest(manifest, version):
    """Fail closed if the pinned release's official checksum manifest changes.

    Explicit other versions still use their own official SHA256SUMS and exact
    native --version checks, but are not certified by this committed lock.
    """
    if version != VSF_VERSION:
        return
    lock = json.loads(RELEASE_LOCK_PATH.read_text(encoding="utf-8"))
    if not isinstance(lock, dict):
        raise ValueError("Invalid committed VideoSubFinder release lock")
    expected = lock.get("sha256sums_sha256", "")
    if (lock.get("schema_version") != 1 or lock.get("version") != version
            or lock.get("manifest_url") != f"{VSF_RELEASE_BASE}/v{version}/SHA256SUMS"
            or not isinstance(expected, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected)):
        raise ValueError("Invalid committed VideoSubFinder release lock")
    actual = file_sha256(manifest)
    if actual != expected:
        raise ValueError(f"Pinned SHA256SUMS mismatch for VideoSubFinder {version}: "
                         f"expected {expected}, got {actual}")


def safe_member(root, name):
    # Archives are portable across hosts: reject Windows drives/backslashes even
    # when installing on POSIX. No symlinks or special files are accepted.
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
        raise ValueError(f"Unsafe archive path: {name}")
    return root.joinpath(*path.parts)


def extract_archive(archive, destination):
    destination = Path(destination)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as package:
            for member in package.infolist():
                target = safe_member(destination, member.filename)
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
                    raise ValueError(f"Unsupported archive member: {member.filename}")
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with package.open(member) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
    else:
        with tarfile.open(archive, "r:gz") as package:
            for member in package:
                target = safe_member(destination, member.name)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with package.extractfile(member) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
                    target.chmod(member.mode & 0o755)
                else:
                    raise ValueError(f"Unsupported archive member: {member.name}")


def preserve_settings(default_file, previous_file):
    """Overlay existing VSE tuning on new defaults, migrating legacy key names."""
    aliases = {"text_procent": "text_percent", "min_text_len_(in_procent)": "min_text_len_in_percent"}
    values = {}
    for path in (default_file, previous_file):
        if path is None or not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if "=" not in line or line.lstrip().startswith(("#", ";")):
                continue
            key, value = (part.strip() for part in line.split("=", 1))
            values[aliases.get(key, key)] = value
    alignment = values.get("text_alignment", "0")
    values["text_alignment"] = {"center": "0", "left": "1", "right": "2", "any": "3"}.get(alignment.lower(), alignment)
    # GPU activation requires --use_cuda at extraction time. Never inherit a
    # GUI setting that bypasses the CLI's compile-time/device support checks.
    values["use_cuda_gpu"] = "0"
    default_file.write_text("".join(f"{key} = {value}\n" for key, value in values.items()), encoding="utf-8")


def download(url, target):
    request = urllib.request.Request(url, headers={"User-Agent": "video-subtitle-extractor/2.3.0"})
    with urllib.request.urlopen(request, timeout=120) as source, Path(target).open("wb") as output:
        shutil.copyfileobj(source, output)


def install(*, version=VSF_VERSION, system=None, arch=None, destination=None,
            cuda=False, archive=None, checksums=None, verify_only=False):
    version = version.lstrip("v")
    if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", version):
        raise ValueError("Version must be a release number, for example 6.10.5")
    system, arch = platform_target(system, arch)
    destination = Path(destination or default_install_dir(ROOT / "backend", system, arch)).resolve()
    host_system, host_arch = platform_target()
    native = system == host_system and arch in (host_arch, "universal")
    if verify_only:
        binary = find_binary(destination, system)
        if not binary:
            raise VideoSubFinderError(f"No CLI installed at {destination}")
        if not native:
            raise VideoSubFinderError("Cannot execute a cross-platform CLI on this host")
        validated_version = verify_binary(binary, version)
        with tempfile.TemporaryDirectory(prefix="vsf-verify-") as temporary:
            run_command([str(binary), "-ccti", "-o", temporary])
        print(f"Verified {binary}: {validated_version} (runtime and settings)")
        return binary
    if bool(archive) != bool(checksums):
        raise ValueError("Offline installation requires both --archive and --checksums")
    asset = release_asset(system, arch, cuda)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vsf-install-", dir=destination.parent) as temporary:
        work = Path(temporary)
        package, manifest = work / asset, work / "SHA256SUMS"
        if archive:
            shutil.copyfile(archive, package)
            shutil.copyfile(checksums, manifest)
        else:
            base = f"{VSF_RELEASE_BASE}/v{version}"
            print(f"Downloading {base}/{asset}", flush=True)
            download(f"{base}/SHA256SUMS", manifest)
        verify_release_manifest(manifest, version)
        expected = checksum_for(manifest.read_text(encoding="utf-8"), asset)
        if not archive:
            download(f"{base}/{asset}", package)
        actual = file_sha256(package)
        if actual != expected:
            raise ValueError(f"SHA-256 mismatch for {asset}: expected {expected}, got {actual}")
        unpacked = work / "unpacked"
        unpacked.mkdir()
        extract_archive(package, unpacked)
        binary = find_binary(unpacked, system)
        if not binary or not (unpacked / "settings" / "general.cfg").is_file():
            raise VideoSubFinderError("Archive is missing the CLI or settings/general.cfg")
        if system != "windows":
            for name in ("VideoSubFinderCli", "VideoSubFinderCli.run"):
                candidate = unpacked / name
                if candidate.is_file():
                    candidate.chmod(0o755)
        previous_settings = destination / "settings" / "general.cfg"
        if not previous_settings.is_file():
            previous_settings = ROOT / "backend" / "subfinder" / system / "settings" / "general.cfg"
        preserve_settings(unpacked / "settings" / "general.cfg", previous_settings)
        if native:
            verify_binary(binary, version)
            # --version exits before reading settings. Empty CCTI validates the
            # migrated settings and runtime without requiring video/OCR tools.
            run_command([str(binary), "-ccti", "-o", str(work / "settings-check")])
        (unpacked / "install.json").write_text(json.dumps({
            "version": version, "platform": system, "arch": arch, "cuda": cuda,
            "asset": asset, "sha256": actual,
            "source": f"{VSF_RELEASE_BASE}/v{version}/{asset}",
            "sha256sums_sha256": file_sha256(manifest), "release_locked": version == VSF_VERSION,
        }, indent=2) + "\n", encoding="utf-8")
        # Keep the old installation intact until checksum, extraction and native
        # launch checks pass; roll back if the final directory replacement fails.
        backup = work / "previous"
        if destination.exists():
            destination.rename(backup)
        try:
            unpacked.rename(destination)
        except BaseException:
            if backup.exists():
                backup.rename(destination)
            raise
    binary = find_binary(destination, system)
    print(f"Installed videosubfinder-cli {version}: {binary} (SHA-256 verified)")
    if not native:
        print("Cross-platform package: execution must be verified on the target OS/CPU.")
    return binary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=VSF_VERSION)
    parser.add_argument("--platform", choices=("linux", "windows", "macos"), dest="system")
    parser.add_argument("--arch", choices=("x64", "arm64", "universal"))
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--cuda", action="store_true", help="Install Linux x64 CUDA 12 variant")
    parser.add_argument("--archive", type=Path, help="Offline release archive")
    parser.add_argument("--checksums", type=Path, help="Offline official SHA256SUMS")
    parser.add_argument("--verify-only", action="store_true")
    try:
        install(**vars(parser.parse_args(argv)))
    except (OSError, ValueError, VideoSubFinderError, tarfile.TarError, zipfile.BadZipFile) as error:
        print(f"VideoSubFinder installation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
