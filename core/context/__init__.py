"""
Context Module
==============
Window-level context awareness for Aria.
"""

from .screen_context import ScreenContext, AppCategoryDetector

__all__ = ["ScreenContext", "AppCategoryDetector"]
