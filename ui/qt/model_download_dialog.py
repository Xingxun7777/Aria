"""
Model Download Dialog
=====================
Qt dialog for first-run model download with progress display.
"""

import sys
from typing import List, Optional
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QProgressBar,
    QTextEdit,
    QApplication,
    QMessageBox,
)
from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QFont


class DownloadWorker(QThread):
    """Background worker for model download."""

    progress = Signal(str, int, int)  # model_name, downloaded, total
    status = Signal(str)  # status message
    finished = Signal(int, int)  # success_count, failed_count

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cancelled = False

    def cancel(self):
        """Request cancellation."""
        self._cancelled = True

    def run(self):
        """Run download in background."""
        try:
            from aria.core.model_manager import ModelDownloader, get_missing_models

            missing = get_missing_models()
            if not missing:
                self.status.emit("所有模型已就绪")
                self.finished.emit(0, 0)
                return

            downloader = ModelDownloader(
                progress_callback=lambda m, d, t: self.progress.emit(m, d, t),
                status_callback=lambda s: self.status.emit(s),
            )

            success = 0
            failed = 0

            for model in missing:
                if self._cancelled:
                    self.status.emit("下载已取消")
                    break

                if downloader.download_model(model):
                    success += 1
                else:
                    failed += 1

            self.finished.emit(success, failed)

        except Exception as e:
            self.status.emit(f"下载出错: {e}")
            self.finished.emit(0, 1)


class ModelDownloadDialog(QDialog):
    """
    Dialog for downloading required models on first run.

    Shows:
    - List of required models and sizes
    - Download progress bar
    - Status log
    - Start/Cancel buttons
    """

    def __init__(self, missing_models: List, parent=None):
        super().__init__(parent)
        self.missing_models = missing_models
        self.worker: Optional[DownloadWorker] = None
        self._download_started = False
        self._download_complete = False

        self.setWindowTitle("Aria - 首次运行配置")
        self.setMinimumSize(500, 400)
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)

        self._setup_ui()

    def _setup_ui(self):
        """Setup the dialog UI."""
        layout = QVBoxLayout(self)
        layout.setSpacing(15)

        # Title
        title = QLabel("需要下载语音识别模型")
        title.setFont(QFont("Microsoft YaHei", 14, QFont.Bold))
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        # Description
        total_size = sum(m.size_mb for m in self.missing_models)
        desc = QLabel(
            f"Aria 需要下载 {len(self.missing_models)} 个模型文件 "
            f"(共约 {total_size}MB) 才能正常工作。\n"
            "模型将从 ModelScope 下载并缓存到本地。"
        )
        desc.setWordWrap(True)
        desc.setAlignment(Qt.AlignCenter)
        layout.addWidget(desc)

        # Model list
        model_list = QLabel(
            "\n".join(
                [f"  • {m.description} ({m.size_mb}MB)" for m in self.missing_models]
            )
        )
        model_list.setStyleSheet("color: #666; margin-left: 20px;")
        layout.addWidget(model_list)

        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("等待开始...")
        layout.addWidget(self.progress_bar)

        # Status log
        self.status_log = QTextEdit()
        self.status_log.setReadOnly(True)
        self.status_log.setMaximumHeight(120)
        self.status_log.setStyleSheet(
            "background-color: #f5f5f5; border: 1px solid #ddd; font-family: Consolas;"
        )
        layout.addWidget(self.status_log)

        # Buttons
        btn_layout = QHBoxLayout()

        self.btn_start = QPushButton("开始下载")
        self.btn_start.setMinimumHeight(40)
        self.btn_start.setStyleSheet(
            "QPushButton { background-color: #4CAF50; color: white; font-weight: bold; }"
            "QPushButton:hover { background-color: #45a049; }"
        )
        self.btn_start.clicked.connect(self._start_download)
        btn_layout.addWidget(self.btn_start)

        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setMinimumHeight(40)
        self.btn_cancel.clicked.connect(self._cancel_or_close)
        btn_layout.addWidget(self.btn_cancel)

        layout.addLayout(btn_layout)

        # Note
        note = QLabel("提示: 下载速度取决于网络状况。如果下载缓慢，可以尝试使用 VPN。")
        note.setStyleSheet("color: #999; font-size: 11px;")
        note.setAlignment(Qt.AlignCenter)
        layout.addWidget(note)

    def _log(self, msg: str):
        """Add message to status log."""
        self.status_log.append(msg)
        # Auto-scroll to bottom
        scrollbar = self.status_log.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _start_download(self):
        """Start the download process."""
        if self._download_started:
            return

        self._download_started = True
        self.btn_start.setEnabled(False)
        self.btn_start.setText("下载中...")
        self.progress_bar.setFormat("准备下载...")

        self._log("开始下载模型...")

        # Create and start worker
        self.worker = DownloadWorker(self)
        self.worker.status.connect(self._on_status)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished.connect(self._on_finished)
        self.worker.start()

    def _cancel_or_close(self):
        """Cancel download or close dialog."""
        if self._download_complete:
            self.accept()
            return

        if self.worker and self.worker.isRunning():
            reply = QMessageBox.question(
                self,
                "取消下载",
                "确定要取消下载吗？Aria 需要这些模型才能正常工作。",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                self.worker.cancel()
                self.reject()
        else:
            self.reject()

    def _on_status(self, msg: str):
        """Handle status message from worker."""
        self._log(msg)
        self.progress_bar.setFormat(msg[:50] + "..." if len(msg) > 50 else msg)

    def _on_progress(self, model: str, downloaded: int, total: int):
        """Handle progress update from worker."""
        if total > 0:
            percent = int(downloaded * 100 / total)
            self.progress_bar.setValue(percent)

    def _on_finished(self, success: int, failed: int):
        """Handle download completion."""
        self._download_complete = True

        if failed == 0:
            self._log(f"\n下载完成! 成功: {success}")
            self.progress_bar.setValue(100)
            self.progress_bar.setFormat("下载完成!")
            self.btn_start.setText("完成")
            self.btn_cancel.setText("继续")
            self.btn_cancel.setStyleSheet(
                "QPushButton { background-color: #4CAF50; color: white; font-weight: bold; }"
            )

            # Auto-close after 2 seconds
            QTimer.singleShot(2000, self.accept)
        else:
            self._log(f"\n下载完成，但有 {failed} 个失败")
            self.progress_bar.setFormat(f"部分下载失败 ({failed} 个)")
            self.btn_start.setText("重试")
            self.btn_start.setEnabled(True)
            self._download_started = False


def show_download_dialog_if_needed() -> bool:
    """
    Check for missing models and show download dialog if needed.

    Returns:
        True if all models are available (or download succeeded)
        False if user cancelled or download failed
    """
    try:
        from aria.core.model_manager import get_missing_models

        missing = get_missing_models()
        if not missing:
            return True

        # Need to show dialog
        app = QApplication.instance()
        if app is None:
            app = QApplication(sys.argv)

        dialog = ModelDownloadDialog(missing)
        result = dialog.exec()

        return result == QDialog.Accepted

    except Exception as e:
        print(f"[ModelDownload] Error: {e}")
        # On error, return True to let the app try to continue
        # FunASR will download models lazily if needed
        return True


# For testing
if __name__ == "__main__":
    from aria.core.model_manager import REQUIRED_MODELS

    app = QApplication(sys.argv)

    # Test with all models as "missing"
    dialog = ModelDownloadDialog(REQUIRED_MODELS)
    dialog.show()

    sys.exit(app.exec())
