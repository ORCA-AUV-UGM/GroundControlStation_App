"""FastAPI application entry point for ORCA V4 Ground Control Station backend."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse

from .config import NODES, RECORD_DIR
from .manager import GCSService
from .schemas import (
    ActionResponse,
    ConnectionSettingsRequest,
    ManualKeyEventRequest,
    ManualKeysRequest,
    ManualPwmRequest,
    ManualToggleRequest,
    NodeCommandRequest,
)

gcs = GCSService()


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()
    gcs.start(loop)
    try:
        yield
    finally:
        gcs.close()


app = FastAPI(
    title="ORCA V4 GCS Backend",
    description="FastAPI backend for ORCA V4 Ground Control Station (TCP telemetry, manual control, MJPEG proxy & MP4 recorder).",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health_check() -> dict:
    return {"status": "ok", "connected": gcs.jetson.link}


@app.get("/api/state")
def get_state() -> dict:
    return gcs.get_state()


@app.post("/api/connection", response_model=ActionResponse)
def update_connection(payload: ConnectionSettingsRequest) -> ActionResponse:
    state = gcs.apply_connection_settings(
        host=payload.host,
        port=payload.port,
        camera_http_port=payload.camera_http_port,
    )
    return ActionResponse(
        ok=True,
        message=f"Target koneksi diubah ke {payload.host}:{payload.port}",
        data=state["connection"],
    )


# ---------------- NODE / MISSION CONTROL ----------------
def _execute_node_command(action: str, node: str) -> ActionResponse:
    if node not in NODES:
        raise HTTPException(status_code=404, detail=f"Node tidak dikenal: {node}")
    try:
        gcs.jetson.send_node_command(action, node)
    except ValueError as exc:
        gcs.add_log(str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ConnectionError as exc:
        gcs.add_log("Command rejected: Jetson offline")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RuntimeError as exc:
        gcs.add_log(str(exc))
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    return ActionResponse(ok=True, message=f"Perintah {action.upper()} {node} dikirim.")


@app.post("/api/nodes/{node}/start", response_model=ActionResponse)
def start_node(node: str) -> ActionResponse:
    return _execute_node_command("start", node)


@app.post("/api/nodes/{node}/stop", response_model=ActionResponse)
def stop_node(node: str) -> ActionResponse:
    return _execute_node_command("stop", node)


@app.post("/api/nodes/command", response_model=ActionResponse)
def node_command(payload: NodeCommandRequest) -> ActionResponse:
    return _execute_node_command(payload.action, payload.node)


# ---------------- MANUAL CONTROL & ARM/DISARM ----------------
@app.post("/api/manual/toggle", response_model=ActionResponse)
def toggle_manual(payload: ManualToggleRequest) -> ActionResponse:
    try:
        enabled = gcs.jetson.toggle_manual(payload.enabled)
    except (ConnectionError, RuntimeError) as exc:
        gcs.add_log(str(exc))
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    gcs.add_log("Kontrol manual " + ("DIAKTIFKAN" if enabled else "DIMATIKAN"))
    return ActionResponse(
        ok=True,
        message="Manual control updated",
        data=gcs.jetson.manual_status_dict(),
    )


@app.post("/api/manual/arm", response_model=ActionResponse)
def request_arm() -> ActionResponse:
    try:
        gcs.jetson.request_arm()
    except ValueError as exc:
        gcs.add_log(str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ConnectionError as exc:
        gcs.add_log(str(exc))
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    gcs.add_log("ARM diminta.")
    return ActionResponse(
        ok=True,
        message="ARM diminta.",
        data=gcs.jetson.manual_status_dict(),
    )


@app.post("/api/manual/disarm", response_model=ActionResponse)
def request_disarm() -> ActionResponse:
    try:
        gcs.jetson.request_disarm()
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    gcs.add_log("DISARM diminta.")
    return ActionResponse(
        ok=True,
        message="DISARM diminta.",
        data=gcs.jetson.manual_status_dict(),
    )


@app.post("/api/manual/keys", response_model=ActionResponse)
def set_manual_keys(payload: ManualKeysRequest) -> ActionResponse:
    held = gcs.jetson.set_held_keys(payload.held_keys)
    return ActionResponse(
        ok=True,
        message="Held keys updated",
        data={"held_keys": held, "pwm": gcs.jetson.compute_current_pwm()},
    )


@app.post("/api/manual/key", response_model=ActionResponse)
def update_manual_key(payload: ManualKeyEventRequest) -> ActionResponse:
    held = gcs.jetson.update_key(payload.key, payload.pressed)
    return ActionResponse(
        ok=True,
        message="Key state updated",
        data={"held_keys": held, "pwm": gcs.jetson.compute_current_pwm()},
    )


@app.post("/api/manual/pwm", response_model=ActionResponse)
def set_manual_pwm(payload: ManualPwmRequest) -> ActionResponse:
    try:
        pwm = gcs.jetson.set_manual_pwm(
            forward=payload.forward,
            lateral=payload.lateral,
            throttle=payload.throttle,
            yaw=payload.yaw,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ActionResponse(ok=True, message="Manual PWM updated", data={"pwm": pwm})


# ---------------- CAMERA STREAM & CONTROL ----------------
@app.get("/api/camera/status")
def camera_status() -> dict:
    return gcs.camera.status_dict()


@app.post("/api/camera/start", response_model=ActionResponse)
def camera_start() -> ActionResponse:
    gcs.camera.start()
    return ActionResponse(ok=True, message="Kamera dimulai", data=gcs.camera.status_dict())


@app.post("/api/camera/stop", response_model=ActionResponse)
def camera_stop() -> ActionResponse:
    gcs.camera.stop()
    return ActionResponse(ok=True, message="Kamera dihentikan", data=gcs.camera.status_dict())


@app.post("/api/camera/reconnect", response_model=ActionResponse)
def camera_reconnect() -> ActionResponse:
    gcs.camera.reconnect()
    return ActionResponse(ok=True, message="Menghubungkan ulang kamera", data=gcs.camera.status_dict())


@app.get("/api/camera/snapshot")
def camera_snapshot() -> Response:
    jpg, _, age = gcs.camera.get_latest_frame()
    if jpg is None or age > 5.0:
        raise HTTPException(status_code=503, detail="Frame kamera belum tersedia")
    return Response(content=jpg, media_type="image/jpeg")


@app.get("/api/camera/stream")
async def camera_stream() -> StreamingResponse:
    async def frame_generator() -> AsyncGenerator[bytes, None]:
        last_seq = -1
        while not gcs.stop_event.is_set():
            jpg, seq = await asyncio.to_thread(gcs.camera.wait_next_frame, last_seq, 1.0)
            if jpg is not None and seq != last_seq:
                last_seq = seq
                header = (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(jpg)}\r\n\r\n".encode("ascii")
                )
                yield header + jpg + b"\r\n"
            else:
                await asyncio.sleep(0.05)

    return StreamingResponse(
        frame_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store"},
    )


# ---------------- MP4 RECORDING ----------------
@app.get("/api/recording/status")
def recording_status() -> dict:
    return gcs.recorder.status_dict()


@app.post("/api/recording/start", response_model=ActionResponse)
def recording_start() -> ActionResponse:
    try:
        path = gcs.recorder.start(
            camera_enabled=gcs.camera.enabled.is_set(),
            last_frame_age=gcs.camera.last_frame_age(),
        )
    except ValueError as exc:
        gcs.add_log(str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Gagal membuat folder rekaman: {exc}") from exc

    return ActionResponse(
        ok=True,
        message=f"Mulai merekam ke {path}",
        data=gcs.recorder.status_dict(),
    )


@app.post("/api/recording/stop", response_model=ActionResponse)
def recording_stop() -> ActionResponse:
    stopped = gcs.recorder.stop()
    return ActionResponse(
        ok=stopped,
        message="Menghentikan rekaman..." if stopped else "Tidak ada rekaman aktif",
        data=gcs.recorder.status_dict(),
    )


@app.get("/api/recording/files")
def list_recordings() -> dict:
    if not RECORD_DIR.exists():
        return {"directory": str(RECORD_DIR), "files": []}
    files = []
    for p in sorted(RECORD_DIR.glob("*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True):
        stat = p.stat()
        files.append(
            {
                "name": p.name,
                "size_bytes": stat.st_size,
                "modified_at": stat.st_mtime,
            }
        )
    return {"directory": str(RECORD_DIR), "files": files}


@app.get("/api/recording/files/{filename}")
def download_recording(filename: str) -> FileResponse:
    safe_name = Path(filename).name
    target = (RECORD_DIR / safe_name).resolve()
    if target.parent != RECORD_DIR.resolve() or not target.is_file():
        raise HTTPException(status_code=404, detail="File rekaman tidak ditemukan")
    return FileResponse(path=str(target), media_type="video/mp4", filename=safe_name)


# ---------------- LOGS ----------------
@app.get("/api/logs")
def get_logs() -> dict:
    return {"logs": gcs.get_logs()}


@app.delete("/api/logs", response_model=ActionResponse)
def clear_logs() -> ActionResponse:
    gcs.clear_logs()
    return ActionResponse(ok=True, message="Log dibersihkan")


# ---------------- WEBSOCKET REALTIME ----------------
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    queue = gcs.register_ws_client()

    async def sender() -> None:
        await websocket.send_json({"type": "state", "data": gcs.get_state()})
        while True:
            event = await queue.get()
            await websocket.send_json(event)

    async def receiver() -> None:
        while True:
            msg = await websocket.receive_json()
            mtype = msg.get("type")
            try:
                if mtype == "manual_keys":
                    gcs.jetson.set_held_keys(msg.get("held_keys", []))
                elif mtype == "manual_key":
                    gcs.jetson.update_key(str(msg.get("key", "")), bool(msg.get("pressed")))
                elif mtype == "manual_pwm":
                    gcs.jetson.set_manual_pwm(
                        forward=int(msg.get("forward", 1500)),
                        lateral=int(msg.get("lateral", 1500)),
                        throttle=int(msg.get("throttle", 1500)),
                        yaw=int(msg.get("yaw", 1500)),
                    )
                elif mtype == "toggle_manual":
                    enabled = gcs.jetson.toggle_manual(bool(msg.get("enabled")))
                    gcs.add_log("Kontrol manual " + ("DIAKTIFKAN" if enabled else "DIMATIKAN"))
                elif mtype == "arm":
                    gcs.jetson.request_arm()
                    gcs.add_log("ARM diminta.")
                elif mtype == "disarm":
                    gcs.jetson.request_disarm()
                    gcs.add_log("DISARM diminta.")
                elif mtype == "node_command":
                    gcs.jetson.send_node_command(
                        str(msg.get("action", "")), str(msg.get("node", ""))
                    )
                elif mtype == "ping":
                    await websocket.send_json({"type": "pong"})
            except (ValueError, ConnectionError, RuntimeError) as exc:
                await websocket.send_json(
                    {"type": "ack", "data": {"ok": False, "message": str(exc)}}
                )

    sender_task = asyncio.create_task(sender())
    receiver_task = asyncio.create_task(receiver())
    try:
        done, pending = await asyncio.wait(
            {sender_task, receiver_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc and not isinstance(exc, WebSocketDisconnect):
                raise exc
    except WebSocketDisconnect:
        pass
    finally:
        gcs.unregister_ws_client(queue)
