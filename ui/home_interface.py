import os
import cv2
import threading
import atexit
import multiprocessing
import time
from multiprocessing import managers
from PySide6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton
from PySide6.QtCore import Qt, Slot, QTimer, QRect, QRectF, Signal
from PySide6 import QtCore, QtWidgets, QtGui
from qfluentwidgets import (qconfig, PushButton, CardWidget, SubtitleLabel, PlainTextEdit,
                           FluentIcon, HollowHandleStyle)
from ui.setting_interface import SettingInterface
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

        self.is_drawing = False
        self.selection_rect = QRect()
        self.drag_start_pos = None
        self.resize_edge = None
        self.edge_size = 10  # Resize 

        # 添加自动滚动控制标志
        self.auto_scroll = True

        # 获取屏幕大小
        screen = QtWidgets.QApplication.primaryScreen().size()
        self.screen_width = screen.width()
        self.screen_height = screen.height()
        
        # 设置视频预览区域大小（根据屏幕宽度动态调整）
        self.video_preview_width = 960
        self.video_preview_height = self.video_preview_width * 9 // 16
        if self.screen_width // 2 < 960:
            self.video_preview_width = 640
            self.video_preview_height = self.video_preview_width * 9 // 16

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
        
        # 视频预览区域和进度条容器
        video_container = CardWidget(self)
        video_container.setObjectName('videoContainer')
        video_layout = QVBoxLayout()
        video_layout.setSpacing(0)
        video_layout.setContentsMargins(2, 2, 2, 2)
        video_layout.setAlignment(Qt.AlignCenter)
        
        # 创建内部黑色背景容器
        black_container = QWidget(self)
        black_container.setObjectName('blackContainer')
        black_container.setStyleSheet("""
            #blackContainer {
                background-color: black;
                border-radius: 8px;
            }
        """)
        black_layout = QVBoxLayout()
        black_layout.setContentsMargins(0, 0, 0, 0)
        black_layout.setSpacing(0)
        black_layout.setAlignment(Qt.AlignCenter)

        # 初始化界面控件
        self.video_display = QtWidgets.QLabel()
        self.video_display.setStyleSheet("""
            background-color: black;
            border-top-left-radius: 8px;
            border-top-right-radius: 8px;
        """)
        self.video_display.setFixedSize(self.video_preview_width, self.video_preview_height)
        self.video_display.setMouseTracking(True)
        self.video_display.mousePressEvent = self.selection_mouse_press
        self.video_display.mouseMoveEvent = self.selection_mouse_move
        self.video_display.mouseReleaseEvent = self.selection_mouse_release

        self.video_slider = QtWidgets.QSlider(Qt.Horizontal)
        self.video_slider.setMinimum(1)
        self.video_slider.setFixedHeight(22)
        self.video_slider.setMaximum(100)  # 默认最大值设为100，与进度百分比一致
        self.video_slider.setValue(1)
        self.video_slider.valueChanged.connect(self.slider_changed)
        self.video_slider.setStyle(HollowHandleStyle({
            "handle.color": QtGui.QColor(255, 255, 255),
            "handle.ring-width": 4,
            "handle.hollow-radius": 6,
            "handle.margin": 1
        }))
        
        self.output_text = PlainTextEdit()
        self.output_text.setMinimumHeight(150)
        self.output_text.setReadOnly(True)
        self.output_text.document().setDocumentMargin(10)        
        # 连接滚动条值变化信号
        self.output_text.verticalScrollBar().valueChanged.connect(self.on_scroll_change)

        # 视频预览区域
        self.video_display.setObjectName('videoDisplay')
        black_layout.addWidget(self.video_display, 0, Qt.AlignCenter)

        # 进度条和滑块容器
        control_container = QWidget(self)
        control_layout = QVBoxLayout()
        control_layout.setContentsMargins(8, 8, 8, 8)
        control_layout.addWidget(self.video_slider)

        control_container.setLayout(control_layout)
        control_container.setStyleSheet("""
            background-color: black;
            border-bottom-left-radius: 8px;
            border-bottom-right-radius: 8px;        
        """)
        black_layout.addWidget(control_container)
        
        black_container.setLayout(black_layout)
        video_layout.addWidget(black_container)
        video_container.setLayout(video_layout)
        left_layout.addWidget(video_container)

        # 输出文本区域
        output_container = CardWidget(self)
        output_layout = QVBoxLayout()
        output_layout.setContentsMargins(0, 0, 0, 0)
        output_layout.addWidget(self.output_text)
        output_container.setLayout(output_layout)
        left_layout.addWidget(output_container)

        main_layout.addLayout(left_layout, 2)

        # 右侧设置区域
        right_layout = QVBoxLayout()
        right_layout.setSpacing(20)

        # 设置容器
        settings_container = CardWidget(self)
        settings_container.setLayout(SettingInterface(settings_container))
        right_layout.addWidget(settings_container)
        
        right_layout.addStretch()
        
        # 操作标题
        operation_title = SubtitleLabel('')
        right_layout.addWidget(operation_title)
        
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
    
    
    def selection_mouse_press(self, event):
        if not self.video_cap:
            return
            
        pos = event.pos()
        
        # 检查点击是否在实际视频区域内（排除黑边）
        if hasattr(self, 'border_left') and hasattr(self, 'border_top'):
            if (pos.x() < self.border_left or 
                pos.x() >= self.video_preview_width - self.border_right or
                pos.y() < self.border_top or 
                pos.y() >= self.video_preview_height - self.border_bottom):
                return  # 点击在黑边区域，忽略
        
        if self.selection_rect.isEmpty():
            # 如果没有选择框，开始绘制
            self.is_drawing = True
            self.selection_rect.setTopLeft(pos)
            self.selection_rect.setBottomRight(pos)
        else:
            edge = self.get_resize_edge(pos)
            if edge:
                # 如果在边缘，准备调整大小
                self.resize_edge = edge
            elif self.selection_rect.contains(pos):
                # 如果在选择框内部，准备拖动
                self.drag_start_pos = pos
                # 不要设置 is_drawing = True，这会导致绘制新区域
            else:
                # 如果在选择框外部，开始绘制新区域
                self.is_drawing = True
                self.selection_rect.setTopLeft(pos)
                self.selection_rect.setBottomRight(pos)

    def selection_mouse_move(self, event):
        if not self.video_cap:
            return
            
        pos = event.pos()
        if self.is_drawing and not self.resize_edge:
            self.selection_rect.setBottomRight(pos)
            self.update_preview_with_rect()
        elif self.resize_edge:
            self.resize_selection(pos)
            self.update_preview_with_rect()
        elif self.drag_start_pos:
            delta = pos - self.drag_start_pos
            self.selection_rect.translate(delta.x(), delta.y())
            self.drag_start_pos = pos
            self.update_preview_with_rect()
        else:
            edge = self.get_resize_edge(pos)
            if edge:
                if edge in ['top', 'bottom']:
                    self.video_display.setCursor(Qt.SizeVerCursor)
                elif edge in ['left', 'right']:
                    self.video_display.setCursor(Qt.SizeHorCursor)
                elif edge in ['topleft', 'bottomright']:
                    self.video_display.setCursor(Qt.SizeFDiagCursor)
                elif edge in ['topright', 'bottomleft']:
                    self.video_display.setCursor(Qt.SizeBDiagCursor)
            elif self.selection_rect.contains(pos):
                # 当鼠标在选择框内部时，显示移动光标
                self.video_display.setCursor(Qt.SizeAllCursor)
            else:
                self.video_display.setCursor(Qt.ArrowCursor)

    def selection_mouse_release(self, event):
        self.is_drawing = False
        self.resize_edge = None
        self.drag_start_pos = None
        if not self.selection_rect.isEmpty():
            self.update_subtitle_area()

    def get_resize_edge(self, pos):
        if self.selection_rect.isEmpty():
            return None
            
        rect = self.selection_rect
        x, y = pos.x(), pos.y()
        
        # 首先检查点是否在选择框的扩展边缘区域内
        # 扩展区域为选择框向外扩展edge_size像素的区域
        extended_rect = rect.adjusted(-self.edge_size, -self.edge_size, 
                                     self.edge_size, self.edge_size)
        if not extended_rect.contains(pos):
            return None
        
        # 然后检查是否在具体的边缘
        if abs(x - rect.left()) <= self.edge_size:
            if abs(y - rect.top()) <= self.edge_size:
                return 'topleft'
            elif abs(y - rect.bottom()) <= self.edge_size:
                return 'bottomleft'
            return 'left'
        elif abs(x - rect.right()) <= self.edge_size:
            if abs(y - rect.top()) <= self.edge_size:
                return 'topright'
            elif abs(y - rect.bottom()) <= self.edge_size:
                return 'bottomright'
            return 'right'
        elif abs(y - rect.top()) <= self.edge_size:
            return 'top'
        elif abs(y - rect.bottom()) <= self.edge_size:
            return 'bottom'
        return None

    def resize_selection(self, pos):
        if not self.resize_edge:
            return
            
        rect = self.selection_rect
        if self.resize_edge == 'left':
            rect.setLeft(pos.x())
        elif self.resize_edge == 'right':
            rect.setRight(pos.x())
        elif self.resize_edge == 'top':
            rect.setTop(pos.y())
        elif self.resize_edge == 'bottom':
            rect.setBottom(pos.y())
        elif self.resize_edge == 'topleft':
            rect.setTopLeft(pos)
        elif self.resize_edge == 'topright':
            rect.setTopRight(pos)
        elif self.resize_edge == 'bottomleft':
            rect.setBottomLeft(pos)
        elif self.resize_edge == 'bottomright':
            rect.setBottomRight(pos)

    def update_preview_with_rect(self):
        if not self.video_cap or not self.video_cap.isOpened():
            return
            
        frame_no = self.video_slider.value()
        self.video_cap.set(cv2.CAP_PROP_POS_FRAMES, frame_no)
        ret, frame = self.video_cap.read()
        if ret:
            # 考虑等比缩放和黑边
            if hasattr(self, 'scaled_width') and hasattr(self, 'scaled_height'):
                # 调整选择框坐标，考虑黑边偏移
                x_adjusted = max(0, self.selection_rect.x() - self.border_left)
                y_adjusted = max(0, self.selection_rect.y() - self.border_top)
                
                # 如果选择框超出了实际视频区域，需要调整宽度和高度
                w_adjusted = min(self.selection_rect.width(), self.scaled_width - x_adjusted)
                h_adjusted = min(self.selection_rect.height(), self.scaled_height - y_adjusted)
                
                # 转换为原始视频坐标
                scale_x = self.frame_width / self.scaled_width
                scale_y = self.frame_height / self.scaled_height
                
                x = int(x_adjusted * scale_x)
                y = int(y_adjusted * scale_y)
                w = int(w_adjusted * scale_x)
                h = int(h_adjusted * scale_y)
            else:
                # 旧的计算方式作为备用
                scale_x = self.frame_width / self.video_preview_width
                scale_y = self.frame_height / self.video_preview_height
                x = int(self.selection_rect.x() * scale_x)
                y = int(self.selection_rect.y() * scale_y)
                w = int(self.selection_rect.width() * scale_x)
                h = int(self.selection_rect.height() * scale_y)
            
            frame_with_rect = cv2.rectangle(frame.copy(), (x, y), (x + w, y + h), (0, 255, 0), 3)
            resized_frame = self._img_resize(frame_with_rect)
            self.update_video_display(resized_frame)

    def slider_changed(self, value):
        if self.video_cap is not None and self.video_cap.isOpened():
            frame_no = self.video_slider.value()
            self.video_cap.set(cv2.CAP_PROP_POS_FRAMES, frame_no)
            ret, frame = self.video_cap.read()
            if ret:
                self.update_preview_with_rect()
            
    def update_preview(self, frame):
        # 如果有选择框，使用选择框的坐标
        if not self.selection_rect.isEmpty():
            x = int(self.selection_rect.x())
            y = int(self.selection_rect.y())
            w = int(self.selection_rect.width())
            h = int(self.selection_rect.height())
            frame_with_rect = cv2.rectangle(frame.copy(), (x, y), (x + w, y + h), (0, 255, 0), 3)
            resized_frame = self._img_resize(frame_with_rect)
            self.update_video_display(resized_frame)
        else:
            # 没有选择框时直接显示原始帧
            resized_frame = self._img_resize(frame)
            self.update_video_display(resized_frame)


    def update_video_display(self, frame):
        # 将 OpenCV 帧（BGR 格式）转换为 QImage 并显示在 QLabel 上
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb_frame.shape
        bytes_per_line = ch * w
        image = QtGui.QImage(rgb_frame.data, w, h, bytes_per_line, QtGui.QImage.Format_RGB888)
        pix = QtGui.QPixmap.fromImage(image)
        
        # 创建带圆角的图像
        rounded_pix = QtGui.QPixmap(pix.size())
        rounded_pix.fill(Qt.transparent)  # 填充透明背景
        
        painter = QtGui.QPainter(rounded_pix)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)  # 抗锯齿
        
        # 创建圆角路径
        path = QtGui.QPainterPath()
        rect = QRectF(0, 0, pix.width(), pix.height())
        
        # 手动创建只有左上和右上圆角的路径
        radius = 8
        path.moveTo(radius, 0)
        path.lineTo(pix.width() - radius, 0)
        path.arcTo(pix.width() - radius * 2, 0, radius * 2, radius * 2, 90, -90)
        path.lineTo(pix.width(), pix.height())
        path.lineTo(0, pix.height())
        path.lineTo(0, radius)
        path.arcTo(0, 0, radius * 2, radius * 2, 180, -90)
        path.closeSubpath()
        
        painter.setClipPath(path)
        painter.drawPixmap(0, 0, pix)
        painter.end()
        
        self.video_display.setPixmap(rounded_pix)

    def _img_resize(self, image):
        height, width = image.shape[:2]
        
        # 计算等比缩放后的尺寸
        target_ratio = self.video_preview_width / self.video_preview_height
        image_ratio = width / height
        
        if image_ratio > target_ratio:
            # 宽度适配，高度按比例缩放
            new_width = self.video_preview_width
            new_height = int(new_width / image_ratio)
            top_border = (self.video_preview_height - new_height) // 2
            bottom_border = self.video_preview_height - new_height - top_border
            left_border = 0
            right_border = 0
        else:
            # 高度适配，宽度按比例缩放
            new_height = self.video_preview_height
            new_width = int(new_height * image_ratio)
            left_border = (self.video_preview_width - new_width) // 2
            right_border = self.video_preview_width - new_width - left_border
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
        if self.video_cap is None or not self.video_paths:
            self.append_output(tr['SubtitleExtractorGUI']['OpenVideoFirst'])
        else:
            # 禁用部分按钮
            self.run_button.setEnabled(False)
            self.file_button.setEnabled(False)
            
            # 获取字幕区域坐标（使用选择框的坐标并进行缩放）
            if hasattr(self, 'scaled_width') and hasattr(self, 'scaled_height'):
                # 调整选择框坐标，考虑黑边偏移
                x_adjusted = max(0, self.selection_rect.x() - self.border_left)
                y_adjusted = max(0, self.selection_rect.y() - self.border_top)
                
                # 如果选择框超出了实际视频区域，需要调整宽度和高度
                w_adjusted = min(self.selection_rect.width(), self.scaled_width - x_adjusted)
                h_adjusted = min(self.selection_rect.height(), self.scaled_height - y_adjusted)
                
                # 转换为原始视频坐标
                scale_x = self.frame_width / self.scaled_width
                scale_y = self.frame_height / self.scaled_height
                
                self.xmin = int(x_adjusted * scale_x)
                self.xmax = int((x_adjusted + w_adjusted) * scale_x)
                self.ymin = int(y_adjusted * scale_y)
                self.ymax = int((y_adjusted + h_adjusted) * scale_y)
            else:
                # 横屏视频，直接使用原来的计算方式
                scale_x = self.frame_width / self.video_preview_width
                scale_y = self.frame_height / self.video_preview_height
                
                self.xmin = int(self.selection_rect.x() * scale_x)
                self.xmax = int((self.selection_rect.x() + self.selection_rect.width()) * scale_x)
                self.ymin = int(self.selection_rect.y() * scale_y)
                self.ymax = int((self.selection_rect.y() + self.selection_rect.height()) * scale_y)
            
            # 确保坐标在有效范围内
            self.xmin = max(0, min(self.xmin, self.frame_width))
            self.xmax = max(0, min(self.xmax, self.frame_width))
            self.ymin = max(0, min(self.ymin, self.frame_height))
            self.ymax = max(0, min(self.ymax, self.frame_height))
                
            self.append_output(f"{tr['SubtitleExtractorGUI']['SubtitleArea']}：({self.ymin},{self.ymax},{self.xmin},{self.xmax})")
            
            # 保存字幕区域配置（保存相对比例而非绝对像素值）
            config.subtitleSelectionAreaY.value = self.ymin / self.frame_height
            config.subtitleSelectionAreaH.value = (self.ymax - self.ymin) / self.frame_height
            config.subtitleSelectionAreaX.value = self.xmin / self.frame_width
            config.subtitleSelectionAreaW.value = (self.xmax - self.xmin) / self.frame_width
            
            qconfig.save()
            
            # 当前视频的字幕区域
            current_subtitle_area = (self.ymin, self.ymax, self.xmin, self.xmax)
                
            # 开启后台线程处理视频
            def task():
                while self.video_paths:
                    try:
                        video_path = self.video_paths.pop(0)
                        
                        if not self.video_cap:
                            self.load_video(video_path)
                        
                        # 为每个视频重新计算字幕区域
                        subtitle_area = self.calculate_subtitle_area_for_video(current_subtitle_area)
                        process = self.run_subtitle_extractor_process(video_path, subtitle_area)
                    except Exception as e:
                        print(e)
                        self.append_output(f"Error: {e}")
                        self.video_paths.append(video_path)
                        break
                    finally:
                        self.progress_signal.emit(100, True)
                        
                        if self.video_cap:
                            self.video_cap.release()
                            self.video_cap = None

            threading.Thread(target=task, daemon=True).start()

    def calculate_subtitle_area_for_video(self, reference_area=None):
        """
        为每个视频计算正确的字幕区域
        
        Args:
            reference_area: 参考字幕区域 (ymin, ymax, xmin, xmax)，如果为None则使用配置中的比例
            
        Returns:
            tuple: 调整后的字幕区域 (ymin, ymax, xmin, xmax)
        """
        # 使用保存的比例计算新视频的字幕区域
        y_ratio = config.subtitleSelectionAreaY.value
        h_ratio = config.subtitleSelectionAreaH.value
        x_ratio = config.subtitleSelectionAreaX.value
        w_ratio = config.subtitleSelectionAreaW.value
        
        # 计算像素坐标
        ymin = int(y_ratio * self.frame_height)
        ymax = int((y_ratio + h_ratio) * self.frame_height)
        xmin = int(x_ratio * self.frame_width)
        xmax = int((x_ratio + w_ratio) * self.frame_width)
        
        # 确保坐标在有效范围内
        xmin = max(0, min(xmin, self.frame_width))
        xmax = max(0, min(xmax, self.frame_width))
        ymin = max(0, min(ymin, self.frame_height))
        ymax = max(0, min(ymax, self.frame_height))
        
        return (ymin, ymax, xmin, xmax)

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
            print(f"字幕提取进程出错: {str(e)}")
            import traceback
            print(traceback.format_exc())
        finally:
            print("字幕提取进程结束")
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
        # 处理完成后恢复界面可用性
        self.run_button.setEnabled(True)
        self.file_button.setEnabled(True)
        self.se = None
        # 重置视频滑块
        self.video_slider.setValue(1)

    @Slot(int, bool)
    def update_progress(self, progress_total, isFinished):
        try:
            pos = min(self.frame_count - 1, int(progress_total / 100 * self.frame_count))
            if pos != self.video_slider.value():
                self.video_slider.setValue(pos)
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

    def update_subtitle_area(self):
        scale_x = self.frame_width / self.video_preview_width
        scale_y = self.frame_height / self.video_preview_height
        
        self.xmin = int(self.selection_rect.x() * scale_x)
        self.ymin = int(self.selection_rect.y() * scale_y)
        self.xmax = int((self.selection_rect.x() + self.selection_rect.width()) * scale_x)
        self.ymax = int((self.selection_rect.y() + self.selection_rect.height()) * scale_y)
        
        # Ensure coordinates are within valid range
        self.xmin = max(0, min(self.xmin, self.frame_width))
        self.xmax = max(0, min(self.xmax, self.frame_width))
        self.ymin = max(0, min(self.ymin, self.frame_height))
        self.ymax = max(0, min(self.ymax, self.frame_height))

    def load_video(self, video_path):
        self.video_path = video_path
        self.video_cap = cv2.VideoCapture(self.video_path)
        if self.video_cap.isOpened():
            ret, frame = self.video_cap.read()
            if ret:
                self.frame_count = int(self.video_cap.get(cv2.CAP_PROP_FRAME_COUNT))
                self.frame_height = int(self.video_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                self.frame_width = int(self.video_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                self.fps = self.video_cap.get(cv2.CAP_PROP_FPS)
                
                # 计算原始视频中的坐标
                y_orig = int(self.frame_height * config.subtitleSelectionAreaY.value)
                h_orig = int(self.frame_height * config.subtitleSelectionAreaH.value)
                x_orig = int(self.frame_width * config.subtitleSelectionAreaX.value)
                w_orig = int(self.frame_width * config.subtitleSelectionAreaW.value)
                
                # 将原始视频坐标转换为显示区域坐标
                scale_x = self.video_preview_width / self.frame_width
                scale_y = self.video_preview_height / self.frame_height
                
                x_display = int(x_orig * scale_x)
                y_display = int(y_orig * scale_y)
                w_display = int(w_orig * scale_x)
                h_display = int(h_orig * scale_y)
                
                # 确保坐标在显示区域有效范围内
                x_display = max(0, min(x_display, self.video_preview_width))
                y_display = max(0, min(y_display, self.video_preview_height))
                w_display = max(1, min(w_display, self.video_preview_width - x_display))
                h_display = max(1, min(h_display, self.video_preview_height - y_display))
                
                # 设置选择框并更新显示
                self.selection_rect.setRect(x_display, y_display, w_display, h_display)
                
                # 更新视频显示和选择框
                resized_frame = self._img_resize(frame)
                self.update_video_display(resized_frame)
                self.video_slider.setMaximum(self.frame_count)
                self.video_slider.setValue(1)
                self.update_preview_with_rect()
                
                # 更新字幕区域参数
                self.update_subtitle_area()

    def open_file(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self,
            tr['SubtitleExtractorGUI']['Open'],
            "",
            "All Files (*.*);;MP4 Files (*.mp4);;FLV Files (*.flv);;WMV Files (*.wmv);;AVI Files (*.avi)"
        )
        if files:
            self.video_paths = files
            for video in self.video_paths:
                self.append_output(f"{tr['SubtitleExtractorGUI']['OpenVideoSuccess']}：{video}")
            self.load_video(self.video_paths[0])

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