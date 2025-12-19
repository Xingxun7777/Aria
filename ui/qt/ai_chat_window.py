"""
AI Chat Window
==============
Floating chat dialog for AI conversations with context.
Designed for VoiceType v1.1 action-driven architecture.

UI Spec (from Gemini consultation):
- Size: 400x500px, resizable
- Quote area: Top section showing selected text summary (max 2 lines)
- Chat area: Bubble-style messages with Markdown support
- Action buttons: Copy, Insert (replace original), Retry, Close
"""

import sys
import ctypes
from typing import Optional, List
from dataclasses import dataclass

from PySide6.QtCore import (
    Qt,
    Signal,
    QTimer,
    QSize,
)
from PySide6.QtGui import (
    QCursor,
    QGuiApplication,
    QColor,
    QPainter,
    QBrush,
    QPen,
    QFont,
    QTextDocument,
)
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QTextEdit,
    QPushButton,
    QScrollArea,
    QFrame,
    QSizePolicy,
    QApplication,
)


@dataclass
class ChatMessage:
    """A single chat message."""

    role: str  # "user" or "assistant"
    content: str
    is_streaming: bool = False


class MessageBubble(QFrame):
    """A single message bubble in the chat."""

    def __init__(self, message: ChatMessage, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.message = message
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(0)

        # Message content
        self._content_label = QLabel()
        self._content_label.setWordWrap(True)
        self._content_label.setTextFormat(Qt.RichText)
        self._content_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._update_content()

        layout.addWidget(self._content_label)

        # Style based on role
        if self.message.role == "user":
            self.setStyleSheet(
                """
                MessageBubble {
                    background: #2563EB;
                    border-radius: 12px;
                    margin-left: 40px;
                }
                QLabel {
                    color: white;
                    font-size: 13px;
                }
            """
            )
        else:
            self.setStyleSheet(
                """
                MessageBubble {
                    background: #374151;
                    border-radius: 12px;
                    margin-right: 40px;
                }
                QLabel {
                    color: #E5E7EB;
                    font-size: 13px;
                }
            """
            )

    def _update_content(self):
        """Update content label with message text."""
        content = self.message.content
        if self.message.is_streaming:
            content += " ▌"  # Cursor indicator

        # Simple markdown-to-HTML conversion
        html = self._markdown_to_html(content)
        self._content_label.setText(html)

    def _markdown_to_html(self, text: str) -> str:
        """Convert simple markdown to HTML."""
        import re

        # Code blocks (```code```)
        text = re.sub(
            r"```(\w*)\n?(.*?)```",
            r'<pre style="background:#1F2937;padding:8px;border-radius:4px;overflow-x:auto;"><code>\2</code></pre>',
            text,
            flags=re.DOTALL,
        )

        # Inline code (`code`)
        text = re.sub(
            r"`([^`]+)`",
            r'<code style="background:#1F2937;padding:2px 4px;border-radius:2px;">\1</code>',
            text,
        )

        # Bold (**text**)
        text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)

        # Italic (*text*)
        text = re.sub(r"\*([^*]+)\*", r"<i>\1</i>", text)

        # Line breaks
        text = text.replace("\n", "<br>")

        return text

    def update_content(self, content: str, is_streaming: bool = False):
        """Update message content (for streaming updates)."""
        self.message.content = content
        self.message.is_streaming = is_streaming
        self._update_content()


class AIChatWindow(QWidget):
    """
    Floating AI chat dialog window.

    Signals:
        insertRequested: Emitted when user wants to insert AI response
        closed: Emitted when window is closed
    """

    insertRequested = Signal(str)  # Emits text to insert
    closed = Signal()

    # Style constants
    WINDOW_WIDTH = 400
    WINDOW_HEIGHT = 500
    BG_COLOR = QColor(30, 30, 30, 250)
    BORDER_COLOR = QColor(64, 64, 64)
    HEADER_COLOR = QColor(40, 40, 40)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)

        self.setWindowFlags(
            Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)

        self._messages: List[ChatMessage] = []
        self._context_text: str = ""
        self._request_id: Optional[str] = None
        self._is_generating: bool = False

        self._init_ui()
        self._setup_drag()

    def _init_ui(self):
        """Initialize UI components."""
        self.setFixedSize(self.WINDOW_WIDTH, self.WINDOW_HEIGHT)

        # Main layout
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(1, 1, 1, 1)
        main_layout.setSpacing(0)

        # Container with background
        container = QFrame()
        container.setStyleSheet(
            f"""
            QFrame {{
                background: {self.BG_COLOR.name()};
                border: 1px solid {self.BORDER_COLOR.name()};
                border-radius: 8px;
            }}
        """
        )
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(0)

        # Header with title and close button
        header = QFrame()
        header.setFixedHeight(36)
        header.setStyleSheet(
            f"""
            QFrame {{
                background: {self.HEADER_COLOR.name()};
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
                border-bottom: 1px solid {self.BORDER_COLOR.name()};
            }}
        """
        )
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 0, 8, 0)

        title = QLabel("AI 对话")
        title.setStyleSheet("color: #E5E7EB; font-size: 13px; font-weight: bold;")
        header_layout.addWidget(title)
        header_layout.addStretch()

        close_btn = QPushButton("×")
        close_btn.setFixedSize(24, 24)
        close_btn.setStyleSheet(
            """
            QPushButton {
                background: transparent;
                color: #9CA3AF;
                font-size: 18px;
                border: none;
                border-radius: 4px;
            }
            QPushButton:hover {
                background: #374151;
                color: #EF4444;
            }
        """
        )
        close_btn.clicked.connect(self.close)
        header_layout.addWidget(close_btn)

        container_layout.addWidget(header)

        # Context quote area
        self._quote_frame = QFrame()
        self._quote_frame.setStyleSheet(
            """
            QFrame {
                background: #1F2937;
                border-left: 3px solid #3B82F6;
                margin: 8px;
                padding: 8px;
                border-radius: 4px;
            }
        """
        )
        quote_layout = QVBoxLayout(self._quote_frame)
        quote_layout.setContentsMargins(8, 4, 8, 4)

        self._quote_label = QLabel()
        self._quote_label.setWordWrap(True)
        self._quote_label.setStyleSheet("color: #9CA3AF; font-size: 12px;")
        self._quote_label.setMaximumHeight(50)
        quote_layout.addWidget(self._quote_label)

        container_layout.addWidget(self._quote_frame)

        # Chat messages area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            """
            QScrollArea {
                border: none;
                background: transparent;
            }
            QScrollBar:vertical {
                background: #1F2937;
                width: 8px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical {
                background: #4B5563;
                border-radius: 4px;
            }
        """
        )

        self._messages_container = QWidget()
        self._messages_layout = QVBoxLayout(self._messages_container)
        self._messages_layout.setContentsMargins(8, 8, 8, 8)
        self._messages_layout.setSpacing(8)
        self._messages_layout.addStretch()

        scroll.setWidget(self._messages_container)
        container_layout.addWidget(scroll, 1)

        # Input area
        input_frame = QFrame()
        input_frame.setStyleSheet(
            f"""
            QFrame {{
                background: {self.HEADER_COLOR.name()};
                border-top: 1px solid {self.BORDER_COLOR.name()};
                border-bottom-left-radius: 8px;
                border-bottom-right-radius: 8px;
            }}
        """
        )
        input_layout = QHBoxLayout(input_frame)
        input_layout.setContentsMargins(8, 8, 8, 8)
        input_layout.setSpacing(8)

        self._input_edit = QTextEdit()
        self._input_edit.setPlaceholderText("输入问题...")
        self._input_edit.setMaximumHeight(60)
        self._input_edit.setStyleSheet(
            """
            QTextEdit {
                background: #374151;
                color: #E5E7EB;
                border: 1px solid #4B5563;
                border-radius: 4px;
                padding: 4px 8px;
                font-size: 13px;
            }
            QTextEdit:focus {
                border-color: #3B82F6;
            }
        """
        )
        input_layout.addWidget(self._input_edit, 1)

        self._send_btn = QPushButton("发送")
        self._send_btn.setFixedSize(60, 32)
        self._send_btn.setStyleSheet(
            """
            QPushButton {
                background: #3B82F6;
                color: white;
                border: none;
                border-radius: 4px;
                font-size: 13px;
            }
            QPushButton:hover {
                background: #2563EB;
            }
            QPushButton:disabled {
                background: #4B5563;
                color: #9CA3AF;
            }
        """
        )
        self._send_btn.clicked.connect(self._on_send_clicked)
        input_layout.addWidget(self._send_btn)

        container_layout.addWidget(input_frame)

        # Action buttons bar
        actions_frame = QFrame()
        actions_frame.setStyleSheet("background: transparent;")
        actions_layout = QHBoxLayout(actions_frame)
        actions_layout.setContentsMargins(8, 4, 8, 8)
        actions_layout.setSpacing(8)

        self._copy_btn = QPushButton("复制回答")
        self._insert_btn = QPushButton("插入文本")
        self._retry_btn = QPushButton("重试")

        for btn in [self._copy_btn, self._insert_btn, self._retry_btn]:
            btn.setFixedHeight(28)
            btn.setStyleSheet(
                """
                QPushButton {
                    background: #374151;
                    color: #E5E7EB;
                    border: 1px solid #4B5563;
                    border-radius: 4px;
                    padding: 0 12px;
                    font-size: 12px;
                }
                QPushButton:hover {
                    background: #4B5563;
                }
                QPushButton:disabled {
                    color: #6B7280;
                }
            """
            )

        self._copy_btn.clicked.connect(self._on_copy_clicked)
        self._insert_btn.clicked.connect(self._on_insert_clicked)
        self._retry_btn.clicked.connect(self._on_retry_clicked)

        actions_layout.addWidget(self._copy_btn)
        actions_layout.addWidget(self._insert_btn)
        actions_layout.addWidget(self._retry_btn)
        actions_layout.addStretch()

        container_layout.addWidget(actions_frame)

        main_layout.addWidget(container)

        # Initially disable action buttons
        self._update_action_buttons()

    def _setup_drag(self):
        """Setup window dragging."""
        self._drag_pos = None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() == Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None

    def show_with_context(
        self, context_text: str, request_id: str, initial_question: Optional[str] = None
    ):
        """
        Show chat window with context.

        Args:
            context_text: Selected text as context
            request_id: Unique request ID
            initial_question: Optional initial question from user
        """
        self._context_text = context_text
        self._request_id = request_id
        self._messages.clear()

        # Update quote
        display_text = context_text
        if len(context_text) > 150:
            display_text = context_text[:147] + "..."
        self._quote_label.setText(display_text)

        # Clear messages
        self._clear_messages_ui()

        # Position near cursor
        self._position_near_cursor()

        self.show()
        self.raise_()
        self._input_edit.setFocus()

        # If there's an initial question, send it
        if initial_question:
            self._input_edit.setPlainText(initial_question)
            self._on_send_clicked()

    def _position_near_cursor(self):
        """Position window near cursor."""
        cursor_pos = QCursor.pos()
        screen = QGuiApplication.screenAt(cursor_pos)
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is None:
            return

        screen_geo = screen.availableGeometry()

        # Position to the right of cursor
        x = cursor_pos.x() + 20
        y = cursor_pos.y() - self.height() // 2

        # Ensure within screen bounds
        if x + self.width() > screen_geo.right():
            x = cursor_pos.x() - self.width() - 20
        if y + self.height() > screen_geo.bottom():
            y = screen_geo.bottom() - self.height() - 20
        if y < screen_geo.top():
            y = screen_geo.top() + 20

        self.move(x, y)

    def _clear_messages_ui(self):
        """Clear message bubbles from UI."""
        while self._messages_layout.count() > 1:  # Keep the stretch
            item = self._messages_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _add_message(self, message: ChatMessage) -> MessageBubble:
        """Add a message to the chat."""
        self._messages.append(message)
        bubble = MessageBubble(message)

        # Insert before the stretch
        self._messages_layout.insertWidget(self._messages_layout.count() - 1, bubble)
        self._update_action_buttons()

        # Scroll to bottom
        QTimer.singleShot(50, self._scroll_to_bottom)

        return bubble

    def _scroll_to_bottom(self):
        """Scroll chat to bottom."""
        scroll = self._messages_container.parent()
        if isinstance(scroll, QScrollArea):
            scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())

    def _update_action_buttons(self):
        """Update action button states."""
        has_response = any(m.role == "assistant" for m in self._messages)
        self._copy_btn.setEnabled(has_response)
        self._insert_btn.setEnabled(has_response)
        self._retry_btn.setEnabled(has_response and not self._is_generating)
        self._send_btn.setEnabled(not self._is_generating)

    def _on_send_clicked(self):
        """Handle send button click."""
        text = self._input_edit.toPlainText().strip()
        if not text:
            return

        self._input_edit.clear()

        # Add user message
        user_msg = ChatMessage(role="user", content=text)
        self._add_message(user_msg)

        # Emit signal for LLM worker (to be connected in main.py)
        self._is_generating = True
        self._update_action_buttons()

        # Add placeholder for assistant response
        assistant_msg = ChatMessage(
            role="assistant", content="思考中...", is_streaming=True
        )
        self._current_bubble = self._add_message(assistant_msg)

    def update_response(self, content: str, is_final: bool = False):
        """Update the current assistant response (for streaming)."""
        if hasattr(self, "_current_bubble"):
            self._current_bubble.update_content(content, is_streaming=not is_final)

        if is_final:
            self._is_generating = False
            self._update_action_buttons()

    def show_error(self, error_msg: str):
        """Show error in current response."""
        if hasattr(self, "_current_bubble"):
            self._current_bubble.update_content(
                f"错误: {error_msg}", is_streaming=False
            )
            self._current_bubble.setStyleSheet(
                """
                MessageBubble {
                    background: #7F1D1D;
                    border-radius: 12px;
                    margin-right: 40px;
                }
                QLabel {
                    color: #FCA5A5;
                    font-size: 13px;
                }
            """
            )
        self._is_generating = False
        self._update_action_buttons()

    def _get_last_response(self) -> Optional[str]:
        """Get the last assistant response."""
        for msg in reversed(self._messages):
            if msg.role == "assistant":
                return msg.content
        return None

    def _on_copy_clicked(self):
        """Copy last response to clipboard."""
        response = self._get_last_response()
        if response:
            clipboard = QApplication.clipboard()
            clipboard.setText(response)
            print(f"[AIChatWindow] Copied response to clipboard")

    def _on_insert_clicked(self):
        """Insert last response to original location."""
        response = self._get_last_response()
        if response:
            self.insertRequested.emit(response)
            print(f"[AIChatWindow] Insert requested: {response[:50]}...")

    def _on_retry_clicked(self):
        """Retry the last question."""
        # Find last user message
        for msg in reversed(self._messages):
            if msg.role == "user":
                # Remove last assistant response
                if self._messages and self._messages[-1].role == "assistant":
                    self._messages.pop()
                    # Remove from UI
                    if self._messages_layout.count() > 1:
                        item = self._messages_layout.takeAt(
                            self._messages_layout.count() - 2
                        )
                        if item.widget():
                            item.widget().deleteLater()

                # Re-send
                self._input_edit.setPlainText(msg.content)
                self._on_send_clicked()
                break

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)

    def get_context(self) -> str:
        """Get the context text."""
        return self._context_text

    def get_conversation(self) -> List[dict]:
        """Get conversation history for LLM."""
        return [{"role": m.role, "content": m.content} for m in self._messages]
