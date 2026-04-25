"""
Packet Structure (12 bytes total, transmitted as hex string = 24 hex chars):
  [0:2]   - tx/rx mode   (2 bytes) : 0x0001 = TX (gate kirim), 0x0002 = RX (ack balik)
  [2:4]   - pintu-x      (2 bytes) : 0x0001 = Gate 1, 0x0002 = Gate 2, dst
  [4:12]  - data KTP     (8 bytes) : UID RFID e-KTP (hex)

Contoh hex string: 0001000112AB34CD56EF7890
"""

import struct
from dataclasses import dataclass
from enum import IntEnum


class Mode(IntEnum):
    TX = 0x0001  # Gate mengirim data tap baru
    RX = 0x0002  # Acknowledgement / balik dari base


@dataclass
class GatePacket:
    mode: Mode
    gate_id: int
    ktp_uid: str  # 8 byte UID sebagai hex string (16 chars)
    raw_hex: str  # raw hex yang diterima, untuk logging

    @property
    def mode_label(self) -> str:
        return "TX" if self.mode == Mode.TX else "RX"

    def to_dict(self) -> dict:
        return {
            "mode": self.mode_label,
            "gate_id": self.gate_id,
            "ktp_uid": self.ktp_uid,
            "raw_hex": self.raw_hex,
        }


def parse_hex_packet(hex_str: str) -> GatePacket:
    """
    Parse hex string 24 karakter menjadi GatePacket.
    Raises ValueError jika format tidak valid.
    """
    hex_str = hex_str.strip().upper()

    if len(hex_str) != 24:
        raise ValueError(f"Panjang hex harus 24 karakter, dapat: {len(hex_str)}")

    raw_bytes = bytes.fromhex(hex_str)

    # Unpack: big-endian, 2 unsigned short (2+2 bytes) + 8 bytes raw
    mode_raw, gate_id = struct.unpack(">HH", raw_bytes[0:4])
    ktp_bytes = raw_bytes[4:12]
    ktp_uid = ktp_bytes.hex().upper()

    try:
        mode = Mode(mode_raw)
    except ValueError:
        raise ValueError(f"Mode tidak dikenal: {hex(mode_raw)}")

    return GatePacket(
        mode=mode,
        gate_id=gate_id,
        ktp_uid=ktp_uid,
        raw_hex=hex_str,
    )


def build_hex_packet(mode: Mode, gate_id: int, ktp_uid: str) -> str:
    """
    Build hex packet dari komponen. Kebalikan dari parse.
    ktp_uid: 16 char hex string (8 bytes)
    """
    if len(ktp_uid) != 16:
        raise ValueError("ktp_uid harus 16 karakter hex (8 bytes)")

    header = struct.pack(">HH", int(mode), gate_id)
    ktp_bytes = bytes.fromhex(ktp_uid)
    return (header + ktp_bytes).hex().upper()
