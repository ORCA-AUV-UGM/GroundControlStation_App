# ORCA V4 GCS FastAPI Backend

Backend FastAPI hasil pemisahan logika jaringan, telemetri, kontrol manual, kamera MJPEG, dan perekam MP4 dari `gcs_new_banget.py`.

## Instalasi

```bash
python3 -m pip install -r backend/requirements.txt
```

## Menjalankan Server

Dari root folder project (`GCS/`):

```bash
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
```

Dokumentasi interaktif Swagger UI tersedia di: `http://localhost:8000/docs`.

## Ringkasan Endpoint

- **State & Koneksi**
  - `GET /api/health` — status kesehatan backend & link TCP Jetson
  - `GET /api/state` — snapshot lengkap (koneksi, manual, telemetri, kamera, rekaman, log)
  - `POST /api/connection` — ubah `host`, `port`, `camera_http_port` dan reconnect
- **Kontrol Node & Misi (`depth`, `attitude`, `proximity`, `bridge`, `final`, `qualification`)**
  - `POST /api/nodes/{node}/start`
  - `POST /api/nodes/{node}/stop`
  - `POST /api/nodes/command`
- **Kontrol Manual & ARM/DISARM**
  - `POST /api/manual/toggle` (`{"enabled": true/false}`)
  - `POST /api/manual/arm`
  - `POST /api/manual/disarm`
  - `POST /api/manual/keys` (`{"held_keys": ["w", "Up"]}`)
  - `POST /api/manual/key` (`{"key": "w", "pressed": true}`)
  - `POST /api/manual/pwm` (`{"forward": 1500, "lateral": 1500, "throttle": 1500, "yaw": 1500}`)
- **Kamera MJPEG Proxy**
  - `GET /api/camera/stream` — endpoint MJPEG (`<img src="http://localhost:8000/api/camera/stream" />`)
  - `GET /api/camera/snapshot` — frame JPEG terbaru
  - `GET /api/camera/status`
  - `POST /api/camera/start` | `POST /api/camera/stop` | `POST /api/camera/reconnect`
- **Perekaman MP4 (`~/Videos/ORCA_V4`)**
  - `GET /api/recording/status`
  - `POST /api/recording/start` | `POST /api/recording/stop`
  - `GET /api/recording/files` | `GET /api/recording/files/{filename}`
- **Log Sistem**
  - `GET /api/logs` | `DELETE /api/logs`
- **WebSocket Real-time (`ws://localhost:8000/ws`)**
  - Mengirim event: `state`, `telemetry`, `link`, `ack`, `log`, `record_done`
  - Menerima perintah: `manual_keys`, `manual_key`, `manual_pwm`, `toggle_manual`, `arm`, `disarm`, `node_command`, `ping`
