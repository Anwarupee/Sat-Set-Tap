"""
Mock packet generator — untuk dry run tanpa hardware.
Bisa dijalankan terpisah untuk simulasi pengiriman dari LoRa gateway.

Output: hex string ke stdout, satu per baris.
Pipe ke receiver: python mock_gen.py | python receiver.py --stdin
Atau jalankan langsung: python receiver.py --mock
"""

import random
import time
import sys
import struct
from enum import IntEnum


class Mode(IntEnum):
    TX = 0x0001
    RX = 0x0002


# Simulasi 10 "warga" dengan KTP UID tetap
REGISTERED_UIDS = [
    "12AB34CD56EF7890",
    "AABBCCDDEEFF0011",
    "DEADBEEF12345678",
    "CAFEBABE87654321",
    "0011223344556677",
    "FEEDFACE11223344",
    "BAADF00D99887766",
    "DEADDEAD12341234",
    "CAFECAFE56785678",
    "11223344AABBCCDD",
]

# Simulasi orang yang tidak terdaftar (invalid)
UNREGISTERED_UIDS = [
    "FFFFFFFFFFFFFFFF",
    "0000000000000000",
]


def build_packet(mode: Mode, gate_id: int, ktp_uid: str) -> str:
    header = struct.pack(">HH", int(mode), gate_id)
    ktp_bytes = bytes.fromhex(ktp_uid)
    return (header + ktp_bytes).hex().upper()


def generate_scenario():
    """
    Skenario simulasi:
    - 80% orang terdaftar, masuk normal
    - 10% orang yang sama mencoba masuk lagi (duplikat)
    - 10% UID tidak terdaftar / tidak dikenal
    """
    roll = random.random()
    gate_id = random.randint(1, 3)

    if roll < 0.8:
        uid = random.choice(REGISTERED_UIDS)
        label = "registered"
    elif roll < 0.9:
        # Duplikat: ambil dari UID yang sudah pernah dipilih
        uid = REGISTERED_UIDS[0]  # selalu orang pertama untuk simulasi duplikat
        label = "DUPLICATE"
    else:
        uid = random.choice(UNREGISTERED_UIDS)
        label = "unregistered"

    packet = build_packet(Mode.TX, gate_id, uid)
    return packet, gate_id, uid, label


if __name__ == "__main__":
    interval = float(sys.argv[1]) if len(sys.argv) > 1 else 1.5

    print(f"# Mock generator — interval {interval}s. Ctrl+C untuk stop.", file=sys.stderr)
    print(f"# Format: [hex_packet] <- [gate_id] [uid] [label]", file=sys.stderr)
    print(file=sys.stderr)

    while True:
        try:
            packet, gate_id, uid, label = generate_scenario()
            # Print ke stdout untuk di-pipe, dan info ke stderr
            print(packet, flush=True)
            print(f"  → Gate {gate_id} | {uid} | {label}", file=sys.stderr)
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\n# Mock generator berhenti.", file=sys.stderr)
            break
