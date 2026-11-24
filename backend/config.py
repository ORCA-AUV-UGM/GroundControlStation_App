"""Configuration and constants for ORCA V4 GCS backend."""
from pathlib import Path

DEFAULT_JETSON_HOST = "192.168.55.100"
DEFAULT_JETSON_PORT = 5000
DEFAULT_CAMERA_PORT = 8081

NODES = ("depth", "attitude", "proximity", "bridge", "final", "qualification")
MISSION_NODES = ("final", "qualification")

KEYS = {
    "w": ("forward", 1700),
    "s": ("forward", 1300),
    "a": ("lateral", 1300),
    "d": ("lateral", 1700),
    "Up": ("throttle", 1700),
    "Down": ("throttle", 1300),
    "Left": ("yaw", 1300),
    "Right": ("yaw", 1700),
}

# Also accept lowercase/browser KeyboardEvent.key aliases for frontend convenience
KEY_ALIASES = {
    "W": "w",
    "S": "s",
    "A": "a",
    "D": "d",
    "ArrowUp": "Up",
    "ArrowDown": "Down",
    "ArrowLeft": "Left",
    "ArrowRight": "Right",
}

NEUTRAL_PWM = 1500
MIN_PWM = 1100
MAX_PWM = 1900

MANUAL_TICK_INTERVAL = 0.1
HEARTBEAT_TIMEOUT = 3.0
RECONNECT_DELAY = 2.0

RECORD_FPS = 15.0
RECORD_DIR = Path.home() / "Videos" / "ORCA_V4"
MAX_LOG_ENTRIES = 300
