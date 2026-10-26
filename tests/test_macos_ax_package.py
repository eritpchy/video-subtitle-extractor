"""Portable contracts; never imply final Mac ZIP or external AX acceptance."""
import copy
import hashlib
import json
from pathlib import Path
import plistlib
import shutil
import struct

import pytest

from scripts import package_macos_ax_candidate as module


def test_pins_identify_exact_base_artifacts_not_latest():
    pins = module.read_json(module.PINS)
    assert pins["base"]["artifact_id"] == 12623898599
    assert pins["base"]["run_id"] == 40259119763
    assert pins["base"]["archive_bytes"] == 517468847
    assert pins["base"]["archive_sha256"] == "5cec865b8ea53f8ebd545a5ad12ac1e3231a0ea7334360205459401ff389fdd5"
    assert pins["candidate"]["run_id"] == pins["evidence"]["run_id"] == 40260642504
    assert pins["candidate"]["plugin_sha256"] == "d23ea2a1e3201ec85f4fd35ee8bff32d7217ce6b0bbb51058339f1abca612941"
    assert pins["production_integration_allowed"] is False


def test_json_does_not_allow_duplicate_evidence_keys(tmp_path):
    path = tmp_path / "receipt.json"
    path.write_text('{"passed":false,"passed":true}')
    with pytest.raises(ValueError, match="Duplicate JSON"):
        module.read_json(path)


def test_ci_branch_and_workspace_guard_remains_independent(tmp_path, monkeypatch):
    monkeypatch.setattr(module.qt, "require_hosted_macos", lambda: None)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/fix/restore-release-matrix")
    with pytest.raises(RuntimeError, match="dedicated CI branch"):
        module.require_ci(tmp_path)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/fix/macos-accessibility")
    with pytest.raises(RuntimeError, match="isolated"):
        module.require_ci(tmp_path)
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    assert module.require_ci(module.ROOT / "build/macos-ax-package") == module.ROOT / "build/macos-ax-package"


def artifact():
    pin = module.read_json(module.PINS)["candidate"]
    return pin, {"id": pin["artifact_id"], "name": pin["artifact_name"], "expired": False,
                 "digest": "sha256:" + pin["artifact_sha256"],
                 "workflow_run": {"id": pin["run_id"], "head_sha": pin["commit"]}}


def test_exact_artifact_container_digest_is_required():
    pin, observed = artifact()
    assert module.validate_artifact_metadata(observed, pin) == pin["artifact_sha256"]


@pytest.mark.parametrize("field,value", [("id", 1), ("name", "latest"), ("expired", True),
                                          ("digest", "sha256:" + "0" * 64),
                                          ("digest", ""), ("workflow_run", {"id": 1, "head_sha": "0" * 40})])
def test_foreign_expired_or_replaced_artifact_is_rejected(field, value):
    pin, observed = artifact()
    observed[field] = value
    with pytest.raises(RuntimeError):
        module.validate_artifact_metadata(observed, pin)


@pytest.mark.parametrize("tamper", [None, "extracted-file", "archive", "pin"])
def test_input_receipts_bind_container_and_extracted_files_before_staging(tmp_path, tamper):
    settings = copy.deepcopy(module.read_json(module.PINS))
    receipts = {}
    for key in ("base", "candidate", "evidence"):
        directory = tmp_path / "inputs" / key
        directory.mkdir(parents=True)
        (directory / "unchanged.txt").write_text(key)
        archive = tmp_path / "inputs" / (key + ".zip")
        archive.write_bytes(("unit-test container " + key).encode())
        digest = module.qt.sha256(archive)
        settings[key]["artifact_sha256"] = digest
        pin = settings[key]
        observed = {"id": pin["artifact_id"], "name": pin["artifact_name"], "expired": False,
                    "digest": "sha256:" + digest,
                    "workflow_run": {"id": pin["run_id"], "head_sha": pin["commit"]}}
        receipts[key] = {"pin": copy.deepcopy(pin), "metadata": observed, "downloaded_sha256": digest,
                         "downloaded_bytes": archive.stat().st_size,
                         "extracted_files": module.archives.inventory(directory)}
    if tamper == "extracted-file":
        (tmp_path / "inputs/candidate/unchanged.txt").write_text("changed license/source")
    elif tamper == "archive":
        (tmp_path / "inputs/candidate.zip").write_bytes(b"different artifact")
    elif tamper == "pin":
        receipts["candidate"]["pin"]["artifact_id"] = 1
    module.qt.write_json(tmp_path / "reports/input-receipts.json", receipts)
    if tamper:
        with pytest.raises(RuntimeError):
            module.verify_inputs(tmp_path, settings)
    else:
        module.verify_inputs(tmp_path, settings)


def test_reseal_changes_only_the_one_retained_plugin_record(tmp_path):
    plugin = tmp_path / "libqcocoa.dylib"
    plugin.write_bytes(b"new plugin")
    original = {"retained": [{"path": module.qt.PLUGIN_RELATIVE, "sha256": "a" * 64, "size": 3},
                              {"path": "original.dist-info/RECORD", "sha256": "b" * 64, "size": 4}],
                "retained_bytes": 7, "removed": [{"path": "unused", "sha256": "c" * 64}],
                "original_RECORD_unchanged": True}
    saved = copy.deepcopy(original)
    result = module.reseal_pruning(original, module.qt.PLUGIN_RELATIVE, "a" * 64, plugin)
    assert original == saved
    assert result["retained_bytes"] == 4 + len(b"new plugin")
    assert result["retained"][1:] == original["retained"][1:]
    assert result["removed"] == original["removed"]
    assert result["retained"][0]["sha256"] == hashlib.sha256(b"new plugin").hexdigest()
    with pytest.raises(RuntimeError, match="original Cocoa"):
        module.reseal_pruning(original, module.qt.PLUGIN_RELATIVE, "wrong", plugin)


def overlay():
    plugin = "runtime/lib/PySide6/Qt/plugins/platforms/libqcocoa.dylib"
    before = {plugin: "old", "runtime/inference-prune.json": "old", "bundle-manifest.json": "old",
              "runtime/lib/QtCore": "official", "vendor.dist-info/RECORD": "original"}
    after = {**before, plugin: "new", "runtime/inference-prune.json": "new", "bundle-manifest.json": "new",
             "diagnostics/macos-qt-ax/provenance.json": "new"}
    return plugin, before, after


def test_overlay_is_only_one_vendor_binary_plus_sealed_metadata():
    plugin, before, after = overlay()
    assert module.check_overlay_delta(before, after, plugin)["removed"] == []


@pytest.mark.parametrize("operation", ["record", "core", "remove", "extra"])
def test_overlay_rejects_any_other_vendor_or_application_change(operation):
    plugin, before, after = overlay()
    if operation == "record": after["vendor.dist-info/RECORD"] = "changed"
    elif operation == "core": after["runtime/lib/QtCore"] = "rebuilt"
    elif operation == "remove": del after["runtime/lib/QtCore"]
    else: after["gui.py"] = "unreviewed"
    with pytest.raises(RuntimeError, match="Unexpected diagnostic overlay"):
        module.check_overlay_delta(before, after, plugin)


@pytest.mark.parametrize("tamper", ["manifest", "new-wrapper", "payload"])
def test_runtime_cannot_rewrite_the_manifest_or_add_wrapper_files(tmp_path, tamper):
    payload = tmp_path / module.APP
    payload.mkdir(parents=True)
    (payload / "bundle-manifest.json").write_text("original sealed manifest")
    (payload / "gui.py").write_text("original app source")
    sealed = module.archives.inventory(tmp_path)
    module.assert_inventory_unchanged(tmp_path, sealed)
    if tamper == "manifest":
        (payload / "bundle-manifest.json").write_text("changed manifest")
    elif tamper == "new-wrapper":
        (payload.parent / "unexpected-executable").write_text("new executable")
    else:
        (payload / "gui.py").write_text("changed app source")
    with pytest.raises(RuntimeError, match="sealed application/wrapper/manifest"):
        module.assert_inventory_unchanged(tmp_path, sealed)


def macho(version, *, old=False, cpu=0x0100000C):
    command = (struct.pack("<4I", 0x24, 16, version, 0x1A0000) if old else
               struct.pack("<6I", 0x32, 24, 1, version, 0x1A0000, 0))
    subtype = 3 if cpu == 0x01000007 else 0
    return struct.pack("<8I", 0xFEEDFACF, cpu, subtype, 6, 1, len(command), 0, 0) + command


def test_actual_macho_minimum_wins_over_wheel_filename_tag(tmp_path):
    # Both the vendor wheel and candidate really declare 14.4, not the 14.0 tag.
    path = tmp_path / "pyside6-macosx_14_0.dylib"
    path.write_bytes(macho(0x0E0400))
    assert len(module.macho_minimums(path)) == 1
    assert module.macho_minimums(path)[0]["cpu"] == 0x0100000C
    assert module.macho_minimums(path)[0]["minimum_encoded"] == 0x0E0400
    assert module.audit_minimum_system(tmp_path)["minimum_macos_version"] == "14.4"


def test_legacy_load_command_and_highest_slice_are_included(tmp_path):
    first, second = macho(0x0D0000, old=True, cpu=0x01000007), macho(0x0F0102)
    offset = 8 + 2 * 20
    fat = (struct.pack(">II", 0xCAFEBABE, 2)
           + struct.pack(">IIIII", 0x01000007, 3, offset, len(first), 0)
           + struct.pack(">IIIII", 0x0100000C, 0, offset + len(first), len(second), 0)
           + first + second)
    (tmp_path / "universal.dylib").write_bytes(fat)
    report = module.audit_minimum_system(tmp_path)
    assert report["minimum_macos_version"] == "15.1.2"
    assert len(report["images"]["universal.dylib"]) == 2


@pytest.mark.parametrize("content", [struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0, 6, 0, 0, 0, 0),
                                      macho(0x0E0400)[:-1],
                                      struct.pack(">II", 0xCAFEBABE, 999)])
def test_missing_or_malformed_macho_floor_never_becomes_compatible(tmp_path, content):
    path = tmp_path / "bad.dylib"
    path.write_bytes(content)
    with pytest.raises((RuntimeError, struct.error)):
        module.macho_minimums(path)


def test_non_binary_source_and_license_are_not_runtime_deployment_targets(tmp_path):
    path = tmp_path / "LICENSE.txt"
    path.write_text("Original source license")
    assert module.macho_minimums(path) == []


def candidate_fixture(tmp_path, monkeypatch):
    candidate, evidence = tmp_path / "candidate", tmp_path / "evidence"
    candidate.mkdir(); evidence.mkdir()
    settings = copy.deepcopy(module.read_json(module.PINS))
    pins = copy.deepcopy(module.qt.pins())
    plugin = candidate / "libqcocoa.dylib"
    plugin.write_bytes(b"fixed candidate binary")
    settings["candidate"]["plugin_sha256"] = module.qt.sha256(plugin)
    acceptance = {"production_integration_allowed": False, "external_ax_client_tested": False,
                  "user_macos_26_6_2_acceptance_passed": False, "source_commit": settings["candidate"]["commit"],
                  "status": "diagnostic-candidate-passed", "candidate_sha256": settings["candidate"]["plugin_sha256"],
                  "stock_regression_reproduced": True, "native_in_process_ax_passed": True,
                  "official_wheel_frameworks_retained": True, "gui_api_theme_regressions_passed": True}
    module.qt.write_json(evidence / "candidate-acceptance.json", acceptance)
    settings["candidate"]["acceptance_sha256"] = module.qt.sha256(evidence / "candidate-acceptance.json")
    (candidate / "cocoa-modified-source").mkdir()
    for name in pins["patched_sources"]:
        path = candidate / "cocoa-modified-source" / Path(name).name
        path.write_text("reviewed changed source")
        pins["patched_sources"][name] = module.qt.sha256(path)
    (candidate / "cocoa-modified-source/qcocoaapplication.mm").write_text("Copyright (c) 2007-2008, Apple, Inc.")
    (candidate / "LICENSES").mkdir()
    for name in ("LGPL-3.0-only.txt", "GPL-3.0-only.txt", "BSD-3-Clause.txt"):
        (candidate / "LICENSES" / name).write_text("Original license text. " * 10)
    (candidate / "MODIFICATIONS.txt").write_text("NOT AN OFFICIAL QT BINARY OR RELEASE")
    manifest = {"diagnostic_only": True, "production_integration_allowed": False, "pins": pins,
                "acceptance_evidence_sha256": settings["candidate"]["acceptance_sha256"],
                "candidate_sha256": settings["candidate"]["plugin_sha256"]}
    module.qt.write_json(candidate / "candidate.json", manifest)
    monkeypatch.setattr(module.qt, "pins", lambda: pins)
    monkeypatch.setattr(module.qt, "macho_arm64", lambda _: {"uuid": settings["candidate"]["plugin_uuid"]})
    monkeypatch.setattr(module.qt, "validate_link_contract", lambda *_: None)
    return candidate, evidence, settings


def test_candidate_evidence_and_license_inputs_must_match(tmp_path, monkeypatch):
    candidate, evidence, settings = candidate_fixture(tmp_path, monkeypatch)
    assert module.validate_candidate(candidate, evidence, settings)["production_integration_allowed"] is False
    (candidate / "LICENSES/LGPL-3.0-only.txt").unlink()
    with pytest.raises(RuntimeError, match="license"):
        module.validate_candidate(candidate, evidence, settings)


def test_candidate_may_not_promote_diagnostic_success_to_production(tmp_path, monkeypatch):
    candidate, evidence, settings = candidate_fixture(tmp_path, monkeypatch)
    path = candidate / "candidate.json"
    data = module.read_json(path);data["production_integration_allowed"] = True
    module.qt.write_json(path, data)
    with pytest.raises(RuntimeError, match="not production approval"):
        module.validate_candidate(candidate, evidence, settings)


def test_final_zip_workflow_is_independent_and_does_not_publish():
    workflow = (module.ROOT / ".github/workflows/macos-ax-package.yml").read_text()
    assert "branches: [fix/macos-accessibility]" in workflow
    assert "workflow_dispatch:" in workflow and "workflow_call:" not in workflow
    assert "contents: read" in workflow and "actions: read" in workflow
    assert "contents: write" not in workflow and "gh release" not in workflow
    assert "package_macos_ax_candidate.py verify" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "NOT-FOR-RELEASE" in workflow


def test_small_end_to_end_overlay_reseals_metadata_without_changing_base_or_frameworks(tmp_path, monkeypatch):
    """Exercise real ZIP/manifest/pruning/overlay code without running native code."""
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    candidate, evidence, settings = candidate_fixture(fixture, monkeypatch)
    pins = module.qt.pins()
    work = tmp_path / "work"
    inputs = work / "inputs"
    inputs.mkdir(parents=True)
    copied_candidate = inputs / "candidate/build/macos-qt-ax/candidate"
    shutil.copytree(candidate, copied_candidate)
    shutil.copytree(evidence, inputs / "evidence")
    for name in ("staging.json", "runtime-load.json"):
        module.qt.write_json(inputs / "evidence" / name, {"original": True})
    rebuild = inputs / "candidate/build/macos-qt-ax/rebuild-inputs"
    rebuild.mkdir()
    source = rebuild / "qtbase-everywhere-src-6.12.0.tar.xz"
    patch = rebuild / "qtbase-772484-ps6.patch"
    source.write_bytes(b"tiny unit-test source archive")
    patch.write_bytes(b"tiny unit-test patch")
    pins["source"]["sha256"] = module.qt.sha256(source)
    pins["patch"]["decoded_sha256"] = module.qt.sha256(patch)
    (copied_candidate / "macos-accessibility-pins.json").write_text(json.dumps(pins))
    scripts = inputs / "candidate/scripts"
    scripts.mkdir()
    constraints = inputs / "candidate/native/qt/constraints-macos-ax.txt"
    constraints.parent.mkdir(parents=True)
    constraints.write_text("PySide6-Essentials==6.12.0\n")
    for name in ("build_macos_qt_accessibility.py", "macos_accessibility_probe.py"):
        (scripts / name).write_text("# Rebuild source supplied by pinned artifact\n")
    original_root = tmp_path / "base-root"
    payload = original_root / module.APP
    packages = payload / "runtime/lib/python3.12/site-packages"
    packages.mkdir(parents=True)
    original_plugin = packages / module.qt.PLUGIN_RELATIVE
    original_plugin.parent.mkdir(parents=True)
    original_plugin.write_bytes(b"original vendor plugin")
    pins["wheel"]["original_cocoa_sha256"] = module.qt.sha256(original_plugin)
    record = packages / "pyside6_essentials-6.12.0.dist-info/RECORD"
    record.parent.mkdir()
    record.write_text("upstream record,never rewritten\n")
    for name in settings["official_frameworks"]:
        library = packages / f"PySide6/Qt/lib/{name}.framework/Versions/A/{name}"
        library.parent.mkdir(parents=True)
        library.write_bytes(macho(0x0E0400))
        settings["official_frameworks"][name] = module.qt.sha256(library)
    retained = [{"path": p.relative_to(packages).as_posix(), "size": p.stat().st_size,
                 "sha256": module.qt.sha256(p)} for p in packages.rglob("*") if p.is_file()]
    pruning = {"schema": 1, "engine": "native", "retained": retained, "removed": [],
               "retained_bytes": sum(p["size"] for p in retained), "removed_bytes": 0,
               "original_RECORD_unchanged": True}
    module.qt.write_json(payload / "runtime/inference-prune.json", pruning)
    info_path = payload.parent / "Info.plist"
    info_path.write_bytes(plistlib.dumps({"LSMinimumSystemVersion": "14.0", "CFBundleName": "VSE"}))
    manifest = {"commit": settings["base"]["commit"], "version": "2.3.0", "platform": "macos",
                "arch": "arm64", "ocr_backend": "native", "build_profile": "native-cpu",
                "runtime_python": "runtime/bin/python3.12", "files": module.archives.inventory(payload),
                "app_files": {"Contents/Info.plist": module.qt.sha256(info_path)}}
    module.qt.write_json(payload / "bundle-manifest.json", manifest)
    base_dir = inputs / "base"
    base_dir.mkdir()
    base = base_dir / settings["base"]["archive"]
    module.archives.write_archive(original_root, base)
    settings["base"].update(archive_sha256=module.qt.sha256(base), archive_bytes=base.stat().st_size)
    from scripts.release_guard import required_acceptance_checks
    receipt = {"schema": 1, "archive": base.name, "archive_sha256": settings["base"]["archive_sha256"],
               **{key: manifest[key] for key in ("commit", "version", "platform", "arch", "build_profile")},
               "checks": {name: "passed" for name in required_acceptance_checks("native-cpu")},
               "gpu_inference_verified": False, "gpu_validation": "not-run-server-ci"}
    module.qt.write_json(base.with_suffix(".zip.acceptance.json"), receipt)
    manifest_path = copied_candidate / "candidate.json"
    candidate_manifest = module.read_json(manifest_path)
    candidate_manifest["pins"] = pins
    module.qt.write_json(manifest_path, candidate_manifest)
    pin_path = tmp_path / "package-pins.json"
    module.qt.write_json(pin_path, settings)
    monkeypatch.setattr(module, "PINS", pin_path)
    monkeypatch.setattr(module, "require_ci", lambda value: value)
    monkeypatch.setattr(module, "verify_inputs", lambda *_: None)
    final_payload = work / "staging" / module.NAME / module.APP
    monkeypatch.setattr(module, "bundled_runtime_info", lambda *_: {
        "packages": str(final_payload / "runtime/lib/python3.12/site-packages"),
        "qt": "6.12.0", "pyside": "6.12.0", "shiboken": "6.12.0"})
    monkeypatch.setattr(module.qt, "verify_symbols", lambda *_: {"tested": "no native execution in unit test"})
    monkeypatch.setattr(module.qt, "run", lambda *_args, **_kwargs: "")
    check_file = module.check_file
    # Keep all hashes real, but avoid a 51 MB fixture solely for the pinned size.
    monkeypatch.setattr(module, "check_file", lambda path, digest, size=None:
                        check_file(path, digest, None if path == source else size))
    module.package(work)
    output = work / "output" / (module.NAME + ".zip")
    unpacked = module.archives.extract_archive(output, tmp_path / "readback") / module.APP
    resealed = module.read_json(unpacked / "bundle-manifest.json")
    module.verify_payload(unpacked, resealed)
    assert module.qt.sha256(base) == settings["base"]["archive_sha256"]
    assert (unpacked / record.relative_to(payload)).read_bytes() == record.read_bytes()
    assert plistlib.loads((unpacked.parent / "Info.plist").read_bytes())["LSMinimumSystemVersion"] == "14.4"
    provenance = resealed["diagnostic_candidate"]
    assert provenance["production_integration_allowed"] is False
    assert provenance["external_ax_client_tested"] is False
    assert provenance["macos_14_0_compatibility_verified"] is False
    assert (unpacked / module.NOTICE_DIR / "base-inference-prune.json").read_bytes() == (payload / "runtime/inference-prune.json").read_bytes()
