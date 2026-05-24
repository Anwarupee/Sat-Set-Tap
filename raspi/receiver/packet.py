"""
Packet Structure v3 — 12 bytes total = 24 char hex string

  Byte 0    : Command    (1 byte)  — 0x01=TAP_REQUEST, 0x02=RESP_OK, 0x03=RESP_DENY
  Byte 1    : Gate ID    (1 byte)  — ID gate pengirim (1-255)
  Byte 2-5  : KTP UID    (4 byte)  — UID asli dari PN532
  Byte 6    : Hop Count  (1 byte)  — sisa hop (mulai 3, relay kurangi 1, buang jika 0)
  Byte 7    : Reserved   (1 byte)  — 0x00 (untuk ekspansi nanti)
  Byte 8-9  : Timestamp  (2 byte)  — detik sejak boot (0-65535)
  Byte 10-11: CRC-16     (2 byte)  — CRC dari byte 0-9

Contoh:
  01 01 188235CA 03 00 001E A3F2
  ^  ^  ^^^^^^^^ ^  ^  ^^^^ ^^^^
  CMD G1  UID   HOP RES  TS  CRC
"""

import struct
import time
from dataclasses import dataclass
from enum import IntEnum


class Command(IntEnum):
    TAP_REQUEST  = 0x01
    RESPONSE_OK  = 0x02
    RESPONSE_DENY= 0x03
    PING         = 0x04

COMMAND_LABELS = {
    Command.TAP_REQUEST:   "TAP_REQ",
    Command.RESPONSE_OK:   "RESP_OK",
    Command.RESPONSE_DENY: "RESP_DENY",
    Command.PING:          "PING",
}

PACKET_SIZE     = 12
HEX_STRING_LEN  = 24
MAX_HOP_COUNT   = 3   # packet mulai dengan hop=3, relay kurangi 1 tiap lompat


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE dari byte 0-9."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = (crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


@dataclass
class GatePacket:
    command:   Command
    gate_id:   int
    ktp_uid:   str    # 8-char hex (4 bytes)
    hop_count: int    # sisa hop
    reserved:  int    # selalu 0x00
    timestamp: int    # detik sejak boot
    crc:       int
    raw_hex:   str

    @property
    def command_label(self) -> str:
        return COMMAND_LABELS.get(self.command, f"UNKNOWN({hex(self.command)})")

    @property
    def is_crc_valid(self) -> bool:
        computed = crc16(bytes.fromhex(self.raw_hex)[0:10])
        return computed == self.crc

    @property
    def can_relay(self) -> bool:
        """Apakah packet masih boleh di-relay (hop_count > 0)."""
        return self.hop_count > 0

    def to_dict(self) -> dict:
        return {
            "command":   self.command_label,
            "gate_id":   self.gate_id,
            "ktp_uid":   self.ktp_uid,
            "hop_count": self.hop_count,
            "timestamp": self.timestamp,
            "crc_valid": self.is_crc_valid,
            "raw_hex":   self.raw_hex,
        }


def parse_hex_packet(hex_str: str) -> GatePacket:
    """Parse 24-char hex string -> GatePacket. Raises ValueError jika invalid."""
    hex_str = hex_str.strip().upper()

    if len(hex_str) != HEX_STRING_LEN:
        raise ValueError(
            f"Panjang hex harus {HEX_STRING_LEN} karakter, dapat: {len(hex_str)}"
        )

    raw = bytes.fromhex(hex_str)

    command_raw = raw[0]
    gate_id     = raw[1]
    ktp_uid     = raw[2:6].hex().upper()
    hop_count   = raw[6]
    reserved    = raw[7]
    timestamp,  = struct.unpack(">H", raw[8:10])
    crc_recv,   = struct.unpack(">H", raw[10:12])

    try:
        command = Command(command_raw)
    except ValueError:
        raise ValueError(f"Command tidak dikenal: {hex(command_raw)}")

    computed = crc16(raw[0:10])
    if computed != crc_recv:
        raise ValueError(
            f"CRC mismatch — diterima: {hex(crc_recv)}, dihitung: {hex(computed)}"
        )

    return GatePacket(command, gate_id, ktp_uid, hop_count, reserved, timestamp, crc_recv, hex_str)


def build_hex_packet(
    command:   Command,
    gate_id:   int,
    ktp_uid:   str,
    hop_count: int = MAX_HOP_COUNT,
    timestamp: int = None,
) -> str:
    """Build 24-char hex packet. CRC dihitung otomatis."""
    if len(ktp_uid) != 8:
        raise ValueError(f"ktp_uid harus 8 char hex (4 bytes), dapat: {len(ktp_uid)}")
    if not (1 <= gate_id <= 255):
        raise ValueError(f"gate_id harus 1-255, dapat: {gate_id}")
    if not (0 <= hop_count <= 255):
        raise ValueError(f"hop_count harus 0-255")

    if timestamp is None:
        timestamp = int(time.time()) % 65535

    payload = (
        bytes([int(command), gate_id])
        + bytes.fromhex(ktp_uid)
        + bytes([hop_count, 0x00])          # hop_count + reserved
        + struct.pack(">H", timestamp)
    )  # 10 bytes

    crc  = crc16(payload)
    full = payload + struct.pack(">H", crc)  # 12 bytes
    return full.hex().upper()


def decrement_hop(hex_str: str) -> str:
    """
    Kurangi hop_count sebesar 1, hitung ulang CRC.
    Dipakai di relay node sebelum forward.
    Raises ValueError jika hop_count sudah 0.
    """
    packet = parse_hex_packet(hex_str)
    if packet.hop_count == 0:
        raise ValueError("Hop count sudah 0, packet tidak boleh di-relay lagi")

    return build_hex_packet(
        packet.command,
        packet.gate_id,
        packet.ktp_uid,
        hop_count = packet.hop_count - 1,
        timestamp = packet.timestamp,
    )
