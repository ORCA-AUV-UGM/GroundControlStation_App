"""Central GCS service coordinating Jetson TCP, MJPEG camera, recorder, and WebSocket clients."""
import asyncio
from collections import deque
import threading
import time
from typing import Any, Optional

from .camera import CameraManager, VideoRecorder
from .config import (
    DEFAULT_CAMERA_PORT,
    DEFAULT_JETSON_HOST,
    DEFAULT_JETSON_PORT,
    MAX_LOG_ENTRIES,
    NODES,
)
from .jetson import JetsonClient


class GCSService:
    """Thread-safe coordinator between hardware workers and FastAPI endpoints."""

    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self._lock = threading.RLock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws_clients: set[asyncio.Queue] = set()

        self._logs: deque[dict[str, str]] = deque(maxlen=MAX_LOG_ENTRIES)
        self._last_telemetry_at: Optional[float] = None
        self._telemetry: dict[str, Any] = {
            "depth": None,
            "pressure": None,
            "roll": None,
            "pitch": None,
            "yaw": None,
            "proximity": None,
            "mission": None,
            "sensor_age": None,
            "processes": {k: False for k in NODES},
            "raw": None,
        }

        self.recorder = VideoRecorder(
            on_done=self._handle_record_done,
            on_log=self.add_log,
        )
        self.camera = CameraManager(
            stop_event=self.stop_event,
            recorder=self.recorder,
            host=DEFAULT_JETSON_HOST,
            port=DEFAULT_CAMERA_PORT,
        )
        self.jetson = JetsonClient(
            stop_event=self.stop_event,
            on_event=self._handle_jetson_event,
            host=DEFAULT_JETSON_HOST,
            port=DEFAULT_JETSON_PORT,
        )

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self.add_log("Ground station siap. Hubungkan Jetson pada jaringan yang sama.")
        self.jetson.start()
        self.camera.start()

    def close(self) -> None:
        self.jetson.arm_state = "disarm"
        self.camera.stop()
        self.stop_event.set()
        self.recorder.join(timeout=5.0)

    def register_ws_client(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        with self._lock:
            self._ws_clients.add(q)
        return q

    def unregister_ws_client(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._ws_clients.discard(q)

    def _broadcast(self, event: dict[str, Any]) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with self._lock:
            clients = list(self._ws_clients)
        if not clients:
            return

        def _dispatch() -> None:
            for q in clients:
                if q.full():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    pass

        loop.call_soon_threadsafe(_dispatch)

    def add_log(self, message: str) -> dict[str, str]:
        entry = {
            "timestamp": time.strftime("%H:%M:%S"),
            "message": str(message),
        }
        with self._lock:
            self._logs.append(entry)
        self._broadcast({"type": "log", "data": entry})
        return entry

    def get_logs(self) -> list[dict[str, str]]:
        with self._lock:
            return list(self._logs)

    def clear_logs(self) -> None:
        with self._lock:
            self._logs.clear()

    def _handle_record_done(self, path: str, frames: int, error: Optional[str]) -> None:
        if error:
            self.add_log(error)
        elif frames > 0:
            self.add_log(f"Video tersimpan: {path}")
        else:
            self.add_log("Tidak ada frame yang dapat direkam.")
        self._broadcast(
            {
                "type": "record_done",
                "data": {
                    "path": path,
                    "frames": frames,
                    "error": error,
                    **self.recorder.status_dict(),
                },
            }
        )

    def _handle_jetson_event(self, item: dict[str, Any]) -> None:
        etype = item.get("type")
        if etype == "link":
            connected = bool(item.get("connected"))
            self.add_log("TCP " + ("CONNECTED" if connected else "DISCONNECTED"))
            self._broadcast(
                {
                    "type": "link",
                    "data": {
                        "connected": connected,
                        "manual": self.jetson.manual_status_dict(),
                    },
                }
            )
        elif etype == "ack":
            ok = bool(item.get("ok"))
            msg = str(item.get("message", ""))
            self.add_log(("OK: " if ok else "ERROR: ") + msg)
            self._broadcast({"type": "ack", "data": {"ok": ok, "message": msg}})
        elif etype == "telemetry":
            parsed = self._parse_telemetry(item)
            self._broadcast({"type": "telemetry", "data": parsed})

    def _parse_telemetry(self, item: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        status = item.get("processes") or {}
        processes = {k: bool(status.get(k)) for k in NODES}

        sensor = item.get("sensor") or {}
        values = sensor.get("values") or {}
        updated = sensor.get("updated") or {}

        depth = values.get("depth") or {}
        attitude = values.get("attitude") or {}
        prox = values.get("proximity") or {}

        latest_sensor_ts = max(
            (v for v in updated.values() if isinstance(v, (int, float))),
            default=None,
        )
        sensor_age = (
            round(max(0.0, now - latest_sensor_ts), 2)
            if latest_sensor_ts is not None
            else None
        )

        parsed = {
            "depth": depth.get("depth"),
            "pressure": depth.get("pressure"),
            "roll": attitude.get("roll"),
            "pitch": attitude.get("pitch"),
            "yaw": attitude.get("yaw"),
            "proximity": "TRIGGERED" if prox.get("triggered") else ("IDLE" if prox else None),
            "mission": values.get("mission"),
            "sensor_age": sensor_age,
            "processes": processes,
            "raw": item,
        }
        with self._lock:
            self._last_telemetry_at = now
            self._telemetry = parsed
        return parsed

    def apply_connection_settings(self, host: str, port: int, camera_http_port: int) -> dict:
        clean_host = host.strip()
        self.jetson.reconnect(clean_host, port)
        self.camera.reconnect(host=clean_host, port=camera_http_port)
        self.add_log(f"Target koneksi diubah: {clean_host}:{port} (kamera:{camera_http_port})")
        return self.get_state()

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            telemetry = dict(self._telemetry)
            last_telemetry_at = self._last_telemetry_at
            logs = list(self._logs)

        return {
            "connection": {
                "host": self.jetson.host,
                "port": self.jetson.port,
                "camera_http_port": self.camera.port,
                "connected": self.jetson.link,
                "last_telemetry_at": last_telemetry_at,
            },
            "manual": self.jetson.manual_status_dict(),
            "telemetry": telemetry,
            "camera": self.camera.status_dict(),
            "recording": self.recorder.status_dict(),
            "logs": logs,
        }
