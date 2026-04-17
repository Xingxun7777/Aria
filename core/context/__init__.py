"""
Context Module
==============
Window-level context awareness for Aria v1.2.
"""

from .screen_context import ScreenContext, AppCategoryDetector

__all__ = ["ScreenContext", "AppCategoryDetector"]
