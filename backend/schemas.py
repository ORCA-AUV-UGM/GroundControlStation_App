"""Pydantic request and response schemas for ORCA V4 GCS API."""
from typing import Dict, List, Literal, Optional
from pydantic import BaseModel, Field

from .config import MAX_PWM, MIN_PWM, NEUTRAL_PWM


class ConnectionSettingsRequest(BaseModel):
    host: str = Field(..., min_length=1, description="Jetson IP or hostname")
    port: int = Field(5000, ge=1, le=65535, description="Jetson TCP telemetry port")
    camera_http_port: int = Field(8081, ge=1, le=65535, description="Jetson MJPEG HTTP port")


class ManualToggleRequest(BaseModel):
    enabled: bool


class ManualKeysRequest(BaseModel):
    held_keys: List[str] = Field(default_factory=list)


class ManualKeyEventRequest(BaseModel):
    key: str
    pressed: bool


class ManualPwmRequest(BaseModel):
    forward: int = Field(NEUTRAL_PWM, ge=MIN_PWM, le=MAX_PWM)
    lateral: int = Field(NEUTRAL_PWM, ge=MIN_PWM, le=MAX_PWM)
    throttle: int = Field(NEUTRAL_PWM, ge=MIN_PWM, le=MAX_PWM)
    yaw: int = Field(NEUTRAL_PWM, ge=MIN_PWM, le=MAX_PWM)


class NodeCommandRequest(BaseModel):
    action: Literal["start", "stop"]
    node: str


class ActionResponse(BaseModel):
    ok: bool
    message: str
    data: Optional[Dict] = None
