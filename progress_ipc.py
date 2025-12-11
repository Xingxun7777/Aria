# progress_ipc.py
# IPC communication for startup splash screen progress
# Uses multiprocessing Pipe for reliable cross-process communication

import json
import socket
import time
from dataclasses import dataclass, asdict
from typing import Optional, Callable, Tuple
from multiprocessing.connection import Client, Listener


@dataclass
class ProgressEvent:
    """Progress event data structure."""
    stage: str       # "python_env", "funasr_model", "qt_ui", "audio_capture", "done", "error"
    message: str     # User-facing message
    percent: int     # 0-100


# Stage definitions with default percentages
STAGES = {
    "python_env": (5, "初始化 Python 环境..."),
    "funasr_model": (50, "加载语音识别模型..."),
    "qt_ui": (80, "准备用户界面..."),
    "audio_capture": (95, "初始化音频设备..."),
    "done": (100, "准备就绪！"),
    "error": (0, "启动失败"),
}


def find_free_port() -> int:
    """Find an available port for IPC communication."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('localhost', 0))
        return s.getsockname()[1]


class ProgressReporter:
    """
    Main process uses this to send progress updates to splash process.

    Designed to be no-op safe: if connection fails, operations silently succeed.
    This ensures the main application isn't blocked by splash communication issues.
    """

    def __init__(self, address: Tuple[str, int] = None):
        self._address = address
        self._conn = None
        self._connected = False

    def connect(self, timeout: float = 2.0) -> bool:
        """Attempt to connect to the splash listener."""
        if not self._address:
            return False

        try:
            self._conn = Client(self._address)
            self._connected = True
            return True
        except Exception as e:
            print(f"[ProgressReporter] Connection failed: {e}")
            self._connected = False
            return False

    def emit(self, stage: str, message: str = None, percent: int = None):
        """
        Send a progress event to the splash screen.

        Args:
            stage: Stage identifier (see STAGES)
            message: Optional custom message (uses default if None)
            percent: Optional custom percentage (uses default if None)
        """
        if not self._connected:
            return

        # Get defaults from STAGES if not provided
        if stage in STAGES:
            default_percent, default_message = STAGES[stage]
            if percent is None:
                percent = default_percent
            if message is None:
                message = default_message
        else:
            # Unknown stage, use provided values or defaults
            percent = percent or 0
            message = message or stage

        event = ProgressEvent(stage=stage, message=message, percent=percent)

        try:
            self._conn.send(json.dumps(asdict(event)))
        except Exception as e:
            print(f"[ProgressReporter] Send failed: {e}")
            self._connected = False

    def emit_stage(self, stage: str):
        """Convenience method to emit a predefined stage."""
        self.emit(stage)

    def done(self):
        """Signal that loading is complete."""
        self.emit("done")

    def error(self, message: str):
        """Signal an error occurred."""
        self.emit("error", message=message, percent=0)

    def close(self):
        """Close the connection."""
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
        self._connected = False


class ProgressListener:
    """
    Splash process uses this to receive progress updates.

    Designed for Qt integration - call poll() from a QTimer
    or use blocking recv() in a separate thread.
    """

    def __init__(self, address: Tuple[str, int]):
        self._address = address
        self._listener = None
        self._conn = None
        self._callback: Optional[Callable[[str, str, int], None]] = None

    def start(self, timeout: float = 30.0) -> bool:
        """
        Start listening for connections.

        Args:
            timeout: How long to wait for main process connection (seconds)

        Returns:
            True if connection established, False on timeout
        """
        import threading

        try:
            self._listener = Listener(self._address)

            # Use a thread to implement timeout since Listener.accept() is blocking
            result = [None]  # Use list to allow modification in thread
            error = [None]

            def accept_connection():
                try:
                    result[0] = self._listener.accept()
                except Exception as e:
                    error[0] = e

            accept_thread = threading.Thread(target=accept_connection, daemon=True)
            accept_thread.start()
            accept_thread.join(timeout=timeout)

            if accept_thread.is_alive():
                # Timeout - thread still running
                print(f"[ProgressListener] Connection timeout after {timeout}s")
                try:
                    self._listener.close()
                except:
                    pass
                return False

            if error[0]:
                print(f"[ProgressListener] Accept error: {error[0]}")
                return False

            if result[0]:
                self._conn = result[0]
                return True

            return False
        except Exception as e:
            print(f"[ProgressListener] Start failed: {e}")
            return False

    def set_callback(self, callback: Callable[[str, str, int], None]):
        """Set callback function for progress events: callback(stage, message, percent)"""
        self._callback = callback

    def poll(self, timeout: float = 0.1) -> Optional[ProgressEvent]:
        """
        Non-blocking poll for progress event.

        Returns:
            ProgressEvent if available, None otherwise
        """
        if not self._conn:
            return None

        try:
            if self._conn.poll(timeout):
                data = self._conn.recv()
                event_dict = json.loads(data)
                event = ProgressEvent(**event_dict)

                if self._callback:
                    self._callback(event.stage, event.message, event.percent)

                return event
        except EOFError:
            # Connection closed
            return ProgressEvent(stage="done", message="连接已关闭", percent=100)
        except Exception as e:
            print(f"[ProgressListener] Poll error: {e}")

        return None

    def recv_blocking(self) -> Optional[ProgressEvent]:
        """
        Blocking receive for progress event.
        Use in a separate thread or when blocking is acceptable.
        """
        if not self._conn:
            return None

        try:
            data = self._conn.recv()
            event_dict = json.loads(data)
            event = ProgressEvent(**event_dict)

            if self._callback:
                self._callback(event.stage, event.message, event.percent)

            return event
        except EOFError:
            return ProgressEvent(stage="done", message="连接已关闭", percent=100)
        except Exception as e:
            print(f"[ProgressListener] Recv error: {e}")
            return None

    def close(self):
        """Close listener and connection."""
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
        if self._listener:
            try:
                self._listener.close()
            except Exception:
                pass


# Test function
if __name__ == "__main__":
    import multiprocessing

    def test_sender(address):
        """Test sender process."""
        time.sleep(0.5)  # Let listener start
        reporter = ProgressReporter(address)
        if reporter.connect():
            print("[Sender] Connected!")
            reporter.emit_stage("python_env")
            time.sleep(0.5)
            reporter.emit_stage("funasr_model")
            time.sleep(1)
            reporter.emit("funasr_model", "模型加载完成", 50)
            time.sleep(0.5)
            reporter.emit_stage("qt_ui")
            time.sleep(0.5)
            reporter.emit_stage("audio_capture")
            time.sleep(0.3)
            reporter.done()
            reporter.close()
            print("[Sender] Done!")

    def test_receiver(address):
        """Test receiver process."""
        listener = ProgressListener(address)
        if listener.start():
            print("[Receiver] Connected!")
            while True:
                event = listener.poll()
                if event:
                    print(f"[Receiver] Stage: {event.stage}, Msg: {event.message}, %: {event.percent}")
                    if event.stage in ("done", "error"):
                        break
            listener.close()
            print("[Receiver] Done!")

    # Run test
    port = find_free_port()
    address = ('localhost', port)
    print(f"Testing IPC on port {port}")

    receiver = multiprocessing.Process(target=test_receiver, args=(address,))
    sender = multiprocessing.Process(target=test_sender, args=(address,))

    receiver.start()
    time.sleep(0.1)
    sender.start()

    sender.join()
    receiver.join(timeout=5)

    print("Test complete!")
