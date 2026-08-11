"""Pure geometry helpers for the screenshot module.

NO Qt imports here — these are pure functions over plain data so the multi-
display, multi-DPR logic can be tested without spinning up a QApplication.

Vocabulary
----------
- LOGICAL pixels: what Qt's geometry()/QScreen sees. With DPR=1.5, a 4K
  panel reports 2560x1440. Mouse events arrive in logical pixels.
- PHYSICAL pixels: what the framebuffer / output PNG should contain. The
  same panel's framebuffer is 3840x2160. Saving / clipboard / pin must use
  physical pixels to preserve detail.

The conversion between the two is always done relative to a specific screen
because Windows + Qt expose a non-uniform DPR per monitor (effectively
independent "islands" in logical coordinates).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence


@dataclass(frozen=True)
class ScreenInfo:
    """A snapshot of one monitor's geometry suitable for pure math.

    `logical_x/y/w/h` mirror `QScreen.geometry()` (device-independent).
    `dpr` mirrors `QScreen.devicePixelRatio()`.
    The physical rect is derived: width_px = round(w * dpr), and the
    physical origin equals the logical origin scaled by THIS screen's dpr
    (Windows + Qt 6 reports per-monitor DPR but uses logical-pixel origins;
    physical origins are not exposed in a portable way, so we treat each
    monitor as locally physical-relative to its own logical origin).
    """

    name: str
    logical_x: int
    logical_y: int
    logical_w: int
    logical_h: int
    dpr: float = 1.0
    is_primary: bool = False

    @property
    def logical_right(self) -> int:
        return self.logical_x + self.logical_w

    @property
    def logical_bottom(self) -> int:
        return self.logical_y + self.logical_h

    @property
    def physical_w(self) -> int:
        return int(round(self.logical_w * self.dpr))

    @property
    def physical_h(self) -> int:
        return int(round(self.logical_h * self.dpr))


@dataclass(frozen=True)
class SelectionRect:
    """A rectangular selection expressed in LOGICAL global pixels.

    Empty or zero-area selections are represented with w==0 or h==0.
    The selection may span multiple screens; per-screen clipping happens at
    capture time, not here.
    """

    x: int
    y: int
    w: int
    h: int

    @property
    def is_empty(self) -> bool:
        return self.w <= 0 or self.h <= 0

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h


def normalize_drag_rect(
    start_x: int, start_y: int, end_x: int, end_y: int
) -> SelectionRect:
    """Normalize an arbitrary drag (start, end) into a positive-area rect.

    Mouse drags can go up-left as well as down-right, producing negative
    width/height if expressed naively. Selections are always stored as
    (x, y, w, h) with w >= 0 and h >= 0.
    """

    x1, x2 = (start_x, end_x) if start_x <= end_x else (end_x, start_x)
    y1, y2 = (start_y, end_y) if start_y <= end_y else (end_y, start_y)
    return SelectionRect(x=x1, y=y1, w=x2 - x1, h=y2 - y1)


def screens_union_bounds(screens: Sequence[ScreenInfo]) -> SelectionRect:
    """Bounding rectangle covering all screens in logical pixels.

    Used to size the multi-screen overlay region. Returns an empty
    selection if `screens` is empty.
    """

    if not screens:
        return SelectionRect(0, 0, 0, 0)

    left = min(s.logical_x for s in screens)
    top = min(s.logical_y for s in screens)
    right = max(s.logical_right for s in screens)
    bottom = max(s.logical_bottom for s in screens)
    return SelectionRect(x=left, y=top, w=right - left, h=bottom - top)


def clamp_to_screen_union(
    rect: SelectionRect, screens: Sequence[ScreenInfo]
) -> SelectionRect:
    """Clip a selection to the union of screen rectangles.

    Qt high-DPI multi-monitor layouts can have gaps ("islands of screens").
    We intentionally clip to the bounding box of all screens rather than
    to each screen individually — the per-screen split happens in
    `split_by_screen`. This guards against selections that started off
    any screen (which Windows mouse capture sometimes reports).
    """

    if rect.is_empty or not screens:
        return rect

    bounds = screens_union_bounds(screens)
    x1 = max(rect.x, bounds.x)
    y1 = max(rect.y, bounds.y)
    x2 = min(rect.right, bounds.right)
    y2 = min(rect.bottom, bounds.bottom)
    if x2 <= x1 or y2 <= y1:
        return SelectionRect(rect.x, rect.y, 0, 0)
    return SelectionRect(x=x1, y=y1, w=x2 - x1, h=y2 - y1)


@dataclass(frozen=True)
class ScreenSlice:
    """A per-screen slice of a multi-screen selection.

    `screen` is the source monitor. `logical_local` is the selection
    expressed in that screen's local logical coords (origin at screen's
    top-left). `logical_global` keeps the original global coords so the
    caller can stitch slices back together.
    """

    screen: ScreenInfo
    logical_local: SelectionRect
    logical_global: SelectionRect


def split_by_screen(
    rect: SelectionRect, screens: Sequence[ScreenInfo]
) -> List[ScreenSlice]:
    """Return per-screen intersections of a global selection.

    For each screen, the intersection with `rect` is computed and translated
    into screen-local coords. Screens with no intersection are omitted.
    Output order matches `screens`.
    """

    out: List[ScreenSlice] = []
    if rect.is_empty:
        return out

    for s in screens:
        x1 = max(rect.x, s.logical_x)
        y1 = max(rect.y, s.logical_y)
        x2 = min(rect.right, s.logical_right)
        y2 = min(rect.bottom, s.logical_bottom)
        if x2 <= x1 or y2 <= y1:
            continue
        global_slice = SelectionRect(x=x1, y=y1, w=x2 - x1, h=y2 - y1)
        local_slice = SelectionRect(
            x=x1 - s.logical_x,
            y=y1 - s.logical_y,
            w=x2 - x1,
            h=y2 - y1,
        )
        out.append(
            ScreenSlice(
                screen=s, logical_local=local_slice, logical_global=global_slice
            )
        )
    return out


def logical_to_physical_rect(
    local_rect: SelectionRect, screen: ScreenInfo
) -> SelectionRect:
    """Convert a screen-local logical rect to physical pixels for that screen.

    Use this when stitching per-screen pixmaps into one combined physical-
    pixel output image. Coordinates are screen-relative (origin at screen
    top-left), since DPR is per-screen.
    """

    dpr = screen.dpr
    return SelectionRect(
        x=int(round(local_rect.x * dpr)),
        y=int(round(local_rect.y * dpr)),
        w=int(round(local_rect.w * dpr)),
        h=int(round(local_rect.h * dpr)),
    )


def physical_to_logical_point(px: int, py: int, screen: ScreenInfo) -> tuple[int, int]:
    """Convert a screen-local physical point to logical coordinates.

    Used when reporting the user's selection back in logical pixels so the
    pin window can re-appear at the same position in Qt's coordinate space.
    """

    dpr = screen.dpr if screen.dpr > 0 else 1.0
    return int(round(px / dpr)), int(round(py / dpr))


def place_toolbar(
    selection: SelectionRect,
    bar_w: int,
    bar_h: int,
    screens: Sequence[ScreenInfo],
    gap: int = 8,
) -> tuple[int, int]:
    """Return (x, y) in logical global pixels for the action toolbar.

    Strategy (Snipaste-style):
    1. Try BELOW the selection, aligned to its right edge.
    2. If that overflows the host screen, try ABOVE the selection.
    3. If neither fits, dock to the selection's INNER bottom-right.
    The host screen is the one containing the selection's bottom-right
    corner; if that point is in a gap, fall back to the union bounds.
    """

    if selection.is_empty:
        return selection.x, selection.y

    host: ScreenInfo | None = None
    if screens:
        for s in screens:
            if (
                s.logical_x <= selection.right <= s.logical_right
                and s.logical_y <= selection.bottom <= s.logical_bottom
            ):
                host = s
                break
        if host is None:
            for s in screens:
                if (
                    s.logical_x <= selection.x + selection.w // 2 < s.logical_right
                    and s.logical_y <= selection.y + selection.h // 2 < s.logical_bottom
                ):
                    host = s
                    break

    if host is not None:
        host_right = host.logical_right
        host_bottom = host.logical_bottom
        host_left = host.logical_x
        host_top = host.logical_y
    else:
        bounds = screens_union_bounds(screens)
        host_right = bounds.right
        host_bottom = bounds.bottom
        host_left = bounds.x
        host_top = bounds.y

    # Strategy 1: BELOW, right-aligned
    bx = min(selection.right - bar_w, host_right - bar_w)
    bx = max(bx, host_left)
    by = selection.bottom + gap
    if by + bar_h <= host_bottom:
        return bx, by

    # Strategy 2: ABOVE
    by = selection.y - bar_h - gap
    if by >= host_top:
        return bx, by

    # Strategy 3: inside, bottom-right — clamp to BOTH host edges so a
    # giant cross-screen selection can't push the toolbar past the right
    # or bottom of the host screen.
    by = max(host_top, min(selection.bottom - bar_h - gap, host_bottom - bar_h))
    bx = max(host_left, min(selection.right - bar_w - gap, host_right - bar_w))
    return bx, by
