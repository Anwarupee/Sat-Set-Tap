"""
Redis handler v4 — dengan sistem pre-registration + token.

Key schema:
  gate:registered:{uid}  → Hash: uid, name, token_max, token_left, registered_at, last_entry
  gate:registered:all    → Set : semua UID terdaftar
  gate:event:{uid}       → Hash: detail event terakhir
  gate:log               → List: 1000 event terakhir
  gate:uid:seen          → Set : UID yang sudah masuk HARI INI (di-flush tiap event)
  gate:stats:{gate_id}   → Hash: total_tap, last_seen
"""

import json
import time
from redis import Redis


class GateRedis:
    def __init__(self, host="localhost", port=6379, db=0, password=None):
        self.r = Redis(
            host=host, port=port, db=db,
            password=password, decode_responses=True
        )

    def ping(self) -> bool:
        try:
            return self.r.ping()
        except Exception:
            return False

    def store_tap(self, packet) -> dict:
        """
        Proses TAP_REQUEST dengan sistem token.

        Logic:
          1. Cek apakah UID terdaftar (gate:registered:{uid})
          2. Jika tidak terdaftar → DENY (tidak dikenal)
          3. Jika terdaftar, cek token_left
          4. token_left > 0 → ALLOW, kurangi token, catat entry
          5. token_left = 0 → DENY (token habis)
        """
        from datetime import datetime
        now     = datetime.now()
        now_iso = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond//1000:03d}"
        uid     = packet.ktp_uid
        reg_key = f"gate:registered:{uid}"

        # ── Cek registrasi ───────────────────────────────────
        reg_data = self.r.hgetall(reg_key)

        if not reg_data:
            # UID tidak terdaftar
            self._log_event(packet, "denied_unregistered", now_iso)
            self._update_stats(packet.gate_id, now_iso)
            return {
                "status":   "denied_unregistered",
                "ktp_uid":  uid,
                "gate_id":  packet.gate_id,
                "message":  "UID tidak terdaftar",
            }

        token_left = int(reg_data.get("token_left", 0))
        token_max  = int(reg_data.get("token_max",  3))

        if token_left <= 0:
            # Token habis
            self._log_event(packet, "denied_token_empty", now_iso, reg_data)
            self._update_stats(packet.gate_id, now_iso)
            return {
                "status":   "denied_token_empty",
                "ktp_uid":  uid,
                "gate_id":  packet.gate_id,
                "token_left": 0,
                "token_max":  token_max,
                "message":  "Token habis",
            }

        # ── Allow — kurangi token ─────────────────────────────
        new_token = token_left - 1
        self.r.hset(reg_key, mapping={
            "token_left": new_token,
            "last_entry": now_iso,
        })

        self._log_event(packet, "allowed", now_iso, reg_data, new_token)
        self._update_stats(packet.gate_id, now_iso)

        return {
            "status":     "allowed",
            "ktp_uid":    uid,
            "gate_id":    packet.gate_id,
            "token_left": new_token,
            "token_max":  token_max,
            "name":       reg_data.get("name", ""),
            "message":    f"Masuk — token sisa: {new_token}/{token_max}",
        }

    def _log_event(self, packet, status: str, ts: str,
                   reg_data: dict = None, token_left: int = None):
        entry = {
            "command":          packet.command_label,
            "gate_id":          packet.gate_id,
            "ktp_uid":          packet.ktp_uid,
            "status":           status,
            "timestamp":        ts,
            "server_timestamp": ts,
            "crc_valid":        packet.is_crc_valid,
        }
        if reg_data:
            entry["name"]       = reg_data.get("name", "")
            entry["token_max"]  = reg_data.get("token_max", "-")
        if token_left is not None:
            entry["token_left"] = token_left

        self.r.lpush("gate:log", json.dumps(entry))
        self.r.ltrim("gate:log", 0, 999)

        # Simpan detail event per UID
        self.r.hset(f"gate:event:{packet.ktp_uid}", mapping={
            "status":    status,
            "gate_id":   packet.gate_id,
            "timestamp": ts,
            "raw_hex":   packet.raw_hex,
        })

    def _update_stats(self, gate_id: int, ts: str):
        key = f"gate:stats:{gate_id}"
        self.r.hincrby(key, "total_tap", 1)
        self.r.hset(key, "last_seen", ts)

    def get_recent_log(self, count=20) -> list:
        return [json.loads(e) for e in self.r.lrange("gate:log", 0, count - 1)]

    def get_stats(self) -> dict:
        total_reg  = self.r.scard("gate:registered:all") or 0
        total_entry = 0
        gate_stats  = {}

        for key in self.r.scan_iter("gate:stats:*"):
            gid  = key.split(":")[-1]
            data = self.r.hgetall(key)
            gate_stats[f"gate_{gid}"] = {
                "total_tap": int(data.get("total_tap", 0)),
                "last_seen": data.get("last_seen", "-"),
            }

        # Hitung berapa yang sudah masuk (token berkurang)
        for uid in self.r.smembers("gate:registered:all"):
            data = self.r.hgetall(f"gate:registered:{uid}")
            if data and data.get("last_entry", "-") != "-":
                total_entry += 1

        return {
            "total_registered":  total_reg,
            "total_entry":       total_entry,
            "total_unique_entries": total_entry,  # backward compat dashboard
            "total_tap":         sum(g["total_tap"] for g in gate_stats.values()),
            "gates":             gate_stats,
        }

    def get_registered_list(self, limit=100) -> list:
        uids   = list(self.r.smembers("gate:registered:all"))[:limit]
        result = []
        for uid in uids:
            data = self.r.hgetall(f"gate:registered:{uid}")
            if data:
                result.append(data)
        return sorted(result, key=lambda x: x.get("registered_at", ""), reverse=True)

    def flush_all(self):
        """DANGER: hapus semua data gate."""
        keys = list(self.r.scan_iter("gate:*"))
        if keys:
            self.r.delete(*keys)

    def flush_event_only(self):
        """Flush event log saja, tapi pertahankan data registrasi."""
        for key in ["gate:log", "gate:uid:seen"]:
            self.r.delete(key)
        for key in self.r.scan_iter("gate:stats:*"):
            self.r.delete(key)
        for key in self.r.scan_iter("gate:event:*"):
            self.r.delete(key)
