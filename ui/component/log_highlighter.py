"""Semantic log colors that also update text already on screen."""

from PySide6.QtGui import QColor, QSyntaxHighlighter, QTextCharFormat
from qfluentwidgets import isDarkTheme, qconfig


class LogHighlighter(QSyntaxHighlighter):
    def __init__(self, document):
        super().__init__(document)
        qconfig.themeChangedFinished.connect(self.rehighlight)

    def highlightBlock(self, text):
        colors = None
        if any(token in text for token in ('【完成】', '【结束】', '成功打开视频')):
            colors = ('#16753b', '#6ccb8c')
        elif '【处理中】' in text:
            colors = ('#1565a0', '#70b7ee')
        elif '【警告】' in text or '【重要提示】' in text:
            colors = ('#8a5700', '#ffd166')
        elif text.startswith('---'):
            colors = ('#626970', '#aab5c4')
        elif any(token in text for token in ('无法打开视频', '请先打开视频', '错误', 'Error')):
            colors = ('#c42b1c', '#ff998f')
        elif text.startswith('  '):
            colors = ('#5f6368', '#b9c1cb')
        if colors:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(colors[isDarkTheme()]))
            # QTextDocument positions use UTF-16, including two units per emoji.
            self.setFormat(0, len(text.encode('utf-16-le')) // 2, fmt)
