"""Decode video frames with a bounded bundled-FFmpeg fallback for missing codecs.

OpenCV can open AV1 metadata while its wheel cannot decode a single frame.
Do not interpret that failure as an empty video or successful empty subtitles.
"""
import math
import os
import subprocess
import shutil
import time
from contextlib import contextmanager
from pathlib import Path
import tempfile

import cv2
import imageio_ffmpeg
import numpy as np


def _ffmpeg_frame(video_path, seconds, stop_event=None, timeout=30):
    if stop_event is not None and stop_event.is_set():
        raise RuntimeError("Video decoding cancelled")
    command = [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner", "-loglevel", "error",
               "-ss", f"{seconds:.9f}", "-i", os.fspath(video_path), "-map", "0:v:0",
               "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-threads", "1", "pipe:1"]
    kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    process = subprocess.Popen(command, **kwargs)
    deadline = time.monotonic() + timeout
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                raise RuntimeError("Video decoding cancelled")
            if time.monotonic() >= deadline:
                raise RuntimeError(f"FFmpeg frame decode timed out after {timeout} seconds")
            try:
                output, error = process.communicate(timeout=min(.1, max(.001, deadline - time.monotonic())))
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode or not output:
            detail = error.decode("utf-8", "replace").strip()[-1500:]
            raise RuntimeError(f"Cannot decode video frame at {seconds:.3f}s: {video_path}; {detail}")
        frame = cv2.imdecode(np.frombuffer(output, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise RuntimeError(f"FFmpeg returned an invalid video frame: {video_path}")
        return frame
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()


def read_frame(cap, video_path, *, frame_index=None, milliseconds=None, stop_event=None, on_fallback=None):
    """Read a BGR frame, preserving frame/timestamp targeting and full-frame ROI.

    Explicit frame indices are zero-based. Never trust OpenCV's cursor after
    a failed decode: some AV1 backends report huge negative positions. Failed
    decoding inside the reported range raises rather than silently giving EOF.
    """
    if milliseconds is not None:
        if not math.isfinite(milliseconds) or milliseconds < 0:
            raise ValueError("Video timestamp must be finite and nonnegative")
        cap.set(cv2.CAP_PROP_POS_MSEC, milliseconds)
        seconds = milliseconds / 1000
        fps = cap.get(cv2.CAP_PROP_FPS)
        index = seconds * fps
    else:
        if frame_index is None:
            raise ValueError("Specify an explicit frame index or timestamp")
        current = cap.get(cv2.CAP_PROP_POS_FRAMES)
        # Keep ordinary sequential decoding linear. Seeking each frame can
        # repeatedly decode a long GOP from its keyframe. A broken cursor is
        # only a reason to seek, never the source of the requested timestamp.
        if not math.isfinite(current) or abs(current - frame_index) > .001:
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        index = frame_index
        fps = cap.get(cv2.CAP_PROP_FPS)
        if not math.isfinite(index) or index < 0 or not math.isfinite(fps) or fps <= 0:
            raise ValueError("Cannot determine a valid frame timestamp")
        seconds = index / fps
    ret, frame = cap.read()
    if ret and frame is not None:
        return True, frame
    count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    if milliseconds is None and math.isfinite(count) and count > 0 and index >= count:
        return False, None
    frame = _ffmpeg_frame(video_path, seconds, stop_event)
    if on_fallback is not None:
        on_fallback()
    return True, frame


@contextmanager
def compatible_video(video_path, work_dir, *, on_spawn=None, timeout=1800, max_bytes=None):
    """Make a lossless temporary stream for VSF's own limited OpenCV decoder.

    Called only after an actual OpenCV decode failure, not for every input.
    Original dimensions, frame timing and pixels are retained; audio is unused.
    The caller registers the subprocess with its cancellation/process manager.
    """
    with tempfile.TemporaryDirectory(prefix="codec-", dir=work_dir) as temporary:
        target = Path(temporary) / "compatible.mkv"
        command = [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner", "-loglevel", "error",
                   "-y", "-i", os.fspath(video_path), "-map", "0:v:0", "-an", "-sn",
                   "-c:v", "ffv1", "-level", "3", "-threads", "2", "-vsync", "0", str(target)]
        kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        free_bytes = shutil.disk_usage(work_dir).free
        budget = min(20 * 1024**3, int(free_bytes * .8)) if max_bytes is None else max_bytes
        if budget <= 0:
            raise RuntimeError("Insufficient temporary disk space for video compatibility conversion")
        process = subprocess.Popen(command, **kwargs)
        try:
            if on_spawn is not None:
                on_spawn(process)
            deadline = time.monotonic() + timeout
            while True:
                if target.exists() and target.stat().st_size > budget:
                    raise RuntimeError("Video compatibility conversion exceeded its temporary disk budget; pre-convert the video to a supported codec")
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Video compatibility conversion timed out after {timeout} seconds")
                try:
                    _, error = process.communicate(timeout=min(.1, max(.001, deadline - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    continue
            if target.exists() and target.stat().st_size > budget:
                raise RuntimeError("Video compatibility conversion exceeded its temporary disk budget; pre-convert the video to a supported codec")
            if process.returncode or not target.is_file():
                detail = error.decode("utf-8", "replace").strip()[-1500:]
                raise RuntimeError(f"Cannot prepare video compatibility stream: {detail}")
            yield target
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()
