"""Region selection overlay (Snipaste-style) for screenshot capture.

Design choices:
- ONE QWidget per physical screen (not a super-rect spanning all). Qt high-
  DPI with mixed monitor positions has "islands of screens" gaps that
  would distort a single super-rect.
- A shared `SelectionState` (plain Python object) is shared across all
  overlay widgets. Mouse events on any widget update the shared state in
  GLOBAL logical coords. Each widget repaints its own intersection.
- BACKGROUND IS FROZEN: before showing the overlay, each screen is
  captured into a QImage and rendered as the overlay's background. The
  user drags on a static snapshot — no live desktop flicker.
- Toolbar (✓ / ⟲ / 📌 / ✕) appears on the screen containing the
  selection's bottom-right corner, placed by `geometry.place_toolbar`.
- Keyboard: ESC = cancel, Enter = confirm, arrow = 1px adjust the
  selection's bottom-right corner, Shift+arrow = 10px.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import QPushButton, QWidget, QHBoxLayout

from aria.core.screenshot.geometry import (
    ScreenInfo,
    SelectionRect,
    normalize_drag_rect,
    place_toolbar,
    split_by_screen,
    logical_to_physical_rect,
)


# ---------------------------------------------------------------------------
# Shared state across per-screen widgets
# ---------------------------------------------------------------------------


@dataclass
class SelectionState:
    """Shared mutable selection state across all overlay windows.

    `selection` is in GLOBAL logical pixels. `is_dragging` tracks whether
    the user is currently mid-drag (left mouse button down).
    """

    selection: SelectionRect = field(default_factory=lambda: SelectionRect(0, 0, 0, 0))
    drag_start: Optional[QPoint] = None
    has_selection: bool = False  # True after the user releases the mouse


# ---------------------------------------------------------------------------
# Toolbar widget — the floating ✓ ⟲ 📌 ✕ bar
# ---------------------------------------------------------------------------


class SelectionToolbar(QWidget):
    """Small frameless toolbar floated next to the selection."""

    confirmed = Signal()
    pinned = Signal()
    cancelled = Signal()
    reselected = Signal()

    BAR_W = 196
    BAR_H = 40

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(self.BAR_W, self.BAR_H)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)

        # ⟲ Reselect
        btn_reselect = QPushButton("⟲")
        btn_reselect.setToolTip("重新选取 (Esc 取消整个流程)")
        btn_reselect.clicked.connect(self.reselected.emit)
        # 📌 Pin
        btn_pin = QPushButton("📌")
        btn_pin.setToolTip("贴图到桌面 (置顶)")
        btn_pin.clicked.connect(self.pinned.emit)
        # ✕ Cancel
        btn_cancel = QPushButton("✕")
        btn_cancel.setToolTip("取消")
        btn_cancel.clicked.connect(self.cancelled.emit)
        # ✓ Confirm (rightmost / primary action)
        btn_confirm = QPushButton("✓")
        btn_confirm.setToolTip("保存并复制 (Enter)")
        btn_confirm.setDefault(True)
        btn_confirm.clicked.connect(self.confirmed.emit)

        for btn in (btn_reselect, btn_pin, btn_cancel, btn_confirm):
            btn.setFixedSize(40, 32)
            btn.setStyleSheet(
                "QPushButton {"
                " background: rgba(40,40,40,235);"
                " color: white;"
                " border: 1px solid rgba(255,255,255,40);"
                " border-radius: 4px;"
                " font-size: 16px;"
                "}"
                "QPushButton:hover { background: rgba(70,70,70,250); }"
                "QPushButton:pressed { background: rgba(20,120,200,250); }"
            )
            layout.addWidget(btn)


# ---------------------------------------------------------------------------
# Per-screen overlay
# ---------------------------------------------------------------------------


class ScreenOverlay(QWidget):
    """A frameless full-screen overlay covering ONE screen.

    Renders the frozen background, the dim layer, the selection clear-area
    and its border, and a coordinate readout near the cursor. Mouse events
    are translated to global logical coords and routed back to the
    OverlayManager via callbacks.
    """

    def __init__(
        self,
        screen_info: ScreenInfo,
        background: QImage,
        state: SelectionState,
        on_drag_start: Callable[[QPoint], None],
        on_drag_move: Callable[[QPoint], None],
        on_drag_end: Callable[[QPoint], None],
        on_cancel: Callable[[], None],
        on_confirm: Callable[[], None],
        on_key_nudge: Callable[[int, int, bool], None],
    ):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.BypassWindowManagerHint,
        )
        self._screen_info = screen_info
        self._background = background  # frozen capture, physical-pixel QImage
        self._state = state
        self._on_drag_start = on_drag_start
        self._on_drag_move = on_drag_move
        self._on_drag_end = on_drag_end
        self._on_cancel = on_cancel
        self._on_confirm = on_confirm
        self._on_key_nudge = on_key_nudge

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.setGeometry(
            screen_info.logical_x,
            screen_info.logical_y,
            screen_info.logical_w,
            screen_info.logical_h,
        )
        # Make sure the widget is created on the right screen
        for s in QGuiApplication.screens():
            if s.name() == screen_info.name:
                self.windowHandle and self.setScreen(s)
                break

        self._cursor_pos: Optional[QPoint] = None  # local logical

    # --- helpers ---

    def _to_global_logical(self, local: QPoint) -> QPoint:
        return QPoint(
            local.x() + self._screen_info.logical_x,
            local.y() + self._screen_info.logical_y,
        )

    def _global_rect_to_local(self, sel: SelectionRect) -> QRect:
        return QRect(
            sel.x - self._screen_info.logical_x,
            sel.y - self._screen_info.logical_y,
            sel.w,
            sel.h,
        )

    # --- input ---

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_drag_start(self._to_global_logical(event.position().toPoint()))
        elif event.button() == Qt.MouseButton.RightButton:
            self._on_cancel()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        self._cursor_pos = event.position().toPoint()
        self._on_drag_move(self._to_global_logical(event.position().toPoint()))
        self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_drag_end(self._to_global_logical(event.position().toPoint()))
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent):
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self._on_cancel()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._on_confirm()
            return
        # Arrow keys: nudge the selection's bottom-right corner
        # Shift+arrow = 10px, else 1px
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if key == Qt.Key.Key_Left:
            self._on_key_nudge(-1, 0, shift)
            return
        if key == Qt.Key.Key_Right:
            self._on_key_nudge(1, 0, shift)
            return
        if key == Qt.Key.Key_Up:
            self._on_key_nudge(0, -1, shift)
            return
        if key == Qt.Key.Key_Down:
            self._on_key_nudge(0, 1, shift)
            return
        super().keyPressEvent(event)

    # --- paint ---

    def paintEvent(self, event):
        painter = QPainter(self)
        # 1) draw frozen background (full screen)
        painter.drawImage(self.rect(), self._background)

        # 2) dim layer everywhere
        painter.fillRect(self.rect(), QColor(0, 0, 0, 130))

        # 3) clear selection area (re-draw background bright there) + border
        if not self._state.selection.is_empty:
            sel_local = self._global_rect_to_local(self._state.selection)
            inter = sel_local.intersected(self.rect())
            if not inter.isEmpty():
                # Restore the bright background inside the selection by
                # blitting the corresponding source rect from the frozen
                # background. The background is at physical pixels, so
                # the source rect needs to be scaled by this screen's DPR.
                dpr = self._screen_info.dpr or 1.0
                src = QRect(
                    int(inter.x() * dpr),
                    int(inter.y() * dpr),
                    int(inter.width() * dpr),
                    int(inter.height() * dpr),
                )
                painter.drawImage(inter, self._background, src)

                # Selection border
                pen = QPen(QColor(0, 174, 255, 240))
                pen.setWidth(2)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(inter.adjusted(0, 0, -1, -1))

                # Size readout near bottom-right of selection
                text = f"{self._state.selection.w} × {self._state.selection.h}"
                painter.setPen(QColor(255, 255, 255, 230))
                bx = inter.right() - 80
                by = inter.bottom() + 18
                if by > self.height() - 4:
                    by = inter.top() - 6
                painter.drawText(QPoint(bx, by), text)

        # 4) cursor coordinate readout (global logical)
        if self._cursor_pos is not None:
            gx = self._cursor_pos.x() + self._screen_info.logical_x
            gy = self._cursor_pos.y() + self._screen_info.logical_y
            painter.setPen(QColor(255, 255, 255, 200))
            painter.drawText(
                self._cursor_pos + QPoint(14, -8),
                f"({gx}, {gy})",
            )

        painter.end()


# ---------------------------------------------------------------------------
# OverlayManager — coordinates ScreenOverlay instances + toolbar
# ---------------------------------------------------------------------------


class RegionSelectorOverlay:
    """Public API: open the overlay and return user's choice asynchronously
    via callbacks.

    Callbacks receive (image: QImage, dpr: float, sel: SelectionRect) so
    the consumer doesn't have to re-capture — that would race the live
    desktop and produce "what you saw is NOT what you saved". The overlay
    crops the result out of its frozen per-screen background images.

    Usage from main UI thread:
        overlay = RegionSelectorOverlay(
            on_confirm=lambda image, dpr, rect: ...,
            on_pin=lambda image, dpr, rect: ...,
            on_cancel=lambda: ...,
        )
        overlay.start()
    """

    def __init__(
        self,
        on_confirm: Callable[[object, float, SelectionRect], None],
        on_pin: Callable[[object, float, SelectionRect], None],
        on_cancel: Callable[[], None],
    ):
        self._on_confirm_cb = on_confirm
        self._on_pin_cb = on_pin
        self._on_cancel_cb = on_cancel

        self._state = SelectionState()
        self._overlays: List[ScreenOverlay] = []
        self._toolbar: Optional[SelectionToolbar] = None
        self._screens: List[ScreenInfo] = []
        # name -> (ScreenInfo, frozen QImage at physical resolution)
        self._frozen: dict = {}

    def start(self):
        """Freeze the screens and show overlays."""
        from aria.core.screenshot import CaptureEngine

        self._screens = CaptureEngine.enumerate_screens()
        if not self._screens:
            self._on_cancel_cb()
            return

        # Freeze each screen's background. Key by (name, index) so two
        # monitors with identical names (e.g. duplicated displays) don't
        # collide on lookup.
        for idx, s in enumerate(self._screens):
            local_rect = SelectionRect(0, 0, s.logical_w, s.logical_h)
            img = CaptureEngine.grab_screen_logical_rect(s, local_rect)
            self._frozen[(s.name, idx)] = (s, img)

        # Create one overlay per screen
        for idx, s in enumerate(self._screens):
            ov = ScreenOverlay(
                screen_info=s,
                background=self._frozen[(s.name, idx)][1],
                state=self._state,
                on_drag_start=self._on_drag_start,
                on_drag_move=self._on_drag_move,
                on_drag_end=self._on_drag_end,
                on_cancel=self._cancel,
                on_confirm=self._confirm,
                on_key_nudge=self._key_nudge,
            )
            ov.show()
            ov.activateWindow()
            ov.raise_()
            self._overlays.append(ov)

        if self._overlays:
            self._overlays[0].setFocus()

    def _crop_from_frozen(self, sel: SelectionRect):
        """Crop the user's selection out of the frozen backgrounds.

        Returns (QImage, dominant_dpr). The output canvas matches the
        user's selection rectangle one-to-one in logical pixels (scaled
        by the dominant DPR for physical pixels). For multi-screen
        selections, each contributing screen's slice is composited at its
        true logical-global anchor inside that canvas.

        Logical "islands of screens" gaps (regions inside the selection
        bounding box that don't overlap any real screen) are LEFT
        TRANSPARENT. That preserves "what you see is what you get":
        nothing was on those pixels when the user was looking, so
        nothing is in the output. Compressing the gaps would silently
        shrink the output and break the user's framing expectation.
        """
        from PySide6.QtCore import QRect
        from PySide6.QtGui import QImage, QPainter

        slices = split_by_screen(sel, self._screens)
        if not slices:
            return QImage(), 1.0

        slices_sorted = sorted(
            slices,
            key=lambda s: s.logical_local.w * s.logical_local.h,
            reverse=True,
        )
        dominant_dpr = slices_sorted[0].screen.dpr or 1.0

        # Sum of slice widths (no Qt gap pixels), height = max slice height.
        # For non-rectangular layouts we still produce a rectangular image
        # by laying slices out at the dominant DPR, with their original
        # logical-global anchors preserved.
        out_w = int(round(sel.w * dominant_dpr))
        out_h = int(round(sel.h * dominant_dpr))
        if out_w <= 0 or out_h <= 0:
            return QImage(), dominant_dpr

        out = QImage(out_w, out_h, QImage.Format.Format_ARGB32)
        out.fill(0)
        # Carry DPR metadata so a paste into a DPR-aware app (e.g. modern
        # editors) renders at logical size, not 1.5× too big. The PNG
        # file on disk is unaffected — it's just pixels.
        out.setDevicePixelRatio(dominant_dpr)

        painter = QPainter(out)
        try:
            for piece in slices:
                # Find the right frozen background for this screen
                bg = None
                for (name, idx), (info, img) in self._frozen.items():
                    if info is piece.screen:
                        bg = img
                        break
                if bg is None:
                    continue
                # Source rect = local logical rect → physical px on that screen
                src_phys = logical_to_physical_rect(piece.logical_local, piece.screen)
                src_qrect = QRect(src_phys.x, src_phys.y, src_phys.w, src_phys.h)
                # Destination = location within the output relative to sel
                dx = int(round((piece.logical_global.x - sel.x) * dominant_dpr))
                dy = int(round((piece.logical_global.y - sel.y) * dominant_dpr))
                dw = int(round(piece.logical_global.w * dominant_dpr))
                dh = int(round(piece.logical_global.h * dominant_dpr))
                painter.drawImage(QRect(dx, dy, dw, dh), bg, src_qrect)
        finally:
            painter.end()
        return out, dominant_dpr

    # --- event routing from overlays ---

    def _on_drag_start(self, global_pt: QPoint):
        self._state.drag_start = global_pt
        self._state.selection = SelectionRect(global_pt.x(), global_pt.y(), 0, 0)
        self._state.has_selection = False
        self._hide_toolbar()
        self._refresh_all()

    def _on_drag_move(self, global_pt: QPoint):
        if self._state.drag_start is None:
            return
        s = self._state.drag_start
        self._state.selection = normalize_drag_rect(
            s.x(), s.y(), global_pt.x(), global_pt.y()
        )
        self._refresh_all()

    def _on_drag_end(self, global_pt: QPoint):
        if self._state.drag_start is None:
            return
        s = self._state.drag_start
        self._state.selection = normalize_drag_rect(
            s.x(), s.y(), global_pt.x(), global_pt.y()
        )
        self._state.drag_start = None
        if self._state.selection.is_empty:
            return
        self._state.has_selection = True
        self._show_toolbar()
        self._refresh_all()

    def _key_nudge(self, dx: int, dy: int, big: bool):
        if not self._state.has_selection:
            return
        step = 10 if big else 1
        sel = self._state.selection
        # Resize from bottom-right corner (preserves the anchor)
        new_w = max(1, sel.w + dx * step)
        new_h = max(1, sel.h + dy * step)
        self._state.selection = SelectionRect(sel.x, sel.y, new_w, new_h)
        self._show_toolbar()
        self._refresh_all()

    def _show_toolbar(self):
        if self._toolbar is None:
            self._toolbar = SelectionToolbar()
            self._toolbar.confirmed.connect(self._confirm)
            self._toolbar.pinned.connect(self._pin)
            self._toolbar.cancelled.connect(self._cancel)
            self._toolbar.reselected.connect(self._reselect)
        bx, by = place_toolbar(
            self._state.selection,
            self._toolbar.BAR_W,
            self._toolbar.BAR_H,
            self._screens,
        )
        self._toolbar.move(bx, by)
        self._toolbar.show()
        self._toolbar.raise_()

    def _hide_toolbar(self):
        if self._toolbar is not None:
            self._toolbar.hide()

    def _refresh_all(self):
        for ov in self._overlays:
            ov.update()

    # --- user choices ---

    def _confirm(self):
        if not self._state.has_selection or self._state.selection.is_empty:
            return
        sel = self._state.selection
        # Crop BEFORE tearing down: the frozen backgrounds live on the
        # overlay manager, and the overlay widgets that own the on-screen
        # presentation can be destroyed safely once we have the image.
        image, dpr = self._crop_from_frozen(sel)
        self._tear_down()
        try:
            self._on_confirm_cb(image, dpr, sel)
        except Exception as e:
            print(f"[OVERLAY] confirm callback raised: {e}")

    def _pin(self):
        if not self._state.has_selection or self._state.selection.is_empty:
            return
        sel = self._state.selection
        image, dpr = self._crop_from_frozen(sel)
        self._tear_down()
        try:
            self._on_pin_cb(image, dpr, sel)
        except Exception as e:
            print(f"[OVERLAY] pin callback raised: {e}")

    def _reselect(self):
        self._state.selection = SelectionRect(0, 0, 0, 0)
        self._state.has_selection = False
        self._state.drag_start = None
        self._hide_toolbar()
        self._refresh_all()

    def _cancel(self):
        self._tear_down()
        try:
            self._on_cancel_cb()
        except Exception as e:
            print(f"[OVERLAY] cancel callback raised: {e}")

    def _tear_down(self):
        if self._toolbar is not None:
            self._toolbar.close()
            self._toolbar = None
        for ov in self._overlays:
            ov.close()
        self._overlays = []
