"""Portable safety/identity tests; never represented as native Mac acceptance."""
import base64
import hashlib
import io
import json
from pathlib import Path
import signal
import struct
import tarfile

import pytest

from scripts import build_macos_qt_accessibility as module


def binary(commands, *, cpu=0x0100000C):
    body = b"".join(commands)
    return struct.pack("<8I", 0xFEEDFACF, cpu, 0, 8, len(commands), len(body), 0, 0) + body


def string_command(kind, value):
    payload = value.encode() + b"\0"
    size = (12 + len(payload) + 7) & ~7
    return struct.pack("<3I", kind, size, 12) + payload + b"\0" * (size - 12 - len(payload))


def metadata():
    return {"platform": 1, "minimum": 0x0E0400, "sdk": 0x1A0000,
            "rpaths": [module.RPATH], "dependencies": [
                "@rpath/QtCore.framework/Versions/A/QtCore",
                "@rpath/QtGui.framework/Versions/A/QtGui", "/usr/lib/libc++.1.dylib"]}


def test_pins_are_exact_and_never_current_or_latest():
    data = module.pins()
    assert data["qt_version"] == "6.12.0"
    assert data["patch"]["revision"] in data["patch"]["url"]
    assert data["patch"]["patch_set"] == 6
    assert "/current/" not in data["patch"]["url"]
    assert len(data["source"]["sha256"]) == len(data["patch"]["decoded_sha256"]) == 64
    assert data["wheel"]["original_arm64_uuid"] == "028e2655-9fd8-3e6e-a861-d6cd79b760bc"


def test_non_ci_and_user_desktops_cannot_build_or_stage(monkeypatch):
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module.platform, "machine", lambda: "arm64")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_OS", "macOS")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "self-hosted")
    with pytest.raises(RuntimeError, match="GitHub-hosted"):
        module.require_hosted_macos()
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted")
    module.require_hosted_macos()


def test_hash_mismatch_never_materializes_download(tmp_path, monkeypatch):
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"bad"))
    output = tmp_path / "source.tar.xz"
    with pytest.raises(RuntimeError, match="hash mismatch"):
        module.fetch("https://example.invalid", output, "0" * 64)
    assert not output.exists()
    assert not list(tmp_path.glob("*.partial"))


def test_patch_base64_transport_is_decoded_before_hashing(tmp_path, monkeypatch):
    patch = b"fixed upstream patch\n"
    monkeypatch.setattr(module.urllib.request, "urlopen",
                        lambda *a, **k: io.BytesIO(base64.b64encode(patch) + b"\n"))
    output = tmp_path / "candidate.patch"
    module.fetch("https://example.invalid", output, hashlib.sha256(patch).hexdigest(), base64_encoded=True)
    assert output.read_bytes() == patch


@pytest.mark.parametrize("name", ["../escape", "/escape", "wrong-root/a"])
def test_source_archive_cannot_escape_reviewed_root(tmp_path, name):
    archive = tmp_path / "source.tar.xz"
    with tarfile.open(archive, "w:xz") as output:
        item = tarfile.TarInfo(name)
        item.size = 1
        output.addfile(item, io.BytesIO(b"x"))
    with pytest.raises(RuntimeError, match="archive member"):
        module.unpack_source(archive, tmp_path / "extract", "qtbase-everywhere-src-6.12.0")
    assert not (tmp_path / "escape").exists()


def test_macho_arm64_thin_and_universal_identity(tmp_path):
    identifier = bytes.fromhex("028e26559fd83e6ea861d6cd79b760bc")
    commands = [struct.pack("<2I", 0x1B, 24) + identifier,
                struct.pack("<6I", 0x32, 24, 1, 0x0E0400, 0x1A0000, 0),
                string_command(0x8000001C, module.RPATH),
                string_command(0xC, "@rpath/QtCore.framework/Versions/A/QtCore")]
    thin = binary(commands)
    path = tmp_path / "cocoa"
    path.write_bytes(thin)
    observed = module.macho_arm64(path)
    assert observed["uuid"] == "028e2655-9fd8-3e6e-a861-d6cd79b760bc"
    assert observed["rpaths"] == [module.RPATH]
    fat = struct.pack(">7I", 0xCAFEBABE, 1, 0x0100000C, 0, 32, len(thin), 0) + b"\0" * 4 + thin
    path.write_bytes(fat)
    assert module.macho_arm64(path) == observed


def test_macho_missing_arm64_is_rejected(tmp_path):
    path = tmp_path / "x64"
    path.write_bytes(binary([], cpu=0x01000007))
    with pytest.raises(ValueError, match="arm64"):
        module.macho_arm64(path)


def test_only_the_wheel_relative_rpath_and_expected_qt_frameworks_are_allowed():
    data = metadata()
    module.validate_link_contract(data, module.pins())
    for dependency in ("/opt/homebrew/lib/libQtCore.dylib", "@rpath/QtWidgets.framework/Versions/A/QtWidgets"):
        changed = {**data, "dependencies": data["dependencies"] + [dependency]}
        with pytest.raises(RuntimeError, match="dependency"):
            module.validate_link_contract(changed, module.pins())
    for path in ("/tmp/qt-build/lib", "@loader_path/../../../system"):
        with pytest.raises(RuntimeError, match="search path"):
            module.validate_link_contract({**data, "rpaths": [path]}, module.pins())
    with pytest.raises(RuntimeError, match="deployment target"):
        module.validate_link_contract({**data, "minimum": 0x1A0000}, module.pins())


def test_configure_preserves_accessibility_and_only_build_target_is_reviewed():
    command = module.configure_command(Path("source"), Path("install"))
    assert "-DFEATURE_accessibility=ON" in command
    assert "-DCMAKE_SKIP_BUILD_RPATH=ON" in command
    assert "-DCMAKE_OSX_DEPLOYMENT_TARGET=14.4" in command
    assert not any("accessibility=OFF" in part or "no-feature-accessibility" in part for part in command)


def test_qt_imports_must_be_enumerable_and_check_both_frameworks():
    output = "         (undefined) external _QtCoreMethod (from QtCore)\n         (undefined) weak external _QtGuiMethod (from QtGui)"
    assert module.qt_imports(output) == {"QtCore": {"_QtCoreMethod"}, "QtGui": {"_QtGuiMethod"}}
    with pytest.raises(RuntimeError, match="both"):
        module.qt_imports("(undefined) external _onlyCore (from QtCore)")


def test_stage_rejects_an_existing_production_environment_before_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "require_hosted_macos", lambda: None)
    monkeypatch.setattr(module, "runtime_info", lambda _: {"python_prefix": str(tmp_path / "production")})
    with pytest.raises(RuntimeError, match="production/other environments"):
        module.stage(tmp_path / "work", Path("python"), tmp_path / "wheels")


def test_baseline_reproduction_is_narrow_and_never_a_generic_crash():
    event = {"event": "before_native_selected_children"}
    assert module.reproduced_native_failure(-signal.SIGSEGV, [event])
    assert not module.reproduced_native_failure(-signal.SIGSEGV, [{"event": "start"}])
    assert not module.reproduced_native_failure(2, [event])
    for message in ("table-cache-loss: interface missing", "cocoa selected cells differ: empty"):
        assert module.reproduced_native_failure(1, [{"event": "result", "failure_kind": "accessibility_invariant", "message": message}])
    assert not module.reproduced_native_failure(1, [{"event": "result", "failure_kind": "accessibility_invariant", "message": "Keyboard Down did not move"}])


def test_native_success_requires_selection_activation_and_clean_disposal():
    result = {"event": "result", "status": "passed", "mode": "cocoa", "native_cocoa": True,
              "external_ax_client": False, "accessibility_active": True, "qt": "6.12.0"}
    assert module.passed_native_probe(0, [{"event": "closed"}, result])
    assert not module.passed_native_probe(0, [{"event": "before_close"}, result])
    assert not module.passed_native_probe(0, [{"event": "closed"}, {**result, "native_cocoa": False}])
    assert not module.passed_native_probe(-signal.SIGSEGV, [{"event": "closed"}, result])


def test_workflow_is_opt_in_read_only_and_never_replaces_release_packages():
    workflow = (module.ROOT / ".github/workflows/macos-accessibility.yml").read_text()
    assert "  workflow_dispatch:" in workflow
    assert "  push:" not in workflow and "  workflow_call:" not in workflow
    assert "contents: read" in workflow and "contents: write" not in workflow
    assert "package_release.py" not in workflow and "gh release" not in workflow
    assert "--phase stock" in workflow and "--phase candidate" in workflow
    assert "diagnostic-not-for-release" in workflow
