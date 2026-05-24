"""
test_raspi.py — jalankan di RPi untuk verifikasi semua komponen OK.

Usage:
  python3 test_raspi.py

Akan mengecek:
  1. Redis bisa diakses
  2. Packet parser berjalan benar
  3. Simulasi terima UID dari ESP32 dan simpan ke Redis
  4. Baca balik dari Redis untuk konfirmasi
"""

import sys
import os
import time

# Tambah path receiver
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "receiver"))

PASS = "✓"
FAIL = "✗"
SEP  = "-" * 45


def section(title):
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)


def check(label, ok, detail=""):
    icon = PASS if ok else FAIL
    print(f"  {icon}  {label}" + (f"  —  {detail}" if detail else ""))
    return ok


# ── 1. Redis ──────────────────────────────────────────────────
section("1. Cek Redis")

try:
    import redis as redis_lib
    r = redis_lib.Redis(host="localhost", port=6379, decode_responses=True)
    pong = r.ping()
    check("Redis running", pong, "localhost:6379")
except Exception as e:
    check("Redis running", False, str(e))
    print("\n  Jalankan dulu: sudo systemctl start redis-server")
    sys.exit(1)


# ── 2. Packet parser ──────────────────────────────────────────
section("2. Cek Packet Parser")

try:
    from packet import build_hex_packet, parse_hex_packet, Command, normalize_uid_input
    check("Import packet.py", True)
except ImportError:
    # normalize_uid_input mungkin belum ada, coba tanpa
    try:
        from packet import build_hex_packet, parse_hex_packet, Command
        check("Import packet.py", True)
    except ImportError as e:
        check("Import packet.py", False, str(e))
        print("  Pastikan file receiver/packet.py sudah di-copy ke RPi")
        sys.exit(1)

# Test build & parse
try:
    h = build_hex_packet(Command.TAP_REQUEST, 1, "12AB34CD", 42)
    p = parse_hex_packet(h)
    ok = (p.gate_id == 1 and p.ktp_uid == "12AB34CD" and p.is_crc_valid)
    check("Build & parse packet", ok, f"hex={h}")
except Exception as e:
    check("Build & parse packet", False, str(e))


# ── 3. Simulasi terima UID dari ESP32 ────────────────────────
section("3. Simulasi Terima UID (seperti dari ESP32)")

# Format yang dikirim temanmu: "12:AB:34:CD"
TEST_CASES = [
    ("12:AB:34:CD",   "12AB34CD"),  # format colon
    ("aabbccdd",      "AABBCCDD"),  # lowercase
    ("12AB34CD",      "12AB34CD"),  # sudah benar
    ("DE:AD:BE:EF",   "DEADBEEF"),  # format colon lain
]

def normalize_uid(raw: str) -> str:
    uid = raw.strip().replace(":", "").replace(" ", "").replace("-", "").upper()
    bytes.fromhex(uid)  # validasi hex
    if len(uid) < 8:
        raise ValueError(f"UID terlalu pendek: {uid}")
    return uid[:8]  # ambil 4 byte pertama

all_ok = True
for raw, expected in TEST_CASES:
    try:
        result = normalize_uid(raw)
        ok = result == expected
        check(f"normalize '{raw}'", ok, f"-> {result}")
        all_ok = all_ok and ok
    except Exception as e:
        check(f"normalize '{raw}'", False, str(e))
        all_ok = False


# ── 4. Simpan ke Redis & baca balik ──────────────────────────
section("4. Simpan ke Redis & Baca Balik")

try:
    from redis_handler import GateRedis
    db = GateRedis()

    # Bersihkan data test lama
    db.flush_all()

    # Simulasi 3 tap
    TEST_UIDS = ["12AB34CD", "AABBCCDD", "DEADBEEF"]
    for i, uid in enumerate(TEST_UIDS):
        ts = i * 10
        hex_pkt = build_hex_packet(Command.TAP_REQUEST, 1, uid, ts)
        pkt     = parse_hex_packet(hex_pkt)
        result  = db.store_tap(pkt)
        check(f"Tap UID {uid}", result["status"] == "new", result["status"])

    # Test duplikat
    hex_pkt = build_hex_packet(Command.TAP_REQUEST, 1, "12AB34CD", 99)
    pkt     = parse_hex_packet(hex_pkt)
    result  = db.store_tap(pkt)
    check("Duplikat terdeteksi", result["status"] == "duplicate", result["status"])

    # Baca stats
    stats = db.get_stats()
    total = stats["total_unique_entries"]
    check("Total unique di Redis", total == 3, f"{total} entries")

    # Bersihkan setelah test
    db.flush_all()
    check("Cleanup Redis", True)

except Exception as e:
    check("Redis store/read", False, str(e))


# ── 5. Info sistem ───────────────────────────────────────────
section("5. Info Sistem RPi")

import subprocess

def run_cmd(cmd):
    try:
        return subprocess.check_output(cmd, shell=True, text=True).strip()
    except:
        return "N/A"

ip      = run_cmd("hostname -I | awk '{print $1}'")
redis_v = run_cmd("redis-server --version | awk '{print $3}'")
py_v    = run_cmd("python3 --version")
ports   = run_cmd("ls /dev/ttyUSB* /dev/ttyAMA* 2>/dev/null || echo 'tidak ada serial port'")

print(f"  IP RPi      : {ip}")
print(f"  Redis ver   : {redis_v}")
print(f"  Python ver  : {py_v}")
print(f"  Serial ports: {ports}")

print(f"\n{SEP}")
print("  Semua cek selesai.")
print(f"  Jalankan receiver: python3 receiver/receiver.py --mock")
print(SEP)
