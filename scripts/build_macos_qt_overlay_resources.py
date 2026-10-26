#!/usr/bin/env python3
"""Prepare reproducible, unpublished Qt overlay assets; never install or sign code.

Inputs are the immutable hosted-CI candidate and original QtBase/PS6 bytes.
Historical diagnostic receipts are preserved, not promoted to production approval.
The source tar contains complete QtBase with exactly the three PS6 hunks applied.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import lzma
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile
import tempfile
import unicodedata
import zipfile

ROOT = Path(__file__).resolve().parents[1]
RESOURCE_ROOT = "vse-qt6.12.0-macos-arm64-ax772484-ps6"
RESOURCE_NAME = RESOURCE_ROOT + ".zip"
SOURCE_NAME = "vse-qtbase6.12.0-ax772484-ps6-source.tar.xz"
NOTICE_DIR = "VSE-AX-OVERLAY"
PLUGIN_SHA256 = "d23ea2a1e3201ec85f4fd35ee8bff32d7217ce6b0bbb51058339f1abca612941"
CANDIDATE_SHA256 = "bb07a028a411edc3fa4ccfbd884846d00e618529bb7a503d5e71f6b609a4ed13"
NOTICE_SHA256 = "4b2bb35df76f8eaa67e0bf5f4806a2c4d12ab051a507de8ba5573a3bd205fb7f"
BUILD_REQUIREMENTS = "native/ocr/requirements-build.txt"
BUILD_REQUIREMENTS_SHA256 = "4a75137cf00a67e805f84dff51e26fec68eea39262a7fe8ee15fb92067f458f4"
TEST_SOURCE = "tests/auto/other/qaccessibilitymac/tst_qaccessibilitymac.mm"
HISTORICAL_FILES = {
    "scripts/build_macos_qt_accessibility.py": "9a3c18e129ea4fe57f6789f4182d487a81885fee52b0e49b8c549bf7b997b77e",
    "scripts/macos_accessibility_probe.py": "e8e5a64adff8a358b428f442927f904cf74f01f49e78c2aa280c23b973ea2dd1",
    "native/qt/constraints-macos-ax.txt": "c5f6a9221d338892bb1c8637307dfa504847335a51b61a794f01d7696ae95279",
    "native/qt/macos-accessibility-pins.json": "fba0911a15284c2f5a3c69f401cabee1b3792e048c6dcd81d72f9e1161eccc6b",
    "docs/audits/macos-accessibility-crash.md": "66192a2a5060b0dacefa4fdca3186fcc82763aef51eae5703833a4e16c95364d",
}
MAX_MEMBERS = 100_000
MAX_SOURCE_BYTES = 2 * 1024**3
MAX_CANDIDATE_BYTES = 16 * 1024**2
MAX_CANDIDATE_ENTRIES = 4096


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(data) -> bytes:
    return (json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def regular_file(path: Path) -> None:
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"Not a regular file (symlinks are forbidden): {path}")


def sha256(path: Path) -> str:
    regular_file(path)
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_checked(path: Path, expected: str) -> bytes:
    regular_file(path)
    data = path.read_bytes()
    if digest(data) != expected:
        raise ValueError(f"Input SHA256 mismatch: {path}")
    return data


def safe_name(name: str) -> str:
    if (not name or name.startswith("/") or "\\" in name or ":" in name
            or any(unicodedata.category(c) in {"Cc", "Cf"} for c in name)):
        raise ValueError(f"Unsafe archive name: {name!r}")
    if any(part in {"", ".", ".."} for part in name.split("/")):
        raise ValueError(f"Unsafe archive name: {name!r}")
    return name


def validate_names(entries: list[tuple[str, bool]]) -> None:
    seen, canonical = {}, {}
    for name, is_dir in entries:
        safe_name(name)
        if name in seen:
            raise ValueError(f"Duplicate archive member: {name}")
        seen[name] = is_dir
        parts = name.split("/")
        for count in range(1, len(parts) + 1):
            path = "/".join(parts[:count])
            folded = unicodedata.normalize("NFC", path).casefold()
            if folded in canonical and canonical[folded] != path:
                raise ValueError(f"Ambiguous archive path: {path}")
            canonical[folded] = path
    for name in seen:
        for parent in PurePosixPath(name).parents:
            if str(parent) != "." and seen.get(str(parent)) is False:
                raise ValueError(f"File/directory archive conflict: {name}")


def source_members(source: tarfile.TarFile, root: str) -> list[tarfile.TarInfo]:
    safe_name(root)
    if "/" in root:
        raise ValueError("Source root must be one path component")
    members, entries, total = [], [], 0
    for item in source:
        if len(members) >= MAX_MEMBERS:
            raise ValueError("Source member count limit exceeded")
        name = item.name.rstrip("/") if item.isdir() else item.name
        if (name != root and not name.startswith(root + "/")) or name.startswith(root + "/" + NOTICE_DIR):
            raise ValueError(f"Unexpected source root/notice entry: {name}")
        if item.type not in {tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE} or item.size < 0 or (item.isdir() and item.size):
            raise ValueError(f"Unsupported source archive member: {name}")
        if item.pax_headers.keys() - {"path", "size", "mtime", "atime", "ctime", "uid", "gid", "uname", "gname"}:
            raise ValueError(f"Unsupported source PAX metadata: {name}")
        total += item.size
        if total > MAX_SOURCE_BYTES:
            raise ValueError("Source expanded size limit exceeded")
        item.name = name
        entries.append((name, item.isdir()))
        members.append(item)
    if not members or not any(item.name == root and item.isdir() for item in members):
        raise ValueError("Source root directory is missing")
    validate_names(entries)
    return members


def candidate_files(candidate: Path) -> dict[str, bytes]:
    if not stat.S_ISDIR(candidate.lstat().st_mode):
        raise ValueError("Candidate root must be a real directory")
    result, entries, total = {}, [], 0
    for directory, dirs, files in os.walk(candidate, followlinks=False):
        for name in sorted(dirs + files):
            path = Path(directory) / name
            mode = path.lstat().st_mode
            relative = path.relative_to(candidate).as_posix()
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ValueError(f"Unsupported candidate file: {relative}")
            entries.append((relative, stat.S_ISDIR(mode)))
            if len(entries) > MAX_CANDIDATE_ENTRIES:
                raise ValueError("Candidate entry count limit exceeded")
            if stat.S_ISREG(mode):
                total += path.stat().st_size
                if total > MAX_CANDIDATE_BYTES:
                    raise ValueError(f"Candidate input unexpectedly large: {relative}")
                result[relative] = path.read_bytes()
    validate_names(entries)
    return result


def apply_patch(originals: dict[str, bytes], patch: bytes) -> dict[str, bytes]:
    """Apply only the explicit, already SHA-verified three-file PS6 contract."""
    paths = re.findall(rb"^diff --git a/(\S+) b/(\S+)$", patch, re.MULTILINE)
    expected = set(originals)
    if (len(paths) != len(expected) or {a.decode() for a, _ in paths} != expected
            or any(a != b for a, b in paths)):
        raise ValueError("Patch touches unexpected or duplicate paths")
    with tempfile.TemporaryDirectory(prefix="vse-qt-ps6-") as temp:
        root = Path(temp)
        for name, data in originals.items():
            safe_name(name)
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        patch_path = root / "overlay.patch"
        patch_path.write_bytes(patch)
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_COUNT="0")
        for command in (["git", "init", "--quiet", "--template=", temp],
                        ["git", "-C", temp, "apply", "--check", str(patch_path)],
                        ["git", "-C", temp, "apply", str(patch_path)]):
            subprocess.run(command, env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return {name: (root / name).read_bytes() for name in originals}


def tar_info(name: str, size: int = 0, *, directory=False, executable=False) -> tarfile.TarInfo:
    item = tarfile.TarInfo(name)
    item.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    item.mode = 0o755 if directory or executable else 0o644
    item.size = 0 if directory else size
    item.uid = item.gid = item.mtime = 0
    item.uname = item.gname = ""
    return item


class HashReader:
    def __init__(self, stream):
        self.stream = stream
        self.hash = hashlib.sha256()

    def read(self, size=-1):
        data = self.stream.read(size)
        self.hash.update(data)
        return data


def rebuild_instructions() -> bytes:
    return b'''VSE Qt Cocoa overlay corresponding source / rebuild instructions

This is NOT an official Qt binary or Qt release. The original candidate.json
is historical evidence, not a production approval; its false guard is unchanged.
The preserved MODIFICATIONS.txt describes the historical CI artifact layout;
the current corresponding source is the separately SHA-bound source asset. The
original unmodified Qt archive is available at the URL and hash in pins.
The source tree is complete QtBase 6.12.0 with exactly Gerrit 772484 PS6 applied:
two Cocoa implementation files and its upstream macOS accessibility regression
test. The original patch, pins, all original notices/licenses, and the exact
historical build scripts are included. No accessibility features are disabled.

To rebuild the already-patched tree on macOS arm64, select Xcode 26.0.1 build
17A400 (SDK 26.0) and Python 3.12. In a separate build-tools venv, install the
exact hash-pinned CMake/Ninja wheels with:
  python -m pip install --require-hashes -r VSE-AX-OVERLAY/native/ocr/requirements-build.txt
This requirements file has the exact bytes from the original build commit.
VSE-AX-OVERLAY/native/qt/constraints-macos-ax.txt constrains the separate Qt/GUI
runtime acceptance environment, NOT the CMake/Ninja build tools.
From a fresh separate build folder, run the source configure path with these
arguments:
  -opensource -confirm-license -release -nomake examples -nomake tests
  -prefix /absolute/unused-install-prefix -- -DCMAKE_OSX_ARCHITECTURES=arm64
  -DCMAKE_OSX_DEPLOYMENT_TARGET=14.4 -DCMAKE_SKIP_BUILD_RPATH=OFF
  -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -DFEATURE_accessibility=ON
Then run:
  cmake --build . --target QCocoaIntegrationPlugin --parallel 3
Copy only plugins/platforms/libqcocoa.dylib to a separate staging directory.
Following relocate_candidate() in the included historical build script, remove
only the reviewed build/lib LC_RPATH(s), add @loader_path/../../lib, ad-hoc sign
the copy, and verify it. Never replace QtCore/QtGui/PySide/Shiboken. Rebuilds can
have different Mach-O UUID/signature bytes: ABI and native/external AX acceptance
are required independently; this preparation script never builds/signs/installs.

Alternatively, run the included historical build script in the matching VSE
source/hosted-CI layout. It downloads the exact original archive by SHA and
applies the exact patch, rather than patching this already-patched tree again.
The source-manifest.json records original and patched file-inventory digests
and individual changed-file hashes. resource-manifest.json in the binary ZIP
binds this complete source asset by SHA256 and byte size. ZIP manifests exclude
themselves to avoid a cyclic hash; the whole asset SHA is pinned by consumers.
Licenses include LGPL-3.0-only plus applicable GPL/BSD/third-party notices.
'''


def build_source(archive: Path, output: Path, settings: dict, patch: bytes,
                 candidate: dict[str, bytes], extras: dict[str, bytes]) -> dict:
    if sha256(archive) != settings["source"]["sha256"]:
        raise ValueError("Original source SHA256 mismatch")
    if digest(patch) != settings["patch"]["decoded_sha256"]:
        raise ValueError("Original patch SHA256 mismatch")
    root = settings["source"]["root"]
    changes = set(settings["patched_sources"]) | {TEST_SOURCE}
    with tarfile.open(archive, "r:xz") as source:
        members = source_members(source, root)
        by_name = {m.name: m for m in members}
        originals = {}
        for name in changes:
            item = by_name.get(root + "/" + name)
            if item is None or not item.isfile() or item.size > 1024**2:
                raise ValueError(f"Expected patch source missing/oversized: {name}")
            with source.extractfile(item) as stream:
                originals[name] = stream.read()
        replacements = apply_patch(originals, patch)
        for name, expected in settings["patched_sources"].items():
            if digest(replacements[name]) != expected:
                raise ValueError(f"Patched source SHA256 mismatch: {name}")
        # License/attribution and all Cocoa source in the small ZIP must match
        # the corresponding original or precisely patched complete source tree.
        checked = set()
        for name, data in candidate.items():
            if name.startswith("LICENSES/"):
                relative = name
            elif name.startswith("cocoa-modified-source/"):
                relative = "src/plugins/platforms/cocoa/" + name.split("/", 1)[1]
            else:
                continue
            item = by_name.get(root + "/" + relative)
            if item is None or not item.isfile():
                raise ValueError(f"Candidate source/license missing upstream: {name}")
            if relative in replacements:
                expected_data = replacements[relative]
            else:
                with source.extractfile(item) as stream:
                    expected_data = stream.read()
            if data != expected_data:
                raise ValueError(f"Candidate source/license differs: {name}")
            checked.add(relative)
        expected_checked = {m.name[len(root) + 1:] for m in members if m.isfile() and (
            m.name.startswith(root + "/LICENSES/") or m.name.startswith(root + "/src/plugins/platforms/cocoa/"))}
        if checked != expected_checked:
            raise ValueError("Candidate omitted corresponding Cocoa source/license files")
        original_inventory, patched_inventory = {}, {}
        metadata = {**extras, "qtbase-772484-ps6.patch": patch,
                    "candidate.json": candidate["candidate.json"],
                    "REBUILD.txt": rebuild_instructions(),
                    "MODIFICATIONS.txt": candidate["MODIFICATIONS.txt"]}
        with output.open("xb") as raw, lzma.LZMAFile(raw, "w", format=lzma.FORMAT_XZ,
                check=lzma.CHECK_CRC64, preset=6) as compressed, tarfile.open(
                fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as target:
            # Preserve the original pinned archive's order; no random filesystem
            # enumeration or backward compressed-stream seek during the copy.
            for item in members:
                relative = item.name[len(root) + 1:]
                if item.isdir():
                    target.addfile(tar_info(item.name, directory=True))
                    continue
                with source.extractfile(item) as stream:
                    reader = HashReader(stream)
                    if relative in replacements:
                        while reader.read(1024 * 1024):
                            pass
                        data = replacements[relative]
                        target.addfile(tar_info(item.name, len(data), executable=bool(item.mode & 0o111)), io.BytesIO(data))
                        patched_inventory[relative] = digest(data)
                    else:
                        target.addfile(tar_info(item.name, item.size, executable=bool(item.mode & 0o111)), reader)
                        patched_inventory[relative] = reader.hash.hexdigest()
                    original_inventory[relative] = reader.hash.hexdigest()
            changed = {name for name in original_inventory if original_inventory[name] != patched_inventory[name]}
            if changed != changes:
                raise ValueError("Source change set differs from the three-file PS6 contract")
            receipt = {"schema": 1, "asset_kind": "qtbase-corresponding-source",
                       "pins": settings, "original_source_sha256": settings["source"]["sha256"],
                       "original_source_bytes": archive.stat().st_size,
                       "original_file_count": len(original_inventory),
                       "original_file_inventory_sha256": digest(json_bytes(original_inventory)),
                       "patched_file_inventory_sha256": digest(json_bytes(patched_inventory)),
                       "changed_files": {name: {"original_sha256": original_inventory[name],
                                                "patched_sha256": patched_inventory[name]} for name in sorted(changed)},
                       "added_files_prefix": NOTICE_DIR + "/",
                       "production_integration_allowed": False,
                       "metadata_files": {name: digest(data) for name, data in sorted(metadata.items())}}
            metadata["source-manifest.json"] = json_bytes(receipt)
            entries = [(NOTICE_DIR, True)]
            for name in metadata:
                safe_name(name)
                entries.append((NOTICE_DIR + "/" + name, False))
            validate_names(entries)
            directories = {NOTICE_DIR}
            for name in metadata:
                directories.update(str(p) for p in PurePosixPath(NOTICE_DIR + "/" + name).parents if str(p) != ".")
            for name in sorted(directories):
                target.addfile(tar_info(root + "/" + name, directory=True))
            for name, data in sorted(metadata.items()):
                target.addfile(tar_info(root + "/" + NOTICE_DIR + "/" + name, len(data)), io.BytesIO(data))
    if sha256(archive) != settings["source"]["sha256"]:
        raise ValueError("Original source changed while preparing asset")
    return receipt


def write_resource_zip(output: Path, files: dict[str, bytes]) -> None:
    validate_names([(name, False) for name in files])
    with output.open("xb") as raw, zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_STORED) as archive:
        # STORE deliberately avoids dependence on a host zlib implementation.
        for name, data in sorted(files.items()):
            item = zipfile.ZipInfo(RESOURCE_ROOT + "/" + name, date_time=(1980, 1, 1, 0, 0, 0))
            item.create_system = 3
            item.external_attr = (stat.S_IFREG | (0o755 if name == "libqcocoa.dylib" else 0o644)) << 16
            item.compress_type = zipfile.ZIP_STORED
            archive.writestr(item, data)


def build_resources(*, candidate_dir: Path, source_archive: Path, patch_path: Path,
                    historical_root: Path, output_dir: Path) -> dict:
    """Create both assets into a NEW directory, publishing neither on failure."""
    settings = json.loads(read_checked(historical_root / "native/qt/macos-accessibility-pins.json",
                                      HISTORICAL_FILES["native/qt/macos-accessibility-pins.json"]))
    candidate = candidate_files(candidate_dir)
    if digest(candidate["candidate.json"]) != CANDIDATE_SHA256:
        raise ValueError("Historical candidate receipt SHA256 mismatch")
    if digest(candidate["MODIFICATIONS.txt"]) != NOTICE_SHA256:
        raise ValueError("Historical modification notice SHA256 mismatch")
    receipt = json.loads(candidate["candidate.json"])
    if (receipt["pins"] != settings or receipt.get("diagnostic_only") is not True
            or receipt.get("production_integration_allowed") is not False
            or receipt.get("candidate_sha256") != PLUGIN_SHA256
            or digest(candidate["libqcocoa.dylib"]) != PLUGIN_SHA256):
        raise ValueError("Historical candidate identity/scope differs")
    if json.loads(candidate["macos-accessibility-pins.json"]) != settings:
        raise ValueError("Candidate pins differ")
    allowed = {"libqcocoa.dylib", "candidate.json", "macos-accessibility-pins.json", "MODIFICATIONS.txt"}
    if any(n not in allowed and not n.startswith(("LICENSES/", "cocoa-modified-source/")) for n in candidate):
        raise ValueError("Unexpected candidate payload file")
    extras = {name: read_checked(historical_root / name, expected) for name, expected in HISTORICAL_FILES.items()}
    # The original CI artifact omitted this repository file. Its fixed digest
    # equals commit 561de09; do not silently substitute later build tool pins.
    extras[BUILD_REQUIREMENTS] = read_checked(ROOT / BUILD_REQUIREMENTS, BUILD_REQUIREMENTS_SHA256)
    if candidate["macos-accessibility-pins.json"] != extras["native/qt/macos-accessibility-pins.json"]:
        raise ValueError("Historical candidate pins bytes differ")
    patch = read_checked(patch_path, settings["patch"]["decoded_sha256"])
    output_resolved = output_dir.resolve()
    if any(output_resolved.is_relative_to(root.resolve()) for root in (candidate_dir, historical_root)):
        raise ValueError("Output directory must be outside the immutable input trees")
    if output_dir.exists() or output_dir.is_symlink():
        raise ValueError("Output directory must not exist")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".qt-overlay-", dir=output_dir.parent) as temp:
        staging = Path(temp) / "assets"
        staging.mkdir()
        source_output = staging / SOURCE_NAME
        source_receipt = build_source(source_archive, source_output, settings, patch, candidate, extras)
        source_asset = {"name": SOURCE_NAME, "sha256": sha256(source_output), "bytes": source_output.stat().st_size}
        files = {**candidate, "qtbase-772484-ps6.patch": patch, "REBUILD.txt": rebuild_instructions(),
                 **{"rebuild/" + name: data for name, data in extras.items()},
                 "source-manifest.json": json_bytes(source_receipt)}
        manifest = {"schema": 1, "asset_kind": "vse-qt-cocoa-overlay-resource",
                    "qt_version": settings["qt_version"], "arch": "arm64",
                    "plugin": {"path": "libqcocoa.dylib", "sha256": PLUGIN_SHA256,
                               "bytes": len(files["libqcocoa.dylib"])},
                    "source_asset": source_asset, "pins": settings,
                    "historical_candidate": {"path": "candidate.json", "sha256": CANDIDATE_SHA256,
                                             "diagnostic_only": True, "production_integration_allowed": False},
                    "production_integration_allowed": False,
                    "scope": "Preparation only; preserving historical candidate evidence is not production approval",
                    "files": {name: digest(data) for name, data in sorted(files.items())}}
        files["resource-manifest.json"] = json_bytes(manifest)
        resource = staging / RESOURCE_NAME
        write_resource_zip(resource, files)
        result = {"schema": 1, "resource_asset": {"name": RESOURCE_NAME, "sha256": sha256(resource),
                  "bytes": resource.stat().st_size}, "source_asset": source_asset,
                  "resource_manifest_sha256": digest(files["resource-manifest.json"]),
                  "production_integration_allowed": False}
        (staging / "resource-build-receipt.json").write_bytes(json_bytes(result))
        for asset in (resource, source_output):
            (staging / (asset.name + ".sha256")).write_text(f"{sha256(asset)}  {asset.name}\n", encoding="ascii")
        if output_dir.exists() or output_dir.is_symlink():
            raise ValueError("Output directory appeared during build")
        staging.rename(output_dir)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-dir", required=True, type=Path)
    parser.add_argument("--source-archive", required=True, type=Path)
    parser.add_argument("--patch", required=True, type=Path, dest="patch_path")
    parser.add_argument("--historical-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(build_resources(**vars(args)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
