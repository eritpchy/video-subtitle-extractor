"""Focused backend lifecycle checks; no model download or OCR inference."""
import queue
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.main import SubtitleExtractor
from backend.tools.process_manager import ProcessManager


def bare_extractor():
    extractor = SubtitleExtractor.__new__(SubtitleExtractor)
    extractor.lock = threading.RLock()
    extractor.subtitle_ocr_progress_queue = queue.Queue()
    extractor.video_cap = Mock()
    return extractor


def test_failed_extraction_releases_lock_and_capture():
    extractor = bare_extractor()
    extractor._run_impl = Mock(side_effect=RuntimeError("decoder failed"))
    with pytest.raises(RuntimeError, match="decoder failed"):
        extractor.run()
    assert not extractor.isFinished and not extractor.vsf_running
    extractor.video_cap.release.assert_called_once()
    assert extractor._ocr_progress_stop.is_set()
    acquired = []
    def acquire_on_another_thread():
        with extractor.lock:
            acquired.append(True)
    thread = threading.Thread(target=acquire_on_another_thread, daemon=True)
    thread.start()
    thread.join(timeout=2)
    assert acquired == [True]


def test_failed_extraction_terminates_live_ocr_worker(monkeypatch):
    extractor = bare_extractor()
    child = Mock()
    child.is_alive.side_effect = [True, False]
    manager = Mock()
    monkeypatch.setattr(ProcessManager, "instance", lambda: manager)
    def fail():
        extractor._ocr_process = child
        extractor._ocr_process_id = "test-child"
        raise RuntimeError("search failed")
    extractor._run_impl = fail
    with pytest.raises(RuntimeError, match="search failed"):
        extractor.run()
    child.terminate.assert_called_once()
    child.join.assert_called_once_with(timeout=5)
    manager.remove_process.assert_called_once_with("test-child")


def test_timestamp_frame_conversion_matches_srt_mapping():
    extractor = bare_extractor()
    extractor.fps = 25
    assert extractor._timestamp_to_frameno(4000) == 100
    assert extractor._frameno_to_milliseconds(100) == 4000
    extractor.manage_process(123)  # GUI override hook must be a bound method.


def test_invalid_video_fails_without_constructing_detector(tmp_path, monkeypatch):
    import backend.main as main
    detector = Mock(side_effect=AssertionError("detector must be lazy"))
    monkeypatch.setattr(main, "SubtitleDetect", detector)
    with pytest.raises(ValueError, match="Cannot open video"):
        SubtitleExtractor(tmp_path / "missing.mp4", work_dir=tmp_path / "work")
    detector.assert_not_called()


def test_failed_worker_with_pending_tasks_exits_python(tmp_path):
    script = Path(__file__).with_name("helpers") / "failed_worker_cleanup.py"
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", VSE_CONFIG_FILE=str(tmp_path / "config.json"))
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                            env=env, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAILED_WORKER_CLEANUP_OK" in result.stdout
