"""Timeline path, process lifecycle and real audio-to-subtitle regressions."""
import os
from pathlib import Path
import sys
import subprocess
import threading
import time
import wave

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "cocoa" if sys.platform == "darwin" else "offscreen")
from PySide6 import QtCore, QtTest, QtWidgets

from backend.tools.python_runner import AsyncPythonRunner
from backend.tools.process_manager import ProcessManager
from ui.timeline_sync_interface import TimelineSyncInterface


def test_runner_completion_drains_logs_and_can_restart_without_signalling_exited_pid(monkeypatch):
    runner = AsyncPythonRunner()
    logs, exits, signals = [], [], []
    runner.register_callback(stdout=logs.append, complete=exits.append)
    monkeypatch.setattr(ProcessManager.instance(), "terminate_by_pid", signals.append)
    assert not runner.is_running()
    for value in ("first", "second"):
        runner.start("-c", args=[f"print({value!r})"])
        assert runner.wait(10)
        assert runner.returncode == 0
        assert not runner.is_running()
    runner.stop()
    assert logs == ["first\n", "second\n"]
    assert exits == [0, 0]
    assert not signals


def test_runner_immediate_stop_restart_suppresses_old_completion(monkeypatch):
    runner = AsyncPythonRunner()
    ready = threading.Event()
    exits = []
    runner.register_callback(stdout=lambda line: ready.set(), complete=exits.append)
    runner.start("-c", args=["import threading; print('ready', flush=True); threading.Event().wait(30)"])
    old_process = runner.process
    old_monitor = runner._monitor_thread
    assert ready.wait(10)
    # Use the actual owned child handle; don't rely on a platform kill utility.
    monkeypatch.setattr(ProcessManager.instance(), "terminate_by_pid", lambda pid: old_process.terminate())
    runner.stop()
    runner.start("-c", args=["pass"])
    assert runner.wait(10)
    old_monitor.join(10)
    assert not old_monitor.is_alive()
    assert old_process.poll() is not None
    assert exits == [0]


def test_queued_old_qt_completion_does_not_change_restarted_job():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    widget = TimelineSyncInterface(None)
    try:
        widget.show()
        widget.python_runner.start("-c", args=["pass"])
        assert widget.python_runner.wait(10)  # Completion is queued, not delivered.
        widget.python_runner.start("-c", args=["import threading; threading.Event().wait(30)"])
        widget.run_button.setVisible(False)
        widget.stop_button.setVisible(True)
        app.processEvents()
        assert widget.python_runner.is_running()
        assert widget.stop_button.isVisible()
        assert not widget.run_button.isVisible()
        assert "Synced subtitle:" not in widget.output_text.toPlainText()
    finally:
        widget.python_runner.stop()
        widget.python_runner.wait(10)
        widget.close()
        widget.deleteLater()
        app.processEvents()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


@pytest.mark.parametrize("live_child", [False, True])
def test_process_manager_interpreter_shutdown_does_not_create_threads(live_child):
    root = Path(__file__).resolve().parents[1]
    source = "from backend.tools.process_manager import ProcessManager; manager=ProcessManager.instance();"
    if live_child:
        source += "import subprocess,sys; manager.add_process(subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']));"
    result = subprocess.run([sys.executable, "-c", source], cwd=root, capture_output=True,
                            text=True, timeout=15)
    assert result.returncode == 0
    assert "Traceback" not in result.stderr
    assert "can't register atexit" not in result.stderr


def test_process_manager_normal_cleanup_retains_live_child_termination():
    manager = ProcessManager()
    process = subprocess.Popen([sys.executable, "-c", "import threading; threading.Event().wait(30)"])
    try:
        manager.add_process(process)
        manager.terminate_all()
        assert process.poll() is not None
        assert not manager.processes
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


@pytest.mark.parametrize("source_name", ["destination.srt", "destination.synced.srt"])
def test_timeline_never_overwrites_source_or_existing_synced_output(tmp_path, monkeypatch, source_name):
    from ui import timeline_sync_interface as timeline
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    widget = TimelineSyncInterface(None)
    errors, starts = [], []
    source = tmp_path / source_name
    source.write_bytes(b"original subtitle bytes")
    existing = tmp_path / "destination.synced.srt"
    if existing != source:
        existing.write_bytes(b"existing synced subtitle bytes")
    before = {path: path.read_bytes() for path in (source, existing)}
    for name in ("source.wav", "destination.wav"):
        (tmp_path / name).write_bytes(b"video bytes")
    widget.option.source_video.value = str(tmp_path / "source.wav")
    widget.option.destination_video.value = str(tmp_path / "destination.wav")
    widget.option.source_subtitle.value = str(source)
    monkeypatch.setattr(timeline.InfoBar, "error", lambda **kw: errors.append(kw))
    monkeypatch.setattr(widget.python_runner, "start", lambda *a, **kw: starts.append((a, kw)))
    try:
        widget.run_button_clicked()
        assert not starts
        assert errors and "overwrite" in errors[0]["content"]
        assert all(path.read_bytes() == data for path, data in before.items())
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


def test_timeline_sync_from_unrelated_unicode_cwd_real_audio_e2e(tmp_path, monkeypatch):
    """Run the real GUI launch path and Sushi, and verify a known +2s shift."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    launch = tmp_path / "unrelated launch 字幕"
    launch.mkdir()
    monkeypatch.chdir(launch)
    inputs = tmp_path / "音频 with spaces"
    inputs.mkdir()
    sample_rate = 12000
    source = (np.random.default_rng(42).normal(0, 0.15, sample_rate * 15) * 30000).astype("<i2")
    shifted = np.concatenate([np.zeros(sample_rate * 2, dtype="<i2"), source])
    for name, samples in (("source.wav", source), ("destination.wav", shifted)):
        with wave.open(str(inputs / name), "wb") as stream:
            stream.setparams((1, 2, sample_rate, 0, "NONE", "not compressed"))
            stream.writeframes(samples.tobytes())
    # Previously this source name was also selected as the output and got
    # overwritten. The distinct .synced output must preserve the input bytes.
    subtitles = inputs / "destination.srt"
    original = "1\n00:00:03,000 --> 00:00:04,000\nHELLO WORLD\n\n2\n00:00:06,000 --> 00:00:07,000\nSECOND LINE\n\n"
    subtitles.write_text(original, encoding="utf-8")
    completion_threads = []

    class ObservedTimeline(TimelineSyncInterface):
        def on_complete(self, return_code, generation):
            completion_threads.append(QtCore.QThread.currentThread())
            super().on_complete(return_code, generation)

    widget = ObservedTimeline(None)
    try:
        widget.option.source_video.value = str(inputs / "source.wav")
        widget.option.destination_video.value = str(inputs / "destination.wav")
        widget.option.source_subtitle.value = str(subtitles)
        widget.show()
        widget.run_button_clicked()
        deadline = time.monotonic() + 30
        while not completion_threads and time.monotonic() < deadline:
            app.processEvents()
            QtTest.QTest.qWait(10)
        assert completion_threads, widget.output_text.toPlainText()
        assert completion_threads == [app.thread()]
        assert widget.python_runner.returncode == 0, widget.output_text.toPlainText()
        output = Path(widget.output_path).read_text(encoding="utf-8-sig")
        assert "00:00:05,000 --> 00:00:06,000" in output
        assert "00:00:08,000 --> 00:00:09,000" in output
        assert "HELLO WORLD" in output and "SECOND LINE" in output
        assert subtitles.read_text(encoding="utf-8") == original
        assert "Synced subtitle:" in widget.output_text.toPlainText()
    finally:
        widget.python_runner.stop()
        widget.close()
        widget.deleteLater()
        app.processEvents()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


def test_process_manager_does_not_signal_reaped_child(monkeypatch):
    class Child:
        pid = 12345
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            pass

        def wait(self, timeout):
            self.returncode = 0
            return 0

        def kill(self):
            pytest.fail("must not kill a reaped child")

    manager = ProcessManager()
    signals = []
    monkeypatch.setattr(manager, "terminate_by_pid", signals.append)
    manager.terminate_by_process(Child())
    assert not signals
