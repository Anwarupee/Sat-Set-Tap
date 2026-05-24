# Packet Format — TheGate v3

## Overview

Setiap komunikasi antar node menggunakan format packet tetap **12 bytes**, dikirim sebagai **hex string 24 karakter** via LoRa atau Serial.

## Struktur

```
Byte  0     1     2  3  4  5     6        7        8  9       10 11
     [CMD] [GID] [  KTP UID  ] [HOP]  [RESERVED] [TIMESTAMP] [CRC-16]
      1B    1B      4 bytes     1B       1B          2 bytes    2 bytes
```

| Byte | Field | Size | Values |
|------|-------|------|--------|
| 0 | Command | 1B | `0x01` TAP_REQUEST · `0x02` RESPONSE_OK · `0x03` RESPONSE_DENY · `0x04` PING |
| 1 | Gate ID | 1B | `0x01`–`0xFF` (max 255 gate) |
| 2–5 | KTP UID | 4B | UID chip e-KTP dari PN532 |
| 6 | Hop Count | 1B | Dimulai dari `3`, relay kurangi 1 tiap lompat. `0` = tidak boleh di-relay lagi |
| 7 | Reserved | 1B | `0x00` (untuk ekspansi) |
| 8–9 | Timestamp | 2B | Detik sejak boot, big-endian (0–65535) |
| 10–11 | CRC-16 | 2B | CRC-16/CCITT-FALSE dari byte 0–9, big-endian |

## Contoh

Tap Gate 1, UID `188235CA`, hop=3, ts=30:

```
01 01 18 82 35 CA 03 00 00 1E FC 93
^  ^  ^^^^^^^^^^ ^  ^  ^^^^^  ^^^^^
│  │  KTP UID    │  │  TS=30  CRC
│  Gate 1        │  Reserved
TAP_REQ          Hop=3
```

Hex string: `010118823 5CA030000 1EFC93`

## CRC-16 Algorithm

CRC-16/CCITT-FALSE (polynomial `0x1021`, init `0xFFFF`).

**Python:**
```python
def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = (crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc
```

**C++ (Arduino):**
```cpp
uint16_t crc16(uint8_t *data, uint8_t len) {
  uint16_t crc = 0xFFFF;
  for (uint8_t i = 0; i < len; i++) {
    crc ^= (uint16_t)data[i] << 8;
    for (uint8_t j = 0; j < 8; j++)
      crc = (crc & 0x8000) ? (crc << 1) ^ 0x1021 : crc << 1;
  }
  return crc;
}
```

## Serial Format (Gateway → RPi)

```
DATA:<hex_24char>:<rssi>\n     ← data dari gate
RESP:<hex_24char>\n            ← response dari RPi ke gateway
# comment\n                   ← log/debug, diabaikan RPi
```

## LoRa Parameters

Semua node wajib pakai parameter yang sama:

```
Frekuensi : 433 MHz
SF        : 9
Bandwidth : 125 kHz
Coding Rate: 4/5
Sync Word : 0x12
```