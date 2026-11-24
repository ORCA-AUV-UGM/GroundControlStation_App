# ORCA V4 — Ground Control Station

Desktop telemetry untuk Jetson + ROS 2 Humble + Pixhawk, sesuai source `Orca_Workstation2.zip`.

## File
- `ground_station.py`: GUI laptop (Python 3 + tkinter), telemetry, launch sensor/bridge/misi, kendali keyboard, dan tombol kamera.
- `jetson_server.py`: komunikasi TCP 5000, ROS 2 subscriber/publisher, proses node.
- `camera_server.py`: streaming MJPEG terpisah di HTTP 8081, opsional.
- `run_jetson.sh`: skrip pembantu untuk menemukan folder workspace, memuat ROS 2, lalu menjalankan server.

## Jalankan di Jetson
Salin folder ini ke Jetson. Buka terminal dari folder telemetri, lalu:

```bash
chmod +x run_jetson.sh
./run_jetson.sh
```

Skrip mendeteksi `~/Orca_Workstation2`, `~/Orca_Workstation`, atau workspace `*/install/setup.bash` di home. Jika tidak cocok, jalankan eksplisit:

```bash
./run_jetson.sh /path/ke/workspace/install/setup.bash
```

Workspace harus telah di-`colcon build` dan package `mavpy` dan `orca_msgs` terpasang. ROS 2 standar di `/opt/ros/humble`. Server hanya mengatur proses yang diluncurkan melalui GCS; jangan jalankan node yang sama dari terminal lain.

### Kamera (terminal Jetson kedua)

```bash
sudo apt install python3-opencv
python3 camera_server.py --device /dev/video0 --port 8081
```

Periksa nomor kamera dengan `v4l2-ctl --list-devices`. Video MJPEG dipisahkan dari channel kontrol TCP. Untuk bandwidth rendah, H.264 lebih efisien; aplikasi ini memakai MJPEG demi kompatibilitas browser.

## Jalankan di laptop

```bash
# Ubuntu/Debian, sekali instal:
sudo apt install python3-tk
python3 ground_station.py
```

GUI menghubungkan ke Jetson pada `192.168.55.100:5000` (bisa diganti). Tombol `OPEN LIVE CAMERA` membuka live feed pada browser di port 8081.

## Topic dipakai dari workspace
| Fungsi | Topic | Tipe |
|---|---|---|
| Depth | `/orca/sensor/depth` | `orca_msgs/OrcaDepth` |
| Attitude | `/orca/sensor/attitude` | `orca_msgs/OrcaAttitude` |
| Proximity | `/orca/start_trigger` | `orca_msgs/OrcaProximity` |
| Mission state | `/orca/mission/state` | `std_msgs/String` |
| Manual control | `/orca/manual/control` | `orca_msgs/OrcaControl` |

Executable: `mavpy depth_node`, `attitude_node`, `proximity_node`, `control_bridge`, `final`, `qualification_mission`.

## Langkah operasional
1. Pastikan Jetson dan laptop tersambung Ethernet serta `ping 192.168.55.100` berhasil.
2. Jalankan Jetson server; jalankan camera_server jika ingin video.
3. Buka GCS laptop, periksa CONNECTED.
4. Jalankan tiga sensor masing-masing dengan tombol START.
5. Jalankan CONTROL BRIDGE.
6. Pilih manual keyboard ATAU satu autonomous mission. Hentikan mission sebelum masuk manual.
7. Gunakan DISARM, verifikasi pada Pixhawk, baru hentikan proses.

## Peringatan keselamatan dan batasan
- **Belum diuji pada hardware ORCA V4.** Uji dahulu tanpa thruster/propeler aktif.
- ACK di GUI hanya menandakan server memproses perintah, **bukan** konfirmasi ARM/DISARM dari Pixhawk.
- Tidak ada autentikasi atau enkripsi; hanya gunakan LAN yang terisolasi dan tepercaya, jangan expose port 5000/8081 ke Internet.
- Koneksi putus mencoba mengirim neutral + disarm tetapi tidak menjamin Pixhawk menerimanya. Failsafe Pixhawk dan kill switch perangkat keras tetap wajib.
- Status RUN artinya proses yang dibuat oleh aplikasi masih berjalan, bukan health check sensor atau kendaraan.
- Telemetry camera tidak memakai topic ROS 2; kamera USB diakses langsung oleh `camera_server.py`. Jangan buka kamera yang sedang digunakan node lain tanpa memastikan dukungan multi-reader.
- GUI menampilkan mission state hanya bila topic tersebut dipublikasikan oleh node mission.
