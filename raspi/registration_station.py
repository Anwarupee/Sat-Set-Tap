"""
registration_station.py v2 — Jalankan di RPi untuk registrasi H-1.

Arduino Uno + 2x PN532 colok ke RPi via USB Serial.
RPi baca UID dari Arduino, simpan ke Redis lokal.

Usage:
  python3 registration_station.py --serial /dev/ttyUSB0
  python3 registration_station.py --serial /dev/ttyUSB0 --tokens 3
  python3 registration_station.py --manual   (input UID manual, tanpa Arduino)
"""

import argparse, time, logging, json
import redis as redis_lib

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("registration")

TOKEN_MAX = 3


class RegistrationStation:
    def __init__(self, host="localhost", port=6379, password=None):
        self.r = redis_lib.Redis(
            host=host, port=port, password=password, decode_responses=True
        )
        self.r.ping()
        log.info(f"Redis OK @ {host}:{port}")

    def register_uid(self, uid: str, name: str = "") -> dict:
        uid     = uid.strip().upper()
        reg_key = f"gate:registered:{uid}"
        existing = self.r.hgetall(reg_key)

        if existing:
            return {
                "status":        "already",
                "uid":           uid,
                "name":          existing.get("name", ""),
                "token_left":    int(existing.get("token_left", 0)),
                "token_max":     int(existing.get("token_max", TOKEN_MAX)),
                "registered_at": existing.get("registered_at", "-"),
            }

        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.r.hset(reg_key, mapping={
            "uid":           uid,
            "name":          name,
            "token_max":     TOKEN_MAX,
            "token_left":    TOKEN_MAX,
            "registered_at": now,
            "last_entry":    "-",
        })
        self.r.sadd("gate:registered:all", uid)

        log.info(f"✓ DAFTAR | UID: {uid} | Token: {TOKEN_MAX}x | {now}")
        return {
            "status":     "new",
            "uid":        uid,
            "name":       name,
            "token_left": TOKEN_MAX,
            "token_max":  TOKEN_MAX,
        }

    def get_stats(self) -> dict:
        return {
            "total_registered": self.r.scard("gate:registered:all") or 0,
            "token_max": TOKEN_MAX,
        }

    def list_registered(self, limit=50) -> list:
        uids   = list(self.r.smembers("gate:registered:all"))[:limit]
        result = []
        for uid in uids:
            data = self.r.hgetall(f"gate:registered:{uid}")
            if data:
                result.append(data)
        return sorted(result, key=lambda x: x.get("registered_at",""), reverse=True)


def normalize_uid(raw: str) -> str:
    uid = raw.strip().replace(":","").replace(" ","").replace("-","").upper()
    bytes.fromhex(uid)
    return uid[:8]


def run_serial(station: RegistrationStation, port: str, baud=9600):
    try:
        import serial
    except ImportError:
        log.error("Install pyserial: sudo apt install python3-serial")
        return

    log.info(f"Mode SERIAL — {port} @ {baud}")
    log.info("Tempelkan KTP ke reader untuk mendaftarkan peserta...\n")

    count = 0
    with serial.Serial(port, baud, timeout=2) as ser:
        while True:
            try:
                raw  = ser.readline().decode("ascii", errors="ignore").strip()
                if not raw: continue

                # Log dari Arduino (baris #)
                if raw.startswith("#"):
                    log.debug(f"Arduino: {raw[1:].strip()}")
                    continue

                if raw == "READY":
                    log.info("Arduino reader siap — tempelkan kartu")
                    continue

                if raw.startswith("ERR:"):
                    log.error(f"Arduino: {raw[4:]}")
                    continue

                if raw.startswith("UID:"):
                    uid_raw = raw[4:]
                    try:
                        uid = normalize_uid(uid_raw)
                    except Exception:
                        log.error(f"UID tidak valid: '{uid_raw}'")
                        ser.write(b"DUP\n")
                        continue

                    result = station.register_uid(uid)
                    count += 1

                    if result["status"] == "new":
                        log.info(f"✓ TERDAFTAR #{count} | {uid} | Token: {result['token_left']}x")
                        ser.write(b"OK\n")
                    else:
                        log.warning(f"⚠ SUDAH ADA  | {uid} | "
                                    f"Token: {result['token_left']}/{result['token_max']}x "
                                    f"| Daftar: {result['registered_at']}")
                        ser.write(b"DUP\n")

            except KeyboardInterrupt:
                log.info(f"\nRegistrasi selesai. Total terdaftar sesi ini: {count} peserta")
                s = station.get_stats()
                log.info(f"Total keseluruhan: {s['total_registered']} peserta")
                break
            except Exception as e:
                log.error(f"Error: {e}")
                time.sleep(1)


def run_manual(station: RegistrationStation):
    log.info("Mode MANUAL — ketik UID, 'list', 'stats', atau 'quit'\n")
    while True:
        try:
            raw = input("UID> ").strip()
            if not raw: continue
            if raw.lower() == "quit": break

            if raw.lower() == "stats":
                s = station.get_stats()
                print(f"  Total terdaftar : {s['total_registered']} peserta")
                print(f"  Token per orang : {s['token_max']}x\n")
                continue

            if raw.lower() == "list":
                entries = station.list_registered()
                print(f"\n  {'UID':<12} {'Nama':<20} {'Token':<8} {'Waktu Daftar'}")
                print(f"  {'-'*55}")
                for e in entries:
                    print(f"  {e['uid']:<12} {e.get('name','-'):<20} "
                          f"{e['token_left']}/{e['token_max']:<4} {e['registered_at']}")
                print()
                continue

            try:
                uid = normalize_uid(raw)
            except Exception:
                log.error(f"Format tidak valid: '{raw}'")
                continue

            name   = input("Nama (Enter skip)> ").strip()
            result = station.register_uid(uid, name)

            if result["status"] == "new":
                print(f"  ✓ Terdaftar! UID: {uid} | Token: {result['token_left']}x\n")
            else:
                print(f"  ⚠ Sudah ada | UID: {uid} | "
                      f"Token: {result['token_left']}/{result['token_max']}x\n")

        except (KeyboardInterrupt, EOFError):
            log.info("Selesai.")
            break


def main():
    parser = argparse.ArgumentParser(description="Registration Station v2")
    parser.add_argument("--redis-host", default="localhost")
    parser.add_argument("--redis-port", type=int, default=6379)
    parser.add_argument("--redis-pass", default=None)
    parser.add_argument("--tokens",     type=int, default=3)

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--serial", metavar="PORT", help="Port Arduino (e.g. /dev/ttyUSB0)")
    mode.add_argument("--manual", action="store_true")
    args = parser.parse_args()

    global TOKEN_MAX
    TOKEN_MAX = args.tokens

    station = RegistrationStation(args.redis_host, args.redis_port, args.redis_pass)
    s       = station.get_stats()
    log.info(f"Total sudah terdaftar: {s['total_registered']} peserta")
    log.info(f"Token per peserta: {TOKEN_MAX}x\n")

    if args.serial:
        run_serial(station, args.serial)
    else:
        run_manual(station)


if __name__ == "__main__":
    main()