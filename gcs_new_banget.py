#!/usr/bin/env python3
"""ORCA V4 GCS: Embedded MJPEG streaming & MP4 recorder with Figma-styled CustomTkinter UI."""
import json
import queue
import socket
import threading
import time
import io
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime

try:
    from PIL import Image, ImageTk
except ImportError as exc:
    raise SystemExit("Pillow belum terpasang. Jalankan: python3 -m pip install pillow") from exc

import tkinter as tk
from tkinter import messagebox
import customtkinter as ctk

NODES = ['depth', 'attitude', 'proximity', 'bridge', 'final', 'qualification']
KEYS = {
    'w': ('forward', 1700), 's': ('forward', 1300),
    'a': ('lateral', 1300), 'd': ('lateral', 1700),
    'Up': ('throttle', 1700), 'Down': ('throttle', 1300),
    'Left': ('yaw', 1300), 'Right': ('yaw', 1700)
}

# Palet warna sesuai Desain Figma
BG_APP = "#F3F4F6"
BG_CARD = "#FFFFFF"
BORDER_CARD = "#E5E7EB"
TEXT_MAIN = "#1F2937"
TEXT_MUTED = "#6B7280"
PRIMARY_BLUE = "#2563EB"
CAM_BG = "#1E232A"


class ModernGCS(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")

        self.title("ORCA V4 | Ground Control Station")
        self.geometry("1300x860")
        self.minsize(1100, 800)
        self.configure(fg_color=BG_APP)

        # Komunikasi Jaringan
        self.events = queue.Queue()
        self.tx = queue.Queue(maxsize=300)
        self.stop = threading.Event()
        self.link = False
        self.manual_enabled = False
        self.held = set()
        self.arm_state = 'disarm'

        # Kamera & Rekaman MP4
        self.camera_enabled = threading.Event()
        self.camera_lock = threading.Lock()
        self.camera_latest = None
        self.camera_seq = 0
        self.camera_last_frame_time = 0.0
        self.camera_error = ''
        self.camera_response = None
        self.camera_photo = None
        self.camera_displayed_seq = -1
        self.camera_display_times = []
        self.camera_status = tk.StringVar(value='CAMERA: OFFLINE')
        self.camera_fps = tk.StringVar(value='FPS: —')
        self.camera_http_port = 8081

        self.record_active = threading.Event()
        self.record_queue = queue.Queue(maxsize=8)
        self.record_thread = None
        self.record_path = None
        self.record_status = tk.StringVar(value='REC: OFF')
        self.recorded_frames = 0
        self.record_fps = 15.0

        # State Telemetri & Input
        self.hostname = tk.StringVar(value='192.168.55.100')
        self.port = tk.IntVar(value=5000)
        self.status = tk.StringVar(value='DISCONNECTED')
        self.arm_badge_text = tk.StringVar(value='ARM: —')
        self.metrics = {name: tk.StringVar(value='—') for name in (
            'depth', 'pressure', 'roll', 'pitch', 'yaw', 'proximity', 'mission', 'sensor_age'
        )}
        self.proc_vars = {k: tk.StringVar(value='OFF') for k in NODES}

        self.last_telemetry = 0
        self.current_host = '192.168.55.100'
        self.current_port = 5000

        self.build_ui()

        # Keyboard & Lifecycle Hooks
        self.bind_all('<KeyPress>', self.key_down)
        self.bind_all('<KeyRelease>', self.key_up)
        self.protocol('WM_DELETE_WINDOW', self.close)

        # Periodic Workers
        self.after(60, self.pump)
        self.after(100, self.manual_tick)
        self.after(30, self.camera_tick)
        self.after(400, self.start_camera)
        threading.Thread(target=self.network, daemon=True).start()

    def build_ui(self):
        # ---------------- NAVBAR ATAS ----------------
        navbar = ctk.CTkFrame(self, fg_color=BG_CARD, height=54, corner_radius=0, border_width=1, border_color=BORDER_CARD)
        navbar.pack(side="top", fill="x")

        nav_left = ctk.CTkFrame(navbar, fg_color="transparent")
        nav_left.pack(side="left", padx=16, pady=8)

        lbl_brand = ctk.CTkLabel(nav_left, text="ORCA V4", font=("Arial", 15, "bold"), text_color=PRIMARY_BLUE)
        lbl_brand.pack(side="left")
        lbl_sub = ctk.CTkLabel(nav_left, text=" / Ground Control Station", font=("Arial", 14), text_color=TEXT_MUTED)
        lbl_sub.pack(side="left", padx=(0, 20))

        ctk.CTkLabel(nav_left, text="Jetson", font=("Arial", 12), text_color=TEXT_MUTED).pack(side="left", padx=(0, 6))
        self.ent_host = ctk.CTkEntry(nav_left, textvariable=self.hostname, width=135, height=28, corner_radius=6)
        self.ent_host.pack(side="left", padx=(0, 8))

        self.ent_port = ctk.CTkEntry(nav_left, textvariable=self.port, width=65, height=28, corner_radius=6)
        self.ent_port.pack(side="left", padx=(0, 8))

        btn_conn = ctk.CTkButton(
            nav_left, text="Hubungkan", width=90, height=28, corner_radius=6,
            fg_color="transparent", border_width=1, border_color=PRIMARY_BLUE,
            text_color=PRIMARY_BLUE, hover_color="#EFF6FF", command=self.apply_network_settings
        )
        btn_conn.pack(side="left")

        # Badges Status Kanan Atas
        nav_right = ctk.CTkFrame(navbar, fg_color="transparent")
        nav_right.pack(side="right", padx=16)

        self.badge_status = ctk.CTkLabel(
            nav_right, textvariable=self.status, font=("Arial", 10, "bold"),
            fg_color="#374151", text_color="#FFFFFF", corner_radius=4, padx=8, pady=2
        )
        self.badge_status.pack(side="left", padx=4)

        self.badge_arm = ctk.CTkLabel(
            nav_right, textvariable=self.arm_badge_text, font=("Arial", 10, "bold"),
            fg_color="#374151", text_color="#FFFFFF", corner_radius=4, padx=8, pady=2
        )
        self.badge_arm.pack(side="left", padx=4)

        # ---------------- LAYOUT KONTEN ----------------
        main_layout = ctk.CTkFrame(self, fg_color="transparent")
        main_layout.pack(fill="both", expand=True, padx=14, pady=12)

        # ================= KOLOM KIRI: KAMERA & TELEMETRI =================
        col_left = ctk.CTkFrame(main_layout, fg_color="transparent")
        col_left.pack(side="left", fill="both", expand=True, padx=(0, 8))

        # Card Kamera Utama
        cam_card = ctk.CTkFrame(col_left, fg_color=BG_CARD, corner_radius=8, border_width=1, border_color=BORDER_CARD)
        cam_card.pack(fill="both", expand=True, pady=(0, 10))

        cam_header = ctk.CTkFrame(cam_card, fg_color="transparent", height=36)
        cam_header.pack(fill="x", padx=12, pady=(8, 4))
        ctk.CTkLabel(cam_header, text="Kamera", font=("Arial", 13, "bold"), text_color=TEXT_MAIN).pack(side="left")

        # Tombol aksi kamera & rekam
        cam_actions = ctk.CTkFrame(cam_header, fg_color="transparent")
        cam_actions.pack(side="right")

        self.btn_rec = ctk.CTkButton(
            cam_actions, text="● REC", width=62, height=24, font=("Arial", 10, "bold"),
            fg_color="#EF4444", hover_color="#DC2626", text_color="#FFFFFF", corner_radius=4,
            command=self.start_recording
        )
        self.btn_rec.pack(side="left", padx=3)

        self.btn_rec_stop = ctk.CTkButton(
            cam_actions, text="STOP REC", width=72, height=24, font=("Arial", 10),
            fg_color="#E5E7EB", hover_color="#D1D5DB", text_color=TEXT_MAIN, corner_radius=4,
            command=self.stop_recording, state="disabled"
        )
        self.btn_rec_stop.pack(side="left", padx=3)

        btn_cam_reload = ctk.CTkButton(
            cam_actions, text="Muat ulang", width=75, height=24, font=("Arial", 11),
            fg_color="transparent", border_width=1, border_color=BORDER_CARD,
            text_color=TEXT_MUTED, hover_color="#F3F4F6", corner_radius=4,
            command=self.reconnect_camera
        )
        btn_cam_reload.pack(side="left", padx=(3, 0))

        # Layar Penampil MJPEG
        self.camera_panel = tk.Frame(cam_card, bg=CAM_BG, highlightthickness=0)
        self.camera_panel.pack(fill="both", expand=True, padx=12, pady=(0, 6))

        self.camera_image_label = tk.Label(
            self.camera_panel, bg=CAM_BG, fg="#9CA3AF",
            text="Menunggu koneksi...", font=("Arial", 12)
        )
        self.camera_image_label.pack(fill="both", expand=True)

        # Bar Info Status Kamera
        cam_footer = ctk.CTkFrame(cam_card, fg_color="transparent", height=24)
        cam_footer.pack(fill="x", padx=12, pady=(0, 8))
        ctk.CTkLabel(cam_footer, textvariable=self.camera_status, font=("Arial", 10), text_color=TEXT_MUTED).pack(side="left")
        ctk.CTkLabel(cam_footer, textvariable=self.record_status, font=("Arial", 10, "bold"), text_color=TEXT_MUTED).pack(side="left", padx=12)
        ctk.CTkLabel(cam_footer, textvariable=self.camera_fps, font=("Arial", 10), text_color=TEXT_MUTED).pack(side="right")

        # Card Grid Metrik Telemetri Horizontal
        bottom_metrics = ctk.CTkFrame(col_left, fg_color="transparent")
        bottom_metrics.pack(fill="x")
        bottom_metrics.columnconfigure((0, 1, 2, 3), weight=1, uniform="metric_cards")

        metric_cards_def = [("Depth", "depth"), ("Pressure", "pressure"), ("Roll", "roll"), ("Pitch", "pitch")]
        for idx, (label_text, var_key) in enumerate(metric_cards_def):
            card = ctk.CTkFrame(bottom_metrics, fg_color=BG_CARD, corner_radius=8, border_width=1, border_color=BORDER_CARD, height=72)
            card.grid(row=0, column=idx, padx=(0 if idx == 0 else 6, 0), sticky="nsew")
            card.pack_propagate(False)

            ctk.CTkLabel(card, text=label_text, font=("Arial", 11), text_color=TEXT_MUTED).pack(anchor="w", padx=12, pady=(8, 0))
            ctk.CTkLabel(card, textvariable=self.metrics[var_key], font=("Arial", 15, "bold"), text_color=TEXT_MAIN).pack(anchor="w", padx=12, pady=(2, 6))

        # ================= KOLOM KANAN: KONTROL, MISI, LOG =================
        col_right = ctk.CTkFrame(main_layout, fg_color="transparent", width=380)
        col_right.pack(side="right", fill="y", padx=(8, 0))
        col_right.pack_propagate(False)

        # 1. Card Kontrol Manual
        ctrl_card = ctk.CTkFrame(col_right, fg_color=BG_CARD, corner_radius=8, border_width=1, border_color=BORDER_CARD)
        ctrl_card.pack(fill="x", pady=(0, 8))

        ctk.CTkLabel(ctrl_card, text="Kontrol manual", font=("Arial", 13, "bold"), text_color=TEXT_MAIN).pack(anchor="w", padx=14, pady=(10, 4))

        self.switch_manual = ctk.CTkSwitch(
            ctrl_card, text="Manual keyboard", font=("Arial", 12),
            command=self.toggle_manual, progress_color=PRIMARY_BLUE
        )
        self.switch_manual.pack(anchor="w", padx=14, pady=(2, 6))

        hint_text = "W/S: maju-mundur • A/D: geser • ↑/↓: naik-turun • ←/→: yaw • Lepas tombol = netral (1500)."
        ctk.CTkLabel(ctrl_card, text=hint_text, font=("Arial", 10), text_color=TEXT_MUTED, wraplength=340, justify="left").pack(anchor="w", padx=14, pady=(0, 6))

        # Visual Keyboard Badges
        keys_row = ctk.CTkFrame(ctrl_card, fg_color="transparent")
        keys_row.pack(anchor="w", padx=14, pady=(0, 10))
        for key_tag in ['W', 'A', 'S', 'D', '↑', '↓', '←', '→']:
            ctk.CTkLabel(
                keys_row, text=key_tag, width=24, height=22, fg_color="#1F2937", text_color="#FFFFFF",
                corner_radius=4, font=("Arial", 10, "bold")
            ).pack(side="left", padx=2)

        # Tombol ARM (Kuning Emas)
        self.btn_arm = ctk.CTkButton(
            ctrl_card, text="Tahan 1 dtk untuk ARM", font=("Arial", 12, "bold"),
            fg_color="#FACC15", hover_color="#EAB308", text_color="#1F2937",
            height=34, corner_radius=6, command=self.request_arm
        )
        self.btn_arm.pack(fill="x", padx=14, pady=(0, 6))

        # Tombol DISARM (Merah/Pink Pastel)
        self.btn_disarm = ctk.CTkButton(
            ctrl_card, text="DISARM / NETRAL", font=("Arial", 12, "bold"),
            fg_color="#F87171", hover_color="#EF4444", text_color="#FFFFFF",
            height=34, corner_radius=6, command=self.request_disarm
        )
        self.btn_disarm.pack(fill="x", padx=14, pady=(0, 6))

        ctk.CTkLabel(
            ctrl_card, text="ARM hanya permintaan. Status asli Pixhawk ada di badge ARM (kanan atas).",
            font=("Arial", 10), text_color=TEXT_MUTED, wraplength=340, justify="left"
        ).pack(anchor="w", padx=14, pady=(0, 10))

        # 2. Card Sensor & Misi Management
        mgmt_card = ctk.CTkFrame(col_right, fg_color=BG_CARD, corner_radius=8, border_width=1, border_color=BORDER_CARD)
        mgmt_card.pack(fill="x", pady=(0, 8))

        ctk.CTkLabel(mgmt_card, text="Sensor & misi", font=("Arial", 13, "bold"), text_color=TEXT_MAIN).pack(anchor="w", padx=14, pady=(10, 6))

        nodes_container = ctk.CTkFrame(mgmt_card, fg_color="transparent")
        nodes_container.pack(fill="x", padx=14, pady=(0, 10))
        for key in NODES:
            row = ctk.CTkFrame(nodes_container, fg_color="transparent")
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(row, text=key.upper(), font=("Arial", 11), text_color=TEXT_MAIN, width=95, anchor="w").pack(side="left")
            ctk.CTkLabel(row, textvariable=self.proc_vars[key], font=("Arial", 11, "bold"), text_color=TEXT_MUTED, width=40, anchor="w").pack(side="left")

            ctk.CTkButton(
                row, text="START", width=50, height=22, font=("Arial", 10), fg_color="#E5E7EB",
                text_color=TEXT_MAIN, hover_color="#D1D5DB", command=lambda k=key: self.command('start', k)
            ).pack(side="left", padx=2)

            ctk.CTkButton(
                row, text="STOP", width=50, height=22, font=("Arial", 10), fg_color="#FEE2E2",
                text_color="#B91C1C", hover_color="#FECACA", command=lambda k=key: self.command('stop', k)
            ).pack(side="left", padx=2)

        # 3. Card Log Sistem
        log_card = ctk.CTkFrame(col_right, fg_color=BG_CARD, corner_radius=8, border_width=1, border_color=BORDER_CARD)
        log_card.pack(fill="both", expand=True)

        ctk.CTkLabel(log_card, text="Log sistem", font=("Arial", 13, "bold"), text_color=TEXT_MAIN).pack(anchor="w", padx=14, pady=(10, 4))

        self.logs = tk.Text(
            log_card, bg="#FFFFFF", fg=TEXT_MAIN, relief="flat", font=("Consolas", 9),
            highlightthickness=1, highlightbackground=BORDER_CARD, state='disabled'
        )
        self.logs.pack(fill="both", expand=True, padx=14, pady=(0, 12))

        self.log('Ground station siap. Hubungkan Jetson pada jaringan yang sama.')

    # ================= LOGIKA KAMERA & PEREKAM MP4 =================
    def reconnect_camera(self):
        self.stop_camera()
        self.after(200, self.start_camera)

    def start_camera(self):
        if self.stop.is_set() or self.camera_enabled.is_set():
            return
        self.current_host = self.hostname.get().strip() or '192.168.55.100'
        self.camera_enabled.set()
        self.camera_status.set('CAMERA: CONNECTING')
        self.camera_image_label.configure(image='', text='Menghubungkan ke stream kamera...')
        threading.Thread(target=self.camera_worker, daemon=True, name='orca-mjpeg').start()

    def stop_camera(self):
        self.stop_recording()
        self.camera_enabled.clear()
        with self.camera_lock:
            response = self.camera_response
            self.camera_response = None
            self.camera_latest = None
        if response is not None:
            try:
                response.close()
            except OSError:
                pass
        self.camera_status.set('CAMERA: STOPPED')
        self.camera_fps.set('FPS: —')
        self.camera_photo = None
        self.camera_image_label.configure(image='', text='KAMERA TERHENTI')

    def camera_worker(self):
        while self.camera_enabled.is_set() and not self.stop.is_set():
            url = f'http://{self.current_host}:{self.camera_http_port}/stream.mjpg'
            response = None
            try:
                request = urllib.request.Request(
                    url, headers={'User-Agent': 'ORCA-V4-GCS/1.0', 'Cache-Control': 'no-cache'}
                )
                response = urllib.request.urlopen(request, timeout=4)
                with self.camera_lock:
                    self.camera_response = response
                    self.camera_error = ''
                buffer = bytearray()
                while self.camera_enabled.is_set() and not self.stop.is_set():
                    chunk = response.read(8192)
                    if not chunk:
                        raise ConnectionError('MJPEG stream closed')
                    buffer.extend(chunk)
                    while True:
                        start = buffer.find(b'\xff\xd8')
                        if start < 0:
                            if len(buffer) > 16384:
                                del buffer[:-1024]
                            break
                        end = buffer.find(b'\xff\xd9', start + 2)
                        if end < 0:
                            if start:
                                del buffer[:start]
                            if len(buffer) > 4_000_000:
                                buffer.clear()
                            break
                        jpg = bytes(buffer[start:end + 2])
                        del buffer[:end + 2]
                        try:
                            with Image.open(io.BytesIO(jpg)) as img:
                                frame = img.convert('RGB')
                            if self.record_active.is_set():
                                try:
                                    self.record_queue.put_nowait((time.monotonic(), jpg))
                                except queue.Full:
                                    try:
                                        self.record_queue.get_nowait()
                                    except queue.Empty:
                                        pass
                                    try:
                                        self.record_queue.put_nowait((time.monotonic(), jpg))
                                    except queue.Full:
                                        pass
                            with self.camera_lock:
                                self.camera_latest = frame
                                self.camera_seq += 1
                                self.camera_last_frame_time = time.monotonic()
                        except (OSError, ValueError):
                            continue
            except (OSError, TimeoutError, ValueError, ConnectionError) as exc:
                with self.camera_lock:
                    self.camera_error = str(exc)
            finally:
                with self.camera_lock:
                    if self.camera_response is response:
                        self.camera_response = None
                if response is not None:
                    try:
                        response.close()
                    except OSError:
                        pass
            if self.camera_enabled.is_set() and not self.stop.is_set():
                self.stop.wait(1.5)

    def start_recording(self):
        if self.record_active.is_set() or (self.record_thread and self.record_thread.is_alive()):
            return
        with self.camera_lock:
            age = (time.monotonic() - self.camera_last_frame_time
                   if self.camera_last_frame_time else float('inf'))
        if not self.camera_enabled.is_set() or age > 2.0:
            self.log('REC ditolak: kamera belum mengirim frame aktif.')
            messagebox.showwarning('ORCA Camera', 'Mulai streaming kamera terlebih dahulu.')
            return
        try:
            import cv2  # noqa: F401
            import numpy  # noqa: F401
        except ImportError:
            messagebox.showerror(
                'ORCA Camera',
                'Perekaman MP4 membutuhkan OpenCV dan NumPy.\nInstall: pip install opencv-python numpy'
            )
            return

        folder = Path.home() / 'Videos' / 'ORCA_V4'
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror('ORCA Camera', f'Gagal membuat folder rekaman: {exc}')
            return

        name = 'ORCA_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:-3] + '.mp4'
        self.record_path = folder / name
        self.recorded_frames = 0
        self.record_queue = queue.Queue(maxsize=8)
        self.record_active.set()
        self.record_status.set('REC: STARTING')
        self.btn_rec.configure(state='disabled')
        self.btn_rec_stop.configure(state='normal')
        self.record_thread = threading.Thread(
            target=self.record_worker, args=(self.record_path,),
            daemon=True, name='orca-video-recorder'
        )
        self.record_thread.start()
        self.log(f'Mulai merekam kamera ke {self.record_path}')

    def stop_recording(self):
        if not self.record_active.is_set():
            return
        self.record_active.clear()
        self.record_status.set('REC: SAVING...')
        self.btn_rec_stop.configure(state='disabled')

    def record_worker(self, destination):
        import cv2
        import numpy as np
        writer = None
        first_at = None
        last_frame = None
        written = 0
        last_input = time.monotonic()
        error = None
        try:
            while self.record_active.is_set() or not self.record_queue.empty():
                try:
                    stamp, jpg = self.record_queue.get(timeout=0.20)
                except queue.Empty:
                    if self.record_active.is_set() and time.monotonic() - last_input > 3.0:
                        error = 'Stream kamera terhenti > 3 detik; rekaman disetop.'
                        self.record_active.clear()
                    continue
                last_input = stamp
                frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
                if frame is None:
                    continue
                if writer is None:
                    h, w = frame.shape[:2]
                    writer = cv2.VideoWriter(
                        str(destination), cv2.VideoWriter_fourcc(*'mp4v'),
                        self.record_fps, (w, h)
                    )
                    if not writer.isOpened():
                        raise OSError('VideoWriter MP4 gagal dibuka (codec mp4v).')
                    first_at = stamp
                    last_frame = frame
                    writer.write(frame)
                    written = 1
                    continue
                if frame.shape[:2] != last_frame.shape[:2]:
                    frame = cv2.resize(frame, (last_frame.shape[1], last_frame.shape[0]))
                target_count = max(written, int((stamp - first_at) * self.record_fps) + 1)
                for _ in range(min(target_count - written, int(self.record_fps * 3))):
                    writer.write(last_frame)
                    written += 1
                last_frame = frame
            if writer is not None and last_frame is not None:
                writer.write(last_frame)
                written += 1
        except Exception as exc:
            error = f'Gagal merekam: {exc}'
        finally:
            if writer is not None:
                writer.release()
            self.record_active.clear()
            self.recorded_frames = written
            self.events.put({
                'type': 'record_done', 'path': str(destination),
                'frames': written, 'error': error
            })

    def camera_tick(self):
        if self.stop.is_set():
            return
        if self.camera_enabled.is_set():
            with self.camera_lock:
                seq = self.camera_seq
                frame = self.camera_latest
                age = time.monotonic() - self.camera_last_frame_time if self.camera_last_frame_time else float('inf')
                error = self.camera_error
            if frame is not None and age < 2.0 and seq != self.camera_displayed_seq:
                self.camera_displayed_seq = seq
                width = max(1, self.camera_image_label.winfo_width() - 4)
                height = max(1, self.camera_image_label.winfo_height() - 4)
                ratio = min(width / frame.width, height / frame.height)
                size = (max(1, int(frame.width * ratio)), max(1, int(frame.height * ratio)))
                resample = getattr(Image, 'Resampling', Image).BILINEAR
                shown = frame.resize(size, resample)
                self.camera_photo = ImageTk.PhotoImage(shown)
                self.camera_image_label.configure(image=self.camera_photo, text='')
                now = time.monotonic()
                self.camera_display_times = [t for t in self.camera_display_times if now - t < 2.0]
                self.camera_display_times.append(now)
                elapsed = now - self.camera_display_times[0] if len(self.camera_display_times) > 1 else 0
                fps = (len(self.camera_display_times) - 1) / elapsed if elapsed > .1 else 0
                self.camera_fps.set(f'FPS: {fps:.1f}')
            if age < 2.0:
                self.camera_status.set('CAMERA: LIVE')
            else:
                self.camera_status.set('CAMERA: RECONNECTING' if error else 'CAMERA: NO FRAMES')
                self.camera_fps.set('FPS: —')
                if self.camera_photo is not None:
                    self.camera_photo = None
                    self.camera_image_label.configure(image='', text='VIDEO TERPUTUS / RECONNECTING...')
        self.after(30, self.camera_tick)

    # ================= LOGIKA GCS & NETWORKING =================
    def apply_network_settings(self):
        self.current_host = self.hostname.get().strip()
        try:
            self.current_port = int(self.port.get())
        except (ValueError, tk.TclError):
            pass
        self.log(f'Target koneksi diubah: {self.current_host}:{self.current_port}')
        self.reconnect_camera()

    def log(self, message):
        self.logs.configure(state='normal')
        self.logs.insert('end', time.strftime('%H:%M:%S ') + str(message) + '\n')
        self.logs.see('end')
        self.logs.configure(state='disabled')

    def queue_command(self, payload):
        if not self.link:
            self.log('Command rejected: Jetson offline')
            return
        try:
            self.tx.put_nowait(payload)
        except queue.Full:
            self.log('TX queue full; command dropped')

    def command(self, action, key):
        if key in ('final', 'qualification') and action == 'start' and self.manual_enabled:
            self.log('Matikan manual sebelum misi otonom')
            return
        self.queue_command({'action': action, 'node': key})

    def toggle_manual(self):
        self.manual_enabled = bool(self.switch_manual.get())
        if not self.manual_enabled:
            self.held.clear()
            self.arm_state = 'disarm'
            self.queue_command({
                'action': 'manual', 'arm_mode': '', 'forward': 1500,
                'lateral': 1500, 'yaw': 1500, 'throttle': 1500
            })
            self.queue_command({'action': 'manual_release'})
        self.log('Kontrol manual ' + ('DIAKTIFKAN' if self.manual_enabled else 'DIMATIKAN'))

    def request_arm(self):
        if not self.manual_enabled:
            self.log('Aktifkan switch manual terlebih dahulu')
            return
        if not self.link:
            self.log('ARM ditolak: Jetson offline')
            return
        self.arm_state = 'arm'
        self.arm_badge_text.set('ARM: REQ')
        self.badge_arm.configure(fg_color="#D97706")
        self.log('ARM diminta.')

    def request_disarm(self):
        self.arm_state = 'disarm'
        self.held.clear()
        self.arm_badge_text.set('ARM: OFF')
        self.badge_arm.configure(fg_color="#374151")
        self.queue_command({
            'action': 'manual', 'arm_mode': 'disarm', 'forward': 1500,
            'lateral': 1500, 'yaw': 1500, 'throttle': 1500
        })
        self.log('DISARM diminta.')

    def key_down(self, event):
        if not self.manual_enabled or isinstance(event.widget, (tk.Entry, tk.Text, ctk.CTkEntry)):
            return
        if event.keysym in KEYS:
            self.held.add(event.keysym)

    def key_up(self, event):
        self.held.discard(event.keysym)

    def manual_tick(self):
        if self.manual_enabled and self.link:
            values = {axis: 1500 for axis in ('forward', 'lateral', 'throttle', 'yaw')}
            for key in self.held:
                if key in KEYS:
                    axis, pwm = KEYS[key]
                    if values[axis] == 1500:
                        values[axis] = pwm
            self.queue_command({'action': 'manual', 'arm_mode': self.arm_state, **values})
        self.after(100, self.manual_tick)

    def pump(self):
        for _ in range(30):
            try:
                item = self.events.get_nowait()
            except queue.Empty:
                break
            if item['type'] == 'link':
                self.link = item['connected']
                self.status.set('CONNECTED' if self.link else 'DISCONNECTED')
                self.badge_status.configure(fg_color="#10B981" if self.link else "#EF4444")
                if not self.link:
                    self.held.clear()
                    self.manual_enabled = False
                    self.arm_state = 'disarm'
                    self.switch_manual.deselect()
                    self.arm_badge_text.set('ARM: —')
                    self.badge_arm.configure(fg_color="#374151")
                self.log('TCP ' + self.status.get())
            elif item['type'] == 'ack':
                self.log(('OK: ' if item.get('ok') else 'ERROR: ') + str(item.get('message')))
            elif item['type'] == 'record_done':
                self.btn_rec.configure(state='normal')
                self.btn_rec_stop.configure(state='disabled')
                self.record_thread = None
                if item.get('error'):
                    self.record_status.set('REC: ERROR')
                    self.log(item['error'])
                elif item.get('frames', 0) > 0:
                    self.record_status.set('REC: SAVED')
                    self.log('Video tersimpan: ' + item['path'])
                else:
                    self.record_status.set('REC: EMPTY')
                    self.log('Tidak ada frame yang dapat direkam.')
            elif item['type'] == 'telemetry':
                self.last_telemetry = time.time()
                self.display_telemetry(item)
        self.after(60, self.pump)

    def display_telemetry(self, item):
        status = item.get('processes', {})
        for k in NODES:
            self.proc_vars[k].set('RUN' if status.get(k) else 'OFF')
        sensor = item.get('sensor', {})
        values = sensor.get('values', {})
        depth = values.get('depth') or {}
        attitude = values.get('attitude') or {}
        prox = values.get('proximity') or {}

        for key, val, unit in [
            ('depth', depth.get('depth'), 'm'), ('pressure', depth.get('pressure'), 'mbar'),
            ('roll', attitude.get('roll'), '°'), ('pitch', attitude.get('pitch'), '°'),
            ('yaw', attitude.get('yaw'), '°')
        ]:
            self.metrics[key].set(f'{val:.2f} {unit}' if isinstance(val, (float, int)) else '—')
        self.metrics['proximity'].set('TRIGGERED' if prox.get('triggered') else ('IDLE' if prox else '—'))
        self.metrics['mission'].set(str(values.get('mission') or '—'))

    def network(self):
        while not self.stop.is_set():
            try:
                host = self.current_host
                port = self.current_port
                with socket.create_connection((host, port), timeout=2.0) as s:
                    s.settimeout(0.1)
                    self.events.put({'type': 'link', 'connected': True})
                    buf = b''
                    last_rx = time.monotonic()
                    while not self.stop.is_set():
                        try:
                            while True:
                                cmd = self.tx.get_nowait()
                                s.sendall((json.dumps(cmd) + '\n').encode())
                        except queue.Empty:
                            pass
                        try:
                            packet = s.recv(8192)
                            if not packet:
                                break
                            last_rx = time.monotonic()
                            buf += packet
                            if len(buf) > 100000:
                                raise ConnectionError('RX packet too large')
                            while b'\n' in buf:
                                line, buf = buf.split(b'\n', 1)
                                try:
                                    self.events.put(json.loads(line))
                                except json.JSONDecodeError:
                                    pass
                        except socket.timeout:
                            pass
                        if time.monotonic() - last_rx > 3:
                            raise ConnectionError('Telemetry heartbeat timeout')
            except (OSError, ConnectionError, ValueError) as e:
                self.events.put({'type': 'ack', 'ok': False, 'message': 'Connection: ' + str(e)})
            finally:
                self.events.put({'type': 'link', 'connected': False})
                while not self.tx.empty():
                    try:
                        self.tx.get_nowait()
                    except queue.Empty:
                        break
            self.stop.wait(2.0)

    def close(self):
        self.arm_state = 'disarm'
        self.stop_camera()
        self.stop.set()
        if self.record_thread is not None and self.record_thread.is_alive():
            self.record_thread.join(timeout=5.0)
        self.destroy()


if __name__ == '__main__':
    app = ModernGCS()
    app.mainloop()
