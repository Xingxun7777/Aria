"""Screen capture engine using Qt's QScreen.grabWindow.

Choice rationale: PySide6 is already a runtime dependency. QScreen handles
per-monitor DPR correctly on Windows 10/11. The returned QPixmap carries
its own devicePixelRatio so the framebuffer pixel count is preserved.

Important: `QScreen.grabWindow(winid, x, y, w, h)` takes
device-INDEPENDENT (logical) pixels. The output pixmap has full physical
resolution. We always pass logical local-screen coordinates.

This module imports Qt because it must call QScreen APIs. It is therefore
NOT importable from headless contexts — guard imports at call sites if
necessary. Geometry math lives in `geometry.py` and stays Qt-free for
testability.
"""

from __future__ import annotations

from typing import List, Optional

from .geometry import ScreenInfo, ScreenSlice, SelectionRect, split_by_screen


def _qt_modules():
    """Lazy import so unit tests of geometry.py don't need PySide6."""
    from PySide6.QtGui import QGuiApplication, QImage, QPainter, QPixmap

    return QGuiApplication, QImage, QPainter, QPixmap


class CaptureEngine:
    """Captures screen regions via Qt and returns QImage in physical pixels.

    All public methods MUST be called on the Qt main thread because Qt
    forbids QScreen/QPixmap mutation off-thread.
    """

    @staticmethod
    def enumerate_screens() -> List[ScreenInfo]:
        """Return a snapshot of every connected monitor as ScreenInfo.

        The first entry is always the primary screen so callers can do
        `enumerate_screens()[0]` for the "full screen" command. The rest
        follow Qt's enumeration order (typically left-to-right by
        position).
        """
        QGuiApplication, _, _, _ = _qt_modules()
        app = QGuiApplication.instance()
        if app is None:
            raise RuntimeError(
                "QGuiApplication not initialized; cannot enumerate screens"
            )

        primary = app.primaryScreen()
        screens = list(app.screens())
        # Ensure primary is first; rest in Qt order
        ordered = [primary] + [s for s in screens if s is not primary]
        result: List[ScreenInfo] = []
        for s in ordered:
            geom = s.geometry()
            result.append(
                ScreenInfo(
                    name=s.name(),
                    logical_x=geom.x(),
                    logical_y=geom.y(),
                    logical_w=geom.width(),
                    logical_h=geom.height(),
                    dpr=float(s.devicePixelRatio()),
                    is_primary=(s is primary),
                )
            )
        return result

    @staticmethod
    def grab_primary_screen() -> "object":
        """Capture the whole primary screen, return a QImage (physical px).

        Returns a QImage rather than QPixmap because QImage is movable
        across threads (for PNG encoding) and can be losslessly written
        to PNG / clipboard.
        """
        QGuiApplication, _QImage, _, _ = _qt_modules()
        app = QGuiApplication.instance()
        if app is None:
            raise RuntimeError("QGuiApplication not initialized")

        primary = app.primaryScreen()
        # Passing -1 / 0,0,-1,-1 captures the whole screen at its native
        # device pixel ratio. The returned QPixmap has devicePixelRatio()
        # equal to the screen's DPR.
        pixmap = primary.grabWindow(0, 0, 0, -1, -1)
        return pixmap.toImage()

    @staticmethod
    def grab_screen_logical_rect(
        screen_info: ScreenInfo, local_rect: SelectionRect
    ) -> "object":
        """Capture a logical-pixel rectangle from a specific screen.

        `local_rect` is in that screen's local logical coordinates.
        Returns a QImage at physical pixel size for the screen's DPR.
        """
        QGuiApplication, _, _, _ = _qt_modules()
        app = QGuiApplication.instance()
        if app is None:
            raise RuntimeError("QGuiApplication not initialized")

        target_screen = None
        for s in app.screens():
            if s.name() == screen_info.name:
                target_screen = s
                break
        if target_screen is None:
            target_screen = app.primaryScreen()

        pixmap = target_screen.grabWindow(
            0,
            local_rect.x,
            local_rect.y,
            local_rect.w,
            local_rect.h,
        )
        return pixmap.toImage()

    @classmethod
    def grab_global_rect(
        cls,
        global_rect: SelectionRect,
        screens: Optional[List[ScreenInfo]] = None,
    ) -> "object":
        """Capture a global (multi-screen) logical rectangle.

        Splits `global_rect` into per-screen slices, captures each at full
        physical resolution, then composites the results into one QImage
        sized in physical pixels. The output pixel dimensions are computed
        from the dominant DPR (the slice covering the largest area) so
        mixed-DPR setups still produce a single coherent image.
        """
        _, QImage, QPainter, _ = _qt_modules()
        if screens is None:
            screens = cls.enumerate_screens()

        slices: List[ScreenSlice] = split_by_screen(global_rect, screens)
        if not slices:
            return QImage()

        # Pick the dominant DPR — the screen contributing the largest area.
        # Mixed-DPR composites would otherwise look uneven; we resample to
        # the dominant grid below by drawing each piece at its own DPR onto
        # a target sized for the dominant one.
        slices_sorted = sorted(
            slices,
            key=lambda s: s.logical_local.w * s.logical_local.h,
            reverse=True,
        )
        dominant_dpr = slices_sorted[0].screen.dpr or 1.0

        out_w = int(round(global_rect.w * dominant_dpr))
        out_h = int(round(global_rect.h * dominant_dpr))
        if out_w <= 0 or out_h <= 0:
            return QImage()

        # ARGB32 is universally writable and PNG-friendly.
        composite = QImage(out_w, out_h, QImage.Format.Format_ARGB32)
        composite.fill(0)
        composite.setDevicePixelRatio(dominant_dpr)

        painter = QPainter(composite)
        try:
            for s in slices:
                piece = cls.grab_screen_logical_rect(s.screen, s.logical_local)
                # Position in dominant-DPR physical pixels:
                dx = int(round((s.logical_global.x - global_rect.x) * dominant_dpr))
                dy = int(round((s.logical_global.y - global_rect.y) * dominant_dpr))
                dw = int(round(s.logical_global.w * dominant_dpr))
                dh = int(round(s.logical_global.h * dominant_dpr))
                # Use drawImage with target QRect to handle DPR mismatches:
                # the source QImage already carries its native dimensions,
                # Qt will scale during draw. For same-DPR this is 1:1.
                painter.drawImage(
                    _qt_rect(dx, dy, dw, dh),
                    piece,
                    _qt_rect(0, 0, piece.width(), piece.height()),
                )
        finally:
            painter.end()
        return composite


def _qt_rect(x: int, y: int, w: int, h: int):
    """Tiny wrapper for QRect to keep imports localized."""
    from PySide6.QtCore import QRect

    return QRect(x, y, w, h)
