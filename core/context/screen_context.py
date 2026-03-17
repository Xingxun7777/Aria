"""
Screen Context Module
=====================
Detects foreground window info and categorizes applications.
Provides runtime context for AI Polish prompt injection.
"""

from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class ScreenContext:
    """Runtime screen context data (not persisted)."""

    window_title: str = ""
    process_name: str = ""  # e.g., "WeChat.exe"
    app_category: str = "通用"  # e.g., "聊天", "编程", "邮件"


class AppCategoryDetector:
    """Detect application category from process name."""

    # Built-in category mappings
    DEFAULT_CATEGORY_MAP: Dict[str, str] = {
        # 聊天
        "WeChat.exe": "聊天",
        "QQ.exe": "聊天",
        "Telegram.exe": "聊天",
        "Discord.exe": "聊天",
        "Slack.exe": "聊天",
        "Teams.exe": "聊天",
        "ms-teams.exe": "聊天",
        # 编程
        "Code.exe": "编程",
        "devenv.exe": "编程",
        "pycharm64.exe": "编程",
        "idea64.exe": "编程",
        "webstorm64.exe": "编程",
        "cursor.exe": "编程",
        # 邮件
        "OUTLOOK.EXE": "邮件",
        "Thunderbird.exe": "邮件",
        # 浏览器
        "chrome.exe": "浏览器",
        "msedge.exe": "浏览器",
        "firefox.exe": "浏览器",
        "brave.exe": "浏览器",
        # 文档
        "WINWORD.EXE": "文档",
        "EXCEL.EXE": "表格",
        "POWERPNT.EXE": "演示",
        # 笔记
        "Obsidian.exe": "笔记",
        "Notion.exe": "笔记",
        "Typora.exe": "笔记",
        # 终端
        "WindowsTerminal.exe": "终端",
        "cmd.exe": "终端",
        "powershell.exe": "终端",
        "pwsh.exe": "终端",
        # 设计
        "Photoshop.exe": "设计",
        "blender.exe": "3D建模",
        "krita.exe": "绘画",
    }

    def __init__(self, user_overrides: Optional[Dict[str, str]] = None):
        """Initialize with optional user-defined category overrides.

        Args:
            user_overrides: Dict mapping process_name -> category from hotwords.json
        """
        self._category_map = dict(self.DEFAULT_CATEGORY_MAP)
        if user_overrides:
            self._category_map.update(user_overrides)

    @classmethod
    def detect(
        cls,
        process_name: str,
        user_overrides: Optional[Dict[str, str]] = None,
    ) -> str:
        """Detect app category from process name.

        Args:
            process_name: e.g., "WeChat.exe"
            user_overrides: Optional user-defined overrides from config

        Returns:
            Category string, e.g., "聊天", "编程", defaults to "通用"
        """
        if not process_name:
            return "通用"

        # Check user overrides first (case-insensitive)
        if user_overrides:
            for key, val in user_overrides.items():
                if key.lower() == process_name.lower():
                    return val

        # Check built-in map (case-insensitive)
        for key, val in cls.DEFAULT_CATEGORY_MAP.items():
            if key.lower() == process_name.lower():
                return val

        return "通用"
