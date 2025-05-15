import cv2
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QSizePolicy
from PySide6.QtCore import Qt, Signal, QRect, QRectF, QTimer, QObject, QEvent
from PySide6 import QtCore, QtWidgets, QtGui
from qfluentwidgets import qconfig, CardWidget, HollowHandleStyle

from backend.config import config, tr

class VideoDisplayComponent(QWidget):
    """视频显示组件，包含视频预览和选择框功能"""
    
    # 定义信号
    selection_changed = Signal(QRect)  # 选择框变化信号
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent = parent
        
        # 初始化变量
        self.is_drawing = False
        self.selection_rect = QRect()
        self.drag_start_pos = None
        self.resize_edge = None
        self.edge_size = 10  # 调整大小的边缘区域
        self.enable_mouse_events = True  # 控制是否启用鼠标事件
        
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
            
        # 视频相关参数
        self.frame_width = None
        self.frame_height = None
        self.scaled_width = None
        self.scaled_height = None
        self.border_left = 0
        self.border_top = 0
        
        # 保存选择框的相对位置和大小（相对于实际视频的比例）
        self.selection_ratio = None

        self.__initWidget()
        
    def __initWidget(self):
        """初始化组件"""
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(0)
        main_layout.setContentsMargins(0, 0, 0, 0)
        
        # 视频预览区域和进度条容器
        self.video_container = CardWidget(self)
        self.video_container.setObjectName('videoContainer')
        video_layout = QVBoxLayout()
        video_layout.setSpacing(0)
        video_layout.setContentsMargins(2, 2, 2, 2)
        video_layout.setAlignment(Qt.AlignCenter)
        
        # 创建内部黑色背景容器
        self.black_container = QWidget(self)
        self.black_container.setObjectName('blackContainer')
        self.black_container.setStyleSheet("""
            #blackContainer {
                background-color: black;
                border-radius: 10px;
                border: 0px solid transparent;
            }
        """)
        black_layout = QVBoxLayout()
        black_layout.setContentsMargins(0, 0, 0, 0)
        black_layout.setSpacing(0)
        black_layout.setAlignment(Qt.AlignCenter)
        
        # 视频显示标签
        self.video_display = QtWidgets.QLabel()
        self.video_display.setStyleSheet("""
            background-color: black;
            border-top-left-radius: 10px;
            border-top-right-radius: 10px;
            border: 0px solid transparent;
        """)
        self.video_display.setMinimumSize(self.video_preview_width, self.video_preview_height)
        
        self.video_display.setMouseTracking(True)
        self.video_display.setScaledContents(True)
        self.video_display.setAlignment(Qt.AlignCenter)
        self.video_display.mousePressEvent = self.selection_mouse_press
        self.video_display.mouseMoveEvent = self.selection_mouse_move
        self.video_display.mouseReleaseEvent = self.selection_mouse_release
        
        # 视频滑块
        self.video_slider = QtWidgets.QSlider(Qt.Horizontal)
        self.video_slider.setMinimum(1)
        self.video_slider.setFixedHeight(22)
        self.video_slider.setMaximum(100)  # 默认最大值设为100，与进度百分比一致
        self.video_slider.setValue(1)
        self.video_slider.setStyle(HollowHandleStyle({
            "handle.color": QtGui.QColor(255, 255, 255),
            "handle.ring-width": 4,
            "handle.hollow-radius": 6,
            "handle.margin": 1
        }))
        
        # 视频预览区域
        self.video_display.setObjectName('videoDisplay')
        # black_layout.addWidget(self.video_display, 0, Qt.AlignCenter)
        # 创建一个容器来保持比例
        ratio_container = QWidget()
        ratio_layout = QVBoxLayout(ratio_container)
        ratio_layout.setContentsMargins(0, 0, 0, 0)
        ratio_layout.addWidget(self.video_display)

        # 设置固定的宽高比
        ratio_container.setFixedHeight(ratio_container.width() * 9 // 16)
        ratio_container.setMinimumWidth(self.video_preview_width)

        # 添加到布局
        black_layout.addWidget(ratio_container)

        # 添加一个事件过滤器来处理大小变化
        class RatioEventFilter(QObject):
            def eventFilter(self, obj, event):
                if event.type() == QEvent.Resize:
                    obj.setFixedHeight(obj.width() * 9 // 16)
                return False

        ratio_filter = RatioEventFilter(ratio_container)
        ratio_container.installEventFilter(ratio_filter)

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
        
        self.black_container.setLayout(black_layout)
        video_layout.addWidget(self.black_container)
        self.video_container.setLayout(video_layout)
        main_layout.addWidget(self.video_container)
    
    def update_video_display(self, frame, draw_selection=True):
        """更新视频显示"""
        if frame is None:
            return

        # 调整视频帧大小以适应视频预览区域
        frame = cv2.resize(frame, (self.video_preview_width, self.video_preview_height))
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
        painter.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
        
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
        
        # 保存当前的pixmap用于绘制选择框
        self.current_pixmap = rounded_pix.copy()
        
        self.video_display.setPixmap(rounded_pix)
        
        # 如果有保存的选择框比例，根据新视频尺寸重新计算选择框
        if draw_selection and self.selection_ratio is not None and self.scaled_width and self.scaled_height:
            x_ratio, y_ratio, w_ratio, h_ratio = self.selection_ratio
            
            # 计算新的选择框坐标和大小
            x = int(x_ratio * self.scaled_width) + self.border_left
            y = int(y_ratio * self.scaled_height) + self.border_top
            w = int(w_ratio * self.scaled_width)
            h = int(h_ratio * self.scaled_height)
            
            # 创建新的选择框
            self.selection_rect = QRect(x, y, w, h)
            
            # 更新视频显示
            self.update_preview_with_rect()
    
    def update_preview_with_rect(self, rect=None):
        """更新带有选择框的预览"""
        if not hasattr(self, 'current_pixmap') or self.current_pixmap is None:
            return
            
        # 如果提供了新的矩形，使用它
        if rect is not None:
            self.selection_rect = rect
            
        # 创建一个副本用于绘制
        pixmap_copy = self.current_pixmap.copy()
        painter = QtGui.QPainter(pixmap_copy)
        
        # 设置选择框样式
        pen = QtGui.QPen(QtGui.QColor(0, 255, 0))  # 绿色
        pen.setWidth(2)
        painter.setPen(pen)
        
        # 绘制选择框
        painter.drawRect(self.selection_rect)
        painter.end()
        
        # 更新显示
        self.video_display.setPixmap(pixmap_copy)
    
    def selection_mouse_press(self, event):
        """鼠标按下事件处理"""
        if not self.enable_mouse_events:
            return
        pos = event.pos()

        # 检测双击或三击，重置选择框
        if event.type() == QtCore.QEvent.MouseButtonDblClick:
            self.selection_rect = QRect(pos, pos)
            self.resize_edge = None
            self.is_drawing = True
            self.drag_start_pos = pos
            return
        
        # 检查是否在选择框边缘（用于调整大小）
        if self.selection_rect.isValid():
            # 右下角
            if abs(pos.x() - self.selection_rect.right()) <= self.edge_size and abs(pos.y() - self.selection_rect.bottom()) <= self.edge_size:
                self.resize_edge = "bottomright"
                self.drag_start_pos = pos
                return
            # 右上角
            elif abs(pos.x() - self.selection_rect.right()) <= self.edge_size and abs(pos.y() - self.selection_rect.top()) <= self.edge_size:
                self.resize_edge = "topright"
                self.drag_start_pos = pos
                return
            # 左下角
            elif abs(pos.x() - self.selection_rect.left()) <= self.edge_size and abs(pos.y() - self.selection_rect.bottom()) <= self.edge_size:
                self.resize_edge = "bottomleft"
                self.drag_start_pos = pos
                return
            # 左边缘
            elif abs(pos.x() - self.selection_rect.left()) <= self.edge_size and self.selection_rect.top() <= pos.y() <= self.selection_rect.bottom():
                self.resize_edge = "left"
                self.drag_start_pos = pos
                return
            # 右边缘
            elif abs(pos.x() - self.selection_rect.right()) <= self.edge_size and self.selection_rect.top() <= pos.y() <= self.selection_rect.bottom():
                self.resize_edge = "right"
                self.drag_start_pos = pos
                return
            # 上边缘
            elif abs(pos.y() - self.selection_rect.top()) <= self.edge_size and self.selection_rect.left() <= pos.x() <= self.selection_rect.right():
                self.resize_edge = "top"
                self.drag_start_pos = pos
                return
            # 下边缘
            elif abs(pos.y() - self.selection_rect.bottom()) <= self.edge_size and self.selection_rect.left() <= pos.x() <= self.selection_rect.right():
                self.resize_edge = "bottom"
                self.drag_start_pos = pos
                return
            # 左上角
            elif abs(pos.x() - self.selection_rect.left()) <= self.edge_size and abs(pos.y() - self.selection_rect.top()) <= self.edge_size:
                self.resize_edge = "topleft"
                self.drag_start_pos = pos
                return
            # 在选择框内部（用于移动）
            elif self.selection_rect.contains(pos):
                self.resize_edge = "move"
                self.drag_start_pos = pos
                return
        
        # 开始新的选择
        self.is_drawing = True
        self.selection_rect = QRect(pos, pos)
        self.drag_start_pos = pos
        self.resize_edge = None
    
    def selection_mouse_move(self, event):
        """鼠标移动事件处理"""
        if not self.enable_mouse_events:
            return
        pos = event.pos()
        
        # 根据不同的操作模式处理鼠标移动
        if self.is_drawing:  # 绘制新选择框
            self.selection_rect.setBottomRight(pos)
            self.update_preview_with_rect()
        elif self.resize_edge:  # 调整选择框大小或位置
            if self.resize_edge == "move":
                # 移动整个选择框
                dx = pos.x() - self.drag_start_pos.x()
                dy = pos.y() - self.drag_start_pos.y()
                
                # 保存原始选择框尺寸
                original_width = self.selection_rect.width()
                original_height = self.selection_rect.height()
                
                # 计算新位置
                new_rect = self.selection_rect.translated(dx, dy)
                
                # 获取视频显示区域
                display_rect = self.video_display.rect()
                
                # 检查是否超出边界，如果超出则调整位置但保持尺寸
                if new_rect.left() < 0:
                    new_rect.moveLeft(0)
                if new_rect.top() < 0:
                    new_rect.moveTop(0)
                if new_rect.right() > display_rect.width():
                    new_rect.moveRight(display_rect.width())
                if new_rect.bottom() > display_rect.height():
                    new_rect.moveBottom(display_rect.height())
                    
                # 确保尺寸不变
                if new_rect.width() != original_width or new_rect.height() != original_height:
                    # 如果尺寸变了，恢复原始尺寸
                    if new_rect.left() == 0:
                        new_rect.setWidth(original_width)
                    if new_rect.top() == 0:
                        new_rect.setHeight(original_height)
                    if new_rect.right() == display_rect.width():
                        new_rect.setLeft(new_rect.right() - original_width)
                    if new_rect.bottom() == display_rect.height():
                        new_rect.setTop(new_rect.bottom() - original_height)
                    
                self.selection_rect = new_rect
                self.drag_start_pos = pos
            else:
                # 调整选择框大小
                if "left" in self.resize_edge:
                    self.selection_rect.setLeft(pos.x())
                if "right" in self.resize_edge:
                    self.selection_rect.setRight(pos.x())
                if "top" in self.resize_edge:
                    self.selection_rect.setTop(pos.y())
                if "bottom" in self.resize_edge:
                    self.selection_rect.setBottom(pos.y())
                
                # 确保选择框在视频显示区域内
                display_rect = self.video_display.rect()
                if self.selection_rect.left() < 0:
                    self.selection_rect.setLeft(0)
                if self.selection_rect.top() < 0:
                    self.selection_rect.setTop(0)
                if self.selection_rect.right() > display_rect.width():
                    self.selection_rect.setRight(display_rect.width())
                if self.selection_rect.bottom() > display_rect.height():
                    self.selection_rect.setBottom(display_rect.height())
                    
            self.update_preview_with_rect()
        else:
            # 更新鼠标指针形状
            self.update_cursor_shape(pos)
    
    def selection_mouse_release(self, event):
        """鼠标释放事件处理"""
        if not self.enable_mouse_events:
            return
        # 结束绘制或调整
        self.is_drawing = False
        self.resize_edge = None
        
        # 标准化选择框（确保宽度和高度为正）
        self.selection_rect = self.selection_rect.normalized()
        
        # 保存选择框的相对位置和大小
        self.save_selection_ratio()
        
        # 发送选择框变化信号
        self.selection_changed.emit(self.selection_rect)
        
    def update_cursor_shape(self, pos):
        """根据鼠标位置更新光标形状"""
        if not self.selection_rect.isValid():
            self.video_display.setCursor(Qt.ArrowCursor)
            return
            
        # 检查鼠标是否在选择框边缘
        if (abs(pos.x() - self.selection_rect.left()) <= self.edge_size and 
            self.selection_rect.top() <= pos.y() <= self.selection_rect.bottom()):
            self.video_display.setCursor(Qt.SizeHorCursor)
        elif (abs(pos.x() - self.selection_rect.right()) <= self.edge_size and 
              self.selection_rect.top() <= pos.y() <= self.selection_rect.bottom()):
            self.video_display.setCursor(Qt.SizeHorCursor)
        elif (abs(pos.y() - self.selection_rect.top()) <= self.edge_size and 
              self.selection_rect.left() <= pos.x() <= self.selection_rect.right()):
            self.video_display.setCursor(Qt.SizeVerCursor)
        elif (abs(pos.y() - self.selection_rect.bottom()) <= self.edge_size and 
              self.selection_rect.left() <= pos.x() <= self.selection_rect.right()):
            self.video_display.setCursor(Qt.SizeVerCursor)
        elif (abs(pos.x() - self.selection_rect.left()) <= self.edge_size and 
              abs(pos.y() - self.selection_rect.top()) <= self.edge_size):
            self.video_display.setCursor(Qt.SizeFDiagCursor)
        elif (abs(pos.x() - self.selection_rect.right()) <= self.edge_size and 
              abs(pos.y() - self.selection_rect.top()) <= self.edge_size):
            self.video_display.setCursor(Qt.SizeBDiagCursor)
        elif (abs(pos.x() - self.selection_rect.left()) <= self.edge_size and 
              abs(pos.y() - self.selection_rect.bottom()) <= self.edge_size):
            self.video_display.setCursor(Qt.SizeBDiagCursor)
        elif (abs(pos.x() - self.selection_rect.right()) <= self.edge_size and 
              abs(pos.y() - self.selection_rect.bottom()) <= self.edge_size):
            self.video_display.setCursor(Qt.SizeFDiagCursor)
        elif self.selection_rect.contains(pos):
            self.video_display.setCursor(Qt.SizeAllCursor)
        else:
            self.video_display.setCursor(Qt.ArrowCursor)
    
    def set_video_parameters(self, frame_width, frame_height, scaled_width=None, scaled_height=None, border_left=0, border_top=0):
        """设置视频参数"""
        self.frame_width = frame_width
        self.frame_height = frame_height
        self.scaled_width = scaled_width
        self.scaled_height = scaled_height
        self.border_left = border_left
        self.border_top = border_top
    
    def get_selection_coordinates(self):
        """获取选择框坐标"""
        return self.selection_rect
    
    def set_selection_rect(self, rect):
        """设置选择框"""
        self.selection_rect = rect
        self.save_selection_ratio()
        self.update_preview_with_rect()
    
    def load_selection_ratio(self):
        """从配置中加载选择框的相对位置和大小"""
        # 检查是否有有效的视频尺寸
        if not self.scaled_width or not self.scaled_height:
            return False
            
        # 从配置中读取选择框的相对位置和大小
        x_ratio = config.subtitleSelectionAreaX.value
        y_ratio = config.subtitleSelectionAreaY.value
        w_ratio = config.subtitleSelectionAreaW.value
        h_ratio = config.subtitleSelectionAreaH.value
        
        # 检查配置值是否有效
        if x_ratio is None or y_ratio is None or w_ratio is None or h_ratio is None:
            return False

        # 检查配置值是否在有效范围内
        if w_ratio <= 0.01 or h_ratio <= 0.005:
            config.set(config.subtitleSelectionAreaX, config.subtitleSelectionAreaX.defaultValue)
            config.set(config.subtitleSelectionAreaY, config.subtitleSelectionAreaY.defaultValue)
            config.set(config.subtitleSelectionAreaW, config.subtitleSelectionAreaW.defaultValue)
            config.set(config.subtitleSelectionAreaH, config.subtitleSelectionAreaH.defaultValue)
            x_ratio = config.subtitleSelectionAreaX.value
            y_ratio = config.subtitleSelectionAreaY.value
            w_ratio = config.subtitleSelectionAreaW.value
            h_ratio = config.subtitleSelectionAreaH.value

        # 保存选择框比例
        self.selection_ratio = (x_ratio, y_ratio, w_ratio, h_ratio)
        
        # 计算实际像素坐标
        x = int(x_ratio * self.scaled_width) + self.border_left
        y = int(y_ratio * self.scaled_height) + self.border_top
        w = int(w_ratio * self.scaled_width)
        h = int(h_ratio * self.scaled_height)
        
        # 创建选择框
        self.selection_rect = QRect(x, y, w, h)
        
        # 更新预览
        self.update_preview_with_rect()
        
        return True
        
    def save_selection_ratio(self):
        """保存选择框的相对位置和大小（相对于实际视频的比例）"""
        if not self.selection_rect.isValid() or not self.scaled_width or not self.scaled_height:
            return
            
        # 调整选择框坐标，考虑黑边偏移
        x_adjusted = max(0, self.selection_rect.x() - self.border_left)
        y_adjusted = max(0, self.selection_rect.y() - self.border_top)
        
        # 如果选择框超出了实际视频区域，需要调整宽度和高度
        w_adjusted = min(self.selection_rect.width(), self.scaled_width - x_adjusted)
        h_adjusted = min(self.selection_rect.height(), self.scaled_height - y_adjusted)
        
        # 转换为相对比例
        x_ratio = x_adjusted / self.scaled_width
        y_ratio = y_adjusted / self.scaled_height
        w_ratio = w_adjusted / self.scaled_width
        h_ratio = h_adjusted / self.scaled_height
        
        self.selection_ratio = (x_ratio, y_ratio, w_ratio, h_ratio)
    
        config.subtitleSelectionAreaY.value = y_ratio
        config.subtitleSelectionAreaH.value = h_ratio
        config.subtitleSelectionAreaX.value = x_ratio
        config.subtitleSelectionAreaW.value = w_ratio
        
        qconfig.save()
    
    def get_original_coordinates(self):
        """获取选择框在原始视频中的坐标"""
        if not self.selection_rect.isValid() or not self.scaled_width or not self.scaled_height:
            return None
            
        # 调整选择框坐标，考虑黑边偏移
        x_adjusted = max(0, self.selection_rect.x() - self.border_left)
        y_adjusted = max(0, self.selection_rect.y() - self.border_top)
        
        # 如果选择框超出了实际视频区域，需要调整宽度和高度
        w_adjusted = min(self.selection_rect.width(), self.scaled_width - x_adjusted)
        h_adjusted = min(self.selection_rect.height(), self.scaled_height - y_adjusted)
        
        # 转换为原始视频坐标
        scale_x = self.frame_width / self.scaled_width
        scale_y = self.frame_height / self.scaled_height
        
        xmin = int(x_adjusted * scale_x)
        xmax = int((x_adjusted + w_adjusted) * scale_x)
        ymin = int(y_adjusted * scale_y)
        ymax = int((y_adjusted + h_adjusted) * scale_y)
        
        # 确保坐标在有效范围内
        xmin = max(0, min(xmin, self.frame_width))
        xmax = max(0, min(xmax, self.frame_width))
        ymin = max(0, min(ymin, self.frame_height))
        ymax = max(0, min(ymax, self.frame_height))
        
        return (ymin, ymax, xmin, xmax)

    def set_dragger_enabled(self, enabled):
        """设置拖动器是否可用"""
        self.enable_mouse_events = enabled
        self.video_display.setMouseTracking(enabled)
        self.video_display.setCursor(Qt.ArrowCursor)