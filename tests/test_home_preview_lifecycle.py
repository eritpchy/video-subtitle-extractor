"""Real Qt event-loop ownership tests; not a substitute for final Mac GUI QA."""
import os
import multiprocessing
import subprocess
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'cocoa' if sys.platform == 'darwin' else 'offscreen')
import cv2
import numpy as np
from PySide6 import QtCore, QtTest
from backend.config import config
from backend.tools.subtitle_extractor_remote_call import SubtitleExtractorRemoteCall
from test_theme import ThemeTestCase
from ui.home_interface import HomeInterface
from ui.component.task_list_component import TaskStatus


def _large_callback_producer(queue):
    """Spawn-safe producer: exceed the pipe capacity after a delivery error."""
    SubtitleExtractorRemoteCall.remote_call_catch_error(queue, RuntimeError('delivery input'))
    for _ in range(16):
        SubtitleExtractorRemoteCall.remote_call_append_log(queue, 'x' * 65536)
    SubtitleExtractorRemoteCall.remote_call_finish(queue)


class RecordingCapture:
    def __init__(self, owner, path, native=None):
        self.owner, self.path, self.native = owner, path, native
        self.released = False
        self.busy = False
        self.seek_hook = None
        self.calls = []
        self.release_count = 0

    def call(self, name, *args):
        self.calls.append((name, threading.get_ident(), QtCore.QThread.currentThread()))
        if threading.get_ident() != self.owner.gui_ident:
            self.owner.violations.append(('non-GUI capture', name))
        if self.released and name != 'release':
            self.owner.violations.append(('use after release', name))
        if name == 'release':
            self.release_count += 1
            if self.busy:
                self.owner.violations.append(('release overlaps seek', name))
            self.released = True
        if name == 'set':
            self.busy = True
            try:
                if self.seek_hook:
                    self.seek_hook()
                return self.native.set(*args) if self.native else True
            finally:
                self.busy = False
        if self.native is not None:
            return getattr(self.native, name)(*args)
        if name == 'isOpened':
            return not self.released
        if name == 'read':
            return True, np.zeros((180, 320, 3), dtype=np.uint8)
        if name == 'get':
            return {cv2.CAP_PROP_FRAME_COUNT: 64, cv2.CAP_PROP_FRAME_WIDTH: 320,
                    cv2.CAP_PROP_FRAME_HEIGHT: 180, cv2.CAP_PROP_FPS: 25}[args[0]]

    def isOpened(self): return self.call('isOpened')
    def read(self): return self.call('read')
    def get(self, key): return self.call('get', key)
    def set(self, key, value): return self.call('set', key, value)
    def release(self): return self.call('release')


class ControlledWorker:
    """Actual Python waiter threads, with explicit completion barriers."""
    def __init__(self, owner):
        self.owner = owner
        self.jobs = []
        self.gates = {}
        self.done = []

    def __call__(self, job):
        if threading.get_ident() == self.owner.gui_ident:
            self.owner.violations.append(('extraction blocks GUI', 'worker'))
        gate = threading.Event()
        self.gates[id(job)] = gate
        self.jobs.append(job)
        try:
            if not gate.wait(10):
                raise RuntimeError('test worker barrier timed out')
            return SimpleNamespace(exitcode=0)
        finally:
            self.done.append(job)

    def finish(self, job):
        self.gates[id(job)].set()

    def finish_all(self):
        for gate in list(self.gates.values()):
            gate.set()


class HomePreviewLifecycleTests(ThemeTestCase):
    def setUp(self):
        super().setUp()
        self.gui_ident = threading.get_ident()
        self.violations, self.captures, self.workers, self.patches = [], [], [], []
        config.subtitleSelectionAreas.value = '0.6,0.99,0.05,0.95'
        config.saveDirectory.value = self.temp.name
        self.native_capture = cv2.VideoCapture
        self.capture_patch = patch('ui.home_interface.cv2.VideoCapture', side_effect=self.capture)
        self.capture_patch.start()

    def capture(self, path):
        cap = RecordingCapture(self, path)
        self.captures.append(cap)
        return cap

    def tearDown(self):
        for widget in self.widgets:
            if isinstance(widget, HomeInterface):
                widget.shutdown()
        for worker in self.workers:
            worker.finish_all()
        self.pump(lambda: all(len(w.done) == len(w.jobs) for w in self.workers))
        for item in reversed(self.patches):
            item.stop()
        self.capture_patch.stop()
        from helpers.theme_wait import wait_for_widget_animations
        wait_for_widget_animations(self.widgets)
        super().tearDown()
        self.assertEqual(self.violations, [])
        self.assertTrue(all(cap.release_count == 1 for cap in self.captures))

    def pump(self, condition, timeout=3):
        end = time.monotonic() + timeout
        while not condition() and time.monotonic() < end:
            self.app.processEvents()
            QtTest.QTest.qWait(5)
        self.assertTrue(condition(), 'event-loop condition timed out')

    def widget(self, paths=('first.mp4',)):
        widget = HomeInterface()
        self.widgets.append(widget)
        widget.resize(1200, 800)
        widget.show()
        self.app.processEvents()
        for path in paths:
            widget.task_list_component.add_task(str(Path(self.temp.name) / path))
        for target, name in [(widget, 'update_preview'), (widget, 'append_output'),
                             (widget.task_list_component, 'update_task_status'),
                             (widget.task_list_component, 'update_task_progress'),
                             (widget.task_list_component, 'set_current_task')]:
            original = getattr(target, name)
            def observed(*args, _original=original, _name=name, **kwargs):
                if threading.get_ident() != self.gui_ident:
                    self.violations.append(('non-GUI widget', _name))
                return _original(*args, **kwargs)
            item = patch.object(target, name, observed)
            item.start(); self.patches.append(item)
        worker = ControlledWorker(self); self.workers.append(worker)
        item = patch.object(widget, 'run_subtitle_extractor_process', worker)
        item.start(); self.patches.append(item)
        return widget, worker

    def start(self, widget, worker, number=1):
        widget.run_button_clicked()
        self.pump(lambda: len(worker.jobs) == number)
        return worker.jobs[-1]

    def finish(self, widget, worker, job):
        worker.finish(job)
        self.pump(lambda: widget._active_job is not job)
        widget._next_task_timer.stop()

    def test_completion_queued_during_seek_cannot_release_capture_concurrently(self):
        widget, worker = self.widget()
        job = self.start(widget, worker)
        capture = widget.video_cap
        posted = threading.Event()
        original_emit = widget._emit_worker_event
        def observe_emit(job, kind, payload):
            original_emit(job, kind, payload)
            if kind == 'finished': posted.set()
        with patch.object(widget, '_emit_worker_event', observe_emit):
            def during_seek():
                worker.finish(job)
                self.assertTrue(posted.wait(3))
                self.assertFalse(capture.released)
            capture.seek_hook = during_seek
            # Real QSlider signal -> Python slot -> capture.set/read. The
            # worker finishes while this GUI operation is still in progress.
            widget.video_slider.setValue(20)
            capture.seek_hook = None
            self.assertEqual(capture.release_count, 0)
            self.pump(lambda: widget._active_job is None)
        widget._next_task_timer.stop()
        self.assertEqual(capture.release_count, 1)
        self.assertEqual(job.task.status, TaskStatus.COMPLETED)
        self.assertEqual(job.task.progress, 100)
        self.assertTrue(all(thread == self.app.thread() for cap in self.captures for _, _, thread in cap.calls))

    def test_terminal_progress_is_not_task_completion_and_duplicate_run_is_ignored(self):
        widget, worker = self.widget()
        job = self.start(widget, worker)
        widget.run_button_clicked()
        widget._emit_worker_event(job, 'progress', (100, 100, 100, True, 100))
        self.app.processEvents()
        self.assertIs(widget._active_job, job)
        self.assertTrue(widget.running_task)
        self.assertEqual(job.task.status, TaskStatus.PROCESSING)
        self.assertEqual(widget.current_processing_task_index, 0)
        self.assertEqual(len(worker.jobs), 1)
        self.finish(widget, worker, job)
        widget._start_next_task()
        self.assertFalse(widget.running_task)
        self.assertEqual(job.task.status, TaskStatus.COMPLETED)

    def test_stop_restart_discards_old_progress_error_and_completion(self):
        widget, worker = self.widget()
        old = self.start(widget, worker)
        widget.stop_button_clicked()
        new = self.start(widget, worker, 2)
        current_capture = widget.video_cap
        widget._emit_worker_event(old, 'progress', (100, 100, 100, True, 100))
        widget._emit_worker_event(old, 'error', RuntimeError('stale error'))
        worker.finish(old)
        self.pump(lambda: old in worker.done)
        self.app.processEvents()
        self.assertIs(widget._active_job, new)
        self.assertIs(widget.video_cap, current_capture)
        self.assertEqual(new.task.status, TaskStatus.PROCESSING)
        self.assertNotIn('stale error', widget.output_text.toPlainText())
        self.finish(widget, worker, new)

    def test_error_before_zero_exit_cannot_be_overwritten_by_completion(self):
        widget, worker = self.widget()
        job = self.start(widget, worker)
        widget._emit_worker_event(job, 'error', RuntimeError('actual child failure'))
        self.finish(widget, worker, job)
        self.assertEqual(job.task.status, TaskStatus.FAILED)
        self.assertIn('actual child failure', widget.output_text.toPlainText())

    def test_two_task_queue_and_late_first_progress(self):
        widget, worker = self.widget(('a.mp4', 'b.mp4'))
        first = self.start(widget, worker)
        self.finish(widget, worker, first)
        widget._start_next_task()
        self.pump(lambda: len(worker.jobs) == 2)
        second = worker.jobs[-1]
        widget._emit_worker_event(first, 'progress', (100, 100, 100, True, 100))
        self.app.processEvents()
        self.assertEqual(second.task.progress, 0)
        self.assertIs(widget._active_job, second)
        self.finish(widget, worker, second)
        widget._start_next_task()
        self.assertFalse(widget.running_task)
        self.assertTrue(all(t.status == TaskStatus.COMPLETED for t in widget.task_list_component.tasks))

    def test_deleting_preceding_row_keeps_job_progress_floor(self):
        widget, worker = self.widget(('done.mp4', 'active.mp4'))
        widget.task_list_component.update_task_status(0, TaskStatus.COMPLETED)
        job = self.start(widget, worker)
        widget._emit_worker_event(job, 'progress', (80, 80, 80, False, 80))
        self.app.processEvents()
        self.assertEqual(job.task.progress, 80)
        widget.task_list_component.delete_task(0)
        widget._emit_worker_event(job, 'progress', (10, 10, 10, False, 10))
        self.app.processEvents()
        self.assertEqual(job.task.progress, 80)
        self.assertEqual(widget.current_processing_task_index, 0)
        self.finish(widget, worker, job)
        self.assertEqual(job.task.status, TaskStatus.COMPLETED)

    def test_deleted_active_task_releases_preview_and_ignores_its_completion(self):
        widget, worker = self.widget()
        job = self.start(widget, worker)
        capture = widget.video_cap
        widget.task_list_component.delete_task(0)
        self.assertIsNone(widget.video_cap)
        self.assertEqual(capture.release_count, 1)
        widget._emit_worker_event(job, 'progress', (100, 100, 100, True, 100))
        self.finish(widget, worker, job)
        widget._start_next_task()
        self.assertFalse(widget.running_task)
        self.assertEqual(widget.task_list_component.get_all_tasks(), [])
        self.assertEqual(capture.release_count, 1)

    def test_reimport_same_path_does_not_adopt_old_task_or_release_new_preview(self):
        widget, worker = self.widget()
        old = self.start(widget, worker)
        widget.task_list_component.add_task(old.video_path)
        widget.task_list_component.select_task(0)
        replacement = widget.task_list_component.get_task(0)
        capture = widget.video_cap
        self.assertIsNot(replacement, old.task)
        self.finish(widget, worker, old)
        self.assertEqual(replacement.status, TaskStatus.PENDING)
        self.assertEqual(replacement.progress, 0)
        self.assertIs(widget.video_cap, capture)
        self.assertFalse(capture.released)

    def test_close_invalidates_already_queued_callbacks_and_releases_once(self):
        widget, worker = self.widget()
        job = self.start(widget, worker)
        widget._emit_worker_event(job, 'progress', (100, 100, 100, True, 100))
        widget.close()
        worker.finish(job)
        self.pump(lambda: job in worker.done)
        self.app.processEvents()
        widget.shutdown()
        self.assertTrue(widget._closing)
        self.assertFalse(widget.running_task)
        self.assertIsNone(widget.video_cap)
        self.assertFalse(widget._next_task_timer.isActive())

    def test_shutdown_terminates_actual_owned_child_before_widget_deletion(self):
        widget, worker = self.widget()
        job = self.start(widget, worker)
        child = subprocess.Popen([sys.executable, '-c',
                                  'import threading; threading.Event().wait(30)'])
        job.process = child
        try:
            self.assertIsNone(child.poll())
            widget.close()
            self.assertIsNotNone(child.poll())
            self.assertTrue(job.cancelled.is_set())
            self.assertIsNone(widget._active_job)
            self.assertIsNone(widget.video_cap)
            worker.finish(job)
            self.pump(lambda: job in worker.done)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)

    def test_actual_parent_window_close_shuts_down_home_preview(self):
        from gui import SubtitleExtractorGUI
        window = SubtitleExtractorGUI()
        self.widgets.append(window)
        self.assertTrue(window.homeInterface.load_video(str(Path(self.temp.name) / 'preview.mp4')))
        capture = window.homeInterface.video_cap
        window.close()
        self.assertTrue(window.homeInterface._closing)
        self.assertIsNone(window.homeInterface.video_cap)
        self.assertEqual(capture.release_count, 1)

    def test_failed_initial_decode_releases_capture_without_seeking(self):
        widget, _ = self.widget(())
        cap = RecordingCapture(self, 'broken.mp4')
        self.captures.append(cap)
        with patch('ui.home_interface.cv2.VideoCapture', return_value=cap), \
             patch.object(cap, 'read', return_value=(False, None)):
            self.assertFalse(widget.load_video('broken.mp4'))
        self.assertIsNone(widget.video_cap)
        self.assertEqual(cap.release_count, 1)
        self.assertNotIn('set', [row[0] for row in cap.calls])

    def run_process_pipeline(self, *, child_error=False, missing_finish=False, delivery_error=False):
        widget, _ = self.widget()
        observed = []
        class Process:
            exitcode = 0
            pid = 424242
            def __init__(process, *, target, args):
                process.queue = args[0]
                observed.append(('input_snapshot', args[1:]))
            def start(process):
                observed.append(('start', threading.get_ident()))
                SubtitleExtractorRemoteCall.remote_call_append_log(process.queue, ('last child log',))
                SubtitleExtractorRemoteCall.remote_call_update_progress(process.queue, 100, 100, 100, True, 100)
                if child_error:
                    SubtitleExtractorRemoteCall.remote_call_catch_error(process.queue, RuntimeError('last child error'))
                if not missing_finish:
                    SubtitleExtractorRemoteCall.remote_call_finish(process.queue)
            def join(process):
                observed.append(('join', threading.get_ident()))
        manager = SimpleNamespace(add_process=lambda process: None)
        emit = widget._emit_worker_event
        def delivered(job, kind, payload):
            if delivery_error and kind == 'error':
                raise RuntimeError('callback delivery broke')
            emit(job, kind, payload)
        patches = [patch('ui.home_interface.multiprocessing.Process', Process),
                   patch('ui.home_interface.ProcessManager.instance', return_value=manager),
                   patch.object(widget, 'run_subtitle_extractor_process',
                                HomeInterface.run_subtitle_extractor_process.__get__(widget)),
                   patch.object(widget, '_emit_worker_event', delivered)]
        if missing_finish:
            patches.append(patch.object(SubtitleExtractorRemoteCall, 'wait', return_value=False))
        for item in patches: item.start()
        try:
            widget.run_button_clicked()
            task = widget.task_list_component.get_task(0)
            self.pump(lambda: widget._active_job is None)
            widget._next_task_timer.stop()
            self.assertTrue(all(thread != self.gui_ident for name, thread in observed if name in ('start', 'join')))
            self.assertEqual(len([row for row in observed if row[0] == 'start']), 1)
            return task.status, widget.output_text.toPlainText()
        finally:
            for item in reversed(patches): item.stop()

    def test_process_pipeline_drains_successful_terminal_messages(self):
        status, log = self.run_process_pipeline()
        self.assertEqual(status, TaskStatus.COMPLETED)
        self.assertIn('last child log', log)

    def test_process_pipeline_last_error_survives_zero_exit(self):
        status, log = self.run_process_pipeline(child_error=True)
        self.assertEqual(status, TaskStatus.FAILED)
        self.assertIn('last child error', log)

    def test_process_pipeline_missing_finish_is_failure_not_success(self):
        status, log = self.run_process_pipeline(missing_finish=True)
        self.assertEqual(status, TaskStatus.FAILED)
        self.assertIn('callback stream did not finish', log)

    def test_process_pipeline_failed_error_delivery_is_failure_not_success(self):
        status, log = self.run_process_pipeline(child_error=True, delivery_error=True)
        self.assertEqual(status, TaskStatus.FAILED)
        self.assertIn('callback stream did not finish', log)

    def test_real_opencv_video_repeated_seek_complete_and_reload(self):
        video = str(Path(self.temp.name) / 'real preview.avi')
        writer = cv2.VideoWriter(video, cv2.VideoWriter_fourcc(*'MJPG'), 10, (64, 48))
        self.assertTrue(writer.isOpened(), 'required OpenCV MJPG test writer unavailable')
        for i in range(24):
            writer.write(np.full((48, 64, 3), i * 8, dtype=np.uint8))
        writer.release()
        def real_capture(path):
            cap = RecordingCapture(self, path, self.native_capture(path))
            self.captures.append(cap)
            return cap
        with patch('ui.home_interface.cv2.VideoCapture', side_effect=real_capture):
            widget, worker = self.widget((video,))
            for _ in range(3):
                widget.task_list_component.update_task_status(0, TaskStatus.PENDING)
                job = self.start(widget, worker, len(worker.jobs) + 1)
                for frame in (2, 15, 4, 22, 1):
                    widget.video_slider.setValue(frame)
                widget._emit_worker_event(job, 'progress', (99, 100, 99, False, 0))
                self.finish(widget, worker, job)
                widget._start_next_task()
                self.assertEqual(job.task.status, TaskStatus.COMPLETED)
                self.assertIsNone(widget.video_cap)
            self.assertTrue(any(name == 'set' for cap in self.captures for name, _, _ in cap.calls))


class RemoteCallbackDrainTests(unittest.TestCase):
    def test_finish_drains_last_error_and_progress_before_wait_returns(self):
        caller = SubtitleExtractorRemoteCall(); seen = []
        caller.register_error_callback(lambda error: seen.append(('error', str(error))))
        caller.register_update_progress_callback(lambda *args: seen.append(('progress', args)))
        try:
            caller.remote_call_catch_error(caller.queue, RuntimeError('last error'))
            caller.remote_call_update_progress(caller.queue, 100, 100, 100, True, 100)
            caller.remote_call_finish(caller.queue)
            self.assertTrue(caller.wait(3))
            self.assertEqual([name for name, _ in seen], ['error', 'progress'])
        finally:
            caller.stop()
        self.assertFalse(caller.thread.is_alive())

    def test_callback_failure_still_drains_pipe_but_never_claims_success(self):
        caller = SubtitleExtractorRemoteCall()
        def fail(error): raise RuntimeError('delivery failed')
        caller.register_error_callback(fail)
        logs = []
        caller.register_log_callback(logs.append)
        try:
            caller.remote_call_catch_error(caller.queue, RuntimeError('original error'))
            for _ in range(8):
                caller.remote_call_append_log(caller.queue, 'x' * 16384)
            caller.remote_call_finish(caller.queue)
            self.assertFalse(caller.wait(3))
            self.assertIsInstance(caller.error, RuntimeError)
            self.assertTrue(caller.finished)
            self.assertEqual(len(logs), 8)
        finally:
            caller.stop()

    def test_callback_error_does_not_deadlock_actual_child_queue_feeder(self):
        context = multiprocessing.get_context('spawn')
        with patch('backend.tools.subtitle_extractor_remote_call.multiprocessing.Queue', context.Queue):
            caller = SubtitleExtractorRemoteCall()
        def fail(error):
            raise RuntimeError('original delivery failure')
        caller.register_error_callback(fail)
        received = []
        caller.register_log_callback(lambda value: received.append(len(value)))
        child = context.Process(target=_large_callback_producer, args=(caller.queue,))
        try:
            child.start()
            child.join(timeout=15)
            self.assertFalse(child.is_alive(), 'reader abandoned pipe: producer cannot exit')
            self.assertEqual(child.exitcode, 0)
            self.assertFalse(caller.wait(3))
            self.assertTrue(caller.finished)
            self.assertEqual(str(caller.error), 'original delivery failure')
            self.assertEqual(received, [65536] * 16)
        finally:
            if child.is_alive():
                child.terminate()
                child.join(timeout=5)
            caller.stop()

    def test_idle_reader_stop_wakes_and_joins_without_claiming_finish(self):
        caller = SubtitleExtractorRemoteCall()
        self.assertFalse(caller.wait(0.01))
        caller.stop()
        self.assertFalse(caller.thread.is_alive())
        self.assertFalse(caller.wait(0))
