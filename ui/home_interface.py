import os
import cv2
import threading
import atexit
import multiprocessing
import time
import traceback
from multiprocessing import managers
from PySide6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QFileDialog
from PySide6.QtCore import Qt, Slot, QTimer, QRect, QRectF, Signal
from PySide6 import QtCore, QtWidgets, QtGui
from qfluentwidgets import (qconfig, PushButton, CardWidget, SubtitleLabel, PlainTextEdit,
                           FluentIcon, HollowHandleStyle)
from ui.setting_interface import SettingInterface
from ui.component.video_display_component import VideoDisplayComponent
from ui.component.task_list_component import TaskListComponent, TaskStatus
from backend.config import config, tr
from backend.tools.process_manager import ProcessManager
from backend.tools.subtitle_extractor_remote_call import SubtitleExtractorRemoteCall

class HomeInterface(QWidget):
    progress_signal = Signal(int, bool) 
    append_log_signal = Signal(list)
    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setObjectName("HomeInterface")
        # 初始化一些变量
        self.video_paths = []
        self.video_path = None
        self.video_cap = None
        self.fps = None
        self.frame_count = None
        self.frame_width = None
        self.frame_height = None
        self.se = None  # 后台字幕提取器

        # 字幕区域参数
        self.xmin = None
        self.xmax = None
        self.ymin = None
        self.ymax = None

        # 添加自动滚动控制标志
        self.auto_scroll = True
        
        # 当前正在处理的任务索引
        self.current_processing_task_index = -1

        self.__initWidget()
        self.progress_signal.connect(self.update_progress)
        self.append_log_signal.connect(self.append_log)
        print(tr['Main']['AcceleratorWarning'])

    def __initWidget(self):
        """创建主页面"""
        main_layout = QHBoxLayout(self)
        main_layout.setSpacing(8)
        main_layout.setContentsMargins(16, 16, 16, 16)

        # 左侧视频区域
        left_layout = QVBoxLayout()
        left_layout.setSpacing(8)
        
        # 创建视频显示组件
        self.video_display_component = VideoDisplayComponent(self)
        left_layout.addWidget(self.video_display_component)
        
        # 获取视频显示和滑块的引用
        self.video_display = self.video_display_component.video_display
        self.video_slider = self.video_display_component.video_slider
        self.video_slider.valueChanged.connect(self.slider_changed)
        
        # 输出文本区域
        self.output_text = PlainTextEdit()
        self.output_text.setMinimumHeight(150)
        self.output_text.setReadOnly(True)
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
        
        self.run_button = PushButton(tr['SubtitleExtractorGUI']['Run'], self)
        self.run_button.setIcon(FluentIcon.PLAY)
        self.run_button.clicked.connect(self.run_button_clicked)
        
        button_layout.addWidget(self.file_button)
        button_layout.addWidget(self.run_button)
        
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

    
    def slider_changed(self, value):
        if self.video_cap is not None and self.video_cap.isOpened():
            frame_no = self.video_slider.value()
            self.video_cap.set(cv2.CAP_PROP_POS_FRAMES, frame_no)
            ret, frame = self.video_cap.read()
            if ret:
                # 更新预览图像
                self.update_preview(frame)
        
    def on_task_selected(self, index, file_path):
        """处理任务被选中事件
        
        Args:
            index: 任务索引
            file_path: 文件路径
        """
        # 加载选中的视频进行预览
        self.load_video(file_path)
    
    def on_task_deleted(self, index):
        """处理任务被删除事件
        
        Args:
            index: 任务索引
        """
        # 如果删除的是正在处理的任务，则需要更新状态
        if index == self.current_processing_task_index:
            self.current_processing_task_index = -1
        
        # 如果删除的是待处理队列中的任务，则需要从队列中移除
        if index < len(self.video_paths):
            self.video_paths.pop(index)
        
        if len(self.video_paths) > 0:
            # 如果还有任务，选中第一个
            self.load_video(self.video_paths[0])
            self.task_list_component.select_task(0)

    def update_preview(self, frame):
        # 先缩放图像
        resized_frame = self._img_resize(frame)

        # 设置视频参数
        self.video_display_component.set_video_parameters(
            self.frame_width, self.frame_height, 
            self.scaled_width if hasattr(self, 'scaled_width') else None,
            self.scaled_height if hasattr(self, 'scaled_height') else None,
            self.border_left if hasattr(self, 'border_left') else 0,
            self.border_top if hasattr(self, 'border_top') else 0
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
        self.border_left = left_border
        self.border_right = right_border
        self.border_top = top_border
        self.border_bottom = bottom_border
        self.original_width = width
        self.original_height = height
        self.is_vertical = width < height
        self.scaled_width = new_width
        self.scaled_height = new_height
        
        return padded

    def run_button_clicked(self):
        if not self.task_list_component.get_pending_tasks():
            self.append_output(tr['SubtitleExtractorGUI']['OpenVideoFirst'])
            return
            
        try:
            self.run_button.setEnabled(False)
            self.file_button.setEnabled(False)
            # 获取所有待执行的任务
            pending_tasks = self.task_list_component.get_pending_tasks()
            if not pending_tasks:
                # 没有待执行的任务，恢复按钮状态
                self.run_button.setEnabled(True)
                self.file_button.setEnabled(True)
                return
            # 更新视频路径列表，只包含待执行的任务
            self.video_paths = [path for _, path in pending_tasks]
            
            # 开启后台线程处理视频
            def task():
                task_index = 0
                while self.video_paths:
                    try:
                        video_path = self.video_paths.pop(0)
                        
                        # 更新当前处理的任务索引
                        self.current_processing_task_index = pending_tasks[task_index][0]
                        
                        # 更新任务状态为运行中
                        self.task_list_component.update_task_progress(self.current_processing_task_index, 1)
                        
                        # 选中当前任务
                        self.task_list_component.select_task(self.current_processing_task_index)
                        
                        if self.video_cap:
                            self.video_cap.release()
                            self.video_cap = None
                            
                            
                        # 获取字幕区域坐标（直接从视频显示组件获取）
                        subtitle_area = self.video_display_component.get_original_coordinates()
                        if not subtitle_area:
                            self.append_output(tr['SubtitleExtractorGUI']['SelectSubtitleArea'])
                            return
                        self.append_output(f"{tr['SubtitleExtractorGUI']['SubtitleArea']}: {subtitle_area}")
                            
                        self.load_video(video_path)
                        
                        process = self.run_subtitle_extractor_process(video_path, subtitle_area)
                        
                        # 更新任务状态为已完成
                        self.task_list_component.update_task_progress(self.current_processing_task_index, 100, True)
                        
                        task_index += 1
                    except Exception as e:
                        print(e)
                        self.append_output(f"Error: {e}")
                        
                        # 更新任务状态为失败
                        if self.current_processing_task_index >= 0:
                            self.task_list_component.update_task_status(self.current_processing_task_index, TaskStatus.FAILED)
                        
                        self.video_paths.append(video_path)
                        break
                    finally:
                        self.progress_signal.emit(100, True)
                        
                        if self.video_cap:
                            self.video_cap.release()
                            self.video_cap = None

            threading.Thread(target=task, daemon=True).start()
        except Exception as e:
            print(traceback.format_exc())
            self.append_output(f"Error: {e}")
            # 没有待执行的任务，恢复按钮状态
            self.run_button.setEnabled(True)
            self.file_button.setEnabled(True)

    @staticmethod
    def extractor_process(queue, video_path, subtitle_area):
        """
        在子进程中执行字幕提取的函数
        
        Args:
            video_path: 视频文件路径
            subtitle_area: 字幕区域坐标 (ymin, ymax, xmin, xmax)
        """
        extractor = None
        try:
            from backend.main import SubtitleExtractor
            extractor = SubtitleExtractor(video_path, subtitle_area)
            extractor.add_progress_listener(lambda progress, isFinished: SubtitleExtractorRemoteCall.remote_call_update_progress(queue, progress, isFinished))
            extractor.append_output = lambda *args: SubtitleExtractorRemoteCall.remote_call_append_log(queue, args)
            extractor.manage_process = lambda pid: SubtitleExtractorRemoteCall.remote_call_manage_process(queue, pid)
            extractor.run()
        except Exception as e:
            import traceback
            print(traceback.format_exc())
        finally:
            if extractor:
                extractor.isFinished = True
                extractor.vsf_running = False
            SubtitleExtractorRemoteCall.remote_call_finish(queue)
            ProcessManager.instance().terminate_all()
            

    # 修改run_subtitle_extractor_process方法
    def run_subtitle_extractor_process(self, video_path, subtitle_area):
        """
        使用多进程执行字幕提取，并等待进程完成
        
        Args:
            video_path: 视频文件路径
            subtitle_area: 字幕区域坐标 (ymin, ymax, xmin, xmax)
        """

        subtitle_extractor_remote_caller = SubtitleExtractorRemoteCall()
        subtitle_extractor_remote_caller.register_update_progress_callback(self.progress_signal.emit)
        subtitle_extractor_remote_caller.register_log_callback(self.append_log_signal.emit)
        subtitle_extractor_remote_caller.register_manage_process_callback(lambda pid: ProcessManager.instance().add_pid(pid))
        process = multiprocessing.Process(
            target=HomeInterface.extractor_process,
            args=(subtitle_extractor_remote_caller.queue, video_path, subtitle_area)
        )
        try:
            process.start()
            ProcessManager.instance().add_process(process)
            process.join()
        finally:
            subtitle_extractor_remote_caller.stop()
        return process

    @Slot()
    def processing_finished(self):
        pending_tasks = self.task_list_component.get_pending_tasks()
        if pending_tasks:
            # 还有待执行任务, 忽略
            return
        # 处理完成后恢复界面可用性
        self.run_button.setEnabled(True)
        self.file_button.setEnabled(True)
        self.se = None
        # 重置视频滑块
        self.video_slider.setValue(1)
        # 重置当前处理任务索引
        self.current_processing_task_index = -1

    @Slot(int, bool)
    def update_progress(self, progress_total, isFinished):
        try:
            pos = min(self.frame_count - 1, int(progress_total / 100 * self.frame_count))
            if pos != self.video_slider.value():
                self.video_slider.setValue(pos)
            
            # 更新任务进度
            if self.current_processing_task_index >= 0:
                self.task_list_component.update_task_progress(
                    self.current_processing_task_index, 
                    progress_total,
                    isFinished
                )
            
            # 检查是否完成
            if isFinished:
                self.processing_finished()
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
        # 将所有参数转换为字符串并用空格连接
        text = ' '.join(str(arg) for arg in args).rstrip()
        self.output_text.appendPlainText(text)
        print(*args)  # 保持原始的 print 行为
        # 如果启用了自动滚动，则滚动到底部
        if self.auto_scroll:
            scrollbar = self.output_text.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

    def load_video(self, video_path):
        self.video_path = video_path
        self.video_cap = cv2.VideoCapture(self.video_path)
        if not self.video_cap.isOpened():
            return False
        ret, frame = self.video_cap.read()
        if not ret:
            return False
        self.frame_count = int(self.video_cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.frame_height = int(self.video_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.frame_width = int(self.video_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.fps = self.video_cap.get(cv2.CAP_PROP_FPS)
        
        self.update_preview(frame)
        self.video_display_component.load_selection_ratio()
        self.video_slider.setMaximum(self.frame_count)
        self.video_slider.setValue(1)
        self.video_display_component.set_dragger_enabled(True)
        return True


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
                # 添加到视频路径列表
                self.video_paths.append(path)
            # 选中第一个任务
            self.task_list_component.select_task(0)

    def closeEvent(self, event):
        """窗口关闭时断开信号连接"""
        try:
            # 断开信号连接
            self.progress_signal.disconnect(self.update_progress)
            self.append_log_signal.disconnect(self.append_log)
            
            # 释放视频资源
            if self.video_cap:
                self.video_cap.release()
                self.video_cap = None
                
            # 确保所有子进程都已终止
            ProcessManager.instance().terminate_all()
        except Exception as e:
            print(f"关闭窗口时出错: {str(e)}")
        super().closeEvent(event)
    