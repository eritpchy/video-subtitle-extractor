import os
import cv2
import threading
import multiprocessing
import traceback
from copy import deepcopy
from dataclasses import dataclass, field
from PySide6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout
from PySide6.QtCore import Slot, QRect, Signal, Qt, QThread, QTimer, QSignalBlocker
from PySide6.QtGui import QTextCharFormat
from PySide6 import QtWidgets
from qfluentwidgets import (PushButton, CardWidget, PlainTextEdit, FluentIcon)
from ui.setting_interface import SettingInterface
from ui.component.video_display_component import VideoDisplayComponent
from ui.component.task_list_component import TaskListComponent, TaskStatus, TaskOptions
from ui.component.log_highlighter import LogHighlighter
from ui.icon.my_fluent_icon import MyFluentIcon
from backend.config import config, tr
from backend.tools.subtitle_extractor_remote_call import SubtitleExtractorRemoteCall
from backend.tools.process_manager import ProcessManager

@dataclass(eq=False)
class _ExtractionJob:
    """One immutable input snapshot and its independently cancellable process."""
    task: object
    video_path: str
    output_path: str
    options: dict
    cancelled: threading.Event = field(default_factory=threading.Event)
    lock: object = field(default_factory=threading.Lock)
    process: object = None
    subprocess_pids: list = field(default_factory=list)
    last_progress: int = 0


class HomeInterface(QWidget):
    worker_event = Signal(object, str, object)
    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setObjectName("HomeInterface")
        # 初始化一些变量
        self.video_path = None
        self.video_cap = None
        self.fps = None
        self.frame_count = None
        self.frame_width = None
        self.frame_height = None
        self.se = None  # 后台字幕提取器

        # 添加自动滚动控制标志
        self.auto_scroll = True
        self.running_task = False
        self._active_job = None
        self._closing = False
        self._worker_signal_lock = threading.Lock()
        self._next_task_timer = QTimer(self)
        self._next_task_timer.setSingleShot(True)
        self._next_task_timer.timeout.connect(self._start_next_task)
        
        # 当前正在处理的任务索引
        self.current_processing_task_index = -1

        self.__init_widgets()
        self.worker_event.connect(self._on_worker_event, Qt.ConnectionType.QueuedConnection)
        print(tr['Main']['AcceleratorWarning'])

    def __init_widgets(self):
        """创建主页面"""
        main_layout = QHBoxLayout(self)
        main_layout.setSpacing(8)
        main_layout.setContentsMargins(16, 16, 16, 16)

        # 左侧视频区域
        left_layout = QVBoxLayout()
        left_layout.setSpacing(8)
        
        # 创建视频显示组件
        self.video_display_component = VideoDisplayComponent(self)
        self.video_display_component.ab_sections_changed.connect(self.ab_sections_changed)
        self.video_display_component.selections_changed.connect(self.selections_changed)
        left_layout.addWidget(self.video_display_component)
        
        # 获取视频显示和滑块的引用
        self.video_display = self.video_display_component.video_display
        self.video_slider = self.video_display_component.video_slider
        self.video_slider.valueChanged.connect(self.slider_changed)
        
        # 输出文本区域
        self.output_text = PlainTextEdit()
        self.output_text.setMinimumHeight(150)
        self.output_text.setReadOnly(True)
        self.log_highlighter = LogHighlighter(self.output_text.document())
        self.output_text.document().setDocumentMargin(10)
        # 连接滚动条值变化信号
        self.output_text.verticalScrollBar().valueChanged.connect(self.on_scroll_change)
        
        output_container = CardWidget(self)
        output_layout = QVBoxLayout()
        output_layout.setContentsMargins(0, 0, 0, 0)
        output_layout.addWidget(self.output_text)
        output_container.setLayout(output_layout)
        left_layout.addWidget(output_container)

        main_layout.addLayout(left_layout, 2)

        # 右侧设置区域
        right_layout = QVBoxLayout()
        right_layout.setSpacing(10)

        # 设置容器
        settings_container = CardWidget(self)
        settings_container.setLayout(SettingInterface(settings_container))
        right_layout.addWidget(settings_container)
        
        # 添加任务列表容器
        task_list_container = CardWidget(self)
        task_list_layout = QHBoxLayout()
        task_list_layout.setContentsMargins(0, 0, 0, 0)
        task_list_layout.setSpacing(0)
        self.task_list_component = TaskListComponent(self)
        self.task_list_component.task_selected.connect(self.on_task_selected)
        self.task_list_component.task_deleted.connect(self.on_task_deleted)
        task_list_layout.addWidget(self.task_list_component)
        task_list_container.setLayout(task_list_layout)
        right_layout.addWidget(task_list_container, 1)  # 占满剩余空间
        
        # 操作按钮容器
        button_container = CardWidget(self)
        button_layout = QHBoxLayout()
        button_layout.setContentsMargins(16, 16, 16, 16)
        button_layout.setSpacing(8)
        
        self.file_button = PushButton(tr['SubtitleExtractorGUI']['Open'], self)
        self.file_button.setIcon(FluentIcon.FOLDER)
        self.file_button.clicked.connect(self.open_file)
        button_layout.addWidget(self.file_button)
        
        self.run_button = PushButton(tr['SubtitleExtractorGUI']['Run'], self)
        self.run_button.setIcon(FluentIcon.PLAY)
        self.run_button.clicked.connect(self.run_button_clicked)
        button_layout.addWidget(self.run_button)
        
        self.stop_button = PushButton(tr['SubtitleExtractorGUI']['Stop'], self)
        self.stop_button.setIcon(MyFluentIcon.Stop)
        self.stop_button.setVisible(False)
        self.stop_button.clicked.connect(self.stop_button_clicked)
        
        button_layout.addWidget(self.stop_button)
        
        button_container.setLayout(button_layout)
        right_layout.addWidget(button_container)

        main_layout.addLayout(right_layout, 1)
    
    def on_scroll_change(self, value):
        """监控滚动条位置变化"""
        scrollbar = self.output_text.verticalScrollBar()
        # 如果滚动到底部，启用自动滚动
        if value == scrollbar.maximum():
            self.auto_scroll = True
        # 如果用户向上滚动，禁用自动滚动
        elif self.auto_scroll and value < scrollbar.maximum():
            self.auto_scroll = False

    
    def _require_gui_thread(self):
        if QThread.currentThread() != self.thread():
            raise RuntimeError("Preview and task widgets belong to the GUI thread")

    def _release_preview(self):
        self._require_gui_thread()
        capture, self.video_cap = self.video_cap, None
        if capture is not None:
            capture.release()

    @Slot(int)
    def slider_changed(self, value):
        self._require_gui_thread()
        capture = self.video_cap
        if not self._closing and capture is not None and capture.isOpened():
            capture.set(cv2.CAP_PROP_POS_FRAMES, value)
            ret, frame = capture.read()
            if ret:
                # 更新预览图像
                self.update_preview(frame)

    def ab_sections_changed(self, ab_sections):
        get_current_task_index = self.task_list_component.get_current_task_index()
        if get_current_task_index == -1:
            return
        self.task_list_component.update_task_option(get_current_task_index, TaskOptions.AB_SECTIONS, ab_sections)

    def selections_changed(self, selections):
        get_current_task_index = self.task_list_component.get_current_task_index()
        if get_current_task_index == -1:
            return
        self.task_list_component.update_task_option(get_current_task_index, TaskOptions.SUB_AREAS, selections)

    def on_task_selected(self, index, file_path):
        """处理任务被选中事件
        
        Args:
            index: 任务索引
            file_path: 文件路径
        """
        # 加载选中的视频进行预览
        if not self.load_video(file_path):
            return False
        ab_sections = self.task_list_component.get_task_option(index, TaskOptions.AB_SECTIONS, [])
        self.video_display_component.set_ab_sections(ab_sections)
        self.apply_task_selection(index)
        return True

    def apply_task_selection(self, index):
        """
        从任务列表中获取选区信息
        如果选区信息不存在, 则从已设置的选区中加载
        如果仍然不存在已设置选区则从配置文件中加载
        """
        selections = self.task_list_component.get_task_option(index, TaskOptions.SUB_AREAS, [])
        # 优先从已设置选区中加载
        if len(selections) <= 0:
            tasks = self.task_list_component.get_all_tasks()
            for i, _ in enumerate(tasks):
                selections = self.task_list_component.get_task_option(i, TaskOptions.SUB_AREAS, []).copy()
                if len(selections) > 0:
                    self.append_output('update subarea', selections)
                    self.task_list_component.update_task_option(index, TaskOptions.SUB_AREAS, selections)
                    break
        
        if len(selections) <= 0:
            selections = self.video_display_component.load_selections_from_config()

        self.video_display_component.set_selection_rects(selections)
        self.task_list_component.update_task_option(index, TaskOptions.SUB_AREAS, selections)
        return selections

    
    def on_task_deleted(self, index):
        """处理任务被删除事件
        
        Args:
            index: 任务索引
        """
        # 如果删除的是正在处理的任务，则需要更新状态
        self.current_processing_task_index = self._active_task_index()
        
        task = self.task_list_component.get_task(0)
        if task:
            # 如果还有任务，选中第一个
            self.task_list_component.select_task(0)
        else:
            self._release_preview()

    def update_preview(self, frame):
        # 先缩放图像
        resized_frame = self._img_resize(frame)

        # 设置视频参数
        self.video_display_component.set_video_parameters(
            self.frame_width, self.frame_height, 
            self.scaled_width if hasattr(self, 'scaled_width') else None,
            self.scaled_height if hasattr(self, 'scaled_height') else None,
            self.border_left if hasattr(self, 'border_left') else 0,
            self.border_top if hasattr(self, 'border_top') else 0,
            self.fps if self.fps is not None else 30,
        )
        
        # 更新视频显示（这会同时保存current_pixmap）
        self.video_display_component.update_video_display(resized_frame)

    def _img_resize(self, image):
        height, width = image.shape[:2]
        
        video_preview_width = self.video_display_component.video_preview_width
        video_preview_height = self.video_display_component.video_preview_height
        # 计算等比缩放后的尺寸
        target_ratio = video_preview_width / video_preview_height
        image_ratio = width / height
        
        if image_ratio > target_ratio:
            # 宽度适配，高度按比例缩放
            new_width = video_preview_width
            new_height = int(new_width / image_ratio)
            top_border = (video_preview_height - new_height) // 2
            bottom_border = video_preview_height - new_height - top_border
            left_border = 0
            right_border = 0
        else:
            # 高度适配，宽度按比例缩放
            new_height = video_preview_height
            new_width = int(new_height * image_ratio)
            left_border = (video_preview_width - new_width) // 2
            right_border = video_preview_width - new_width - left_border
            top_border = 0
            bottom_border = 0
        
        # 先缩放图像
        resized = cv2.resize(image, (new_width, new_height))
        
        # 添加黑边以填充到目标尺寸
        padded = cv2.copyMakeBorder(
            resized, 
            top_border, bottom_border, 
            left_border, right_border, 
            cv2.BORDER_CONSTANT, 
            value=[0, 0, 0]
        )
        
        # 保存边框信息，用于坐标转换
        self.border_left = left_border / video_preview_width
        self.border_right = right_border / video_preview_width
        self.border_top = top_border / video_preview_height
        self.border_bottom = bottom_border / video_preview_height
        self.original_width = width
        self.original_height = height
        self.is_vertical = width < height
        self.scaled_width = new_width / video_preview_width
        self.scaled_height = new_height / video_preview_height
        
        return padded

    def _active_task_index(self):
        if self._active_job is None:
            return -1
        # Row numbers and paths can be reused after deletion/re-import. Only
        # the original Task object may receive this job's results.
        return next((i for i, task in enumerate(self.task_list_component.get_all_tasks())
                     if task is self._active_job.task), -1)

    def stop_button_clicked(self):
        self._require_gui_thread()
        job = self._active_job
        index = self._active_task_index()
        self._active_job = None  # Invalidate queued callbacks before waiting.
        self.running_task = False
        self._next_task_timer.stop()
        try:
            if job is not None:
                with job.lock:
                    job.cancelled.set()
                    process = job.process
                    pids = list(job.subprocess_pids)
                for pid in pids:
                    ProcessManager.instance().terminate_by_pid(pid)
                if process is not None:
                    ProcessManager.instance().terminate_by_process(process)
            if index >= 0:
                self.task_list_component.update_task_status(index, TaskStatus.PENDING)
        finally:
            self.current_processing_task_index = -1
            self._release_preview()
            self.run_button.setVisible(True)
            self.stop_button.setVisible(False)

    def run_button_clicked(self):
        self._require_gui_thread()
        if self._closing or self.running_task:
            return
        if not self.task_list_component.get_pending_tasks():
            self.append_output(tr['SubtitleExtractorGUI']['OpenVideoFirst'])
            return
        self.running_task = True
        self.run_button.setVisible(False)
        self.stop_button.setVisible(True)
        self._next_task_timer.stop()
        self._start_next_task()

    @Slot()
    def _start_next_task(self):
        self._require_gui_thread()
        if self._closing or not self.running_task or self._active_job is not None:
            return
        while self.running_task:
            pending = self.task_list_component.get_pending_tasks()
            if not pending:
                self.processing_finished()
                return
            index, task = pending[0]
            self.current_processing_task_index = index
            try:
                self.task_list_component.set_current_task(index)
                if not self.on_task_selected(index, task.path):
                    self.append_output(tr['SubtitleExtractorGUI']['OpenVideoFailed'].format(task.path))
                    self.task_list_component.update_task_status(index, TaskStatus.FAILED)
                    continue
                subtitle_areas = self.task_list_component.get_task_option(index, TaskOptions.SUB_AREAS, [])
                if not subtitle_areas:
                    self.append_output(tr['SubtitleExtractorGUI']['SelectSubtitleArea'].format(task.path))
                    self.task_list_component.update_task_status(index, TaskStatus.FAILED)
                    continue
                self.video_display_component.save_selections_to_config()
                self.task_list_component.update_task_progress(index, 0)
                self.task_list_component.update_task_status(index, TaskStatus.PROCESSING)
                options = deepcopy(task.options)
                for key, value in options.items():
                    if key == TaskOptions.SUB_AREAS.value:
                        areas = self.video_display_component.preview_coordinates_to_video_coordinates(value)
                        options[key] = areas[0] if areas else None
                task.output_path = None
                job = _ExtractionJob(task, task.path, task.output_path, options)
                self._active_job = job
                # This thread must never read/change QWidget, TaskList or
                # VideoCapture state. All such work stays in the slots above.
                threading.Thread(target=self._extract_job, args=(job,), daemon=True).start()
                return
            except Exception as error:
                self.append_output(f"Error: {error}")
                self.task_list_component.update_task_status(index, TaskStatus.FAILED)
                self._active_job = None
                self._release_preview()

    def _emit_worker_event(self, job, kind, payload):
        # Serialize emission with shutdown. Checking only after emit raises is
        # too late if a background thread races native QObject destruction.
        with self._worker_signal_lock:
            if self._closing:
                return
            self.worker_event.emit(job, kind, payload)

    def _extract_job(self, job):
        exitcode, error = None, None
        try:
            process = self.run_subtitle_extractor_process(job)
            exitcode = process.exitcode if process is not None else None
        except Exception as exc:
            error = str(exc)
            print(traceback.format_exc())
        finally:
            self._emit_worker_event(job, 'finished', (exitcode, error))

    @Slot(object, str, object)
    def _on_worker_event(self, job, kind, payload):
        self._require_gui_thread()
        if self._closing or job is not self._active_job or job.cancelled.is_set():
            return
        self.current_processing_task_index = self._active_task_index()
        if kind == 'progress':
            if self.current_processing_task_index >= 0:
                self.update_progress(*payload)
        elif kind == 'log':
            self.append_output(*payload)
        elif kind == 'error':
            self.on_task_error(payload)
        elif kind == 'finished':
            exitcode, error = payload
            index = self.current_processing_task_index
            if error:
                self.append_output(f"Error: {error}")
            if index >= 0:
                if exitcode == 0 and not error and job.task.status == TaskStatus.PROCESSING:
                    self.update_progress(100, 100, 100, True, 100)
                    job.task.output_path = job.output_path
                    self.task_list_component.update_task_status(index, TaskStatus.COMPLETED)
                else:
                    self.task_list_component.update_task_status(index, TaskStatus.FAILED)
            if index >= 0 and self.video_path == job.video_path:
                self.video_slider.setValue(1)
                self._release_preview()
            self._active_job = None
            self.current_processing_task_index = -1
            self._next_task_timer.start(1000)

    @staticmethod
    def extractor_process(queue, video_path, output_path, options):
        """
        在子进程中执行字幕提取的函数
        
        Args:
            video_path: 视频文件路径
            output_path: 输出文件路径
            options: 选项
        """
        sr = None
        try:
            from backend.main import SubtitleExtractor
            sr = SubtitleExtractor(video_path)
            sr.subtitle_output_path = output_path
            for key in options:
                setattr(sr, key, options[key])
            sr.add_progress_listener(lambda progress_ocr, progress_frame_extract, progress_total, isFinished, progress_post=0: SubtitleExtractorRemoteCall.remote_call_update_progress(queue, progress_ocr, progress_frame_extract, progress_total, isFinished, progress_post))
            sr.append_output = lambda *args: SubtitleExtractorRemoteCall.remote_call_append_log(queue, args)
            sr.manage_process = lambda pid: SubtitleExtractorRemoteCall.remote_call_manage_process(queue, pid)
            sr.run()
        except Exception as e:
            traceback.print_exc()
            SubtitleExtractorRemoteCall.remote_call_catch_error(queue, e)
        finally:
            if sr:
                sr.isFinished = True
                sr.vsf_running = False
            SubtitleExtractorRemoteCall.remote_call_finish(queue)
            ProcessManager.instance().terminate_all()
            

    def run_subtitle_extractor_process(self, job):
        """Worker-only process management; no preview or widget access."""
        caller = SubtitleExtractorRemoteCall()
        caller.register_update_progress_callback(
            lambda *args: self._emit_worker_event(job, 'progress', args))
        caller.register_log_callback(lambda args: self._emit_worker_event(job, 'log', args))
        caller.register_error_callback(lambda error: self._emit_worker_event(job, 'error', error))

        def manage_process(pid):
            with job.lock:
                cancelled = job.cancelled.is_set()
                if not cancelled:
                    ProcessManager.instance().add_pid(pid)
                    job.subprocess_pids.append(pid)
            if cancelled:
                ProcessManager.instance().terminate_by_pid(pid)

        caller.register_manage_process_callback(manage_process)
        process = multiprocessing.Process(
            target=HomeInterface.extractor_process,
            args=(caller.queue, job.video_path, job.output_path, job.options))
        try:
            with job.lock:
                if job.cancelled.is_set():
                    return None
                process.start()
                job.process = process
                ProcessManager.instance().add_process(process)
            process.join()
            # The child sends FINISH after its final ERROR/progress/logs. A
            # successful exit alone does not prove those messages were handled.
            if process.exitcode == 0 and not job.cancelled.is_set() and not caller.wait(5):
                raise RuntimeError("Extractor callback stream did not finish")
            print(f"Process exited with code {process.exitcode}")
        finally:
            caller.stop()
        return process

    @Slot()
    def processing_finished(self):
        self._require_gui_thread()
        pending_tasks = self.task_list_component.get_pending_tasks()
        if self._active_job is not None or pending_tasks:
            # 还有待执行任务, 忽略
            return
        # Only the process-completion slot advances/finishes a task batch.
        self.running_task = False
        self.run_button.setVisible(True)
        self.stop_button.setVisible(False)
        self.se = None
        # 重置视频滑块
        self.video_slider.setValue(1)
        # 重置当前处理任务索引
        self.current_processing_task_index = -1

    @Slot(int, int, int, bool, int)
    def update_progress(self, progress_remover, progress_finder, progress_total, isFinished, progress_post=0):
        self._require_gui_thread()
        try:
            job = self._active_job
            if (self.frame_count and job is not None
                    and self.video_path == job.video_path):
                pos = min(self.frame_count - 1, int(progress_remover / 100 * self.frame_count))
                if pos != self.video_slider.value():
                    self.video_slider.setValue(pos)

            # 更新任务进度（加权：帧提取5% + OCR 90% + 后处理5%）
            if self.current_processing_task_index >= 0:
                displayed = min(100, int(progress_remover * 0.9 + progress_finder * 0.05 + progress_post * 0.05))
                # 确保进度不会回退
                task_idx = self.current_processing_task_index
                last_progress = job.last_progress if job is not None else -1
                if displayed >= last_progress or isFinished:
                    if job is not None:
                        job.last_progress = displayed
                    self.task_list_component.update_task_progress(
                        task_idx,
                        displayed,
                    )
            
            # isFinished belongs to OCR progress, not process/queue lifetime.
            # The tagged worker 'finished' event is the only completion owner.
        except Exception as e:
            # 捕获任何异常，防止崩溃
            print(f"更新进度时出错: {str(e)}")

    @Slot(list)
    def append_log(self, log):
        self.append_output(*log)

    def append_output(self, *args):
        """添加文本到输出区域并控制滚动
        Args:
            *args: 要输出的内容，多个参数将用空格连接
        """
        text = ' '.join(str(arg) for arg in args).rstrip()

        # The highlighter updates both new and existing messages on theme changes.
        cursor = self.output_text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(text + '\n', QTextCharFormat())
        print(*args)  # 保持原始的 print 行为
        # 如果启用了自动滚动，则滚动到底部
        if self.auto_scroll:
            scrollbar = self.output_text.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

    @Slot(object)
    def on_task_error(self, e):
        self.append_output(tr['SubtitleExtractorGUI']['ErrorDuringProcessing'].format(str(e)))
        if self.current_processing_task_index >= 0:
            self.task_list_component.update_task_status(self.current_processing_task_index, TaskStatus.FAILED)

    def load_video(self, video_path):
        self._require_gui_thread()
        if self._closing:
            return False
        self._release_preview()
        self.video_path = video_path
        capture = cv2.VideoCapture(video_path)
        try:
            if not capture.isOpened():
                return False
            ret, frame = capture.read()
            if not ret:
                return False
            self.frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
            self.frame_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
            self.frame_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
            self.fps = capture.get(cv2.CAP_PROP_FPS)
            self.video_cap = capture
            self.update_preview(frame)
            # The first decoded frame is already displayed. Do not cause
            # implicit seeks from range/value changes during replacement.
            with QSignalBlocker(self.video_slider):
                self.video_slider.setMaximum(self.frame_count)
                self.video_slider.setValue(1)
            self.video_display_component.set_dragger_enabled(True)
            return True
        finally:
            if self.video_cap is not capture:
                capture.release()

    def open_file(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self,
            tr['SubtitleExtractorGUI']['Open'],
            "",
            "All Files (*.*);;MP4 Files (*.mp4);;FLV Files (*.flv);;WMV Files (*.wmv);;AVI Files (*.avi)"
        )
        if files:
            files_loaded = []
            # 倒序打开, 确保第一个视频截图显示在屏幕上
            for path in reversed(files):
                if self.load_video(path):
                    self.append_output(f"{tr['SubtitleExtractorGUI']['OpenVideoSuccess']}: {path}")
                    files_loaded.append(path)
                else:
                    self.append_output(f"{tr['SubtitleExtractorGUI']['OpenVideoFailed']}: {path}")
            # 正序添加, 确保任务列表顺序一致
            for path in reversed(files_loaded):
                # 添加到任务列表
                self.task_list_component.add_task(path)
            index = max(0, self.task_list_component.find_task_index_by_path(path))
            self.task_list_component.select_task(index)

    def shutdown(self):
        """Called explicitly by the main window, before its children die."""
        self._require_gui_thread()
        if self._closing:
            return
        with self._worker_signal_lock:
            self._closing = True
        self.stop_button_clicked()

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)
