"""Pinned screenshot floating window.

Snipaste-style: a frameless always-on-top window showing a cropped
screenshot. Supports:
- Drag to move (left-click anywhere)
- Mouse-wheel zoom anchored to cursor position
- Double-click to copy image to clipboard
- Right-click context menu (copy / save-as / close / opacity)
- ESC to close
- WDA_EXCLUDEFROMCAPTURE so the pin doesn't show up in NEW screenshots

Multiple pins coexist — each window is independent.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QFileDialog,
    QMenu,
    QWidget,
    QApplication,
)


_PIN_REGISTRY: list["PinWindow"] = []


def _exclude_from_capture(widget: QWidget) -> bool:
    """Try to mark a window as invisible to screen capture.

    Only effective on Windows 10 build 19041+ / Windows 11. Returns True
    on success. Failure is silent because older Windows falls back to
    hiding pins manually before each capture.
    """
    if sys.platform != "win32":
        return False
    try:
        winver = sys.getwindowsversion()
        if winver.major < 10 or (winver.major == 10 and winver.build < 19041):
            return False
    except Exception:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
        WDA_EXCLUDEFROMCAPTURE = 0x11
        hwnd = wintypes.HWND(int(widget.winId()))
        return bool(user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE))
    except Exception:
        return False


class PinWindow(QWidget):
    """A single pinned screenshot window."""

    MIN_SCALE = 0.10
    MAX_SCALE = 5.00

    closed = Signal(object)  # emits self

    def __init__(
        self,
        image: QImage,
        global_x: int,
        global_y: int,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        self._image = image
        # Treat the QImage's deviceIndependentSize as the natural display
        # size so a 1.5x physical pixmap doesn't render at 1.5x logical.
        dpr = image.devicePixelRatio() or 1.0
        if dpr <= 0:
            dpr = 1.0
        self._base_logical_size = QSize(
            max(1, int(round(image.width() / dpr))),
            max(1, int(round(image.height() / dpr))),
        )
        self._scale = 1.0
        self._drag_offset: Optional[QPoint] = None

        self.move(global_x, global_y)
        self._apply_scaled_size()

        self.setWindowOpacity(1.0)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        _PIN_REGISTRY.append(self)

    def showEvent(self, event):
        super().showEvent(event)
        _exclude_from_capture(self)

    def closeEvent(self, event):
        if self in _PIN_REGISTRY:
            _PIN_REGISTRY.remove(self)
        self.closed.emit(self)
        super().closeEvent(event)

    # --- sizing ---

    def _apply_scaled_size(self):
        w = max(1, int(round(self._base_logical_size.width() * self._scale)))
        h = max(1, int(round(self._base_logical_size.height() * self._scale)))
        self.resize(w, h)

    # --- paint ---

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(self.rect(), self._image)
        # Hover-style thin border so the user can find the pin edges
        painter.setPen(Qt.GlobalColor.darkGray)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))

    # --- input ---

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.pos()
        elif event.button() == Qt.MouseButton.RightButton:
            self._show_menu(event.globalPosition().toPoint())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._drag_offset is not None and (
            event.buttons() & Qt.MouseButton.LeftButton
        ):
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self._copy_to_clipboard()
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event: QWheelEvent):
        """Zoom anchored to cursor position (not centered) — visual
        creators care about staying focused on the pixel under the
        cursor when zooming in."""
        delta = event.angleDelta().y()
        if delta == 0:
            return
        old_scale = self._scale
        factor = 1.1 if delta > 0 else (1 / 1.1)
        new_scale = max(self.MIN_SCALE, min(self.MAX_SCALE, old_scale * factor))
        if abs(new_scale - old_scale) < 1e-6:
            return

        # Anchor: keep the image point under the cursor fixed in screen
        # coordinates.
        cursor_in_widget = event.position().toPoint()
        ratio = new_scale / old_scale
        new_w = max(1, int(round(self._base_logical_size.width() * new_scale)))
        new_h = max(1, int(round(self._base_logical_size.height() * new_scale)))

        new_x = self.x() + int(cursor_in_widget.x() - cursor_in_widget.x() * ratio)
        new_y = self.y() + int(cursor_in_widget.y() - cursor_in_widget.y() * ratio)

        self._scale = new_scale
        self.setGeometry(new_x, new_y, new_w, new_h)

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        if event.key() in (Qt.Key.Key_0,):
            # Reset to 100% — keep the window's top-left anchored, no
            # cursor-based pivot (Key_0 doesn't carry a meaningful cursor pos)
            self._scale = 1.0
            self._apply_scaled_size()
            return
        super().keyPressEvent(event)

    # --- actions ---

    def _copy_to_clipboard(self):
        try:
            QApplication.clipboard().setImage(self._image)
        except Exception as e:
            print(f"[PIN] copy failed: {e}")

    def _save_as(self):
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        default_name = f"Aria_截图_{ts}.png"
        try:
            home_pics = Path.home() / "Pictures"
            start_dir = str(home_pics if home_pics.exists() else Path.home())
        except Exception:
            start_dir = ""
        path, _ = QFileDialog.getSaveFileName(
            self,
            "另存为",
            f"{start_dir}/{default_name}" if start_dir else default_name,
            "PNG 图片 (*.png);;JPEG (*.jpg);;BMP (*.bmp)",
        )
        if path:
            try:
                self._image.save(path)
            except Exception as e:
                print(f"[PIN] save failed: {e}")

    def _set_opacity(self, value: float):
        self.setWindowOpacity(max(0.1, min(1.0, value)))

    def _show_menu(self, global_pos: QPoint):
        menu = QMenu(self)
        act_copy = QAction("复制图片 (双击)", self)
        act_copy.triggered.connect(self._copy_to_clipboard)
        menu.addAction(act_copy)
        act_save = QAction("另存为...", self)
        act_save.triggered.connect(self._save_as)
        menu.addAction(act_save)
        menu.addSeparator()
        act_reset = QAction("100% 重置缩放 (按 0)", self)
        act_reset.triggered.connect(
            lambda: (setattr(self, "_scale", 1.0), self._apply_scaled_size())
        )
        menu.addAction(act_reset)
        op_menu = menu.addMenu("透明度")
        for label, val in (("100%", 1.0), ("80%", 0.8), ("50%", 0.5), ("25%", 0.25)):
            a = QAction(label, self)
            a.triggered.connect(lambda _checked=False, v=val: self._set_opacity(v))
            op_menu.addAction(a)
        menu.addSeparator()
        act_close = QAction("关闭 (Esc)", self)
        act_close.triggered.connect(self.close)
        menu.addAction(act_close)
        if _PIN_REGISTRY and len(_PIN_REGISTRY) > 1:
            act_close_all = QAction(f"关闭全部 ({len(_PIN_REGISTRY)} 个)", self)
            act_close_all.triggered.connect(_close_all_pins)
            menu.addAction(act_close_all)
        menu.exec(global_pos)


def _close_all_pins():
    for w in list(_PIN_REGISTRY):
        try:
            w.close()
        except Exception:
            pass


def show_pin(image: QImage, global_x: int, global_y: int) -> PinWindow:
    """Convenience entry point: construct + show a pin window."""
    win = PinWindow(image, global_x, global_y)
    win.show()
    win.raise_()
    return win
