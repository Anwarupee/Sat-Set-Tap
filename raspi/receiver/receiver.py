"""
receiver.py v5 — Thread-safe serial dengan response queue.
Gate tap bersamaan tidak akan saling block.
"""

import argparse, logging, sys, time, threading, queue, os
from packet import parse_hex_packet, build_hex_packet, Command
from redis_handler import GateRedis

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("receiver")

STATUS_ICON = {
    "allowed":             "✓ MASUK   ",
    "denied_unregistered": "✗ TDK DFTR",
    "denied_token_empty":  "✗ TOKEN HS",
}

# Queue untuk response yang perlu dikirim balik ke gateway
response_queue = queue.Queue()


def send_response_worker(ser):
    """
    Thread terpisah khusus untuk kirim response ke Serial.
    Semua response masuk ke queue dulu, dikirim satu per satu.
    Tidak akan bentrok dengan thread baca Serial.
    """
    while True:
        try:
            line = response_queue.get(timeout=1)
            if line is None:  # signal stop
                break
            ser.write(line.encode("ascii"))
            ser.flush()
            log.debug(f"Serial TX: {line.strip()}")
        except queue.Empty:
            continue
        except Exception as e:
            log.error(f"Serial write error: {e}")


def send_response(command: Command, gate_id: int, ktp_uid: str):
    """Masukkan response ke queue — non-blocking."""
    try:
        hex_pkt = build_hex_packet(command, gate_id, ktp_uid)
        line    = f"RESP:{hex_pkt}\n"
        response_queue.put(line)
        log.info(f"→ QUEUE {command.name} | Gate {gate_id} | UID: {ktp_uid}")
    except Exception as e:
        log.error(f"Gagal queue response: {e}")


def parse_serial_line(line: str):
    line = line.strip()
    if not line or line.startswith("#"): return None
    if line.startswith("DATA:"):
        parts = line.split(":")
        if len(parts) >= 3:
            try:    return parts[1], int(parts[2])
            except: return parts[1], 0
    return None


def handle_packet(hex_str: str, rssi: int, db: GateRedis):
    try:
        packet = parse_hex_packet(hex_str)
    except ValueError as e:
        log.error(f"✗ PARSE/CRC ERR | '{hex_str}' | {e}")
        return

    if packet.command != Command.TAP_REQUEST:
        return

    result = db.store_tap(packet)
    icon   = STATUS_ICON.get(result["status"], "?")
    rssi_w = " ⚡LEMAH" if rssi < -100 else ""

    log.info(
        f"{icon} | Gate {packet.gate_id:>3} "
        f"| UID: {packet.ktp_uid} "
        f"| {result.get('message','')}"
        f"| RSSI: {rssi}{rssi_w}"
    )

    # Kirim response via queue
    allowed = result["status"] == "allowed"
    cmd     = Command.RESPONSE_OK if allowed else Command.RESPONSE_DENY
    send_response(cmd, packet.gate_id, packet.ktp_uid)


def run_serial(db: GateRedis, port: str, baudrate=115200):
    try:
        import serial
    except ImportError:
        log.error("sudo apt install python3-serial")
        sys.exit(1)

    log.info(f"Membuka serial {port} @ {baudrate} baud...")
    try:
        ser = serial.Serial(port, baudrate, timeout=2)
    except Exception as e:
        log.error(f"Gagal buka {port}: {e}")
        sys.exit(1)

    log.info("Serial OK — menunggu data...\n")

    # Start response writer thread
    writer = threading.Thread(target=send_response_worker, args=(ser,), daemon=True)
    writer.start()

    # Main thread: baca Serial
    while True:
        try:
            raw  = ser.readline()
            if not raw: continue
            line = raw.decode("ascii", errors="ignore").strip()
            if not line: continue
            if line.startswith("#"):
                log.debug(f"GW: {line[1:].strip()}")
                continue

            result = parse_serial_line(line)
            if result:
                hex_str, rssi = result
                # Proses di thread baru agar tidak block pembacaan Serial
                t = threading.Thread(
                    target=handle_packet,
                    args=(hex_str, rssi, db),
                    daemon=True
                )
                t.start()

        except KeyboardInterrupt:
            log.info("Receiver berhenti.")
            response_queue.put(None)  # stop writer
            break
        except Exception as e:
            log.error(f"Serial error: {e}")
            time.sleep(0.5)


def run_mock(db: GateRedis):
    import random
    from packet import build_hex_packet

    MOCK_UIDS = ["12AB34CD", "AABBCCDD", "DEADBEEF", "CAFEBABE", "00112233"]
    log.info("Mode MOCK — Ctrl+C keluar.\n")

    # Register beberapa UID dulu untuk test
    for uid in MOCK_UIDS[:3]:
        db.r.hset(f"gate:registered:{uid}", mapping={
            "uid": uid, "name": f"Test {uid[:4]}",
            "token_max": 3, "token_left": 3,
            "registered_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "last_entry": "-",
        })
        db.r.sadd("gate:registered:all", uid)
    log.info(f"Mock: {len(MOCK_UIDS[:3])} UID di-register\n")

    start = int(time.time())
    count = 0
    while True:
        try:
            uid  = random.choice(MOCK_UIDS)
            gate = random.randint(1, 2)
            ts   = (int(time.time()) - start) % 65535
            hex_str = build_hex_packet(Command.TAP_REQUEST, gate, uid, ts)
            handle_packet(hex_str, random.randint(-90, -40), db)
            count += 1
            if count % 5 == 0:
                s = db.get_stats()
                log.info(f"--- Stats: {s['total_registered']} registered, "
                         f"{s['total_entry']} sudah masuk ---")
            time.sleep(2)
        except KeyboardInterrupt:
            log.info("Mock berhenti.")
            break


def run_stdin(db: GateRedis):
    log.info("Mode STDIN — ketik hex 24 char. Ctrl+C keluar.")
    while True:
        try:
            line = input("hex> ").strip()
            if line: handle_packet(line, 0, db)
        except (KeyboardInterrupt, EOFError):
            break


def main():
    parser = argparse.ArgumentParser(description="Gate Receiver v5")
    group  = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--serial", metavar="PORT")
    group.add_argument("--mock",   action="store_true")
    group.add_argument("--stdin",  action="store_true")
    parser.add_argument("--redis-host", default="localhost")
    parser.add_argument("--redis-port", type=int, default=6379)
    parser.add_argument("--redis-pass", default=os.getenv("REDIS_PASS", ""))
    parser.add_argument("--baud",       type=int, default=115200)
    args = parser.parse_args()

    db = GateRedis(host=args.redis_host, port=args.redis_port, password=args.redis_pass)
    if not db.ping():
        log.error("Redis tidak jalan!")
        sys.exit(1)
    log.info(f"Redis OK @ {args.redis_host}:{args.redis_port}")

    if args.serial: run_serial(db, args.serial, args.baud)
    elif args.mock:  run_mock(db)
    elif args.stdin: run_stdin(db)


if __name__ == "__main__":
    main()