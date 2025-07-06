import os
from enum import Enum

from qfluentwidgets import getIconColor, Theme, FluentIconBase


class MyFluentIcon(FluentIconBase, Enum):
    Stop = "stop"

    def path(self, theme=Theme.AUTO):
        # getIconColor() return "white" or "black" according to current theme
        return os.path.abspath(os.path.join(os.path.dirname(__file__), f'{self.value}_{getIconColor(theme)}.svg'))
