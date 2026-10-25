import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import zipfile

import pytest

from backend.tools import videosubfinder as vsf
from scripts import install_videosubfinder as installer


@pytest.mark.parametrize("system,machine,expected", [
    ("Linux", "x86_64", ("linux", "x64")), ("Linux", "aarch64", ("linux", "arm64")),
    ("Windows", "AMD64", ("windows", "x64")), ("Windows", "ARM64", ("windows", "arm64")),
    ("Darwin", "arm64", ("macos", "arm64")), ("Darwin", "x86_64", ("macos", "x64")),
])
def test_platforms(system, machine, expected):
    assert vsf.platform_target(system, machine) == expected


def test_unsupported_platforms_and_cuda():
    with pytest.raises(vsf.VideoSubFinderError):
        vsf.platform_target("FreeBSD", "x86_64")
    with pytest.raises(vsf.VideoSubFinderError):
        vsf.release_asset("windows", "x64", cuda=True)
    assert vsf.release_asset("linux", "x64", cuda=True) == "videosubfinder-cli-linux-cuda12-x64.tar.gz"
    assert vsf.release_asset("macos", "universal") == "videosubfinder-cli-macos-universal.tar.gz"


def test_resolver_prefers_native_and_override(tmp_path):
    directory = tmp_path / "subfinder" / "macos" / "arm64"
    directory.mkdir(parents=True)
    binary = directory / "VideoSubFinderCli.run"
    binary.touch()
    assert vsf.resolve_binary(tmp_path, {}, "Darwin", "arm64") == binary
    other = tmp_path / "custom binary"
    other.touch()
    assert vsf.resolve_binary(tmp_path, {"VSE_VSF_BINARY": str(other)}, "Darwin", "arm64") == other
    with pytest.raises(vsf.VideoSubFinderError, match="does not exist"):
        vsf.resolve_binary(tmp_path, {"VSE_VSF_BINARY": str(other / "bad")})


def test_resolver_does_not_use_legacy_windows_gui(tmp_path):
    directory = tmp_path / "subfinder" / "windows"
    directory.mkdir(parents=True)
    (directory / "VideoSubFinderWXW.exe").touch()
    with pytest.raises(vsf.VideoSubFinderError, match="not installed"):
        vsf.resolve_binary(tmp_path, {}, "Windows", "AMD64")


def test_arguments_are_safe_and_cpu_only(tmp_path):
    video = tmp_path / 'video 字幕 $(echo injected) & spaces.mp4'
    cmd = vsf.build_command("binary with spaces", video, tmp_path, tmp_path / "a.srt",
                            crop=(0.4, 0, 0, 1), threads=1)
    assert cmd[0] == "binary with spaces"
    assert cmd[cmd.index("-i") + 1] == str(video)
    assert cmd[cmd.index("-nthr") + 1] == "2"
    assert "--use_cuda" not in cmd and "-nocrthr" not in cmd
    cmd = vsf.build_command("bin", video, tmp_path, tmp_path / "a.srt", crop=(1, 0, 0, 1), threads=1000, use_cuda=True)
    assert cmd[cmd.index("-nthr") + 1] == "64"
    assert "--use_cuda" in cmd


@pytest.mark.parametrize("crop", [(0, 1, 0, 1), (1, 0, 1, 0), (float("nan"), 0, 0, 1), (1.1, 0, 0, 1)])
def test_bad_crops(crop):
    with pytest.raises(ValueError):
        vsf.build_command("bin", "vid", "out", "out/a.srt", crop=crop)


def test_events_and_frame_math():
    assert vsf.parse_event("Frame: 0_00_00_000__0_00_01_999_meta\n") == ("frame", 0)
    assert vsf.parse_event("Frame: 1_02_03_004__1_02_05_004_meta\n") == ("frame", 3723004)
    assert vsf.parse_event("Progress: 100%\n") == ("progress", 99)
    assert vsf.parse_event("Progress: 15% elapsed=10s\n") == ("progress", 15)
    assert vsf.parse_event("Frame: malformed") == ("log", "Frame: malformed")
    assert vsf.timestamp_to_frame(4000, 25) == 100
    assert vsf.timestamp_to_frame(1001, 29.97) == 30
    with pytest.raises(ValueError):
        vsf.timestamp_to_frame(1000, 0)


def fake_command(source):
    return [sys.executable, "-u", "-c", source]


def test_runner_drains_after_100_and_deduplicates_out_of_order():
    progress, logs = [], []
    result = vsf.run_command(fake_command("""
import sys
print('Progress: 100%', file=sys.stderr)
print('Frame: 0_00_04_000__0_00_06_000_meta', file=sys.stderr)
print('Frame: 0_00_00_000__0_00_01_000_meta', file=sys.stderr)
print('Frame: 0_00_04_000__0_00_06_000_meta', file=sys.stderr)
print('late output on stdout')
print('FINISH', file=sys.stderr)
"""), on_progress=progress.append, on_log=logs.append)
    assert result.timestamps_ms == (0, 4000)
    assert progress == [99, 100]
    assert logs == ["late output on stdout"]


@pytest.mark.parametrize("source,reason", [
    ("import sys; print('Progress: 100%'); print('ERROR: decoder failure'); sys.exit(4)", "exit 4"),
    ("print('Progress: 100%')", "missing FINISH"),
    ("import sys; print('FINISH'); sys.exit(7)", "exit 7"),
])
def test_failure_is_not_success(source, reason):
    progress = []
    with pytest.raises(vsf.VideoSubFinderError, match=reason):
        vsf.run_command(fake_command(source), on_progress=progress.append)
    assert 100 not in progress


def test_runner_drains_both_streams_without_deadlock():
    result = vsf.run_command(fake_command("import sys; print('x' * 200000); print('y' * 200000, file=sys.stderr); print('FINISH', file=sys.stderr)"))
    assert result.returncode == 0


def test_runner_cleans_child_if_callback_fails():
    processes = []
    def fail(process):
        processes.append(process)
        raise RuntimeError("callback failed")
    with pytest.raises(RuntimeError, match="callback failed"):
        vsf.run_command(fake_command("import time; time.sleep(30)"), on_spawn=fail)
    assert processes[0].poll() is not None


def test_verify_version_legacy_crash_and_mismatch(monkeypatch):
    def result(code, text):
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, code, text))
    result(0, "VideoSubFinderCli 6.10\n")
    assert vsf.verify_binary("fake") == "6.10"
    with pytest.raises(vsf.VideoSubFinderError, match="Expected CLI"):
        vsf.verify_binary("fake", "6.11")
    result(-11, "")
    with pytest.raises(vsf.VideoSubFinderError, match="exit -11"):
        vsf.verify_binary("fake")
    result(0, "VideoSubFinderCli 6.9\n")
    with pytest.raises(vsf.VideoSubFinderError, match="Incompatible"):
        vsf.verify_binary("fake")


def test_checksums_require_exact_unique_asset():
    digest = hashlib.sha256(b"content").hexdigest()
    assert installer.checksum_for(f"{digest}  x.tar.gz\n", "x.tar.gz") == digest
    for manifest in ("", f"{digest}  x.tar.gz\n{digest}  x.tar.gz", "invalid  x.tar.gz"):
        with pytest.raises(ValueError):
            installer.checksum_for(manifest, "x.tar.gz")


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/drive", "path\\escape"])
def test_archive_rejects_unsafe_paths(tmp_path, name):
    with pytest.raises(ValueError):
        installer.safe_member(tmp_path, name)


def test_archive_rejects_links(tmp_path):
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as package:
        entry = tarfile.TarInfo("link")
        entry.type = tarfile.SYMTYPE
        entry.linkname = "/tmp/escape"
        package.addfile(entry)
    with pytest.raises(ValueError, match="Unsupported"):
        installer.extract_archive(archive, tmp_path / "out")


def test_settings_preserve_and_migrate(tmp_path):
    new, old = tmp_path / "new.cfg", tmp_path / "old.cfg"
    new.write_text("moderate_threshold = 0.25\ntext_alignment = 0\nnew_option = 1\n")
    old.write_text("moderate_threshold = 0.4\ntext_alignment = Center\ntext_procent = 0.3\nuse_cuda_gpu = 1\n")
    installer.preserve_settings(new, old)
    text = new.read_text()
    assert "moderate_threshold = 0.4" in text and "new_option = 1" in text
    assert "text_alignment = 0" in text and "text_percent = 0.3" in text
    assert "use_cuda_gpu = 0" in text


def test_checksum_failure_leaves_installation_untouched(tmp_path):
    destination = tmp_path / "installed"
    destination.mkdir()
    (destination / "keep").write_text("previous")
    archive, manifest = tmp_path / "package.zip", tmp_path / "SHA256SUMS"
    archive.write_bytes(b"broken")
    manifest.write_text("0" * 64 + "  videosubfinder-cli-windows-x64.zip\n")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        installer.install(system="windows", arch="x64", destination=destination, archive=archive, checksums=manifest)
    assert (destination / "keep").read_text() == "previous"


def test_real_extraction_with_installed_cli(tmp_path):
    if os.environ.get("VSE_RUN_VSF_INTEGRATION") != "1":
        pytest.skip("Set VSE_RUN_VSF_INTEGRATION=1 for installed CLI video extraction")
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    binary = vsf.resolve_binary(Path(__file__).parents[1] / "backend")
    assert vsf.verify_binary(binary) == "6.10"
    video = tmp_path / "input video 字幕.mp4"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 25, (640, 360))
    assert writer.isOpened()
    try:
        for frame_no in range(175):
            frame = np.full((360, 640, 3), (66, 44, 28), dtype=np.uint8)
            text = "HELLO WORLD" if 25 <= frame_no <= 75 else "SECOND LINE" if 100 <= frame_no <= 150 else ""
            if text:
                size = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 1, 2)[0]
                position = ((640 - size[0]) // 2, 305)
                cv2.putText(frame, text, position, cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), 5, cv2.LINE_AA)
                cv2.putText(frame, text, position, cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2, cv2.LINE_AA)
            writer.write(frame)
    finally:
        writer.release()
    output, subtitle = tmp_path / "output 字幕", tmp_path / "output 字幕" / "times.srt"
    progress = []
    result = vsf.run_command(vsf.build_command(binary, video, output, subtitle, crop=(0.4, 0, 0, 1), threads=2), on_progress=progress.append)
    assert len(result.timestamps_ms) >= 2
    assert any(abs(t - 1000) < 500 for t in result.timestamps_ms)
    assert any(abs(t - 4000) < 500 for t in result.timestamps_ms)
    assert subtitle.read_text().count(" --> ") >= 2
    assert progress[-1] == 100
    assert not list((output / "RGBImages").iterdir())


def test_verified_offline_install_preserves_settings(tmp_path, monkeypatch):
    archive, manifest = tmp_path / "package.zip", tmp_path / "SHA256SUMS"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("VideoSubFinderCli.exe", b"fake-test-binary")
        package.writestr("settings/general.cfg", "text_alignment = 0\nnew_option = 1\n")
    digest = installer.file_sha256(archive)
    manifest.write_text(digest + "  videosubfinder-cli-windows-x64.zip\n")
    destination = tmp_path / "installed"
    (destination / "settings").mkdir(parents=True)
    (destination / "settings/general.cfg").write_text("text_alignment = Right\nmoderate_threshold = 0.4\n")
    (destination / "old-binary").write_text("stale")
    monkeypatch.setattr(installer, "verify_binary", lambda *a: "6.10")
    monkeypatch.setattr(installer, "run_command", lambda *a: None)
    binary = installer.install(system="windows", arch="x64", destination=destination, archive=archive, checksums=manifest)
    assert binary == destination / "VideoSubFinderCli.exe"
    assert binary.read_bytes() == b"fake-test-binary"
    assert not (destination / "old-binary").exists()
    settings = (destination / "settings/general.cfg").read_text()
    assert "text_alignment = 2" in settings and "moderate_threshold = 0.4" in settings
    assert "new_option = 1" in settings
    import json
    metadata = json.loads((destination / "install.json").read_text())
    assert metadata["version"] == "6.10" and metadata["sha256"] == digest


def test_invalid_archive_does_not_replace_previous_installation(tmp_path):
    archive, manifest = tmp_path / "package.zip", tmp_path / "SHA256SUMS"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("../escaped.exe", b"bad")
    manifest.write_text(installer.file_sha256(archive) + "  videosubfinder-cli-windows-x64.zip\n")
    destination = tmp_path / "installed"
    destination.mkdir()
    (destination / "keep").write_text("previous")
    with pytest.raises(ValueError, match="Unsafe archive"):
        installer.install(system="windows", arch="x64", destination=destination, archive=archive, checksums=manifest)
    assert (destination / "keep").read_text() == "previous"
    assert not (tmp_path / "escaped.exe").exists()
