"""Portable resource preparation tests, not a claim of native Qt acceptance."""
import io
import json
import os
from pathlib import Path
import tarfile
import zipfile

import pytest

from scripts import build_macos_qt_overlay_resources as module


HEADER = "src/plugins/platforms/cocoa/qcocoaaccessibilityelement.h"
IMPLEMENTATION = "src/plugins/platforms/cocoa/qcocoaaccessibilityelement.mm"


def make_tar(path, entries):
    with tarfile.open(path, "w:xz") as archive:
        for name, data in entries:
            item = tarfile.TarInfo(name)
            if data is None:
                item.type = tarfile.DIRTYPE
                item.mode = 0o755
                archive.addfile(item)
            else:
                item.size = len(data)
                item.mode = 0o755 if name.endswith("configure") else 0o644
                archive.addfile(item, io.BytesIO(data))


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    root = "qtbase-everywhere-src-6.12.0"
    originals = {HEADER: b"old header\n", IMPLEMENTATION: b"old implementation\n", module.TEST_SOURCE: b"old test\n"}
    replacements = {name: data.replace(b"old ", b"new ") for name, data in originals.items()}
    patch = b"".join(
        f"diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n@@ -1 +1 @@\n".encode()
        + b"-" + originals[name] + b"+" + replacements[name] for name in sorted(originals))
    files = {**originals, "LICENSES/LGPL-3.0-only.txt": b"fixture LGPL notice\n",
             "src/plugins/platforms/cocoa/LICENSE.COCOA.txt": b"fixture Cocoa attribution\n",
             "configure": b"#!/bin/sh\n", "README.md": b"untouched\n"}
    archive = tmp_path / "original.tar.xz"
    make_tar(archive, [(root, None)] + [(root + "/" + name, data) for name, data in sorted(files.items())])
    patch_path = tmp_path / "original.patch"
    patch_path.write_bytes(patch)
    settings = {"schema": 1, "qt_version": "6.12.0", "source": {"root": root, "sha256": module.sha256(archive)},
                "patch": {"decoded_sha256": module.digest(patch)},
                "patched_sources": {name: module.digest(replacements[name]) for name in (HEADER, IMPLEMENTATION)}}
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    plugin = b"fixture original signed plugin bytes"
    monkeypatch.setattr(module, "PLUGIN_SHA256", module.digest(plugin))
    receipt = {"pins": settings, "diagnostic_only": True, "production_integration_allowed": False,
               "candidate_sha256": module.PLUGIN_SHA256}
    raw_receipt = json.dumps(receipt, indent=1).encode()
    monkeypatch.setattr(module, "CANDIDATE_SHA256", module.digest(raw_receipt))
    candidate_data = {"libqcocoa.dylib": plugin, "candidate.json": raw_receipt,
                      "MODIFICATIONS.txt": b"historical diagnostic only\n",
                      "macos-accessibility-pins.json": module.json_bytes(settings)}
    monkeypatch.setattr(module, "NOTICE_SHA256", module.digest(candidate_data["MODIFICATIONS.txt"]))
    for name, data in files.items():
        if name.startswith("LICENSES/"):
            candidate_data[name] = data
        elif name.startswith("src/plugins/platforms/cocoa/"):
            candidate_data["cocoa-modified-source/" + name.rsplit("/", 1)[1]] = replacements.get(name, data)
    for name, data in candidate_data.items():
        path = candidate / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    historical = tmp_path / "historical"
    extras = {"native/qt/macos-accessibility-pins.json": module.json_bytes(settings),
              "scripts/build_macos_qt_accessibility.py": b"# exact historical code\n"}
    for name, data in extras.items():
        path = historical / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    monkeypatch.setattr(module, "HISTORICAL_FILES", {name: module.digest(data) for name, data in extras.items()})
    build_requirements = historical / module.BUILD_REQUIREMENTS
    build_requirements.parent.mkdir(parents=True, exist_ok=True)
    build_requirements.write_bytes(b"cmake==4.1.2 --hash=sha256:fixture\nninja==1.13.0 --hash=sha256:fixture\n")
    monkeypatch.setattr(module, "ROOT", historical)
    monkeypatch.setattr(module, "BUILD_REQUIREMENTS_SHA256", module.sha256(build_requirements))
    return {"candidate_dir": candidate, "source_archive": archive, "patch_path": patch_path,
            "historical_root": historical}, settings, files, replacements


def unpack_source_files(path):
    with tarfile.open(path, "r:xz") as archive:
        members = archive.getmembers()
        return {item.name: archive.extractfile(item).read() for item in members if item.isfile()}, members


def test_assets_reproduce_and_keep_receipt_plugin_license_and_all_source_bytes(inputs, tmp_path):
    args, settings, original, replacement = inputs
    first = module.build_resources(**args, output_dir=tmp_path / "one")
    for file in args["candidate_dir"].rglob("*"):
        os.utime(file, (123456789, 123456789))
    second = module.build_resources(**args, output_dir=tmp_path / "two")
    assert first == second
    assert first["production_integration_allowed"] is False
    for name in (module.RESOURCE_NAME, module.SOURCE_NAME):
        assert (tmp_path / "one" / name).read_bytes() == (tmp_path / "two" / name).read_bytes()
    source, members = unpack_source_files(tmp_path / "one" / module.SOURCE_NAME)
    root = settings["source"]["root"]
    for name, data in original.items():
        assert source[root + "/" + name] == replacement.get(name, data)
    assert {name.removeprefix(root + "/") for name in source if "/" + module.NOTICE_DIR + "/" not in name} == set(original)
    assert all(item.uid == item.gid == item.mtime == 0 for item in members)
    source_receipt = json.loads(source[root + "/" + module.NOTICE_DIR + "/source-manifest.json"])
    assert source[root + "/" + module.NOTICE_DIR + "/" + module.BUILD_REQUIREMENTS] == (args["historical_root"] / module.BUILD_REQUIREMENTS).read_bytes()
    assert set(source_receipt["changed_files"]) == set(replacement)
    assert source_receipt["changed_files"][module.TEST_SOURCE]["patched_sha256"] == module.digest(replacement[module.TEST_SOURCE])
    with zipfile.ZipFile(tmp_path / "one" / module.RESOURCE_NAME) as archive:
        prefix = module.RESOURCE_ROOT + "/"
        manifest = json.loads(archive.read(prefix + "resource-manifest.json"))
        assert archive.read(prefix + "rebuild/" + module.BUILD_REQUIREMENTS) == (args["historical_root"] / module.BUILD_REQUIREMENTS).read_bytes()
        assert manifest["source_asset"] == first["source_asset"]
        assert manifest["plugin"] == {"path": "libqcocoa.dylib", "sha256": module.PLUGIN_SHA256, "bytes": len((args["candidate_dir"] / "libqcocoa.dylib").read_bytes())}
        assert manifest["historical_candidate"]["production_integration_allowed"] is False
        assert "resource-manifest.json" not in manifest["files"]
        assert {name.removeprefix(prefix) for name in archive.namelist()} == set(manifest["files"]) | {"resource-manifest.json"}
        for name, expected in manifest["files"].items():
            assert module.digest(archive.read(prefix + name)) == expected
        assert archive.read(prefix + "candidate.json") == (args["candidate_dir"] / "candidate.json").read_bytes()
        assert archive.read(prefix + "libqcocoa.dylib") == (args["candidate_dir"] / "libqcocoa.dylib").read_bytes()
        assert all(item.date_time == (1980, 1, 1, 0, 0, 0) and item.compress_type == zipfile.ZIP_STORED for item in archive.infolist())


@pytest.mark.parametrize("component", ["plugin", "receipt", "source", "patch", "historical", "candidate-pins", "license", "cocoa", "extra", "notice", "build-tools"])
def test_changed_inputs_rejected_without_publishing_assets(inputs, tmp_path, component):
    args, _, _, _ = inputs
    paths = {"plugin": args["candidate_dir"] / "libqcocoa.dylib",
             "receipt": args["candidate_dir"] / "candidate.json", "source": args["source_archive"],
             "patch": args["patch_path"],
             "historical": args["historical_root"] / "scripts/build_macos_qt_accessibility.py",
             "candidate-pins": args["candidate_dir"] / "macos-accessibility-pins.json",
             "license": args["candidate_dir"] / "LICENSES/LGPL-3.0-only.txt",
             "cocoa": args["candidate_dir"] / "cocoa-modified-source/qcocoaaccessibilityelement.mm",
             "extra": args["candidate_dir"] / "unreviewed.txt",
             "notice": args["candidate_dir"] / "MODIFICATIONS.txt",
             "build-tools": args["historical_root"] / module.BUILD_REQUIREMENTS}
    paths[component].write_bytes(b"modified")
    output = tmp_path / "out"
    with pytest.raises((ValueError, json.JSONDecodeError)):
        module.build_resources(**args, output_dir=output)
    assert not output.exists()
    assert not list(tmp_path.glob(".qt-overlay-*"))


def test_missing_license_is_not_silently_omitted(inputs, tmp_path):
    args, _, _, _ = inputs
    (args["candidate_dir"] / "LICENSES/LGPL-3.0-only.txt").unlink()
    with pytest.raises(ValueError, match="omitted"):
        module.build_resources(**args, output_dir=tmp_path / "out")


@pytest.mark.parametrize("entries", [
    [("../escape", False)], [("/root/a", False)], [("root/../a", False)],
    [("root/a\\b", False)], [("root/a:b", False)], [("root/a\x00b", False)],
    [("root/a\u202eb", False)], [("root/a", False), ("root/a", False)],
    [("root/A/x", False), ("root/a/y", False)], [("root/a", False), ("root/a/b", False)],
    [("root/é", False), ("root/e\u0301", False)],
])
def test_unsafe_ambiguous_archive_names_rejected(entries):
    with pytest.raises(ValueError):
        module.validate_names(entries)


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE])
def test_source_rejects_special_files_without_extraction(tmp_path, kind):
    path = tmp_path / "source.tar"
    with tarfile.open(path, "w") as archive:
        archive.addfile(module.tar_info("root", directory=True))
        item = tarfile.TarInfo("root/unsafe")
        item.type = kind
        item.linkname = "../../outside"
        archive.addfile(item)
    with tarfile.open(path) as archive, pytest.raises(ValueError, match="Unsupported"):
        module.source_members(archive, "root")
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize("limit", ["MAX_MEMBERS", "MAX_SOURCE_BYTES"])
def test_source_limits_are_bounded(tmp_path, monkeypatch, limit):
    path = tmp_path / "source.tar.xz"
    make_tar(path, [("root", None), ("root/a", b"abcd")])
    monkeypatch.setattr(module, limit, 1)
    with tarfile.open(path) as archive, pytest.raises(ValueError, match="limit"):
        module.source_members(archive, "root")


def test_patch_cannot_touch_an_unlisted_fourth_file():
    patch = b"diff --git a/a b/a\ndiff --git a/extra b/extra\n"
    with pytest.raises(ValueError, match="unexpected"):
        module.apply_patch({"a": b"before"}, patch)


def test_existing_output_not_overwritten(inputs, tmp_path):
    args, _, _, _ = inputs
    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "sentinel"
    sentinel.write_bytes(b"keep")
    with pytest.raises(ValueError, match="must not exist"):
        module.build_resources(**args, output_dir=output)
    assert sentinel.read_bytes() == b"keep"


def test_candidate_symlink_rejected(inputs, tmp_path):
    args, _, _, _ = inputs
    target = args["candidate_dir"] / "LICENSES/symlink"
    try:
        target.symlink_to(args["source_archive"])
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation not permitted")
    with pytest.raises(ValueError, match="Unsupported"):
        module.build_resources(**args, output_dir=tmp_path / "out")


def test_output_cannot_modify_candidate_or_historical_tree(inputs):
    args, _, _, _ = inputs
    for root in (args["candidate_dir"], args["historical_root"]):
        with pytest.raises(ValueError, match="outside"):
            module.build_resources(**args, output_dir=root / "new-assets")
        assert not (root / "new-assets").exists()


@pytest.mark.parametrize("name", ["root/../escape", "root/VSE-AX-OVERLAY/unreviewed", "elsewhere/file"])
def test_source_rejects_traversal_wrong_root_and_reserved_notice(tmp_path, name):
    path = tmp_path / "bad.tar.xz"
    make_tar(path, [("root", None), (name, b"bad")])
    with tarfile.open(path) as archive, pytest.raises(ValueError):
        module.source_members(archive, "root")


def test_source_must_have_real_root_directory(tmp_path):
    path = tmp_path / "bad.tar.xz"
    make_tar(path, [("root/child", b"data")])
    with tarfile.open(path) as archive, pytest.raises(ValueError, match="root directory"):
        module.source_members(archive, "root")


def test_input_archive_symlink_rejected(inputs, tmp_path):
    args, _, _, _ = inputs
    link = tmp_path / "source-link"
    try:
        link.symlink_to(args["source_archive"])
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation not permitted")
    with pytest.raises(ValueError, match="regular file"):
        module.build_resources(**{**args, "source_archive": link}, output_dir=tmp_path / "out")


@pytest.mark.parametrize("limit", ["MAX_CANDIDATE_BYTES", "MAX_CANDIDATE_ENTRIES"])
def test_candidate_inputs_are_bounded(inputs, tmp_path, monkeypatch, limit):
    args, _, _, _ = inputs
    monkeypatch.setattr(module, limit, 1)
    with pytest.raises(ValueError, match="limit|large"):
        module.build_resources(**args, output_dir=tmp_path / "out")


def test_missing_build_tool_requirements_blocks_all_assets(inputs, tmp_path):
    args, _, _, _ = inputs
    (args["historical_root"] / module.BUILD_REQUIREMENTS).unlink()
    with pytest.raises(FileNotFoundError):
        module.build_resources(**args, output_dir=tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_rebuild_instructions_distinguish_build_and_acceptance_requirements():
    instructions = module.rebuild_instructions().decode()
    assert "pip install --require-hashes -r VSE-AX-OVERLAY/native/ocr/requirements-build.txt" in instructions
    assert "NOT the CMake/Ninja build tools" in instructions
