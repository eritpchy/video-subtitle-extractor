from enum import Enum
from pathlib import Path

from qfluentwidgets import getIconColor, Theme, FluentIconBase


class MyFluentIcon(FluentIconBase, Enum):
    Stop = "stop"

    def path(self, theme=Theme.AUTO):
        # getIconColor() return "white" or "black" according to current theme
        return str(Path(__file__).with_name(f'{self.value}_{getIconColor(theme)}.svg'))
