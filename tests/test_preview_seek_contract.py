"""Standard-library contracts for actual preview methods, without importing Qt/OpenCV.

Methods are compiled from the selected HomeInterface source against fake widgets
and fake decoders. These checks do not prove Qt signal timing, native decoding,
GUI screenshots, process cleanup or physical hardware acceleration.
"""
import ast
import os
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest

SOURCE = Path(os.environ.get('VSE_PREVIEW_SOURCE', Path(__file__).resolve().parents[1] / 'ui/home_interface.py'))


class Capture:
    def __init__(self, path, opened=True, readable=True):
        self.path, self.opened, self.readable = path, opened, readable
        self.position = 0
        self.release_count = 0
        self.calls = []
        self.read_error = None

    def record(self, name):
        if self.release_count and name != 'release':
            raise AssertionError('use after release')
        self.calls.append((name, threading.get_ident()))

    def isOpened(self):
        self.record('isOpened')
        return self.opened

    def read(self):
        self.record('read')
        if self.read_error:
            raise self.read_error
        return self.readable, (self.path, self.position) if self.readable else None

    def set(self, key, position):
        self.record('set')
        self.position = position
        return True

    def get(self, key):
        self.record('get')
        return {1: 64, 2: 320, 3: 180, 4: 25}[key]

    def release(self):
        self.record('release')
        self.release_count += 1
        if self.release_count != 1:
            raise AssertionError('double release')


class Slider:
    def __init__(self, owner):
        self.owner, self.position, self.maximum, self.blocked = owner, 1, None, False

    def setValue(self, value):
        changed = self.position != value
        self.position = value
        if changed and not self.blocked:
            self.owner.slider_changed(value)

    def value(self):
        return self.position

    def setMaximum(self, value):
        self.maximum = value


class BlockSignals:
    def __init__(self, slider):
        self.slider = slider

    def __enter__(self):
        self.before = self.slider.blocked
        self.slider.blocked = True

    def __exit__(self, *args):
        self.slider.blocked = self.before


class Tasks:
    def __init__(self, tasks):
        self.tasks = tasks

    def get_all_tasks(self):
        return self.tasks

    def get_pending_tasks(self):
        return [(index, task) for index, task in enumerate(self.tasks) if task.status == 'pending']

    def get_task(self, index):
        return self.tasks[index] if index < len(self.tasks) else None

    def update_task_status(self, index, status):
        self.tasks[index].status = status


class PreviewSeekContracts(unittest.TestCase):
    def setUp(self):
        self.gui_ident = threading.get_ident()
        self.captures, self.next_captures = [], []
        cv2 = SimpleNamespace(VideoCapture=self.capture, CAP_PROP_POS_FRAMES=0,
                              CAP_PROP_FRAME_COUNT=1, CAP_PROP_FRAME_WIDTH=2,
                              CAP_PROP_FRAME_HEIGHT=3, CAP_PROP_FPS=4)
        module = ast.parse(SOURCE.read_bytes())
        home = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == 'HomeInterface')
        names = {'_require_gui_thread', '_release_preview', '_ensure_preview_capture', 'slider_changed',
                 'load_video', '_on_worker_event', '_active_task_index', 'on_task_deleted', 'processing_finished'}
        methods = [node for node in home.body if isinstance(node, ast.FunctionDef) and node.name in names]
        for method in methods:
            method.decorator_list = []
        subject = ast.ClassDef(name='ActualPreviewMethods', bases=[], keywords=[], body=methods, decorator_list=[])
        selected = ast.fix_missing_locations(ast.Module(body=[subject], type_ignores=[]))
        namespace = {'cv2': cv2, 'QSignalBlocker': BlockSignals,
                     'QThread': SimpleNamespace(currentThread=threading.get_ident),
                     'TaskStatus': SimpleNamespace(PROCESSING='processing', COMPLETED='completed', FAILED='failed')}
        exec(compile(selected, str(SOURCE), 'exec'), namespace)
        self.subject = namespace['ActualPreviewMethods']()
        widget = self.subject
        widget.thread = lambda: self.gui_ident
        widget._closing = False
        widget.video_path = widget.video_cap = None
        widget.frame_count = widget.frame_width = widget.frame_height = widget.fps = None
        widget.frames = []
        widget.update_preview = widget.frames.append
        widget.video_slider = Slider(widget)
        widget.video_display_component = SimpleNamespace(set_dragger_enabled=lambda enabled: None)
        widget._active_job = None
        widget.current_processing_task_index = -1
        widget.task_list_component = Tasks([])
        widget._next_task_timer = SimpleNamespace(start=lambda delay: None)
        widget.run_button = widget.stop_button = SimpleNamespace(setVisible=lambda visible: None)
        widget.append_output = lambda *args: None
        widget.update_progress = lambda *args: None
        widget.running_task = False

    def capture(self, path):
        self.assertEqual(threading.get_ident(), self.gui_ident)
        capture = self.next_captures.pop(0) if self.next_captures else Capture(path)
        self.captures.append(capture)
        return capture

    def tearDown(self):
        self.subject._release_preview()
        self.assertTrue(all(cap.release_count == 1 for cap in self.captures))
        self.assertTrue(all(thread == self.gui_ident for cap in self.captures for _, thread in cap.calls))

    def begin(self, path='first.mp4'):
        widget = self.subject
        self.assertTrue(widget.load_video(path))
        task = SimpleNamespace(status='processing', output_path=None)
        job = SimpleNamespace(task=task, video_path=path, output_path='result.srt', cancelled=threading.Event())
        widget.task_list_component.tasks = [task]
        widget._active_job = job
        widget.running_task = True
        return job

    def test_actual_finish_then_seek_decodes_requested_frame_without_task_reload(self):
        widget = self.subject
        job = self.begin()
        old = widget.video_cap
        widget._on_worker_event(job, 'finished', (0, None))
        self.assertIsNone(widget.video_cap)
        self.assertEqual(old.release_count, 1)
        widget.processing_finished()
        widget.video_slider.setValue(24)
        self.assertIsNotNone(widget.video_cap)
        self.assertIsNot(widget.video_cap, old)
        self.assertEqual(widget.frames[-1], ('first.mp4', 24))
        reopened = widget.video_cap
        widget.video_slider.setValue(30)
        self.assertIs(widget.video_cap, reopened)
        self.assertEqual(len(self.captures), 2)
        self.assertEqual(widget.frames[-1], ('first.mp4', 30))
        self.assertEqual(job.task.status, 'completed')
        self.assertEqual(job.task.output_path, 'result.srt')

    def test_finished_A_does_not_replace_selected_B_and_late_seek_uses_B(self):
        widget = self.subject
        job = self.begin('A.mp4')
        self.assertTrue(widget.load_video('B.mp4'))
        selected = widget.video_cap
        widget._on_worker_event(job, 'finished', (0, None))
        self.assertIs(widget.video_cap, selected)
        widget._release_preview()
        widget.video_slider.setValue(12)
        self.assertEqual(widget.frames[-1], ('B.mp4', 12))
        self.assertEqual(widget.video_cap.path, 'B.mp4')

    def test_deleted_last_task_cannot_reopen_stale_path(self):
        widget = self.subject
        self.begin()
        widget.task_list_component.tasks.clear()
        widget.on_task_deleted(0)
        widget.video_slider.setValue(8)
        self.assertIsNone(widget.video_cap)
        self.assertIsNone(widget.video_path)
        self.assertEqual(len(self.captures), 1)

    def test_failed_initial_decode_invalidates_previous_source(self):
        widget = self.subject
        self.assertTrue(widget.load_video('old.mp4'))
        self.next_captures.append(Capture('broken.mp4', readable=False))
        self.assertFalse(widget.load_video('broken.mp4'))
        widget.video_slider.setValue(8)
        self.assertIsNone(widget.video_cap)
        self.assertIsNone(widget.video_path)
        self.assertEqual(len(self.captures), 2)

    def test_initial_preview_error_does_not_publish_path_or_leak_capture(self):
        widget = self.subject
        def fail(frame):
            raise RuntimeError('mock paint initialization failure')
        widget.update_preview = fail
        with self.assertRaisesRegex(RuntimeError, 'initialization failure'):
            widget.load_video('video.mp4')
        self.assertIsNone(widget.video_cap)
        self.assertIsNone(widget.video_path)
        self.assertEqual(self.captures[0].release_count, 1)

    def test_failed_reopen_is_released_and_later_seek_can_retry(self):
        widget = self.subject
        job = self.begin()
        widget._on_worker_event(job, 'finished', (0, None))
        self.next_captures.append(Capture('first.mp4', opened=False))
        widget.video_slider.setValue(8)
        self.assertIsNone(widget.video_cap)
        self.assertEqual(self.captures[-1].release_count, 1)
        widget.video_slider.setValue(9)
        self.assertEqual(widget.frames[-1], ('first.mp4', 9))
        self.assertEqual(len(self.captures), 3)

    def test_seek_exception_releases_capture_and_preserves_valid_retry_path(self):
        widget = self.subject
        self.assertTrue(widget.load_video('first.mp4'))
        widget.video_cap.read_error = RuntimeError('mock decode failure')
        with self.assertRaisesRegex(RuntimeError, 'decode failure'):
            widget.video_slider.setValue(8)
        self.assertIsNone(widget.video_cap)
        self.assertEqual(widget.video_path, 'first.mp4')
        widget.video_slider.setValue(9)
        self.assertEqual(widget.frames[-1], ('first.mp4', 9))

    def test_closed_capture_is_replaced_once(self):
        widget = self.subject
        self.assertTrue(widget.load_video('first.mp4'))
        old = widget.video_cap
        old.opened = False
        widget.video_slider.setValue(8)
        self.assertEqual(old.release_count, 1)
        self.assertIsNot(widget.video_cap, old)
        self.assertEqual(widget.frames[-1], ('first.mp4', 8))

    def test_closing_and_missing_video_do_not_open_capture(self):
        widget = self.subject
        widget.slider_changed(8)
        self.assertEqual(self.captures, [])
        self.assertTrue(widget.load_video('first.mp4'))
        widget._closing = True
        widget._release_preview()
        widget.video_slider.setValue(9)
        self.assertIsNone(widget.video_cap)
        self.assertEqual(len(self.captures), 1)

    def test_worker_thread_cannot_construct_seek_or_release_capture(self):
        errors = []
        def worker():
            try:
                self.subject.slider_changed(8)
            except RuntimeError as error:
                errors.append(str(error))
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIn('GUI thread', errors[0])
        self.assertEqual(self.captures, [])


if __name__ == '__main__':
    unittest.main()
