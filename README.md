# Gate System — Raspberry Pi Receiver

Bagian **jaringan & Raspberry Pi** dari sistem gate berbasis LoRa + e-KTP.

## Struktur Project

```
gate-system/
├── receiver/
│   ├── packet.py        # Packet parser & builder (12 bytes)
│   ├── redis_handler.py # Redis integration
│   └── receiver.py      # Main entry point
├── mock/
│   ├── mock_gen.py      # Standalone mock generator
│   └── test_packet.py   # Unit tests
└── requirements.txt
```

## Packet Format

Total **12 bytes**, dikirim sebagai **hex string 24 karakter**:

```
[tx/rx mode]  [pintu-x]  [data KTP]
  2 bytes       2 bytes    8 bytes

Contoh: 0001 0001 12AB34CD56EF7890
         TX   G1   UID KTP
```

| Field    | Size   | Keterangan                              |
|----------|--------|-----------------------------------------|
| mode     | 2 byte | `0x0001` = TX (tap baru), `0x0002` = RX |
| gate_id  | 2 byte | Nomor gate (1, 2, 3, ...)               |
| ktp_uid  | 8 byte | UID RFID chip e-KTP                     |

## Setup

```bash
pip install -r requirements.txt

# Pastikan Redis running
sudo systemctl start redis
```

## Cara Pakai

### 1. Dry Run — Input Manual
```bash
cd receiver
python receiver.py --stdin
# hex> 0001000112AB34CD56EF7890
```

### 2. Dry Run — Auto Mock
```bash
python receiver.py --mock
```

### 3. Dari Serial Port (LoRa Hardware)
```bash
python receiver.py --serial /dev/ttyUSB0 --baud 9600
```

### 4. Test Packet Parser
```bash
python mock/test_packet.py
```

## Redis Key Schema

| Key                    | Type | Isi                                          |
|------------------------|------|----------------------------------------------|
| `gate:event:{uid}`     | Hash | Detail event terakhir UID tersebut           |
| `gate:log`             | List | 1000 event terakhir (JSON), newest first     |
| `gate:uid:set`         | Set  | Semua UID yang sudah masuk (cek duplikat)    |
| `gate:stats:{gate_id}` | Hash | Total tap & last seen per gate               |

## Cek Data di Redis

```bash
redis-cli

# Lihat semua UID yang sudah masuk
SMEMBERS gate:uid:set

# Lihat 5 event terakhir
LRANGE gate:log 0 4

# Stats gate 1
HGETALL gate:stats:1

# Detail tap UID tertentu
HGETALL gate:event:12AB34CD56EF7890
```

## Catatan untuk Integrasi LoRa

ESP32 gateway harus mengirim hex string via Serial, **satu packet per baris**:
```
0001000112AB34CD56EF7890\n
0001000212AB34CD56EF1234\n
```
Receiver membaca line by line dan parse setiap baris sebagai packet.
