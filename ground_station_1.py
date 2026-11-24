#!/usr/bin/env python3
"""ORCA V4 GCS: ROS-free laptop GUI with embedded MJPEG camera panel."""
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
from tkinter import ttk, messagebox

NODES = ['depth', 'attitude', 'proximity', 'bridge', 'final', 'qualification']
KEYS = {'w': ('forward', 1700), 's': ('forward', 1300),
        'a': ('lateral', 1300), 'd': ('lateral', 1700),
        'Up': ('throttle', 1700), 'Down': ('throttle', 1300),
        'Left': ('yaw', 1300), 'Right': ('yaw', 1700)}

class GCS:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title('ORCA V4 | Ground Control Station')
        self.root.geometry('1270x930')
        self.root.minsize(1080, 810)
        self.root.configure(bg='#0d1828')
        self.events = queue.Queue()
        self.tx = queue.Queue(maxsize=300)
        self.stop = threading.Event()
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
        # The recorder consumes original MJPEG JPEG frames rather than resized GUI frames.
        self.record_active = threading.Event()
        self.record_queue = queue.Queue(maxsize=8)
        self.record_thread = None
        self.record_path = None
        self.record_status = tk.StringVar(value='REC: OFF')
        self.recorded_frames = 0
        self.record_fps = 15.0
        self.link = False
        self.manual_enabled = False
        self.held = set()
        self.arm_state = 'disarm'  # persistent intent while manual mode is active
        self.hostname = tk.StringVar(value='192.168.55.100')
        self.port = tk.IntVar(value=5000)
        self.status = tk.StringVar(value='DISCONNECTED')
        self.metrics = {name: tk.StringVar(value='—') for name in ('depth', 'pressure', 'roll', 'pitch', 'yaw', 'proximity', 'mission', 'sensor_age')}
        self.proc_vars = {k: tk.StringVar(value='OFF') for k in NODES}
        self.last_telemetry = 0
        self.current_host = '192.168.55.100'
        self.current_port = 5000
        self.build()
        self.root.bind_all('<KeyPress>', self.key_down)
        self.root.bind_all('<KeyRelease>', self.key_up)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.after(60, self.pump)
        self.root.after(100, self.manual_tick)
        self.root.after(30, self.camera_tick)
        self.root.after(400, self.start_camera)  # connect automatically
        threading.Thread(target=self.network, daemon=True).start()

    def build(self):
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('TFrame', background='#0d1828')
        style.configure('TLabel', background='#0d1828', foreground='#e6edf8', font=('Arial', 11))
        style.configure('Heading.TLabel', font=('Arial', 22, 'bold'), foreground='#4dd4ff')
        style.configure('TLabelframe', background='#0d1828', foreground='#4dd4ff')
        style.configure('TLabelframe.Label', background='#0d1828', foreground='#4dd4ff', font=('Arial', 12, 'bold'))
        style.configure('TButton', font=('Arial', 10, 'bold'), padding=6)
        frame = ttk.Frame(self.root, padding=18)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='ORCA V4   /   GROUND CONTROL STATION', style='Heading.TLabel').pack(anchor='w')
        connection = ttk.Frame(frame)
        connection.pack(fill='x', pady=(15, 12))
        ttk.Label(connection, text='JETSON IP').pack(side='left')
        ttk.Entry(connection, textvariable=self.hostname, width=19).pack(side='left', padx=10)
        ttk.Label(connection, text='PORT').pack(side='left')
        ttk.Entry(connection, textvariable=self.port, width=8).pack(side='left', padx=10)
        ttk.Label(connection, textvariable=self.status).pack(side='left', padx=14)
        panels = ttk.Frame(frame)
        panels.pack(fill='both', expand=True)
        left = ttk.Frame(panels)
        right = ttk.Frame(panels)
        left.pack(side='left', fill='both', expand=True, padx=(0, 9))
        right.pack(side='left', fill='both', expand=True, padx=(9, 0))
        camera = ttk.LabelFrame(left, text='LIVE CAMERA / exploreHD — FISHEYE / MISSION', padding=10)
        camera.pack(fill='x', pady=(0, 10))
        self.camera_panel = tk.Frame(camera, height=245, bg='#020812', highlightthickness=1,
                                     highlightbackground='#304962')
        self.camera_panel.pack(fill='x')
        self.camera_panel.pack_propagate(False)
        self.camera_image_label = tk.Label(self.camera_panel, bg='#020812', fg='#a0b8cc',
                                           text='CAMERA CONNECTING...', font=('Arial', 14, 'bold'))
        self.camera_image_label.pack(fill='both', expand=True)
        camera_controls = ttk.Frame(camera)
        camera_controls.pack(fill='x', pady=(8, 0))
        ttk.Button(camera_controls, text='START VIDEO', command=self.start_camera).pack(side='left', padx=(0, 5))
        ttk.Button(camera_controls, text='STOP VIDEO', command=self.stop_camera).pack(side='left', padx=5)
        self.record_button = ttk.Button(camera_controls, text='● RECORD', command=self.start_recording)
        self.record_button.pack(side='left', padx=5)
        self.record_stop_button = ttk.Button(camera_controls, text='STOP REC', command=self.stop_recording, state='disabled')
        self.record_stop_button.pack(side='left', padx=5)
        ttk.Label(camera_controls, textvariable=self.camera_status).pack(side='left', padx=6)
        ttk.Label(camera_controls, textvariable=self.record_status).pack(side='left', padx=6)
        ttk.Label(camera_controls, textvariable=self.camera_fps).pack(side='right')
        ttk.Label(camera, text='Stream otomatis: preview fisheye / anotasi misi jika tersedia; HTTP :8081',
                  font=('Arial', 9)).pack(anchor='w', pady=(5, 0))

        sensors = ttk.LabelFrame(left, text='LIVE TELEMETRY', padding=10)
        sensors.pack(fill='x', pady=(0, 12))
        for name in self.metrics:
            row = ttk.Frame(sensors)
            row.pack(fill='x', pady=2)
            ttk.Label(row, text=name.upper().replace('_', ' '), width=17).pack(side='left')
            ttk.Label(row, textvariable=self.metrics[name], font=('Arial', 13, 'bold')).pack(side='left')
        launch = ttk.LabelFrame(left, text='SENSOR & BRIDGE MANAGEMENT', padding=10)
        launch.pack(fill='x')
        for idx, key in enumerate(('depth', 'attitude', 'proximity', 'bridge')):
            cell = ttk.Frame(launch)
            cell.grid(row=idx // 2, column=idx % 2, sticky='ew', padx=5, pady=3)
            ttk.Label(cell, text=key.upper(), width=10).pack(side='left')
            ttk.Label(cell, textvariable=self.proc_vars[key], width=4).pack(side='left')
            ttk.Button(cell, text='START', command=lambda k=key: self.command('start', k)).pack(side='left', padx=2)
            ttk.Button(cell, text='STOP', command=lambda k=key: self.command('stop', k)).pack(side='left', padx=2)
        launch.columnconfigure(0, weight=1)
        launch.columnconfigure(1, weight=1)
        missions = ttk.LabelFrame(right, text='MISSION CONTROL', padding=14)
        missions.pack(fill='x', pady=(0, 12))
        for key in ('final', 'qualification'):
            self.node_row(missions, key)
        manual = ttk.LabelFrame(right, text='MANUAL KEYBOARD CONTROL', padding=14)
        manual.pack(fill='x', pady=(0, 12))
        self.manual_button = ttk.Button(manual, text='ENABLE MANUAL', command=self.toggle_manual)
        self.manual_button.pack(fill='x', pady=5)
        ttk.Label(manual, text='W/S: Forward | A/D: Lateral\n↑/↓: Throttle | ←/→: Yaw\nRelease key: neutral (1500 PWM)').pack(anchor='w', pady=5)
        ttk.Button(manual, text='REQUEST ARM', command=self.request_arm).pack(fill='x', pady=4)
        ttk.Button(manual, text='DISARM / NEUTRAL', command=self.request_disarm).pack(fill='x', pady=4)
        ttk.Label(manual, text='ARM is a request, not confirmation from Pixhawk.').pack(anchor='w')
        logframe = ttk.LabelFrame(right, text='SYSTEM LOG', padding=10)
        logframe.pack(fill='both', expand=True)
        self.logs = tk.Text(logframe, height=10, bg='#13243b', fg='#cfe9f8', insertbackground='white', state='disabled')
        self.logs.pack(fill='both', expand=True)
        self.log('Ground station ready. Connect Jetson on the same network.')

    def start_camera(self):
        """Start/reconnect video, independent of TCP 5000."""
        if self.stop.is_set() or self.camera_enabled.is_set():
            return
        self.current_host = self.hostname.get().strip() or '192.168.55.100'
        self.camera_enabled.set()
        self.camera_status.set('CAMERA: CONNECTING')
        self.camera_image_label.configure(text='CONNECTING TO CAMERA...')
        threading.Thread(target=self.camera_worker, daemon=True, name='orca-mjpeg').start()

    def stop_camera(self):
        self.stop_recording()
        self.camera_enabled.clear()
        # Closing the HTTP socket also interrupts a pending network read.
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
        self.camera_image_label.configure(image='', text='CAMERA STOPPED')

    def camera_worker(self):
        """Read streaming JPEG frames in background; never manipulate Tk here."""
        while self.camera_enabled.is_set() and not self.stop.is_set():
            url = f'http://{self.current_host}:{self.camera_http_port}/stream.mjpg'
            response = None
            try:
                request = urllib.request.Request(url, headers={
                    'User-Agent': 'ORCA-V4-GCS/1.0', 'Cache-Control': 'no-cache'})
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
                    # MJPEG uses JPEG SOI / EOI markers; HTTP part headers are ignored.
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
                                # Never block the MJPEG reader if disk encoding falls behind.
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
            # Retry after transient interruption. Does not affect manual controls.
            if self.camera_enabled.is_set() and not self.stop.is_set():
                self.stop.wait(1.5)

    def start_recording(self):
        """Record the incoming MJPEG stream on the laptop, independently of GUI FPS."""
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
            import cv2  # optional: installed on the GCS laptop
            import numpy  # noqa: F401
        except ImportError:
            messagebox.showerror('ORCA Camera',
                                 'Perekaman MP4 membutuhkan OpenCV dan NumPy pada laptop.\n'
                                 'Ubuntu: sudo apt install python3-opencv python3-numpy')
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
        self.record_button.configure(state='disabled')
        self.record_stop_button.configure(state='normal')
        self.record_thread = threading.Thread(target=self.record_worker,
                                              args=(self.record_path,), daemon=True,
                                              name='orca-video-recorder')
        self.record_thread.start()
        self.log(f'Mulai merekam kamera ke {self.record_path}')

    def stop_recording(self):
        """Signal recorder to finish and finalize the MP4; never block Tk mainloop."""
        if not self.record_active.is_set():
            return
        self.record_active.clear()
        self.record_status.set('REC: SAVING...')
        self.record_stop_button.configure(state='disabled')
        # Record worker will drain buffered frames and release VideoWriter.

    def record_worker(self, destination):
        """Encode in background and duplicate frames to retain wall-clock duration."""
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
                        error = 'Stream kamera terhenti lebih dari 3 detik; rekaman dihentikan.'
                        self.record_active.clear()
                    continue
                last_input = stamp
                frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
                if frame is None:
                    continue
                if writer is None:
                    h, w = frame.shape[:2]
                    writer = cv2.VideoWriter(str(destination),
                                             cv2.VideoWriter_fourcc(*'mp4v'),
                                             self.record_fps, (w, h))
                    if not writer.isOpened():
                        raise OSError('VideoWriter MP4 gagal dibuka (codec mp4v).')
                    first_at = stamp
                    last_frame = frame
                    writer.write(frame)
                    written = 1
                    continue
                if frame.shape[:2] != last_frame.shape[:2]:
                    frame = cv2.resize(frame, (last_frame.shape[1], last_frame.shape[0]))
                # Derive output timeline from acquisition timestamps, not GUI redraws.
                target_count = max(written, int((stamp - first_at) * self.record_fps) + 1)
                # Bound duplication during large gaps; interrupted streams are auto-stopped.
                for _ in range(min(target_count - written, int(self.record_fps * 3))):
                    writer.write(last_frame)
                    written += 1
                last_frame = frame
            if writer is not None and last_frame is not None:
                # Preserve the last observed frame, without inventing footage after stop.
                writer.write(last_frame)
                written += 1
        except Exception as exc:
            error = f'Gagal merekam: {exc}'
        finally:
            if writer is not None:
                writer.release()
            self.record_active.clear()
            self.recorded_frames = written
            self.events.put({'type': 'record_done', 'path': str(destination),
                             'frames': written, 'error': error})

    def camera_tick(self):
        """Update Tk display on main thread only; drop stale frames."""
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
                fps = (len(self.camera_display_times)-1) / elapsed if elapsed > .1 else 0
                self.camera_fps.set(f'FPS: {fps:.1f}')
            if age < 2.0:
                self.camera_status.set('CAMERA: LIVE')
            else:
                self.camera_status.set('CAMERA: RECONNECTING' if error else 'CAMERA: NO FRAMES')
                self.camera_fps.set('FPS: —')
                # Remove stale picture to avoid presenting old video as live.
                if self.camera_photo is not None:
                    self.camera_photo = None
                    self.camera_image_label.configure(image='', text='VIDEO LOST / RECONNECTING')
        self.root.after(30, self.camera_tick)

    def node_row(self, parent, key):
        row = ttk.Frame(parent)
        row.pack(fill='x', pady=5)
        ttk.Label(row, text=key.upper(), width=17).pack(side='left')
        ttk.Label(row, textvariable=self.proc_vars[key], width=6).pack(side='left')
        ttk.Button(row, text='START', command=lambda: self.command('start', key)).pack(side='left', padx=4)
        ttk.Button(row, text='STOP', command=lambda: self.command('stop', key)).pack(side='left', padx=4)

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
            self.log('Disable manual before autonomous mission')
            return
        self.queue_command({'action': action, 'node': key})

    def toggle_manual(self):
        self.manual_enabled = not self.manual_enabled
        if not self.manual_enabled:
            self.held.clear()
            self.arm_state = 'disarm'
            self.queue_command({'action': 'manual', 'arm_mode': '', 'forward': 1500,
                                'lateral': 1500, 'yaw': 1500, 'throttle': 1500})
            self.queue_command({'action': 'manual_release'})
        self.manual_button.configure(text='DISABLE MANUAL' if self.manual_enabled else 'ENABLE MANUAL')
        self.log('Manual control ' + ('ENABLED' if self.manual_enabled else 'DISABLED'))

    def request_arm(self):
        if not self.manual_enabled:
            self.log('Enable manual before ARM request')
            return
        if not self.link:
            self.log('ARM request rejected: Jetson offline')
            return
        self.arm_state = 'arm'
        self.log('ARM requested; held in manual frames (verify Pixhawk state independently)')

    def request_disarm(self):
        self.arm_state = 'disarm'
        self.held.clear()
        self.queue_command({'action': 'manual', 'arm_mode': 'disarm', 'forward': 1500,
                            'lateral': 1500, 'yaw': 1500, 'throttle': 1500})
        self.log('DISARM request queued')

    def key_down(self, event):
        if not self.manual_enabled or isinstance(event.widget, (tk.Entry, tk.Text)):
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
        self.root.after(100, self.manual_tick)

    def pump(self):
        for _ in range(30):
            try:
                item = self.events.get_nowait()
            except queue.Empty:
                break
            if item['type'] == 'link':
                self.link = item['connected']
                self.status.set('CONNECTED' if self.link else 'DISCONNECTED')
                if not self.link:
                    self.held.clear()
                    self.manual_enabled = False
                    self.arm_state = 'disarm'
                    self.manual_button.configure(text='ENABLE MANUAL')
                self.log('TCP ' + self.status.get())
            elif item['type'] == 'ack':
                self.log(('OK: ' if item.get('ok') else 'ERROR: ') + str(item.get('message')))
            elif item['type'] == 'record_done':
                self.record_button.configure(state='normal')
                self.record_stop_button.configure(state='disabled')
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
        self.current_host = self.hostname.get().strip()
        try:
            self.current_port = int(self.port.get())
        except (ValueError, tk.TclError):
            pass
        self.root.after(60, self.pump)

    def display_telemetry(self, item):
        status = item.get('processes', {})
        for k in NODES:
            self.proc_vars[k].set('RUN' if status.get(k) else 'OFF')
        sensor = item.get('sensor', {})
        values = sensor.get('values', {})
        depth = values.get('depth') or {}
        attitude = values.get('attitude') or {}
        prox = values.get('proximity') or {}
        for key, val, unit in [('depth', depth.get('depth'), 'm'), ('pressure', depth.get('pressure'), 'mbar'),
                               ('roll', attitude.get('roll'), '°'), ('pitch', attitude.get('pitch'), '°'),
                               ('yaw', attitude.get('yaw'), '°')]:
            self.metrics[key].set(f'{val:.2f} {unit}' if isinstance(val, (float, int)) else '—')
        self.metrics['proximity'].set('TRIGGERED' if prox.get('triggered') else ('IDLE' if prox else '—'))
        self.metrics['mission'].set(str(values.get('mission') or '—'))
        stamps = sensor.get('updated', {})
        if stamps:
            max_age = min(time.time() - t for t in stamps.values())
            self.metrics['sensor_age'].set(f'{max_age:.1f} s (most recent topic)')
        else:
            self.metrics['sensor_age'].set('NO DATA')

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
        # Ensure MP4 index is finalized before exiting the process.
        if self.record_thread is not None and self.record_thread.is_alive():
            self.record_thread.join(timeout=5.0)
        self.root.destroy()

if __name__ == '__main__':
    GCS().root.mainloop()
