"""Portable, shell-free integration with videosubfinder-cli 6.10 and newer.

The CLI's progress and frame events are on stderr. A search reporting 100% is
not complete until FINISH *and* a successful process exit have been observed.
This module deliberately has no GUI, OCR or numerical-library dependencies.
"""
from collections import deque
from dataclasses import dataclass
import math
import os
from pathlib import Path
import platform
import re
import subprocess
from typing import Callable, Optional

VSF_VERSION = "6.10.6"
VSF_RELEASE_BASE = "https://github.com/eritpchy/videosubfinder-cli/releases/download"
_FRAME_RE = re.compile(r"\bFrame:\s+(\d+)_(\d+)_(\d+)_(\d+)__")
_PROGRESS_RE = re.compile(r"\bProgress:\s*(\d+(?:\.\d+)?)%")
_VERSION_RE = re.compile(r"^VideoSubFinderCli (\d+)\.(\d+)(?:\.(\d+))?\s*$")
_DECODE_ERROR_RE = re.compile(
    r"^(?:\[[^\]\r\n]+ @ [0-9a-fx]+\]\s+(?:Failed to get pixel format\.?|Get current frame error\.?)\s*$"
    r"|(?:OCVVideo::OpenMovie\(\): )?ERROR: Cannot decode the first frame of video\b)",
    re.IGNORECASE)


class VideoSubFinderError(RuntimeError):
    """The CLI is unavailable, incompatible, or failed to extract subtitles."""


class VideoSubFinderDecodeError(VideoSubFinderError):
    """The native decoder failed, even if an older CLI reports exit zero."""


def platform_target(system=None, machine=None):
    system = system or platform.system()
    machine = (machine or platform.machine()).lower()
    systems = {"Linux": "linux", "Windows": "windows", "Darwin": "macos",
               "linux": "linux", "windows": "windows", "macos": "macos"}
    architectures = {"amd64": "x64", "x86_64": "x64", "x64": "x64",
                     "aarch64": "arm64", "arm64": "arm64", "universal": "universal"}
    if system not in systems or machine not in architectures:
        raise VideoSubFinderError(f"Unsupported VideoSubFinder platform: {system}/{machine}")
    target = systems[system], architectures[machine]
    if target[1] == "universal" and target[0] != "macos":
        raise VideoSubFinderError("Universal CLI packages are only available for macOS")
    return target


def release_asset(system, arch, cuda=False):
    system, arch = platform_target(system, arch)
    if cuda and (system, arch) != ("linux", "x64"):
        raise VideoSubFinderError("The CUDA 12 CLI package supports Linux x64 only")
    target = f"{system}-{'cuda12-' if cuda else ''}{arch}"
    return f"videosubfinder-cli-{target}.{'zip' if system == 'windows' else 'tar.gz'}"


def default_install_dir(backend_dir, system=None, arch=None):
    system, arch = platform_target(system, arch)
    return Path(backend_dir) / "subfinder" / system / arch


def find_binary(directory, system=None):
    system = platform_target(system, "x64")[0]
    names = ("VideoSubFinderCli.exe",) if system == "windows" else ("VideoSubFinderCli.run", "VideoSubFinderCli")
    return next((Path(directory) / name for name in names if (Path(directory) / name).is_file()), None)


def resolve_binary(backend_dir, environ=None, system=None, arch=None):
    env = os.environ if environ is None else environ
    target_system, target_arch = platform_target(system, arch)
    if env.get("VSE_VSF_BINARY"):
        binary = Path(env["VSE_VSF_BINARY"]).expanduser().resolve()
        if binary.is_file():
            return binary
        raise VideoSubFinderError(f"VSE_VSF_BINARY does not exist: {binary}")
    roots = []
    if env.get("VSE_VSF_DIR"):
        roots.append(Path(env["VSE_VSF_DIR"]).expanduser())
    else:
        roots.append(default_install_dir(backend_dir, target_system, target_arch))
        if target_system == "macos":
            roots.append(default_install_dir(backend_dir, target_system, "universal"))
        # Retain support for manually installed modern releases in the old folder.
        # The old GUI executable is deliberately NOT a valid CLI candidate.
        roots.append(Path(backend_dir) / "subfinder" / target_system)
    for root in roots:
        binary = find_binary(root, target_system)
        if binary:
            return binary.resolve()
    raise VideoSubFinderError(
        f"VideoSubFinderCli is not installed for {target_system}/{target_arch}. "
        "Run: python scripts/install_videosubfinder.py (or set VSE_VSF_BINARY).")


def verify_binary(binary, expected_version=None):
    try:
        result = subprocess.run([str(binary), "--version"], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace", timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise VideoSubFinderError(
            f"Cannot start VideoSubFinderCli at {binary}: {error}. "
            "Run: python scripts/install_videosubfinder.py") from error
    match = _VERSION_RE.fullmatch(result.stdout.strip())
    if result.returncode or not match or tuple(map(int, match.groups(default="0"))) < (6, 10, 0):
        raise VideoSubFinderError(
            f"Incompatible VideoSubFinderCli at {binary} (exit {result.returncode}): "
            f"{result.stdout.strip()[:1000]}. Install pinned CLI {VSF_VERSION} with "
            "python scripts/install_videosubfinder.py")
    version = ".".join(part for part in match.groups() if part is not None)
    if expected_version and version != expected_version.lstrip("v"):
        raise VideoSubFinderError(f"Expected CLI {expected_version}, received {version}")
    return version


def timestamp_to_frame(timestamp_ms, fps):
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError("Video frame rate must be positive and finite")
    return round(timestamp_ms * fps / 1000.0)


def parse_events(line):
    """Recover complete records even when native output shares a line.

    VSF writes diagnostics without a newline and constructs progress with
    several stream writes. A worker's complete ``Frame: ...`` record can
    therefore follow diagnostics or interrupt ``Progress: ``. Anchoring at
    column zero silently drops that frame and its subtitle from OCR.
    """
    events = []
    for match in _FRAME_RE.finditer(line):
        hours, minutes, seconds, millis = map(int, match.groups())
        if minutes < 60 and seconds < 60 and millis < 1000:
            events.append((match.start(), "frame", ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis))
    for match in _PROGRESS_RE.finditer(line):
        events.append((match.start(), "progress", min(99.0, max(0.0, float(match.group(1))))))
    if line.strip() == "FINISH":
        return [("finish", None)]
    return [(event, value) for _, event, value in sorted(events)] or [("log", line.rstrip())]


def parse_event(line):
    """Compatibility helper for callers expecting a single event."""
    return parse_events(line)[0]


def build_command(binary, video_path, output_dir, subtitle_path, *, crop, threads=2,
                  decoder="opencv", use_cuda=False):
    top, bottom, left, right = map(float, crop)
    if not all(map(math.isfinite, (top, bottom, left, right))) or not (0 <= bottom < top <= 1 and 0 <= left < right <= 1):
        raise ValueError("Invalid subtitle crop: expected 0 <= bottom < top <= 1 and 0 <= left < right <= 1")
    if decoder not in ("opencv", "ffmpeg"):
        raise ValueError(f"Unsupported video decoder: {decoder}")
    command = [str(binary), "-c", "-r", "-i", str(Path(video_path).resolve()),
               "-o", str(Path(output_dir).resolve()), "-ces", str(Path(subtitle_path).resolve()),
               "-te", str(top), "-be", str(bottom), "-le", str(left), "-re", str(right),
               "-nthr", str(max(2, min(64, int(threads)))), "-dsi", f"--open_video_{decoder}"]
    # OCR acceleration (DirectML, CoreML, CUDA) says nothing about CLI build support.
    # Only an explicit CLI CUDA request opts into the separate CUDA binary.
    if use_cuda:
        command.append("--use_cuda")
    return command


@dataclass(frozen=True)
class ExtractionResult:
    timestamps_ms: tuple
    returncode: int


def run_command(command, *, on_progress: Optional[Callable] = None,
                on_log: Optional[Callable] = None, on_spawn: Optional[Callable] = None):
    kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                  encoding="utf-8", errors="replace", bufsize=1, shell=False)
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    tail, timestamps = deque(maxlen=40), set()
    finished = False
    decode_errors = []
    try:
        process = subprocess.Popen(command, **kwargs)
    except OSError as error:
        raise VideoSubFinderError(f"Could not start VideoSubFinderCli: {error}") from error
    try:
        if on_spawn:
            on_spawn(process)
        for line in process.stdout:
            tail.append(line.rstrip())
            # Older portable OpenCV builds can fail AV1 while Python's wheel
            # decodes it. These are decoder errors, unlike the normal EOF log
            # "Video ended or cannot read new frames" or a subtitle-free clip.
            if _DECODE_ERROR_RE.search(line):
                if len(decode_errors) < 8:
                    decode_errors.append(line.rstrip())
            for event, value in parse_events(line):
                if event == "frame":
                    timestamps.add(value)
                elif event == "progress":
                    if on_progress:
                        on_progress(value)
                elif event == "finish":
                    finished = True
                elif value and on_log:
                    on_log(value)
        returncode = process.wait()
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        raise
    finally:
        process.stdout.close()
    if decode_errors and returncode >= 0 and returncode != 0xC000013A:
        # Cancellation (POSIX signal or Windows Ctrl-C) must never retry.
        raise VideoSubFinderDecodeError("VideoSubFinder native video decoding failed:\n" + "\n".join(decode_errors))
    if returncode != 0 or not finished:
        reason = f"exit {returncode}" if returncode else "missing FINISH marker"
        raise VideoSubFinderError(f"VideoSubFinderCli failed ({reason}):\n" + "\n".join(tail))
    if on_progress:
        on_progress(100)
    return ExtractionResult(tuple(sorted(timestamps)), returncode)
