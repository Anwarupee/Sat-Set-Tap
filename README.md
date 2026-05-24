# Sat Set Tap


> Sistem manajemen akses event berbasis IoT — menggantikan QR/gelang dengan tap e-KTP via LoRa mesh network.

![Status](https://img.shields.io/badge/status-active-brightgreen)
![Platform](https://img.shields.io/badge/platform-ESP32%20%7C%20RPi-blue)
![Protocol](https://img.shields.io/badge/protocol-LoRa%20433MHz-orange)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

---

## Latar Belakang

Sistem gate event konvensional memiliki dua masalah utama:

- **Antrean lambat** — scan QR membutuhkan 3–10 detik per orang
- **Jaringan tidak stabil** — WiFi/4G sering overload di lokasi event ramai

Sat Set Tap menyelesaikan keduanya: e-KTP sebagai token masuk (tap < 1 detik) dan LoRa mesh sebagai infrastruktur jaringan yang bebas internet.

---

## Arsitektur Sistem

```
[Gate Node]              [Relay A]
ESP32 + LoRa   ──LoRa──► ESP32 + LoRa ──LoRa──► [Gateway ESP32]
+ PN532 NFC              [Relay B]                      │
+ Servo SG90   ──LoRa──► ESP32 + LoRa ──LoRa──►   USB Serial
                         (failover)                     │
                                                  [Raspberry Pi]
                                                  receiver.py
                                                  Redis DB
                                                  dashboard.py
                                                        │
                                                  [Browser / Laptop]
                                                  via Tailscale VPN
```

---

## Packet Format

Setiap tap KTP menghasilkan packet **12 bytes (24 char hex)**:

| Byte | Field | Ukuran | Keterangan |
|------|-------|--------|------------|
| 0 | Command | 1 byte | `0x01`=TAP_REQ · `0x02`=RESP_OK · `0x03`=RESP_DENY |
| 1 | Gate ID | 1 byte | ID gate pengirim (1–255) |
| 2–5 | KTP UID | 4 byte | UID chip e-KTP dari PN532 |
| 6 | Hop Count | 1 byte | Sisa hop relay (mulai 3, berkurang tiap lompat) |
| 7 | Reserved | 1 byte | `0x00` |
| 8–9 | Timestamp | 2 byte | Detik sejak boot (0–65535) |
| 10–11 | CRC-16 | 2 byte | Checksum byte 0–9 (CCITT-FALSE) |

Contoh: `0101188235CA030000 1EFC93`

---

## Komponen Hardware

### Per Gate Node
| Komponen | Spek | Fungsi |
|----------|------|--------|
| ESP32 DevKit | Dual-core 240MHz | Mikrokontroler utama |
| LoRa SX1278 | 433MHz · SF9 | Transmisi data |
| PN532 | I2C · 13.56MHz | Baca UID e-KTP |
| Servo SG90 | PWM · GPIO13 | Buka/tutup gate |
| Antena | 433MHz · 12dBi | Jangkauan sinyal |

### Per Relay Node
| Komponen | Spek | Fungsi |
|----------|------|--------|
| ESP32 DevKit | — | Mikrokontroler |
| LoRa SX1278 | 433MHz · SF9 | Terima & forward packet |

### Raspberry Pi (Base)
| Komponen | Keterangan |
|----------|------------|
| Raspberry Pi 4 | Server utama |
| Redis 6.x | Database in-memory |
| Python 3.9 | Runtime receiver & dashboard |
| Tailscale | Remote access VPN |

### Registration Station (H-1)
| Komponen | Keterangan |
|----------|------------|
| Arduino Uno | Mikrokontroler |
| PN532 #1 | I2C (DIP: SW1=ON SW2=OFF) |
| PN532 #2 | SPI (DIP: SW1=OFF SW2=ON) |

---

## Struktur Repository

```
Sat Set Tap/
├── firmware/               # Kode Arduino/ESP32
│   ├── gate_node/          # Node gate + PN532 + Servo
│   ├── relay_node/         # Node relay failover
│   ├── gateway/            # Gateway USB Serial ke RPi
│   └── registration_reader/# Arduino reader H-1
├── raspi/                  # Kode Raspberry Pi
│   ├── receiver/           # Core: packet parser + Redis handler
│   ├── dashboard.py        # Web dashboard FastAPI
│   ├── registration_station.py
│   ├── requirements.txt
│   └── setup/              # Script setup RPi
├── tests/                  # Unit tests + mock
│   ├── test_packet.py
│   ├── test_raspi.py
│   └── mock/
└── docs/                   # Dokumentasi & diagram
```

---

## Setup & Instalasi

### 1. Raspberry Pi

```bash
# Clone repo
<<<<<<< HEAD
git clone https://github.com/Anwarupee/Sat-Set-Tap.git
git clone https://github.com/rpsreal/pySX127x
=======
git clone https://github.com/<username>/TheGate.git
>>>>>>> d191a61 (push)
cd TheGate

# Setup otomatis
bash raspi/setup/setup_raspi.sh

# Install Python dependencies
pip3 install -r raspi/requirements.txt --break-system-packages

# Jalankan receiver
python3 raspi/receiver/receiver.py --serial /dev/ttyUSB0 --redis-pass <password>

# Jalankan dashboard (terminal terpisah)
python3 raspi/dashboard.py
```

### 2. Firmware ESP32

Buka Arduino IDE, install library berikut:
- `LoRa` by Sandeep Mistry
- `Adafruit PN532`
- `ESP32Servo`

Flash masing-masing sketch:

| Sketch | Target |
|--------|--------|
| `firmware/gate_node/` | ESP32 di pintu masuk |
| `firmware/relay_node/` | ESP32 relay (ubah `RELAY_ID`) |
| `firmware/gateway/` | ESP32 yang colok ke RPi |

**Penting:** Pastikan parameter LoRa sama di semua node:
```cpp
#define LORA_FREQ  433E6
#define LORA_SF    9
#define LORA_BW    125E3
#define LORA_SW    0x12
```

### 3. Wiring Gate Node

```
ESP32        LoRa SX1278
GPIO5   ──►  SS/CS
GPIO14  ──►  RST
GPIO2   ──►  DIO0
GPIO18  ──►  SCK
GPIO19  ──►  MISO
GPIO23  ──►  MOSI
3.3V    ──►  VCC   ⚠️ bukan 5V!

ESP32        PN532
GPIO21  ──►  SDA
GPIO22  ──►  SCL

ESP32        Servo SG90
GPIO13  ──►  Signal (kuning)
VIN     ──►  VCC   (merah) — 5V
GND     ──►  GND   (coklat)
```

### 4. Registration Station (H-1)

```bash
# Jalankan di RPi dengan Arduino tersambung
python3 raspi/registration_station.py \
  --serial /dev/ttyUSB0 \
  --redis-pass <password> \
  --tokens 3
```

---

## Cara Pakai

### Hari H-1 — Registrasi Peserta

```bash
python3 raspi/registration_station.py --serial /dev/ttyUSB0 --redis-pass <pass>
```

Tempelkan KTP ke reader → UID tersimpan otomatis dengan **3 token** masuk.

### Hari H — Event Berlangsung

```bash
# Terminal 1: Receiver
python3 raspi/receiver/receiver.py --serial /dev/ttyUSB0 --redis-pass <pass>

# Terminal 2: Dashboard
python3 raspi/dashboard.py
```

Akses dashboard dari browser: `http://<tailscale-ip>:8080`

### Flush Data Sebelum Event

```bash
redis-cli -a <pass> FLUSHDB
```

Atau klik tombol **Flush Event** di dashboard.

---

## Dashboard

Akses via browser di `http://<IP-Tailscale>:8080`

Fitur:
- **Realtime** — update otomatis via WebSocket
- **Stats** — peserta terdaftar, sudah masuk, total tap
- **Chart per gate** — frekuensi tap per menit, toggle Gate 1/2/dst
- **Load balancer** — alert otomatis jika gate ramai tidak merata
- **Event log** — timestamp millisecond, nama peserta, sisa token
- **Flush** — reset event atau reset semua data

---

## Testing

```bash
# Unit test packet parser
python3 tests/test_packet.py

# Test koneksi RPi (Redis + packet)
python3 tests/test_raspi.py

# Dry run dengan mock data
python3 raspi/receiver/receiver.py --mock --redis-pass <pass>
```

---

## Keamanan

- **SSH Key** — password login dinonaktifkan, hanya key-based
- **Redis auth** — password wajib, bind ke Tailscale IP
- **CRC-16** — setiap packet divalidasi, packet corrupt dibuang
- **Tailscale VPN** — akses remote hanya via jaringan privat
- **Token system** — setiap UID punya kuota masuk terbatas

---

## Kontributor

| Nama | Peran |
|------|-------|
| Muhammad Yanuar Andrianto Putra| Network, RPi, Redis, Dashboard |
| Rezan Dzaky Ambrita | Hardware, ESP32, LoRa, PN532 |

**Pembimbing:** [Pak Hudya]
**Institusi:** [CEP CCIT-FTUI]

---

## Lisensi

MIT License — bebas digunakan dengan menyertakan atribusi.

---

<p align="center">
  <sub>Dibuat untuk lomba IoT · Sat Set Tap</sub>
<<<<<<< HEAD
</p>
=======
</p>
>>>>>>> d191a61 (push)
