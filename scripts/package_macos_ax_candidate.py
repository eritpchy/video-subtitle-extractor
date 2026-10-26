"""Make a separately sealed AX diagnostic ZIP; never alter a released archive.

Only the pinned Cocoa plugin, two checksum manifests and the app's minimum-OS
plist metadata may replace base files.
Production integration and user-machine/external-client acceptance remain false.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import build_macos_qt_accessibility as qt
from scripts import macos_ax_archive as archives
from scripts.macos_minimum_version import audit_minimum_system, macho_minimums
from scripts.bundle_bootstrap import sanitize_environment, verify_payload
from scripts.package_release import verify_bundle
from scripts.prune_inference_runtime import verify as verify_pruning
from scripts.release_guard import validate_acceptance_receipt

PINS = ROOT / "native/qt/macos-ax-package-pins.json"
APP = Path("Video Subtitle Extractor.app/Contents/Resources")
NOTICE_DIR = Path("diagnostics/macos-qt-ax")
NAME = "vse-2.3.0-macos-arm64-native-cpu-AX-DIAGNOSTIC"


def read_json(path: Path) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError(f"Missing/unsafe/oversized JSON: {path}")
    result = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    if not isinstance(result, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return result


def require_ci(work: Path) -> Path:
    qt.require_hosted_macos()
    if os.environ.get("GITHUB_REF") != "refs/heads/fix/macos-accessibility":
        raise RuntimeError("Final diagnostic packaging is restricted to its dedicated CI branch")
    if work.resolve() != ROOT / "build/macos-ax-package":
        raise RuntimeError("Only this checkout's isolated build/macos-ax-package workspace is allowed")
    if not re.fullmatch(r"[0-9a-f]{40}", os.environ.get("GITHUB_SHA", "")):
        raise RuntimeError("Exact diagnostic packager source commit is missing")
    return work.resolve()


def check_file(path: Path, digest: str, size: int | None = None) -> None:
    if (path.is_symlink() or not path.is_file() or qt.sha256(path) != digest
            or (size is not None and path.stat().st_size != size)):
        raise RuntimeError(f"Pinned file identity differs: {path.name}")


def bundled_runtime_info(python: Path, environment: dict) -> dict:
    script = """import importlib.metadata,json,sysconfig
from PySide6 import QtCore
print(json.dumps({'packages':sysconfig.get_path('purelib'),'qt':QtCore.qVersion(),
'pyside':importlib.metadata.version('PySide6-Essentials'),'shiboken':importlib.metadata.version('shiboken6')}))
"""
    return json.loads(qt.run([python, "-I", "-B", "-c", script], env=environment).strip())


def validate_artifact_metadata(observed: dict, pin: dict) -> str:
    expected = {"id": pin["artifact_id"], "name": pin["artifact_name"], "expired": False}
    for key, value in expected.items():
        if observed.get(key) != value or type(observed.get(key)) is not type(value):
            raise RuntimeError(f"Artifact identity mismatch: {key}")
    run = observed.get("workflow_run", {})
    if run.get("id") != pin["run_id"] or run.get("head_sha") != pin["commit"]:
        raise RuntimeError("Artifact belongs to another run/source commit")
    digest = observed.get("digest", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise RuntimeError("Artifact API did not provide its content digest")
    if pin.get("artifact_sha256") and digest != "sha256:" + pin["artifact_sha256"]:
        raise RuntimeError("Artifact container digest differs from reviewed evidence")
    return digest.split(":", 1)[1]


def download_inputs(work: Path) -> None:
    work = require_ci(work)
    settings = read_json(PINS)
    inputs = work / "inputs"
    inputs.mkdir(parents=True, exist_ok=False)
    receipts = {}
    for key in ("base", "candidate", "evidence"):
        pin = settings[key]
        endpoint = f"repos/{settings['repository']}/actions/artifacts/{pin['artifact_id']}"
        # gh owns authenticated redirect handling. Never place a token in argv,
        # logs, a signed URL, or a generic cross-origin urllib redirect handler.
        observed = json.loads(qt.run(["gh", "api", endpoint]))
        digest = validate_artifact_metadata(observed, pin)
        archive = inputs / f"{key}.zip"
        with archive.open("xb") as output:
            subprocess.run(["gh", "api", endpoint + "/zip"], stdout=output, check=True)
        check_file(archive, digest)
        archives.extract_archive(archive, inputs / key, require_single_root=False)
        receipts[key] = {"pin": pin, "metadata": observed, "downloaded_sha256": digest,
                         "downloaded_bytes": archive.stat().st_size,
                         "extracted_files": archives.inventory(inputs / key)}
        qt.write_json(work / "reports/input-receipts.json", receipts)


def verify_inputs(work: Path, settings: dict) -> None:
    receipts = read_json(work / "reports/input-receipts.json")
    if set(receipts) != {"base", "candidate", "evidence"}:
        raise RuntimeError("All three fixed input artifacts must have download receipts")
    for key, receipt in receipts.items():
        if receipt["pin"] != settings[key]:
            raise RuntimeError("Input receipt belongs to different pins")
        digest = validate_artifact_metadata(receipt["metadata"], settings[key])
        if digest != receipt["downloaded_sha256"]:
            raise RuntimeError("Input artifact digest changed between steps")
        check_file(work / "inputs" / (key + ".zip"), digest, receipt["downloaded_bytes"])
        if archives.inventory(work / "inputs" / key) != receipt["extracted_files"]:
            raise RuntimeError("Extracted artifact changed after verified download")


def validate_candidate(candidate: Path, evidence: Path, settings: dict) -> dict:
    manifest = read_json(candidate / "candidate.json")
    accepted = evidence / "candidate-acceptance.json"
    check_file(accepted, settings["candidate"]["acceptance_sha256"])
    acceptance = read_json(accepted)
    if (manifest.get("diagnostic_only") is not True
            or manifest.get("production_integration_allowed") is not False
            or acceptance.get("production_integration_allowed") is not False
            or acceptance.get("external_ax_client_tested") is not False
            or acceptance.get("user_macos_26_6_2_acceptance_passed") is not False):
        raise RuntimeError("Diagnostic artifact scope was changed; this is not production approval")
    if (manifest.get("pins") != qt.pins()
            or manifest.get("acceptance_evidence_sha256") != qt.sha256(accepted)
            or acceptance.get("source_commit") != settings["candidate"]["commit"]
            or acceptance.get("status") != "diagnostic-candidate-passed"
            or not all(acceptance.get(name) is True for name in (
                "stock_regression_reproduced", "native_in_process_ax_passed",
                "official_wheel_frameworks_retained", "gui_api_theme_regressions_passed"))):
        raise RuntimeError("Missing or mismatched original candidate acceptance")
    digest = settings["candidate"]["plugin_sha256"]
    plugin = candidate / "libqcocoa.dylib"
    check_file(plugin, digest)
    if manifest.get("candidate_sha256") != digest or acceptance.get("candidate_sha256") != digest:
        raise RuntimeError("Candidate evidence refers to another binary")
    metadata = qt.macho_arm64(plugin)
    qt.validate_link_contract(metadata, qt.pins())
    if metadata.get("uuid") != settings["candidate"]["plugin_uuid"]:
        raise RuntimeError("Candidate Mach-O UUID differs")
    for name, digest in qt.pins()["patched_sources"].items():
        check_file(candidate / "cocoa-modified-source" / Path(name).name, digest)
    for name in ("LGPL-3.0-only.txt", "GPL-3.0-only.txt", "BSD-3-Clause.txt"):
        license_file = candidate / "LICENSES" / name
        if not license_file.is_file() or license_file.stat().st_size < 100:
            raise RuntimeError(f"Required Qt license is missing: {name}")
    if "NOT AN OFFICIAL QT BINARY OR RELEASE" not in (candidate / "MODIFICATIONS.txt").read_text():
        raise RuntimeError("Missing prominent local-modification notice")
    apple_source = (candidate / "cocoa-modified-source/qcocoaapplication.mm").read_text()
    if "Copyright (c) 2007-2008, Apple, Inc." not in apple_source:
        raise RuntimeError("Cocoa BSD copyright attribution is missing")
    return acceptance


def reseal_pruning(original: dict, plugin_relative: str, old_sha: str, plugin: Path) -> dict:
    updated = copy.deepcopy(original)
    entries = [e for e in updated["retained"] if e["path"] == plugin_relative]
    if len(entries) != 1 or entries[0]["sha256"] != old_sha:
        raise RuntimeError("Pruning manifest did not bind the original Cocoa plugin")
    entry = entries[0]
    delta = plugin.stat().st_size - entry["size"]
    entry.update(size=plugin.stat().st_size, sha256=qt.sha256(plugin))
    updated["retained_bytes"] += delta
    return updated


def check_overlay_delta(before: dict, after: dict, plugin: str) -> dict:
    removed = sorted(before.keys() - after.keys())
    changed = sorted(k for k in before.keys() & after.keys() if before[k] != after[k])
    added = sorted(after.keys() - before.keys())
    allowed = sorted((plugin, "runtime/inference-prune.json", "bundle-manifest.json"))
    prefix = NOTICE_DIR.as_posix() + "/"
    if removed or changed != allowed or not added or any(not name.startswith(prefix) for name in added):
        raise RuntimeError(f"Unexpected diagnostic overlay delta: removed={removed}, changed={changed}, added={added[:8]}")
    return {"replaced": changed, "added": added, "removed": removed}


def assert_inventory_unchanged(root: Path, expected: dict) -> None:
    if archives.inventory(root) != expected:
        raise RuntimeError("Final ZIP execution changed its sealed application/wrapper/manifest inventory")


def package(work: Path) -> None:
    work = require_ci(work)
    settings = read_json(PINS)
    if settings.get("production_integration_allowed") is not False or settings.get("diagnostic_only") is not True:
        raise RuntimeError("Candidate pins must not authorize production")
    verify_inputs(work, settings)
    inputs = work / "inputs"
    base = inputs / "base" / settings["base"]["archive"]
    check_file(base, settings["base"]["archive_sha256"], settings["base"]["archive_bytes"])
    candidate = inputs / "candidate/build/macos-qt-ax/candidate"
    rebuild = inputs / "candidate/build/macos-qt-ax/rebuild-inputs"
    evidence = inputs / "evidence"
    validate_candidate(candidate, evidence, settings)
    source = rebuild / "qtbase-everywhere-src-6.12.0.tar.xz"
    patch = rebuild / "qtbase-772484-ps6.patch"
    check_file(source, qt.pins()["source"]["sha256"], 51220268)
    check_file(patch, qt.pins()["patch"]["decoded_sha256"])
    original_root = archives.extract_archive(base, work / "staging")
    root = original_root.parent / NAME
    if root.exists():
        raise RuntimeError("Diagnostic output staging already exists")
    original_root.rename(root)
    payload = root / APP
    manifest = read_json(payload / "bundle-manifest.json")
    if any(manifest.get(key) != value for key, value in {
        "commit": settings["base"]["commit"], "version": "2.3.0", "platform": "macos",
        "arch": "arm64", "ocr_backend": "native", "build_profile": "native-cpu"}.items()):
        raise RuntimeError("Base archive is not the pinned Mac Native package")
    validate_acceptance_receipt(base.with_suffix(".zip.acceptance.json"), base,
                                settings["base"]["archive_sha256"], manifest)
    verify_payload(payload, manifest)
    before = archives.inventory(payload)
    if set(before) != set(manifest["files"]) | {"bundle-manifest.json"}:
        raise RuntimeError("Base payload has unmanifested or missing files")
    env = sanitize_environment(payload, os.environ.copy(), "macos")
    python = payload / manifest["runtime_python"]
    info = bundled_runtime_info(python, env)
    packages = Path(info["packages"]).resolve()
    if not packages.is_relative_to(payload / "runtime") or any(
            info[k] != "6.12.0" for k in ("qt", "pyside", "shiboken")):
        raise RuntimeError("Base package uses a foreign runtime or different Qt")
    plugin = packages / qt.PLUGIN_RELATIVE
    old_sha = qt.pins()["wheel"]["original_cocoa_sha256"]
    check_file(plugin, old_sha)
    for name, digest in settings["official_frameworks"].items():
        check_file(packages / f"PySide6/Qt/lib/{name}.framework/Versions/A/{name}", digest)
    pruning_path = payload / "runtime/inference-prune.json"
    pruning = read_json(pruning_path)
    verify_pruning(packages, pruning)
    symbols = qt.verify_symbols(candidate / "libqcocoa.dylib", packages / "PySide6/Qt")
    qt.run(["codesign", "--verify", "--strict", candidate / "libqcocoa.dylib"])
    plugin_relative = plugin.relative_to(payload).as_posix()
    notice = payload / NOTICE_DIR
    notice.mkdir(parents=True, exist_ok=False)
    shutil.copy2(payload / "bundle-manifest.json", notice / "base-bundle-manifest.json")
    shutil.copy2(pruning_path, notice / "base-inference-prune.json")
    shutil.copy2(candidate / "libqcocoa.dylib", plugin)
    qt.write_json(pruning_path, reseal_pruning(pruning, qt.PLUGIN_RELATIVE, old_sha, plugin))
    verify_pruning(packages, read_json(pruning_path))
    minimum = audit_minimum_system(payload)
    # The official 6.12 wheel's Cocoa binary already declares 14.4 although
    # its wheel tag says macosx_14_0. Change only this new candidate's plist;
    # never edit Mach-O version commands to pretend older-OS compatibility.
    info_path = payload.parent / "Info.plist"
    shutil.copy2(info_path, notice / "base-Info.plist")
    info_plist = plistlib.loads(info_path.read_bytes())
    old_minimum = info_plist.get("LSMinimumSystemVersion")
    info_plist["LSMinimumSystemVersion"] = minimum["minimum_macos_version"]
    info_path.write_bytes(plistlib.dumps(info_plist))
    app_files = archives.inventory(payload.parent.parent)
    outside_payload = {name: digest for name, digest in app_files.items()
                       if not name.startswith("Contents/Resources/")}
    if set(outside_payload) != set(manifest["app_files"]):
        raise RuntimeError("Diagnostic app wrapper file set changed unexpectedly")
    if any(digest != manifest["app_files"][name] for name, digest in outside_payload.items()
           if name != "Contents/Info.plist"):
        raise RuntimeError("Diagnostic packaging changed an app wrapper other than minimum-OS metadata")
    manifest["app_files"] = outside_payload
    qt.write_json(notice / "minimum-system.json", {**minimum, "original_base_plist_minimum": old_minimum,
                  "official_wheel_cocoa_minimum": "14.4", "macos_14_0_compatibility_verified": False})
    for name in ("LICENSES", "cocoa-modified-source"):
        shutil.copytree(candidate / name, notice / name)
    for name in ("candidate.json", "MODIFICATIONS.txt", "macos-accessibility-pins.json"):
        shutil.copy2(candidate / name, notice / name)
    shutil.copy2(source, notice / source.name)
    shutil.copy2(patch, notice / patch.name)
    for name in ("build_macos_qt_accessibility.py", "macos_accessibility_probe.py"):
        shutil.copy2(inputs / "candidate/scripts" / name, notice / name)
    shutil.copy2(inputs / "candidate/native/qt/constraints-macos-ax.txt", notice / "constraints-macos-ax.txt")
    shutil.copy2(ROOT / "native/ocr/requirements-build.txt", notice / "requirements-build.txt")
    for name in ("candidate-acceptance.json", "staging.json", "runtime-load.json"):
        shutil.copy2(evidence / name, notice / ("server-" + name))
    shutil.copy2(PINS, notice / PINS.name)
    for name in ("package_macos_ax_candidate.py", "macos_ax_archive.py", "macos_minimum_version.py"):
        shutil.copy2(ROOT / "scripts" / name, notice / name)
    shutil.copy2(ROOT / "docs/audits/macos-ax-candidate-package.md", notice / "REBUILD-AND-INSTALL.md")
    provenance = {"schema": 1, "diagnostic_only": True, "production_integration_allowed": False,
                  "external_ax_client_tested": False, "user_macos_26_6_2_acceptance_passed": False,
                  "base": settings["base"], "qt_candidate": settings["candidate"],
                  "packager_commit": os.environ.get("GITHUB_SHA"), "modified_vendor_files": [plugin_relative],
                  "minimum_macos_version": minimum["minimum_macos_version"],
                  "macos_14_0_compatibility_verified": False,
                  "original_RECORD_unchanged": True, "official_frameworks": settings["official_frameworks"],
                  "symbols": symbols, "scope": "Independent diagnostic ZIP, not a replacement release"}
    qt.write_json(notice / "provenance.json", provenance)
    manifest["diagnostic_candidate"] = provenance
    manifest["files"] = {k: v for k, v in archives.inventory(payload).items() if k != "bundle-manifest.json"}
    qt.write_json(payload / "bundle-manifest.json", manifest)
    verify_payload(payload, manifest)
    after = archives.inventory(payload)
    delta = check_overlay_delta(before, after, plugin_relative)
    qt.write_json(work / "reports/overlay.json", {**provenance, "delta": delta, "before": before, "after": after})
    output = work / "output" / (NAME + ".zip")
    output.parent.mkdir(exist_ok=False)
    archives.write_archive(root, output)
    output.with_suffix(".zip.sha256").write_text(f"{qt.sha256(output)}  {output.name}\n", encoding="utf-8")
    check_file(base, settings["base"]["archive_sha256"], settings["base"]["archive_bytes"])
    qt.write_json(work / "reports/packaged.json", {"archive": output.name, "sha256": qt.sha256(output),
                  "bytes": output.stat().st_size, "production_integration_allowed": False,
                  "minimum_macos_version": minimum["minimum_macos_version"],
                  "macos_14_0_compatibility_verified": False})


def verify(work: Path) -> None:
    work = require_ci(work)
    settings = read_json(PINS)
    output = work / "output" / (NAME + ".zip")
    packaged = read_json(work / "reports/packaged.json")
    check_file(output, packaged["sha256"], packaged["bytes"])
    # The unchanged production verifier audits the resealed final ZIP and runs
    # its own bundled launcher, all three real CPU OCR modes, GUI and isolation.
    verify_bundle(output, smoke=True)
    root = archives.extract_archive(output, work / "final-zip-readback")
    sealed_inventory = archives.inventory(root)
    qt.write_json(work / "reports/final-zip-sealed-inventory.json", sealed_inventory)
    payload = root / APP
    manifest = read_json(payload / "bundle-manifest.json")
    verify_payload(payload, manifest)
    minimum = audit_minimum_system(payload)
    if (minimum["minimum_macos_version"] != packaged["minimum_macos_version"]
            or plistlib.loads((payload.parent / "Info.plist").read_bytes()).get("LSMinimumSystemVersion")
            != minimum["minimum_macos_version"]):
        raise RuntimeError("Final ZIP minimum OS metadata does not match its actual Mach-O images")
    python = payload / manifest["runtime_python"]
    environment = sanitize_environment(payload, os.environ.copy(), "macos")
    environment.update(QT_QPA_PLATFORM="cocoa", VSE_OFFLINE="1",
                       VSE_CONFIG_FILE=str(work / "reports/final-zip-config.json"))
    script = """import ctypes,json,sysconfig
from pathlib import Path
from PySide6 import QtCore,QtWidgets
app=QtWidgets.QApplication([])
assert app.platformName()=='cocoa'
lib=ctypes.CDLL(None);lib._dyld_image_count.restype=ctypes.c_uint32
lib._dyld_get_image_name.argtypes=[ctypes.c_uint32];lib._dyld_get_image_name.restype=ctypes.c_char_p
images=[lib._dyld_get_image_name(i).decode() for i in range(lib._dyld_image_count())]
images=[str(Path(n).resolve()) for n in images if any(x in n for x in ('libqcocoa.dylib','QtCore.framework/','QtGui.framework/'))]
print('VSE_AX_FINAL_IMAGES='+json.dumps({'qt':QtCore.qVersion(),'packages':sysconfig.get_path('purelib'),'images':images}))
"""
    log = qt.run([python, "-I", "-B", "-c", script], env=environment,
                 log=work / "reports/final-zip-dyld.log")
    records = [json.loads(line.split("=", 1)[1]) for line in log.splitlines() if line.startswith("VSE_AX_FINAL_IMAGES=")]
    if len(records) != 1 or records[0]["qt"] != "6.12.0":
        raise RuntimeError("Missing final ZIP native dyld evidence")
    loaded = records[0]
    packages = Path(loaded["packages"]).resolve()
    if not packages.is_relative_to(payload / "runtime"):
        raise RuntimeError("Final ZIP borrowed a foreign Python package directory")
    expected = {str(packages / qt.PLUGIN_RELATIVE)}
    check_file(packages / qt.PLUGIN_RELATIVE, settings["candidate"]["plugin_sha256"])
    for name, digest in settings["official_frameworks"].items():
        path = packages / f"PySide6/Qt/lib/{name}.framework/Versions/A/{name}"
        check_file(path, digest)
        expected.add(str(path))
    if set(loaded["images"]) != expected:
        raise RuntimeError("Final ZIP loaded Qt images from outside its own official runtime")
    qt.write_json(work / "reports/final-zip-dyld.json", loaded)
    probes = []
    for scenario in ("selected-row", "model-churn"):
        command = [python, "-I", "-B", payload / "scripts/macos_accessibility_probe.py",
                   "--mode", "cocoa", "--scenario", scenario, "--cycles", "5", "--expect-qt", "6.12.0"]
        result = subprocess.run(list(map(str, command)), cwd=payload, env=environment,
                                text=True, capture_output=True, timeout=90)
        (work / f"reports/final-zip-{scenario}.log").write_text(result.stdout + "\n--- STDERR ---\n" + result.stderr)
        records = qt.probe_records(result.stdout)
        if not qt.passed_native_probe(result.returncode, records):
            raise RuntimeError(f"Final ZIP failed Cocoa AX scenario: {scenario}")
        probes.append({"scenario": scenario, "returncode": result.returncode, "result": records[-1]})
    verification = work / "output/test-results" / NAME
    for directory, mode in (("bundle-e2e", "fast"), ("bundle-e2e-accurate", "accurate"), ("bundle-e2e-auto", "auto")):
        report = read_json(verification / directory / "report.json")
        if report.get("result") != "passed" or report.get("mode") != mode:
            raise RuntimeError(f"Final ZIP real OCR mode did not pass: {mode}")
    # Ensure test execution did not mutate the extracted application's sealed bytes.
    verify_payload(payload, manifest)
    assert_inventory_unchanged(root, sealed_inventory)
    check_file(output, packaged["sha256"], packaged["bytes"])
    summary = {**packaged, "status": "diagnostic-final-zip-server-passed", "native_ax_probes": probes,
               "candidate_sha256": settings["candidate"]["plugin_sha256"],
               "base_archive_sha256": settings["base"]["archive_sha256"],
               "official_frameworks_retained": True, "real_cpu_ocr_modes": ["fast", "accurate", "auto"],
               "production_integration_allowed": False, "external_ax_client_tested": False,
               "user_macos_26_6_2_acceptance_passed": False}
    qt.write_json(work / "reports/final-diagnostic-acceptance.json", summary)
    qt.write_json(output.with_suffix(".zip.diagnostic-acceptance.json"), summary)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("download", "package", "verify"))
    parser.add_argument("--work-dir", type=Path, default=ROOT / "build/macos-ax-package")
    args = parser.parse_args()
    {"download": download_inputs, "package": package, "verify": verify}[args.action](args.work_dir)


if __name__ == "__main__":
    main()
