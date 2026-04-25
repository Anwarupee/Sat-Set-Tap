"""
Main receiver untuk Raspberry Pi.

Mode:
  --serial  : Baca dari serial port (LoRa gateway nyata)
  --stdin   : Baca dari stdin untuk dry run / testing manual
  --mock    : Auto-generate mock packets untuk demo

Usage:
  python receiver.py --stdin          # ketik hex manual
  python receiver.py --mock           # auto mock
  python receiver.py --serial /dev/ttyUSB0
"""

import argparse
import sys
import time
import logging

from packet import parse_hex_packet, GatePacket
from redis_handler import GateRedis

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("receiver")


def handle_packet(hex_str: str, db: GateRedis) -> None:
    """Parse hex, simpan ke Redis, print result."""
    try:
        packet = parse_hex_packet(hex_str)
        result = db.store_event(packet)

        if result["status"] == "duplicate":
            log.warning(
                f"⚠  DUPLIKAT  | Gate {packet.gate_id} | UID: {packet.ktp_uid}"
            )
        else:
            log.info(
                f"✓  MASUK     | Gate {packet.gate_id} | UID: {packet.ktp_uid} | {result['timestamp']}"
            )

    except ValueError as e:
        log.error(f"✗  PARSE ERR | Raw: '{hex_str}' | {e}")


def run_stdin(db: GateRedis):
    """Dry run — ketik hex packet manual di terminal."""
    log.info("Mode: STDIN — ketik hex packet (24 char), Enter untuk proses. Ctrl+C untuk keluar.")
    log.info("Contoh: 0001000112AB34CD56EF7890")
    print()
    while True:
        try:
            line = input("hex> ").strip()
            if line:
                handle_packet(line, db)
        except (KeyboardInterrupt, EOFError):
            log.info("Receiver berhenti.")
            break


def run_serial(db: GateRedis, port: str, baudrate: int = 9600):
    """Baca dari serial port — untuk LoRa gateway nyata."""
    try:
        import serial
    except ImportError:
        log.error("pyserial tidak terinstall. Jalankan: pip install pyserial")
        sys.exit(1)

    log.info(f"Mode: SERIAL — membaca dari {port} @ {baudrate} baud")
    with serial.Serial(port, baudrate, timeout=1) as ser:
        log.info("Serial terbuka. Menunggu data...")
        while True:
            try:
                line = ser.readline().decode("ascii", errors="ignore").strip()
                if line:
                    log.debug(f"Raw serial: {line}")
                    handle_packet(line, db)
            except KeyboardInterrupt:
                log.info("Receiver berhenti.")
                break


def run_mock(db: GateRedis):
    """Auto-generate mock packets untuk demo/testing."""
    import random
    from packet import build_hex_packet, Mode

    MOCK_UIDS = [
        "12AB34CD56EF7890",
        "AABBCCDDEEFF0011",
        "DEADBEEF12345678",
        "CAFEBABE87654321",
        "0011223344556677",
    ]

    log.info("Mode: MOCK — generate packet otomatis setiap 2 detik. Ctrl+C untuk keluar.")
    print()

    count = 0
    while True:
        try:
            # Sesekali kirim UID yang sama untuk test duplikat
            uid = random.choice(MOCK_UIDS)
            gate_id = random.randint(1, 3)
            mode = Mode.TX

            hex_str = build_hex_packet(mode, gate_id, uid)
            log.debug(f"Mock packet: {hex_str}")
            handle_packet(hex_str, db)

            count += 1
            if count % 5 == 0:
                stats = db.get_stats()
                log.info(f"--- Stats: {stats['total_unique_entries']} unique entries ---")

            time.sleep(2)

        except KeyboardInterrupt:
            log.info("Mock berhenti.")
            break


def main():
    parser = argparse.ArgumentParser(description="Gate LoRa Receiver")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--stdin", action="store_true", help="Dry run via keyboard input")
    group.add_argument("--mock", action="store_true", help="Auto mock packet generator")
    group.add_argument("--serial", metavar="PORT", help="Serial port (e.g. /dev/ttyUSB0)")

    parser.add_argument("--redis-host", default="localhost")
    parser.add_argument("--redis-port", type=int, default=6379)
    parser.add_argument("--baud", type=int, default=9600)
    args = parser.parse_args()

    # Init Redis
    db = GateRedis(host=args.redis_host, port=args.redis_port)
    if not db.ping():
        log.error(f"Tidak bisa connect ke Redis di {args.redis_host}:{args.redis_port}")
        log.error("Pastikan Redis running: sudo systemctl start redis")
        sys.exit(1)
    log.info(f"Redis OK @ {args.redis_host}:{args.redis_port}")

    if args.stdin:
        run_stdin(db)
    elif args.mock:
        run_mock(db)
    elif args.serial:
        run_serial(db, args.serial, args.baud)


if __name__ == "__main__":
    main()
