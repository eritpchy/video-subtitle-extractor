#!/usr/bin/env python3
"""Build a bounded, source-attributed Cocoa AX candidate on hosted macOS CI.

Never changes a production package, dependency pin, original wheel or RECORD.
The only runtime mutation allowed is one plugin inside work-dir/candidate-venv.
Unmerged upstream code is a diagnostic candidate, not an official Qt release.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import struct
import subprocess
import sys
import tarfile
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
PINS_PATH = ROOT / "native/qt/macos-accessibility-pins.json"
PLUGIN_RELATIVE = "PySide6/Qt/plugins/platforms/libqcocoa.dylib"
RPATH = "@loader_path/../../lib"


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def pins() -> dict:
    return json.loads(PINS_PATH.read_text(encoding="utf-8"))


def require_hosted_macos() -> None:
    if not (sys.platform == "darwin" and platform.machine() == "arm64"
            and os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
            and os.environ.get("RUNNER_OS") == "macOS"):
        raise RuntimeError("This candidate build/staging is restricted to GitHub-hosted macOS arm64 CI")


def run(command, *, cwd=None, log: Path | None = None, env=None) -> str:
    command = list(map(str, command))
    print("+ " + repr(command), flush=True)
    if log:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as output:
            output.write("+ " + repr(command) + "\n")
            output.flush()
            process = subprocess.Popen(command, cwd=cwd, env=env, text=True,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            chunks = []
            for line in process.stdout:
                output.write(line)
                output.flush()  # Retain partial compiler diagnostics even on CI timeout.
                chunks.append(line)
            code = process.wait()
            captured = "".join(chunks)
    else:
        result = subprocess.run(command, cwd=cwd, env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        code, captured = result.returncode, result.stdout
    if code:
        print(captured[-12000:], flush=True)
        raise RuntimeError(f"Command failed ({code}); see {log or command[0]}")
    return captured


def fetch(url: str, destination: Path, expected: str, *, base64_encoded=False) -> None:
    """Hash before using inputs; never trust a mutable current/latest endpoint."""
    if destination.exists():
        if sha256(destination) != expected:
            raise RuntimeError(f"Cached input hash mismatch: {destination}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".partial")
    try:
        with urllib.request.urlopen(url, timeout=120) as response, temporary.open("wb") as output:
            if base64_encoded:
                output.write(base64.b64decode(b"".join(response.read().split()), validate=True))
            else:
                shutil.copyfileobj(response, output, length=1024 * 1024)
        if sha256(temporary) != expected:
            raise RuntimeError(f"Downloaded input hash mismatch: {url}")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def unpack_source(archive: Path, destination: Path, expected_root: str) -> Path:
    if destination.exists():
        raise RuntimeError("Use a fresh source/build directory; cached or locally edited sources are not accepted")
    destination.mkdir(parents=True)
    with tarfile.open(archive, "r:xz") as source:
        members = source.getmembers()
        for member in members:
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != expected_root:
                raise RuntimeError(f"Unsafe/unexpected source archive member: {member.name}")
            if member.ischr() or member.isblk() or member.isfifo():
                raise RuntimeError(f"Special source archive member: {member.name}")
        source.extractall(destination, members=members, filter="data")
    return destination / expected_root


def macho_arm64(path: Path) -> dict:
    """Read public Mach-O load commands; works on Linux for unit tests/audits."""
    raw = path.read_bytes()
    if len(raw) < 32:
        raise ValueError("Truncated Mach-O")
    magic = struct.unpack_from(">I", raw)[0]
    if magic == 0xCAFEBABE:
        count = struct.unpack_from(">I", raw, 4)[0]
        selected = []
        for index in range(count):
            cpu, _, offset, size, _ = struct.unpack_from(">5I", raw, 8 + index * 20)
            if offset + size > len(raw):
                raise ValueError("Mach-O slice outside file")
            if cpu == 0x0100000C:
                selected.append(raw[offset:offset + size])
        if len(selected) != 1:
            raise ValueError("Expected exactly one arm64 Mach-O slice")
        raw = selected[0]
    header = struct.unpack_from("<8I", raw)
    if header[0] != 0xFEEDFACF or header[1] != 0x0100000C:
        raise ValueError("Expected arm64 Mach-O")
    result = {"arch": "arm64", "rpaths": [], "dependencies": []}
    position = 32
    for _ in range(header[4]):
        command, size = struct.unpack_from("<2I", raw, position)
        if size < 8 or position + size > len(raw):
            raise ValueError("Invalid Mach-O load command")
        if command == 0x1B:
            result["uuid"] = str(uuid.UUID(bytes=raw[position + 8:position + 24]))
        elif command == 0x32:
            system, minimum, sdk, _ = struct.unpack_from("<4I", raw, position + 8)
            result.update(platform=system, minimum=minimum, sdk=sdk)
        elif command in (0xC, 0x80000018, 0x8000001F, 0x8000001C):
            offset = struct.unpack_from("<I", raw, position + 8)[0]
            if offset >= size:
                raise ValueError("Invalid Mach-O string offset")
            string = raw[position + offset:position + size].split(b"\0", 1)[0].decode("utf-8")
            result["rpaths" if command == 0x8000001C else "dependencies"].append(string)
        position += size
    return result


def validate_link_contract(metadata: dict, settings: dict) -> None:
    if metadata.get("platform") != 1 or metadata.get("minimum") != 0x0E0400 or metadata.get("sdk") != 0x1A0000:
        raise RuntimeError("Candidate must match wheel macOS 14.4 deployment target and macOS 26.0 SDK")
    if metadata["rpaths"] != [RPATH]:
        raise RuntimeError(f"Unreviewed build/library search path: {metadata['rpaths']}")
    qt = {f"@rpath/Qt{name}.framework/Versions/A/Qt{name}" for name in ("Core", "Gui")}
    if not qt.issubset(metadata["dependencies"]):
        raise RuntimeError("Candidate does not link both expected Qt Core and Gui frameworks")
    for dependency in metadata["dependencies"]:
        if dependency in qt:
            continue
        if not dependency.startswith(("/System/Library/Frameworks/", "/usr/lib/")):
            raise RuntimeError(f"Unreviewed candidate dependency: {dependency}")


def configure_command(source: Path, install: Path) -> list[str]:
    return [str(source / "configure"), "-opensource", "-confirm-license", "-release",
            "-nomake", "examples", "-nomake", "tests", "-prefix", str(install), "--",
            "-DCMAKE_OSX_ARCHITECTURES=arm64", "-DCMAKE_OSX_DEPLOYMENT_TARGET=14.4",
            "-DCMAKE_SKIP_BUILD_RPATH=ON", "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
            "-DFEATURE_accessibility=ON"]


def check_toolchain(settings: dict, reports: Path) -> dict:
    observed = {"xcode": run(["xcodebuild", "-version"]).strip(),
                "sdk": run(["xcrun", "--sdk", "macosx", "--show-sdk-version"]).strip(),
                "clang": run(["xcrun", "clang++", "--version"]).strip(),
                "cmake": run(["cmake", "--version"]).strip(),
                "ninja": run(["ninja", "--version"]).strip(),
                "os": run(["sw_vers"]).strip(), "machine": platform.machine()}
    write_json(reports / "toolchain.json", observed)
    expected_xcode = f"Xcode {settings['xcode_version']}\nBuild version {settings['xcode_build']}"
    if observed["xcode"] != expected_xcode or observed["sdk"] != settings["sdk_version"]:
        raise RuntimeError("Pinned Xcode/SDK is not installed/selected; do not silently use another toolchain")
    if not observed["cmake"].startswith("cmake version " + settings["cmake"] + "\n"):
        raise RuntimeError("CMake version differs from pinned build tools")
    if observed["ninja"] != settings["ninja"]:
        raise RuntimeError("Ninja version differs from pinned build tools")
    return observed


def build(work: Path, jobs: int) -> None:
    require_hosted_macos()
    settings = pins()
    work = work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    reports = work / "reports"
    toolchain = check_toolchain(settings["toolchain"], reports)
    inputs = work / "rebuild-inputs"
    source_archive = inputs / "qtbase-everywhere-src-6.12.0.tar.xz"
    patch = inputs / "qtbase-772484-ps6.patch"
    fetch(settings["source"]["url"], source_archive, settings["source"]["sha256"])
    fetch(settings["patch"]["url"], patch, settings["patch"]["decoded_sha256"], base64_encoded=True)
    source = unpack_source(source_archive, work / "source", settings["source"]["root"])
    # Prevent git apply from discovering the enclosing VSE checkout and using
    # its working-tree prefix instead of this extracted upstream source root.
    run(["git", "init", "--quiet", source], log=reports / "patch.log")
    run(["git", "apply", "--check", patch], cwd=source, log=reports / "patch.log")
    run(["git", "apply", patch], cwd=source, log=reports / "patch.log")
    for name, expected in settings["patched_sources"].items():
        if sha256(source / name) != expected:
            raise RuntimeError(f"Patched source differs from reviewed input: {name}")
    build_dir = work / "qt-build"
    build_dir.mkdir(exist_ok=False)
    command = configure_command(source, work / "unused-install-prefix")
    run(command, cwd=build_dir, log=reports / "configure.log")
    run(["cmake", "--build", build_dir, "--target", "QCocoaIntegrationPlugin", "--parallel", jobs],
        log=reports / "build.log")
    plugin = build_dir / "plugins/platforms/libqcocoa.dylib"
    if not plugin.is_file():
        raise RuntimeError("QCocoaIntegrationPlugin target did not produce the expected plugin")
    artifact = work / "candidate"
    artifact.mkdir(exist_ok=False)
    candidate = artifact / "libqcocoa.dylib"
    shutil.copy2(plugin, candidate)
    # Never allow the candidate to find the just-built QtCore/QtGui. It must load
    # the unmodified official wheel's frameworks in the isolated runtime below.
    if macho_arm64(candidate)["rpaths"]:
        raise RuntimeError("Build unexpectedly embedded an RPATH despite CMAKE_SKIP_BUILD_RPATH")
    run(["install_name_tool", "-add_rpath", RPATH, candidate], log=reports / "relocation.log")
    run(["codesign", "--force", "--sign", "-", candidate], log=reports / "relocation.log")
    run(["codesign", "--verify", "--strict", candidate], log=reports / "relocation.log")
    metadata = macho_arm64(candidate)
    validate_link_contract(metadata, settings)
    shutil.copytree(source / "LICENSES", artifact / "LICENSES")
    shutil.copy2(PINS_PATH, artifact / PINS_PATH.name)
    for name in ("CMakeCache.txt", "compile_commands.json", "config.summary"):
        if (build_dir / name).is_file():
            shutil.copy2(build_dir / name, reports / name)
    notice = ("VSE DIAGNOSTIC CANDIDATE — NOT AN OFFICIAL QT BINARY OR RELEASE\n"
              "Qt 6.12.0 Cocoa platform plugin locally rebuilt with upstream Gerrit 772484 PS6.\n"
              "Modification: native accessibility wrappers no longer delete interfaces owned by Qt views/cache.\n"
              "The upstream change was unmerged when pinned. No accessibility features are disabled.\n"
              "Qt source license notices, including LGPL-3.0-only and Cocoa BSD attribution, remain applicable.\n"
              "The accompanying rebuild-inputs contains the exact original source archive and patch.\n"
              "Rebuild procedure: docs/audits/macos-accessibility-crash.md in the matching VSE source commit.\n"
              "Only libqcocoa.dylib may be staged; never replace QtCore/QtGui/PySide/Shiboken.\n")
    (artifact / "MODIFICATIONS.txt").write_text(notice, encoding="utf-8")
    # Include original Cocoa source/header attribution alongside the library.
    shutil.copytree(source / "src/plugins/platforms/cocoa", artifact / "cocoa-modified-source")
    manifest = {"schema": 1, "diagnostic_only": True, "production_integration_allowed": False,
                "pins": settings, "toolchain": toolchain, "configure_command": command,
                "target": "QCocoaIntegrationPlugin", "candidate_sha256": sha256(candidate),
                "candidate_macho": metadata, "macos_native_ax_passed": False,
                "wheel_abi_and_runtime_passed": False}
    write_json(artifact / "candidate.json", manifest)
    print("Built diagnostic candidate; native wheel/AX validation is still required.")


def runtime_info(python: Path) -> dict:
    script = """import json,sys,sysconfig,importlib.metadata
from PySide6 import QtCore
print(json.dumps({'python_prefix':sys.prefix,'packages':sysconfig.get_path('purelib'),
 'qt':QtCore.qVersion(),'pyside':importlib.metadata.version('PySide6-Essentials'),
 'shiboken':importlib.metadata.version('shiboken6'),'qt_build':QtCore.QLibraryInfo.build()}))
"""
    return json.loads(run([python, "-c", script]).strip())


def export_symbols(path: Path) -> set[str]:
    return set(run(["nm", "-arch", "arm64", "-gUj", path]).splitlines())


def qt_imports(nm_output: str) -> dict[str, set[str]]:
    imported = {"QtCore": set(), "QtGui": set()}
    for line in nm_output.splitlines():
        match = re.search(r"\bexternal\s+(\S+)\s+\(from (QtCore|QtGui)\)", line)
        if match:
            imported[match.group(2)].add(match.group(1))
        elif "(from Qt" in line:
            raise RuntimeError(f"Unrecognized Qt imported-symbol record: {line}")
    if not all(imported.values()):
        raise RuntimeError("Could not enumerate both QtCore and QtGui imports; do not assume ABI compatibility")
    return imported


def verify_symbols(candidate: Path, qt_root: Path) -> dict:
    imported = qt_imports(run(["nm", "-arch", "arm64", "-u", "-m", candidate]))
    result = {}
    for name, symbols in imported.items():
        library = qt_root / f"lib/{name}.framework/Versions/A/{name}"
        missing = symbols - export_symbols(library)
        result[name] = {"sha256": sha256(library), "required_symbols": len(symbols), "missing": sorted(missing)}
        if missing:
            raise RuntimeError(f"Candidate private ABI imports not present in official wheel {name}: {sorted(missing)[:20]}")
    return result


def inventory(directory: Path) -> dict[str, str]:
    return {str(path.relative_to(directory)): sha256(path)
            for path in sorted(directory.rglob("*")) if path.is_file() and path.suffix != ".pyc"}


def stage(work: Path, python: Path, wheelhouse: Path) -> None:
    require_hosted_macos()
    settings = pins()
    work = work.resolve()
    expected_prefix = work / "candidate-venv"
    info = runtime_info(python)
    if Path(info["python_prefix"]).resolve() != expected_prefix:
        raise RuntimeError("Only work-dir/candidate-venv may be changed; production/other environments are forbidden")
    if any(info[key] != settings["qt_version"] for key in ("qt", "pyside", "shiboken")):
        raise RuntimeError("Candidate runtime must use exactly Qt/PySide Essentials/Shiboken 6.12.0")
    wheel = wheelhouse / settings["wheel"]["filename"]
    if not wheel.is_file() or sha256(wheel) != settings["wheel"]["sha256"]:
        raise RuntimeError("Official PySide6 wheel missing or digest differs from pinned PyPI digest")
    packages = Path(info["packages"]).resolve()
    if not packages.is_relative_to(expected_prefix):
        raise RuntimeError("Candidate site-packages escaped isolated virtual environment")
    original = packages / PLUGIN_RELATIVE
    if sha256(original) != settings["wheel"]["original_cocoa_sha256"]:
        raise RuntimeError("Original candidate-venv plugin is not the reviewed untouched vendor binary")
    if macho_arm64(original).get("uuid") != settings["wheel"]["original_arm64_uuid"]:
        raise RuntimeError("Original wheel arm64 UUID mismatch")
    artifact = work / "candidate"
    candidate = artifact / "libqcocoa.dylib"
    manifest = json.loads((artifact / "candidate.json").read_text())
    if manifest["pins"] != settings or sha256(candidate) != manifest["candidate_sha256"]:
        raise RuntimeError("Candidate artifact/provenance changed since build")
    validate_link_contract(macho_arm64(candidate), settings)
    metadata = json.loads(run([python, "-c",
        "import json,sys; from PySide6.QtCore import QPluginLoader; "
        "print(json.dumps(QPluginLoader(sys.argv[1]).metaData()))", candidate]).strip())
    if (metadata.get("version") != 0x060C00 or metadata.get("debug") is not False
            or metadata.get("className") != "QCocoaIntegrationPlugin"
            or metadata.get("MetaData", {}).get("Keys") != ["cocoa"]):
        raise RuntimeError(f"Unexpected candidate Qt plugin metadata: {metadata}")
    symbols = verify_symbols(candidate, packages / "PySide6/Qt")
    before = inventory(packages / "PySide6")
    records_before = {str(path): sha256(path) for path in packages.glob("*.dist-info/RECORD")}
    if not records_before:
        raise RuntimeError("Installed distribution RECORD files are missing")
    backup = work / "reports/stock-libqcocoa.dylib"
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(original, backup)
    shutil.copy2(candidate, original)
    after = inventory(packages / "PySide6")
    changed = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
    if changed != ["Qt/plugins/platforms/libqcocoa.dylib"]:
        raise RuntimeError(f"Unexpected vendor files changed: {changed}")
    if records_before != {str(path): sha256(path) for path in packages.glob("*.dist-info/RECORD")}:
        raise RuntimeError("Original vendor RECORD files changed during candidate staging")
    report = {"runtime": info, "plugin_metadata": metadata, "symbols": symbols, "modified_vendor_files": changed,
              "original_RECORD_unchanged": True, "original_wheel_unchanged": sha256(wheel) == settings["wheel"]["sha256"],
              "candidate_sha256": sha256(original), "official_frameworks_unchanged": True,
              "scope": "Private diagnostic venv only; not a release package"}
    write_json(work / "reports/staging.json", report)
    write_json(work / "reports/official-wheel-file-inventory.json", before)


def verify_runtime(work: Path, python: Path) -> None:
    require_hosted_macos()
    script = r'''import ctypes,json,os
from pathlib import Path
from PySide6 import QtCore,QtWidgets
app=QtWidgets.QApplication([])
if app.platformName() != "cocoa": raise RuntimeError("Not using native Cocoa")
lib=ctypes.CDLL(None)
lib._dyld_image_count.restype=ctypes.c_uint32
lib._dyld_get_image_name.argtypes=[ctypes.c_uint32]
lib._dyld_get_image_name.restype=ctypes.c_char_p
names=[lib._dyld_get_image_name(i).decode() for i in range(lib._dyld_image_count())]
selected=[str(Path(n).resolve()) for n in names if any(x in n for x in ("libqcocoa.dylib","QtCore.framework/","QtGui.framework/"))]
print("VSE_QT_AX_RUNTIME="+json.dumps({"qt":QtCore.qVersion(),"platform":app.platformName(),"images":selected}))
'''
    work = work.resolve()
    info = runtime_info(python)
    if Path(info["python_prefix"]).resolve() != work / "candidate-venv":
        raise RuntimeError("Only the isolated candidate runtime can be verified")
    environment = os.environ.copy()
    for name in ("QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH"):
        environment.pop(name, None)
    environment.update(QT_QPA_PLATFORM="cocoa", QT_DEBUG_PLUGINS="1", PYTHONDONTWRITEBYTECODE="1")
    output = run([python, "-c", script], env=environment, log=work / "reports/runtime-load.log")
    lines = [line.split("=", 1)[1] for line in output.splitlines() if line.startswith("VSE_QT_AX_RUNTIME=")]
    if len(lines) != 1:
        raise RuntimeError("Missing exact runtime image-load evidence")
    result = json.loads(lines[0])
    packages = Path(info["packages"])
    expected = {str((packages / PLUGIN_RELATIVE).resolve())}
    expected.update(str((packages / f"PySide6/Qt/lib/{name}.framework/Versions/A/{name}").resolve())
                    for name in ("QtCore", "QtGui"))
    if set(result["images"]) != expected or result["qt"] != pins()["qt_version"]:
        raise RuntimeError(f"Runtime loaded a rebuilt/system/foreign Qt binary instead of the wheel: {result}")
    write_json(work / "reports/runtime-load.json", result)


def probe_records(output: str) -> list[dict]:
    prefix = "VSE_MACOS_AX_PROBE "
    return [json.loads(line[len(prefix):]) for line in output.splitlines() if line.startswith(prefix)]


def reproduced_native_failure(returncode: int, records: list[dict]) -> bool:
    """Do not mistake permissions, missing APIs, wrong Qt or keyboard failures for the bug."""
    if not records:
        return False
    if returncode == -signal.SIGSEGV:
        return records[-1].get("event") == "before_native_selected_children"
    result = records[-1]
    return (returncode == 1 and result.get("event") == "result"
            and result.get("failure_kind") == "accessibility_invariant"
            and (result.get("message", "").startswith("table-cache-loss:")
                 or result.get("message", "").startswith("cocoa selected cells differ:")))


def passed_native_probe(returncode: int, records: list[dict]) -> bool:
    if len(records) < 2:
        return False
    result = records[-1]
    return (returncode == 0 and records[-2].get("event") == "closed"
            and result.get("event") == "result" and result.get("status") == "passed"
            and result.get("mode") == "cocoa" and result.get("native_cocoa") is True
            and result.get("accessibility_active") is True and result.get("external_ax_client") is False
            and result.get("qt") == pins()["qt_version"])


def probe(work: Path, python: Path, phase: str) -> None:
    require_hosted_macos()
    work = work.resolve()
    info = runtime_info(python)
    if Path(info["python_prefix"]).resolve() != work / "candidate-venv":
        raise RuntimeError("Probe must target this job's disposable candidate-venv")
    original = Path(info["packages"]) / PLUGIN_RELATIVE
    if phase == "stock":
        expected = pins()["wheel"]["original_cocoa_sha256"]
    else:
        expected = json.loads((work / "candidate/candidate.json").read_text())["candidate_sha256"]
    if sha256(original) != expected:
        raise RuntimeError(f"Wrong plugin for {phase} probe")
    results = []
    environment = os.environ.copy()
    environment.update(QT_QPA_PLATFORM="cocoa", PYTHONDONTWRITEBYTECODE="1")
    for scenario in ("selected-row", "model-churn"):
        command = [str(python), str(ROOT / "scripts/macos_accessibility_probe.py"),
                   "--mode", "cocoa", "--scenario", scenario, "--cycles", "5",
                   "--expect-qt", pins()["qt_version"]]
        try:
            completed = subprocess.run(command, cwd=ROOT, env=environment, text=True,
                                       capture_output=True, timeout=90)
        except subprocess.TimeoutExpired as exc:
            write_json(work / f"reports/{phase}-native-probe.json", {"status": "environment-timeout", "scenario": scenario})
            raise RuntimeError("Native probe timed out; not evidence of the targeted crash") from exc
        output = completed.stdout + "\n--- STDERR ---\n" + completed.stderr
        (work / f"reports/{phase}-{scenario}.log").write_text(output, encoding="utf-8")
        records = probe_records(completed.stdout)
        results.append({"scenario": scenario, "returncode": completed.returncode, "records": records,
                        "targeted_failure_reproduced": reproduced_native_failure(completed.returncode, records),
                        "passed": passed_native_probe(completed.returncode, records)})
    report = {"phase": phase, "plugin_sha256": sha256(original), "results": results,
              "targeted_failure_reproduced": any(item["targeted_failure_reproduced"] for item in results),
              "all_passed": all(item["passed"] for item in results), "external_ax_client_tested": False}
    write_json(work / f"reports/{phase}-native-probe.json", report)
    if phase == "candidate" and not report["all_passed"]:
        raise RuntimeError("Candidate failed native selection/model-lifetime acceptance")
    # A baseline with no reproduction is retained as inconclusive; it never
    # becomes a successful fixed-regression claim at finalize().
    print(json.dumps({key: value for key, value in report.items() if key != "results"}))


def finalize(work: Path) -> None:
    require_hosted_macos()
    import xml.etree.ElementTree as ET
    work = work.resolve()
    reports = work / "reports"
    stock = json.loads((reports / "stock-native-probe.json").read_text())
    candidate = json.loads((reports / "candidate-native-probe.json").read_text())
    load = json.loads((reports / "runtime-load.json").read_text())
    staging = json.loads((reports / "staging.json").read_text())
    manifest = json.loads((work / "candidate/candidate.json").read_text())
    tree = ET.parse(reports / "gui-tests.xml")
    suites = list(tree.getroot().iter("testsuite"))
    if (not suites or sum(int(suite.get("tests", "0")) - int(suite.get("skipped", "0")) for suite in suites) <= 0
            or any(int(suite.get("failures", "0")) or int(suite.get("errors", "0")) for suite in suites)):
        raise RuntimeError("GUI/API/theme regression suite did not pass")
    if "GUI_SMOKE_OK:" not in (reports / "gui-smoke.log").read_text():
        raise RuntimeError("Full GUI smoke result is missing")
    if not stock.get("targeted_failure_reproduced"):
        raise RuntimeError("Stock Qt did not reproduce this regression: candidate comparison is inconclusive")
    if not candidate.get("all_passed"):
        raise RuntimeError("Candidate native AX probe did not pass every scenario")
    if (candidate["plugin_sha256"] != staging["candidate_sha256"]
            or candidate["plugin_sha256"] != manifest["candidate_sha256"]
            or sha256(work / "candidate/libqcocoa.dylib") != candidate["plugin_sha256"]):
        raise RuntimeError("Evidence refers to different candidate plugin bytes")
    summary = {"schema": 1, "status": "diagnostic-candidate-passed", "source_commit": os.environ.get("GITHUB_SHA"),
               "candidate_sha256": candidate["plugin_sha256"], "qt": load["qt"],
               "stock_regression_reproduced": True, "native_in_process_ax_passed": True,
               "official_wheel_frameworks_retained": True, "gui_api_theme_regressions_passed": True,
               "production_integration_allowed": False, "external_ax_client_tested": False,
               "user_macos_26_6_2_acceptance_passed": False,
               "remaining": ["Authorized external AX hierarchy reads on the final candidate application",
                             "User-macOS native file-dialog import, selected task rows, preview and OCR flows",
                             "Explicit review before any production-package integration"]}
    write_json(reports / "candidate-acceptance.json", summary)
    manifest.update(wheel_abi_and_runtime_passed=True, macos_native_ax_passed=True,
                    native_ax_scope="in-process synthetic table; not external client or user-machine acceptance",
                    acceptance_evidence_sha256=sha256(reports / "candidate-acceptance.json"))
    write_json(work / "candidate/candidate.json", manifest)
    print(json.dumps(summary, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "stage", "verify-runtime", "probe", "finalize"))
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--python", type=Path)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--phase", choices=("stock", "candidate"))
    args = parser.parse_args()
    if args.action == "build":
        if not 1 <= args.jobs <= 8:
            parser.error("--jobs must be between 1 and 8")
        build(args.work_dir, args.jobs)
    elif args.action == "stage":
        if not args.python or not args.wheelhouse:
            parser.error("stage requires --python and --wheelhouse")
        stage(args.work_dir, args.python, args.wheelhouse)
    elif args.action == "verify-runtime":
        if not args.python:
            parser.error("verify-runtime requires --python")
        verify_runtime(args.work_dir, args.python)
    elif args.action == "probe":
        if not args.python or not args.phase:
            parser.error("probe requires --python and --phase")
        probe(args.work_dir, args.python, args.phase)
    else:
        finalize(args.work_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
