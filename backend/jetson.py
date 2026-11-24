"""TCP telemetry and command client for Jetson ORCA V4 server."""
import json
import queue
import socket
import threading
import time
from typing import Callable, Optional

from .config import (
    DEFAULT_JETSON_HOST,
    DEFAULT_JETSON_PORT,
    HEARTBEAT_TIMEOUT,
    KEY_ALIASES,
    KEYS,
    MANUAL_TICK_INTERVAL,
    MAX_PWM,
    MIN_PWM,
    MISSION_NODES,
    NEUTRAL_PWM,
    NODES,
    RECONNECT_DELAY,
)


class JetsonClient:
    """Maintains a persistent newline-delimited JSON TCP link with the Jetson server."""

    def __init__(
        self,
        stop_event: threading.Event,
        on_event: Callable[[dict], None],
        host: str = DEFAULT_JETSON_HOST,
        port: int = DEFAULT_JETSON_PORT,
    ):
        self._stop = stop_event
        self._on_event = on_event

        self._state_lock = threading.RLock()
        self.host = host
        self.port = port
        self.link = False
        self.manual_enabled = False
        self.arm_state = "disarm"
        self.held_keys: set[str] = set()
        self.override_pwm: Optional[dict[str, int]] = None

        self._tx: queue.Queue = queue.Queue(maxsize=300)
        self._sock_lock = threading.Lock()
        self._active_sock: Optional[socket.socket] = None
        self._reconnect_flag = threading.Event()

        self._net_thread: Optional[threading.Thread] = None
        self._manual_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._net_thread is None:
            self._net_thread = threading.Thread(
                target=self._network_loop, daemon=True, name="orca-tcp"
            )
            self._net_thread.start()
        if self._manual_thread is None:
            self._manual_thread = threading.Thread(
                target=self._manual_loop, daemon=True, name="orca-manual-tick"
            )
            self._manual_thread.start()

    def reconnect(self, host: str, port: int) -> None:
        with self._state_lock:
            self.host = host.strip() or DEFAULT_JETSON_HOST
            self.port = int(port)
        self._reconnect_flag.set()
        with self._sock_lock:
            if self._active_sock is not None:
                try:
                    self._active_sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    self._active_sock.close()
                except OSError:
                    pass

    def queue_command(self, payload: dict) -> None:
        with self._state_lock:
            if not self.link:
                raise ConnectionError("Jetson offline")
        try:
            self._tx.put_nowait(payload)
        except queue.Full as exc:
            raise RuntimeError("TX queue full; command dropped") from exc

    def send_node_command(self, action: str, node: str) -> None:
        if action not in ("start", "stop"):
            raise ValueError("Invalid action; expected 'start' or 'stop'")
        if node not in NODES:
            raise ValueError(f"Unknown node: {node}")
        with self._state_lock:
            if node in MISSION_NODES and action == "start" and self.manual_enabled:
                raise ValueError("Matikan manual sebelum misi otonom")
        self.queue_command({"action": action, "node": node})

    def toggle_manual(self, enabled: bool) -> bool:
        with self._state_lock:
            self.manual_enabled = bool(enabled)
            if not self.manual_enabled:
                self.held_keys.clear()
                self.override_pwm = None
                self.arm_state = "disarm"
                link_up = self.link
            else:
                link_up = self.link

        if not enabled and link_up:
            self.queue_command(
                {
                    "action": "manual",
                    "arm_mode": "",
                    "forward": NEUTRAL_PWM,
                    "lateral": NEUTRAL_PWM,
                    "yaw": NEUTRAL_PWM,
                    "throttle": NEUTRAL_PWM,
                }
            )
            self.queue_command({"action": "manual_release"})
        return self.manual_enabled

    def request_arm(self) -> None:
        with self._state_lock:
            if not self.manual_enabled:
                raise ValueError("Aktifkan switch manual terlebih dahulu")
            if not self.link:
                raise ConnectionError("ARM ditolak: Jetson offline")
            self.arm_state = "arm"

    def request_disarm(self) -> None:
        with self._state_lock:
            self.arm_state = "disarm"
            self.held_keys.clear()
            self.override_pwm = None
            link_up = self.link

        if link_up:
            self.queue_command(
                {
                    "action": "manual",
                    "arm_mode": "disarm",
                    "forward": NEUTRAL_PWM,
                    "lateral": NEUTRAL_PWM,
                    "yaw": NEUTRAL_PWM,
                    "throttle": NEUTRAL_PWM,
                }
            )

    def set_held_keys(self, keys: list[str]) -> list[str]:
        normalized = set()
        for raw in keys:
            k = KEY_ALIASES.get(raw, raw)
            if k in KEYS:
                normalized.add(k)
        with self._state_lock:
            self.held_keys = normalized
            self.override_pwm = None
            return sorted(self.held_keys)

    def update_key(self, key: str, pressed: bool) -> list[str]:
        k = KEY_ALIASES.get(key, key)
        with self._state_lock:
            if pressed:
                if self.manual_enabled and k in KEYS:
                    self.held_keys.add(k)
            else:
                self.held_keys.discard(k)
            self.override_pwm = None
            return sorted(self.held_keys)

    def set_manual_pwm(self, forward: int, lateral: int, throttle: int, yaw: int) -> dict[str, int]:
        values = {"forward": forward, "lateral": lateral, "throttle": throttle, "yaw": yaw}
        for axis, val in values.items():
            if not (MIN_PWM <= val <= MAX_PWM):
                raise ValueError(f"{axis} PWM out of range ({MIN_PWM}-{MAX_PWM})")
        with self._state_lock:
            self.override_pwm = values
            return dict(values)

    def compute_current_pwm(self) -> dict[str, int]:
        with self._state_lock:
            if self.override_pwm is not None:
                return dict(self.override_pwm)
            values = {axis: NEUTRAL_PWM for axis in ("forward", "lateral", "throttle", "yaw")}
            for key in self.held_keys:
                if key in KEYS:
                    axis, pwm = KEYS[key]
                    if values[axis] == NEUTRAL_PWM:
                        values[axis] = pwm
            return values

    def manual_status_dict(self) -> dict:
        with self._state_lock:
            return {
                "enabled": self.manual_enabled,
                "arm_state": self.arm_state,
                "held_keys": sorted(self.held_keys),
                "pwm": self.compute_current_pwm(),
            }

    def _manual_loop(self) -> None:
        while not self._stop.is_set():
            with self._state_lock:
                should_send = self.manual_enabled and self.link
                arm_state = self.arm_state
            if should_send:
                values = self.compute_current_pwm()
                try:
                    self.queue_command({"action": "manual", "arm_mode": arm_state, **values})
                except (ConnectionError, RuntimeError):
                    pass
            self._stop.wait(MANUAL_TICK_INTERVAL)

    def _network_loop(self) -> None:
        while not self._stop.is_set():
            self._reconnect_flag.clear()
            with self._state_lock:
                host = self.host
                port = self.port
            try:
                with socket.create_connection((host, port), timeout=2.0) as s:
                    s.settimeout(0.1)
                    with self._sock_lock:
                        self._active_sock = s
                    with self._state_lock:
                        self.link = True
                    self._on_event({"type": "link", "connected": True})

                    buf = b""
                    last_rx = time.monotonic()
                    while not self._stop.is_set() and not self._reconnect_flag.is_set():
                        try:
                            while True:
                                cmd = self._tx.get_nowait()
                                s.sendall((json.dumps(cmd) + "\n").encode())
                        except queue.Empty:
                            pass

                        try:
                            packet = s.recv(8192)
                            if not packet:
                                break
                            last_rx = time.monotonic()
                            buf += packet
                            if len(buf) > 100000:
                                raise ConnectionError("RX packet too large")
                            while b"\n" in buf:
                                line, buf = buf.split(b"\n", 1)
                                try:
                                    self._on_event(json.loads(line))
                                except json.JSONDecodeError:
                                    pass
                        except socket.timeout:
                            pass

                        if time.monotonic() - last_rx > HEARTBEAT_TIMEOUT:
                            raise ConnectionError("Telemetry heartbeat timeout")
            except (OSError, ConnectionError, ValueError) as exc:
                self._on_event(
                    {"type": "ack", "ok": False, "message": f"Connection: {exc}"}
                )
            finally:
                with self._sock_lock:
                    self._active_sock = None
                with self._state_lock:
                    self.link = False
                    self.held_keys.clear()
                    self.override_pwm = None
                    self.manual_enabled = False
                    self.arm_state = "disarm"
                self._on_event({"type": "link", "connected": False})
                while not self._tx.empty():
                    try:
                        self._tx.get_nowait()
                    except queue.Empty:
                        break

            if not self._reconnect_flag.is_set():
                self._stop.wait(RECONNECT_DELAY)
