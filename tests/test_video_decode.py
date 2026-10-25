"""AV1 fallback regression: metadata-open is not proof that pixels decode."""
import queue
import subprocess
import threading
from unittest.mock import Mock

import cv2
import imageio_ffmpeg
import numpy as np
import pytest

from backend.tools import video_decode, subtitle_ocr


@pytest.fixture
def av1_video(tmp_path):
    path = tmp_path / 'AV1 clip with spaces.mkv'
    result = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-nostdin', '-y', '-v', 'error',
        '-f', 'lavfi', '-i', 'color=red:size=160x96:rate=5:duration=1',
        '-c:v', 'libaom-av1', '-cpu-used', '8', '-threads', '2', str(path)],
        capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    return path


def test_real_av1_decodes_at_frame_and_timestamp(av1_video):
    cap = cv2.VideoCapture(str(av1_video))
    try:
        assert cap.isOpened()
        for kwargs in ([{'frame_index': i} for i in range(5)] + [{'milliseconds': 200}]):
            ok, frame = video_decode.read_frame(cap, av1_video, **kwargs)
            assert ok and frame.shape == (96, 160, 3)
            assert frame[40, 40, 2] > 240 and frame[40, 40, 0] < 10
        assert video_decode.read_frame(cap, av1_video, frame_index=5) == (False, None)
    finally:
        cap.release()


def test_worker_av1_preserves_roi_frame_size_and_completes(av1_video):
    tasks, frames, progress = queue.Queue(), queue.Queue(), queue.Queue()
    tasks.put((5, 1, None, None, 0, subtitle_ocr.SubtitleArea.LOWER_PART))
    tasks.put((5, -1, None, None, None, None))
    subtitle_ocr.ocr_task_producer(frames, tasks, progress, str(av1_video), 'unused')
    number, frame, _, _ = frames.get_nowait()
    assert number == 1 and frame.shape == (96, 160, 3)
    assert not frame[:48].any() and frame[70, 40, 2] > 240
    assert frames.get_nowait() == (-1, 1, None, None)


def test_failed_opencv_read_falls_back_even_when_open(monkeypatch):
    cap = Mock()
    cap.get.side_effect = lambda prop: {cv2.CAP_PROP_FPS: 25, cv2.CAP_PROP_FRAME_COUNT: 100}.get(prop, 0)
    cap.read.return_value = (False, None)
    expected = np.zeros((10, 20, 3), dtype=np.uint8)
    fallback = Mock(return_value=expected)
    monkeypatch.setattr(video_decode, '_ffmpeg_frame', fallback)
    ok, frame = video_decode.read_frame(cap, 'video.mkv', frame_index=25)
    assert ok and frame is expected
    fallback.assert_called_once_with('video.mkv', 1.0, None)


def test_native_decode_does_not_spawn_ffmpeg(monkeypatch):
    cap = Mock()
    cap.get.return_value = 25
    frame = np.zeros((4, 4, 3), dtype=np.uint8)
    cap.read.return_value = (True, frame)
    fallback = Mock(side_effect=AssertionError('unnecessary fallback'))
    monkeypatch.setattr(video_decode, '_ffmpeg_frame', fallback)
    assert video_decode.read_frame(cap, 'video.mp4', frame_index=0)[1] is frame


def test_decoder_failure_is_not_empty_success(tmp_path):
    with pytest.raises(RuntimeError, match='Cannot decode video frame'):
        video_decode._ffmpeg_frame(tmp_path / 'missing.mkv', 0)


def test_cancel_before_spawn(monkeypatch):
    event = threading.Event()
    event.set()
    spawn = Mock()
    monkeypatch.setattr(video_decode.subprocess, 'Popen', spawn)
    with pytest.raises(RuntimeError, match='cancelled'):
        video_decode._ffmpeg_frame('video.mkv', 0, event)
    spawn.assert_not_called()


def test_timeout_kills_and_reaps_decoder(monkeypatch):
    process = Mock()
    process.poll.return_value = None
    monkeypatch.setattr(video_decode.subprocess, 'Popen', Mock(return_value=process))
    with pytest.raises(RuntimeError, match='timed out'):
        video_decode._ffmpeg_frame('video.mkv', 0, timeout=0)
    process.kill.assert_called_once()
    process.communicate.assert_called_once()


def test_real_compatibility_stream_preserves_dimensions_and_timing_and_cleanup(av1_video, tmp_path):
    original = cv2.VideoCapture(str(av1_video))
    expected = [original.get(prop) for prop in (cv2.CAP_PROP_FPS, cv2.CAP_PROP_FRAME_COUNT,
                                               cv2.CAP_PROP_FRAME_WIDTH, cv2.CAP_PROP_FRAME_HEIGHT)]
    original.release()
    with video_decode.compatible_video(av1_video, tmp_path) as compatible:
        cap = cv2.VideoCapture(str(compatible))
        try:
            assert [cap.get(prop) for prop in (cv2.CAP_PROP_FPS, cv2.CAP_PROP_FRAME_COUNT,
                                               cv2.CAP_PROP_FRAME_WIDTH, cv2.CAP_PROP_FRAME_HEIGHT)] == expected
            for _ in range(5):
                assert cap.read()[0]
            assert not cap.read()[0]
        finally:
            cap.release()
        temporary = compatible.parent
    assert not temporary.exists()
    assert av1_video.exists()


def test_compatibility_stream_cleanup_on_consumer_failure(av1_video, tmp_path):
    with pytest.raises(RuntimeError, match='consumer failed'):
        with video_decode.compatible_video(av1_video, tmp_path) as compatible:
            temporary = compatible.parent
            raise RuntimeError('consumer failed')
    assert not temporary.exists()


def test_compatibility_stream_enforces_disk_budget(av1_video, tmp_path):
    with pytest.raises(RuntimeError, match='disk budget'):
        with video_decode.compatible_video(av1_video, tmp_path, max_bytes=1):
            pytest.fail('truncated compatibility stream must not be accepted')
    assert not list(tmp_path.glob('codec-*'))


def test_cancel_during_decode_kills_and_reaps(monkeypatch):
    event = threading.Event()
    process = Mock()
    process.poll.return_value = None
    def communicate(**kwargs):
        if kwargs:
            event.set()
            raise subprocess.TimeoutExpired('ffmpeg', .1)
        return b'', b''
    process.communicate.side_effect = communicate
    monkeypatch.setattr(video_decode.subprocess, 'Popen', Mock(return_value=process))
    with pytest.raises(RuntimeError, match='cancelled'):
        video_decode._ffmpeg_frame('video.mkv', 0, event)
    process.kill.assert_called_once()
    assert process.communicate.call_count == 2


def test_conversion_cancelled_by_process_manager_cleans_temp(av1_video, tmp_path):
    with pytest.raises(RuntimeError, match='compatibility stream'):
        with video_decode.compatible_video(av1_video, tmp_path, on_spawn=lambda process: process.kill()):
            pytest.fail('killed conversion must not succeed')
    assert not list(tmp_path.glob('codec-*'))


def test_compatibility_stream_preserves_variable_frame_timestamps(tmp_path):
    import re
    source = tmp_path/'variable timing.mkv'
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run([ffmpeg, '-nostdin', '-y', '-v', 'error', '-f', 'lavfi', '-i',
                    'testsrc2=size=160x96:rate=5:duration=1', '-vf',
                    r'setpts=if(lt(N\,3)\,N/(5*TB)\,(N+2)/(5*TB))', '-vsync', 'vfr',
                    '-c:v', 'libaom-av1', '-cpu-used', '8', '-threads', '2', str(source)],
                   check=True, capture_output=True, timeout=30)
    def frame_times(path):
        result = subprocess.run([ffmpeg, '-nostdin', '-hide_banner', '-i', str(path),
                                 '-vf', 'showinfo', '-f', 'null', '-'], capture_output=True,
                                text=True, timeout=30, check=True)
        return [float(value) for value in re.findall(r'pts_time:([\d.]+)', result.stderr)]
    before = frame_times(source)
    assert len(set(round(b-a, 2) for a, b in zip(before, before[1:]))) > 1
    with video_decode.compatible_video(source, tmp_path) as compatible:
        assert frame_times(compatible) == before


def test_sequential_supported_decoder_does_not_seek_again(monkeypatch):
    cap = Mock()
    cap.get.side_effect = lambda prop: 4 if prop == cv2.CAP_PROP_POS_FRAMES else 25
    cap.read.return_value = (True, np.zeros((10, 10, 3), dtype=np.uint8))
    video_decode.read_frame(cap, 'supported.mp4', frame_index=4)
    cap.set.assert_not_called()


def test_sequential_native_pixels_match_direct_capture(tmp_path):
    source = tmp_path/'native.mp4'
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-nostdin', '-y', '-v', 'error', '-f', 'lavfi',
                    '-i', 'testsrc2=size=160x96:rate=5:duration=1', '-c:v', 'mpeg4', str(source)],
                   check=True, capture_output=True, timeout=30)
    direct, wrapped = cv2.VideoCapture(str(source)), cv2.VideoCapture(str(source))
    try:
        for index in range(5):
            ok, expected = direct.read()
            ret, actual = video_decode.read_frame(wrapped, source, frame_index=index)
            assert ok and ret and np.array_equal(actual, expected)
        assert video_decode.read_frame(wrapped, source, frame_index=5) == (False, None)
    finally:
        direct.release()
        wrapped.release()
