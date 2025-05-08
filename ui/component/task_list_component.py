import os
from enum import Enum
from PySide6.QtWidgets import QWidget, QVBoxLayout, QMenu, QAbstractItemView, QTableWidgetItem, QHeaderView
from PySide6.QtCore import Qt, Signal, QModelIndex, QUrl
from qfluentwidgets import TableWidget, BodyLabel, FluentIcon, InfoBar, InfoBarPosition
from PySide6.QtGui import QAction, QColor, QBrush
from showinfm import show_in_file_manager

from backend.config import tr

class TaskStatus(Enum):
    PENDING = tr['TaskList']['Pending']
    PROCESSING = tr['TaskList']['Processing']
    COMPLETED = tr['TaskList']['Completed']
    FAILED = tr['TaskList']['Failed']

class TaskListComponent(QWidget):
    """任务列表组件"""
    
    # 定义信号
    task_selected = Signal(int, str)  # 任务被选中时发出信号，参数为任务索引和视频路径
    task_deleted = Signal(int)  # 任务被删除时发出信号，参数为任务索引
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("TaskListComponent")
        
        # 初始化变量
        self.tasks = []  # 存储任务列表
        self.current_task_index = -1  # 当前选中的任务索引
        
        # 创建布局
        self.__initWidget()
        
    def __initWidget(self):
        """初始化组件"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        # 创建表格
        self.table = TableWidget(self)
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels([tr['TaskList']['Name'], tr['TaskList']['Progress'], tr['TaskList']['Status']])
        
        # 设置表格样式
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        
        # 设置列宽模式
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)           # 名称列拉伸填充
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)  # 进度列自适应内容宽度
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)  # 状态列自适应内容宽度
        
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        
        # 连接信号
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_context_menu)
        self.table.clicked.connect(self.on_task_clicked)
        
        layout.addWidget(self.table)
        
    def add_task(self, video_path):
        """添加任务到列表
        
        Args:
            video_path: 视频文件路径
        """
        # 覆盖相同路径的任务
        for row, task in enumerate(self.tasks[:]):
            if task["path"] == video_path:
                self.delete_task(row)
                continue
                
        # 获取文件名
        file_name = os.path.basename(video_path)
        
        # 添加到任务列表
        task = {
            "path": video_path,
            "name": file_name,
            "progress": 0,
            "status": TaskStatus.PENDING
        }
        self.tasks.append(task)
        
        # 更新表格
        row = len(self.tasks) - 1
        self.table.setRowCount(len(self.tasks))
        
        item0 = QTableWidgetItem(file_name)
        item1 = QTableWidgetItem("0%")
        item2 = QTableWidgetItem(TaskStatus.PENDING.value)
        
        # 设置文件名单元格的省略模式为中间省略
        item0.setTextAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        item0.setToolTip(video_path)  # 设置完整路径为工具提示
        # 设置表格的文本省略模式
        self.table.setTextElideMode(Qt.ElideMiddle)
        
        item1.setTextAlignment(Qt.AlignCenter)
        item2.setTextAlignment(Qt.AlignCenter)
        
        self.table.setItem(row, 0, item0)
        self.table.setItem(row, 1, item1)
        self.table.setItem(row, 2, item2)
        
        # 滚动到最新添加的行
        self.table.scrollToBottom()
        return True
        
    def update_task_progress(self, index, progress, is_completed=False):
        """更新任务进度
        
        Args:
            index: 任务索引
            progress: 进度值(0-100)
            is_completed: 是否已完成
        """
        if 0 <= index < len(self.tasks):
            self.tasks[index]["progress"] = progress
            
            # 更新进度单元格
            progress_item = self.table.item(index, 1)
            if progress_item:
                progress_item.setText(f"{progress}%")
            
            # 如果已完成，更新状态
            if is_completed:
                self.update_task_status(index, TaskStatus.COMPLETED)
            elif progress > 0:
                self.update_task_status(index, TaskStatus.PROCESSING)
                
            # 如果是当前处理的任务，滚动到可见区域
            if index == self.current_task_index:
                self.table.scrollTo(self.table.model().index(index, 0))
                
    def update_task_status(self, index, status):
        """更新任务状态
        
        Args:
            index: 任务索引
            status: 任务状态
        """
        if 0 <= index < len(self.tasks):
            self.tasks[index]["status"] = status
            status_item = self.table.item(index, 2)
            if status_item:
                status_item.setText(status.value)
                
                # 根据状态设置不同颜色
                if status == TaskStatus.COMPLETED:
                    status_item.setForeground(QBrush(QColor("#2ecc71")))  # 绿色
                elif status == TaskStatus.PROCESSING:
                    status_item.setForeground(QBrush(QColor("#3498db")))  # 蓝色
                elif status == TaskStatus.FAILED:
                    status_item.setForeground(QBrush(QColor("#e74c3c")))  # 红色
            
            # 如果是当前处理的任务，滚动到可见区域
            if index == self.current_task_index:
                self.table.scrollTo(self.table.model().index(index, 0))
                
            # 选中当前行
            self.table.selectRow(index)
    
    def get_pending_tasks(self):
        """获取所有待处理的任务
        
        Returns:
            list: 待处理任务列表，每项为 (索引, 路径) 元组
        """
        return [(i, task["path"]) for i, task in enumerate(self.tasks) if task["status"] == TaskStatus.PENDING]
    
    def get_all_tasks(self):
        """获取所有任务
        
        Returns:
            list: 所有任务列表
        """
        return self.tasks
        
    def show_context_menu(self, pos):
        """显示右键菜单
        
        Args:
            pos: 鼠标位置
        """
        index = self.table.indexAt(pos)
        if index.isValid():
            menu = QMenu(self)
            
            # 打开视频文件位置
            open_video_location_action = QAction(tr['TaskList']['OpenVideoLocation'], self)
            open_video_location_action.triggered.connect(lambda: self.open_file_location(index.row(), is_subtitle=False))
            menu.addAction(open_video_location_action)
            
            # 打开字幕文件位置（仅当任务已完成时可用）
            open_subtitle_location_action = QAction(tr['TaskList']['OpenSubtitleLocation'], self)
            open_subtitle_location_action.triggered.connect(lambda: self.open_file_location(index.row(), is_subtitle=True))
            menu.addAction(open_subtitle_location_action)
            
            # 删除任务
            delete_action = QAction(tr['TaskList']['DeleteTask'], self)
            delete_action.triggered.connect(lambda: self.delete_task(index.row()))
            menu.addAction(delete_action)
            
            # 显示菜单
            menu.exec_(self.table.viewport().mapToGlobal(pos))
    
    def delete_task(self, row):
        """删除任务
        
        Args:
            row: 行索引
        """
        if 0 <= row < len(self.tasks):
            # 从列表中删除
            del self.tasks[row]
            
            # 从表格中删除
            self.table.removeRow(row)
                
            # 如果删除的是当前任务，重置当前任务索引
            if row == self.current_task_index:
                self.current_task_index = -1
                
            # 发出任务删除信号
            self.task_deleted.emit(row)
    
    def on_task_clicked(self, index):
        """任务被点击时的处理
        
        Args:
            index: 索引
        """
        row = index.row()
        if 0 <= row < len(self.tasks):
            self.current_task_index = row
            # 发出信号，通知外部加载对应视频
            self.task_selected.emit(row, self.tasks[row]["path"])
            
    def set_current_task(self, index):
        """设置当前处理的任务
        
        Args:
            index: 任务索引
        """
        if 0 <= index < len(self.tasks):
            self.current_task_index = index
            self.table.selectRow(index)
            self.table.scrollTo(self.table.model().index(index, 0))
            
    def select_task(self, index):
        """选中指定任务
        
        Args:
            index: 任务索引
        """
        self.set_current_task(index)

    def open_file_location(self, row, is_subtitle=False):
        """打开文件所在位置
        
        Args:
            row: 行索引
            is_subtitle: 是否为字幕文件
        """
        if 0 <= row < len(self.tasks):
            video_path = self.tasks[row]["path"]
            
            if is_subtitle:
                # 如果任务未完成，显示提示
                if self.tasks[row]["status"] != TaskStatus.COMPLETED:
                    InfoBar.warning(
                        title=tr['TaskList']['Warning'],
                        content=tr['TaskList']['SubtitleNotFound'],
                        parent=self.get_root_parent(),
                        duration=3000
                    )
                    return
                    
                # 获取字幕文件路径
                subtitle_path = os.path.splitext(video_path)[0] + '.srt'
                
                path = subtitle_path
            else:
                path = video_path
                
            # 检查视频文件是否存在
            if not os.path.exists(path):
                InfoBar.warning(
                    title=tr['TaskList']['Warning'],
                    content=tr['TaskList']['UnableToLocateFile'],
                    parent=self.get_root_parent(),
                    duration=3000
                )
                return
                
            show_in_file_manager(os.path.abspath(path))

    def get_root_parent(self):
        parent = self
        while parent.parent():
            parent = parent.parent()
        return parent