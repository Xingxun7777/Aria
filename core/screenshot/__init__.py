"""Screenshot module for Aria.

Three voice-triggered capture flows:
- Full screen (primary screen → file + clipboard)
- Region selection (drag-to-select overlay → file + clipboard or pin)
- Pin (floating always-on-top window of the cropped image)
"""

from .geometry import (
    ScreenInfo,
    SelectionRect,
    normalize_drag_rect,
    clamp_to_screen_union,
    place_toolbar,
    logical_to_physical_rect,
    physical_to_logical_point,
)
from .saver import ScreenshotSaver
from .capture import CaptureEngine

__all__ = [
    "ScreenInfo",
    "SelectionRect",
    "normalize_drag_rect",
    "clamp_to_screen_union",
    "place_toolbar",
    "logical_to_physical_rect",
    "physical_to_logical_point",
    "ScreenshotSaver",
    "CaptureEngine",
]
