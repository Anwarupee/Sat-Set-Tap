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
    def __init__(self, host="localhost", port=6379, db=0, password=None, token_enabled=True):
        self.r = Redis(
            host=host, port=port, db=db,
            password=password, decode_responses=True
        )
        self.token_enabled = token_enabled

    def ping(self) -> bool:
        try:
            return self.r.ping()
        except Exception:
            return False

    def store_tap(self, packet, rssi: int = 0) -> dict:
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
            self._log_event(packet, "denied_unregistered", now_iso, rssi=rssi)
            self._update_stats(packet.gate_id, now_iso)
            return {
                "status":   "denied_unregistered",
                "ktp_uid":  uid,
                "gate_id":  packet.gate_id,
                "message":  "UID tidak terdaftar",
            }

        token_left = int(reg_data.get("token_left", 0))
        token_max  = int(reg_data.get("token_max",  3))

        if self.token_enabled:
            if token_left <= 0:
                # Token habis
                self._log_event(packet, "denied_token_empty", now_iso, reg_data, rssi=rssi)
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
            self.r.hset(reg_key, "token_left", new_token)
            self.r.hset(reg_key, "last_entry", now_iso)

            self._log_event(packet, "allowed", now_iso, reg_data, new_token, rssi=rssi)
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
        else:
            # Token disabled — selalu allow untuk UID terdaftar
            self._log_event(packet, "allowed", now_iso, reg_data, rssi=rssi)
            self._update_stats(packet.gate_id, now_iso)

            return {
                "status":     "allowed",
                "ktp_uid":    uid,
                "gate_id":    packet.gate_id,
                "token_left": token_left,
                "token_max":  token_max,
                "name":       reg_data.get("name", ""),
                "message":    f"Masuk (token disabled) — sisa: {token_left}/{token_max}",
            }

    def _log_event(self, packet, status: str, ts: str,
                   reg_data: dict = None, token_left: int = None,
                   rssi: int = 0):
        entry = {
            "command":          packet.command_label,
            "gate_id":          packet.gate_id,
            "ktp_uid":          packet.ktp_uid,
            "status":           status,
            "timestamp":        ts,
            "server_timestamp": ts,
            "crc_valid":        packet.is_crc_valid,
            "rssi":             rssi,
        }
        if reg_data:
            entry["name"]       = reg_data.get("name", "")
            entry["token_max"]  = reg_data.get("token_max", "-")
        if token_left is not None:
            entry["token_left"] = token_left

        self.r.lpush("gate:log", json.dumps(entry))
        self.r.ltrim("gate:log", 0, 999)

        # Simpan detail event per UID
        ekey = f"gate:event:{packet.ktp_uid}"
        self.r.hset(ekey, "status", status)
        self.r.hset(ekey, "gate_id", packet.gate_id)
        self.r.hset(ekey, "timestamp", ts)
        self.r.hset(ekey, "raw_hex", packet.raw_hex)

    def store_latency(self, gate_id: int, latency_ms: int):
        """Simpan latency measurement ke Redis, keep last 50 entries."""
        key = f"gate:latency:{gate_id}"
        self.r.lpush(key, latency_ms)
        self.r.ltrim(key, 0, 49)

    def store_heartbeat(self, gate_id: int, rssi: int = 0):
        """Simpan heartbeat dari gate node ke Redis."""
        from datetime import datetime
        key = f"gate:heartbeat:{gate_id}"
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        self.r.hset(key, "gate_id", gate_id)
        self.r.hset(key, "last_heartbeat", now)
        self.r.hset(key, "rssi", rssi)
        # TTL: expired setelah 10 menit (cleanup otomatis jika gate mati total)
        self.r.expire(key, 600)

    def _update_stats(self, gate_id: int, ts: str):
        key = f"gate:stats:{gate_id}"
        self.r.hincrby(key, "total_tap", 1)
        self.r.hset(key, "last_seen", ts)

    def import_uids(self, rows: list[dict]) -> dict:
        """
        Import massal UID dari list of dict.
        rows: [{"uid":"12AB34CD", "name":"Budi", "token_max":3}, ...]
        Returns: {"total": N, "success": N, "skipped": N, "errors": [msgs]}
        """
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        success = 0
        skipped = 0
        errors = []
        for row in rows:
            uid = row.get("uid", "").strip().upper()
            if not uid:
                continue
            if len(uid) != 8 or not all(c in "0123456789ABCDEF" for c in uid):
                errors.append(f"UID '{uid}' tidak valid (harus 8 char hex)")
                skipped += 1
                continue
            if self.r.hexists(f"gate:registered:{uid}", "uid"):
                skipped += 1
                continue
            name = row.get("name", "").strip()
            try:
                token_max = int(row.get("token_max", 3))
            except (ValueError, TypeError):
                token_max = 3
            key = f"gate:registered:{uid}"
            self.r.hset(key, "uid", uid)
            self.r.hset(key, "name", name or uid)
            self.r.hset(key, "token_max", token_max)
            self.r.hset(key, "token_left", token_max)
            self.r.hset(key, "registered_at", now)
            self.r.hset(key, "last_entry", "-")
            self.r.sadd("gate:registered:all", uid)
            success += 1
        return {"total": len(rows), "success": success, "skipped": skipped, "errors": errors}

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

    def revoke_uid(self, uid: str) -> bool:
        """Hapus satu UID dari registrasi."""
        key = f"gate:registered:{uid}"
        if not self.r.hexists(key, "uid"):
            return False
        self.r.delete(key)
        self.r.delete(f"gate:event:{uid}")
        self.r.srem("gate:registered:all", uid)
        return True

    def get_registered_uids(self, limit=200) -> list:
        """List semua UID terdaftar dengan info."""
        uids = list(self.r.smembers("gate:registered:all"))[:limit]
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
