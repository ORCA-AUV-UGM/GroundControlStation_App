"""MJPEG camera stream proxy and MP4 video recorder for ORCA V4 GCS."""
from datetime import datetime
from pathlib import Path
import queue
import threading
import time
from typing import Callable, Optional
import urllib.error
import urllib.request

from .config import DEFAULT_CAMERA_PORT, DEFAULT_JETSON_HOST, RECORD_DIR, RECORD_FPS


class VideoRecorder:
    """Thread-safe MP4 video recorder consuming JPEG frames from the camera stream."""

    def __init__(
        self,
        on_done: Optional[Callable[[str, int, Optional[str]], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
    ):
        self._on_done = on_done
        self._on_log = on_log
        self.active = threading.Event()
        self._queue: queue.Queue = queue.Queue(maxsize=8)
        self._thread: Optional[threading.Thread] = None
        self.path: Optional[Path] = None
        self.status_text = "REC: OFF"
        self.recorded_frames = 0
        self.fps = RECORD_FPS

    def push_frame(self, jpg: bytes) -> None:
        if not self.active.is_set():
            return
        try:
            self._queue.put_nowait((time.monotonic(), jpg))
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait((time.monotonic(), jpg))
            except queue.Full:
                pass

    def start(self, camera_enabled: bool, last_frame_age: float) -> Path:
        if self.active.is_set() or (self._thread and self._thread.is_alive()):
            raise RuntimeError("Rekaman sudah berjalan.")
        if not camera_enabled or last_frame_age > 2.0:
            raise ValueError("REC ditolak: kamera belum mengirim frame aktif.")

        try:
            import cv2  # noqa: F401
            import numpy  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "Perekaman MP4 membutuhkan OpenCV dan NumPy. Install: pip install opencv-python numpy"
            ) from exc

        RECORD_DIR.mkdir(parents=True, exist_ok=True)
        name = "ORCA_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3] + ".mp4"
        self.path = RECORD_DIR / name
        self.recorded_frames = 0
        self._queue = queue.Queue(maxsize=8)
        self.active.set()
        self.status_text = "REC: STARTING"
        self._thread = threading.Thread(
            target=self._worker,
            args=(self.path,),
            daemon=True,
            name="orca-video-recorder",
        )
        self._thread.start()
        if self._on_log:
            self._on_log(f"Mulai merekam kamera ke {self.path}")
        return self.path

    def stop(self) -> bool:
        if not self.active.is_set():
            return False
        self.active.clear()
        self.status_text = "REC: SAVING..."
        return True

    def join(self, timeout: float = 5.0) -> None:
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _worker(self, destination: Path) -> None:
        import cv2
        import numpy as np

        writer = None
        first_at = None
        last_frame = None
        written = 0
        last_input = time.monotonic()
        error: Optional[str] = None
        try:
            while self.active.is_set() or not self._queue.empty():
                try:
                    stamp, jpg = self._queue.get(timeout=0.20)
                except queue.Empty:
                    if self.active.is_set() and time.monotonic() - last_input > 3.0:
                        error = "Stream kamera terhenti > 3 detik; rekaman disetop."
                        self.active.clear()
                    continue

                last_input = stamp
                frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
                if frame is None:
                    continue

                if writer is None:
                    h, w = frame.shape[:2]
                    writer = cv2.VideoWriter(
                        str(destination),
                        cv2.VideoWriter_fourcc(*"mp4v"),
                        self.fps,
                        (w, h),
                    )
                    if not writer.isOpened():
                        raise OSError("VideoWriter MP4 gagal dibuka (codec mp4v).")
                    first_at = stamp
                    last_frame = frame
                    writer.write(frame)
                    written = 1
                    self.status_text = "REC: RECORDING"
                    continue

                if frame.shape[:2] != last_frame.shape[:2]:
                    frame = cv2.resize(frame, (last_frame.shape[1], last_frame.shape[0]))
                target_count = max(written, int((stamp - first_at) * self.fps) + 1)
                for _ in range(min(target_count - written, int(self.fps * 3))):
                    writer.write(last_frame)
                    written += 1
                last_frame = frame

            if writer is not None and last_frame is not None:
                writer.write(last_frame)
                written += 1
        except Exception as exc:
            error = f"Gagal merekam: {exc}"
        finally:
            if writer is not None:
                writer.release()
            self.active.clear()
            self.recorded_frames = written
            if error:
                self.status_text = "REC: ERROR"
            elif written > 0:
                self.status_text = "REC: SAVED"
            else:
                self.status_text = "REC: EMPTY"

            if self._on_done:
                self._on_done(str(destination), written, error)

    def status_dict(self) -> dict:
        return {
            "active": self.active.is_set(),
            "status": self.status_text,
            "path": str(self.path) if self.path else None,
            "frames": self.recorded_frames,
            "fps": self.fps,
        }


class CameraManager:
    """Connects to Jetson MJPEG stream and caches latest JPEG frames for HTTP streaming & recording."""

    def __init__(
        self,
        stop_event: threading.Event,
        recorder: VideoRecorder,
        host: str = DEFAULT_JETSON_HOST,
        port: int = DEFAULT_CAMERA_PORT,
    ):
        self._stop = stop_event
        self.recorder = recorder
        self.host = host
        self.port = port

        self.enabled = threading.Event()
        self._lock = threading.Lock()
        self._frame_cond = threading.Condition(self._lock)
        self._latest_jpg: Optional[bytes] = None
        self._seq = 0
        self._last_frame_time = 0.0
        self._error = ""
        self._response = None
        self._frame_times: list[float] = []
        self._worker_thread: Optional[threading.Thread] = None

    def start(self, host: Optional[str] = None, port: Optional[int] = None) -> None:
        if host is not None:
            self.host = host.strip() or DEFAULT_JETSON_HOST
        if port is not None:
            self.port = int(port)

        if self._stop.is_set() or self.enabled.is_set():
            return
        self.enabled.set()
        self._worker_thread = threading.Thread(
            target=self._worker, daemon=True, name="orca-mjpeg"
        )
        self._worker_thread.start()

    def stop(self) -> None:
        self.recorder.stop()
        self.enabled.clear()
        with self._lock:
            response = self._response
            self._response = None
            self._latest_jpg = None
            self._frame_times.clear()
            self._frame_cond.notify_all()
        if response is not None:
            try:
                response.close()
            except OSError:
                pass

    def reconnect(self, host: Optional[str] = None, port: Optional[int] = None) -> None:
        self.stop()
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=1.0)
        self.start(host=host, port=port)

    def _worker(self) -> None:
        while self.enabled.is_set() and not self._stop.is_set():
            url = f"http://{self.host}:{self.port}/stream.mjpg"
            response = None
            try:
                request = urllib.request.Request(
                    url,
                    headers={"User-Agent": "ORCA-V4-GCS/1.0", "Cache-Control": "no-cache"},
                )
                response = urllib.request.urlopen(request, timeout=4)
                with self._lock:
                    self._response = response
                    self._error = ""

                buffer = bytearray()
                while self.enabled.is_set() and not self._stop.is_set():
                    chunk = response.read(8192)
                    if not chunk:
                        raise ConnectionError("MJPEG stream closed")
                    buffer.extend(chunk)
                    while True:
                        start = buffer.find(b"\xff\xd8")
                        if start < 0:
                            if len(buffer) > 16384:
                                del buffer[:-1024]
                            break
                        end = buffer.find(b"\xff\xd9", start + 2)
                        if end < 0:
                            if start:
                                del buffer[:start]
                            if len(buffer) > 4_000_000:
                                buffer.clear()
                            break

                        jpg = bytes(buffer[start : end + 2])
                        del buffer[: end + 2]

                        self.recorder.push_frame(jpg)
                        now = time.monotonic()
                        with self._lock:
                            self._latest_jpg = jpg
                            self._seq += 1
                            self._last_frame_time = now
                            self._frame_times = [t for t in self._frame_times if now - t < 2.0]
                            self._frame_times.append(now)
                            self._frame_cond.notify_all()
            except (OSError, TimeoutError, ValueError, ConnectionError, urllib.error.URLError) as exc:
                with self._lock:
                    self._error = str(exc)
            finally:
                with self._lock:
                    if self._response is response:
                        self._response = None
                if response is not None:
                    try:
                        response.close()
                    except OSError:
                        pass
            if self.enabled.is_set() and not self._stop.is_set():
                self._stop.wait(1.5)

    def get_latest_frame(self) -> tuple[Optional[bytes], int, float]:
        with self._lock:
            age = (
                time.monotonic() - self._last_frame_time
                if self._last_frame_time
                else float("inf")
            )
            return self._latest_jpg, self._seq, age

    def wait_next_frame(self, last_seq: int, timeout: float = 1.0) -> tuple[Optional[bytes], int]:
        with self._lock:
            if self._seq == last_seq and self.enabled.is_set() and not self._stop.is_set():
                self._frame_cond.wait(timeout=timeout)
            return self._latest_jpg, self._seq

    def last_frame_age(self) -> float:
        with self._lock:
            return (
                time.monotonic() - self._last_frame_time
                if self._last_frame_time
                else float("inf")
            )

    def status_dict(self) -> dict:
        now = time.monotonic()
        with self._lock:
            enabled = self.enabled.is_set()
            age = now - self._last_frame_time if self._last_frame_time else None
            error = self._error
            recent = [t for t in self._frame_times if now - t < 2.0]
            fps = 0.0
            if len(recent) > 1:
                elapsed = recent[-1] - recent[0]
                if elapsed > 0.1:
                    fps = round((len(recent) - 1) / elapsed, 1)

        if not enabled:
            status = "STOPPED"
        elif age is not None and age < 2.0:
            status = "LIVE"
        elif error:
            status = "RECONNECTING"
        else:
            status = "CONNECTING"

        return {
            "enabled": enabled,
            "status": status,
            "fps": fps if status == "LIVE" else 0.0,
            "error": error,
            "last_frame_age": round(age, 2) if age is not None else None,
            "stream_url": f"http://{self.host}:{self.port}/stream.mjpg",
        }
