"""
Redis handler untuk menyimpan dan query data gate.

Key schema:
  gate:event:{ktp_uid}        → Hash: {mode, gate_id, timestamp, raw_hex}
  gate:log                    → List: raw hex packets (newest first, max 1000)
  gate:uid:set                → Set: semua KTP UID yang sudah tap (untuk cek duplikat)
  gate:stats:{gate_id}        → Hash: {total_tap, last_seen}
"""

import json
import time
from typing import Optional

import redis

from packet import GatePacket


class GateRedis:
    def __init__(self, host: str = "localhost", port: int = 6379, db: int = 0):
        self.r = redis.Redis(host=host, port=port, db=db, decode_responses=True)

    def ping(self) -> bool:
        try:
            return self.r.ping()
        except redis.ConnectionError:
            return False

    def store_event(self, packet: GatePacket) -> dict:
        """
        Simpan event tap ke Redis.
        Return: {"status": "new"|"duplicate", "ktp_uid": ..., "timestamp": ...}
        """
        ts = time.time()
        ts_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))

        # Cek duplikat — apakah UID ini sudah pernah masuk?
        is_duplicate = self.r.sismember("gate:uid:set", packet.ktp_uid)

        # Simpan event detail
        event_key = f"gate:event:{packet.ktp_uid}"
        self.r.hset(event_key, mapping={
            "mode": packet.mode_label,
            "gate_id": packet.gate_id,
            "timestamp": ts_iso,
            "unix_ts": ts,
            "raw_hex": packet.raw_hex,
            "is_duplicate": "1" if is_duplicate else "0",
        })

        # Tambah ke log (list, newest first, max 1000 entries)
        log_entry = json.dumps({**packet.to_dict(), "timestamp": ts_iso, "is_duplicate": is_duplicate})
        self.r.lpush("gate:log", log_entry)
        self.r.ltrim("gate:log", 0, 999)

        # Tambah UID ke set jika belum ada
        if not is_duplicate:
            self.r.sadd("gate:uid:set", packet.ktp_uid)

        # Update stats per gate
        stats_key = f"gate:stats:{packet.gate_id}"
        self.r.hincrby(stats_key, "total_tap", 1)
        self.r.hset(stats_key, "last_seen", ts_iso)

        return {
            "status": "duplicate" if is_duplicate else "new",
            "ktp_uid": packet.ktp_uid,
            "gate_id": packet.gate_id,
            "timestamp": ts_iso,
        }

    def get_recent_log(self, count: int = 20) -> list:
        """Ambil N event terakhir dari log."""
        entries = self.r.lrange("gate:log", 0, count - 1)
        return [json.loads(e) for e in entries]

    def get_stats(self) -> dict:
        """Summary statistik semua gate."""
        total_unique = self.r.scard("gate:uid:set")
        
        # Scan gate stats keys
        gate_stats = {}
        for key in self.r.scan_iter("gate:stats:*"):
            gate_id = key.split(":")[-1]
            gate_stats[f"gate_{gate_id}"] = self.r.hgetall(key)

        return {
            "total_unique_entries": total_unique,
            "gates": gate_stats,
        }

    def is_uid_registered(self, ktp_uid: str) -> bool:
        """Cek apakah UID KTP sudah terdaftar di sistem (boleh masuk)."""
        return self.r.sismember("gate:uid:set", ktp_uid)

    def flush_all(self):
        """DANGER: Hapus semua data gate. Untuk testing only."""
        keys = list(self.r.scan_iter("gate:*"))
        if keys:
            self.r.delete(*keys)
