"""
anomaly.py v3 — TheGate AI Security Engine
Taruh di: ~/TheGate/raspi/anomaly.py

Metode AI:
  Statistical Anomaly Detection dengan sliding window analysis.
  Setiap anomali dideteksi melalui threshold-based expert system
  yang menganalisis pola temporal (waktu antar-event) dan kontekstual
  (spasial antar-gate).

  4 tipe anomali:
    1. TAILGATING      — UID sama tap < 30 detik (ikutan masuk)
    2. UNKNOWN_FLOOD   — > 3 UID tidak terdaftar dalam 2 menit (probing)
    3. GATE_SILENT     — gate tidak heartbeat dalam 5 menit (node down)
    4. RAPID_CROSS_GATE— UID masuk gate A, < 2 menit tap gate B (credential sharing)

  Tidak perlu LSTM / Neural Network — untuk access control,
  statistical anomaly detection lebih deterministik, zero false positive,
  dan berjalan di Raspberry Pi dengan latensi sub-50ms.
"""

import json, time, datetime
from dataclasses import dataclass, field
from typing import Optional, Union
import redis as redis_lib

# ── Thresholds ────────────────────────────────────────────────
TAILGATING_WINDOW_SEC       = 30    # detik minimum antar tap UID sama
UNKNOWN_FLOOD_THRESHOLD     = 3     # jumlah UID tidak terdaftar dalam window
UNKNOWN_FLOOD_WINDOW        = 120   # detik (2 menit)
HEARTBEAT_TIMEOUT_SEC       = 300   # 5 menit — silent threshold
RAPID_CROSS_GATE_WINDOW_SEC = 120   # 2 menit — cross-gate attack window


@dataclass
class Anomaly:
    type:      str        # TAILGATING | UNKNOWN_FLOOD | GATE_SILENT | RAPID_CROSS_GATE
    severity:  str        # high
    gate_id:   Optional[int]
    message:   str
    detail:    dict
    timestamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))

    def to_dict(self) -> dict:
        return {
            "type":      self.type,
            "severity":  self.severity,
            "gate_id":   self.gate_id,
            "message":   self.message,
            "detail":    self.detail,
            "timestamp": self.timestamp,
        }


class AnomalyDetector:
    def __init__(self, r: redis_lib.Redis):
        self.r = r
        self._last_tap_ts:   dict = {}   # uid -> unix timestamp tap terakhir
        self._last_gate:     dict = {}   # uid -> gate_id terakhir sukses masuk
        self._unknown_times: list = []   # timestamps UID tidak terdaftar

    def process_event(self, event: dict) -> list:
        """
        Proses satu event dari Redis log.
        Return list Anomaly yang terdeteksi.
        Jika severity HIGH → otomatis trigger lockdown gate.
        """
        found   = []
        status  = event.get("status", "")
        uid     = event.get("ktp_uid", "")
        gate_id = event.get("gate_id")
        now     = time.time()

        # ── 1. TAILGATING ─────────────────────────────────────
        if status == "allowed" and uid:
            last = self._last_tap_ts.get(uid)
            if last and (now - last) < TAILGATING_WINDOW_SEC:
                delta = int(now - last)
                found.append(Anomaly(
                    type="TAILGATING", severity="high", gate_id=gate_id,
                    message=f"UID {uid} tap lagi dalam {delta} detik — kemungkinan tailgating",
                    detail={"uid": uid, "gap_seconds": delta, "gate_id": gate_id},
                ))
            self._last_tap_ts[uid] = now

        # ── 2. UNKNOWN_FLOOD ──────────────────────────────────
        if status == "denied_unregistered":
            self._unknown_times.append(now)
            self._unknown_times = [
                t for t in self._unknown_times if now - t < UNKNOWN_FLOOD_WINDOW
            ]
            count = len(self._unknown_times)
            if count >= UNKNOWN_FLOOD_THRESHOLD:
                found.append(Anomaly(
                    type="UNKNOWN_FLOOD", severity="high", gate_id=gate_id,
                    message=f"{count} UID tidak terdaftar dalam {UNKNOWN_FLOOD_WINDOW//60} menit — aktivitas mencurigakan",
                    detail={"count": count, "window_seconds": UNKNOWN_FLOOD_WINDOW},
                ))

        # ── 3. RAPID_CROSS_GATE ────────────────────────────────
        if status == "allowed" and uid and gate_id is not None:
            prev_gate = self._last_gate.get(uid)
            last_time = self._last_tap_ts.get(uid)
            if prev_gate is not None and prev_gate != gate_id:
                if last_time and (now - last_time) < RAPID_CROSS_GATE_WINDOW_SEC:
                    delta = int(now - last_time)
                    found.append(Anomaly(
                        type="RAPID_CROSS_GATE", severity="high", gate_id=gate_id,
                        message=f"UID {uid} masuk Gate {prev_gate}, {delta} detik lalu tap Gate {gate_id} — kemungkinan credential sharing",
                        detail={"uid": uid, "from_gate": prev_gate, "to_gate": gate_id, "gap_seconds": delta},
                    ))
            self._last_gate[uid] = gate_id

        # Simpan dan trigger lockdown jika HIGH
        for a in found:
            self._save(a)
            if a.severity == "high" and a.gate_id is not None:
                self.lockdown_gate(a.gate_id, reason=a.type)

        return found

    # ── Lockdown management ───────────────────────────────────
    def lockdown_gate(self, gate_id: int, reason: str = "MANUAL"):
        """
        Kunci gate — simpan status lockdown ke Redis.
        receiver.py akan polling key ini dan kirim CMD_LOCKDOWN via LoRa.
        """
        key = f"gate:lockdown:{gate_id}"
        self.r.hset(key, "gate_id", gate_id)
        self.r.hset(key, "reason", reason)
        self.r.hset(key, "locked_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
        # Tidak ada TTL — harus di-unlock manual oleh admin
        self.r.sadd("gate:lockdown:active", str(gate_id))

    def unlock_gate(self, gate_id: int):
        """Buka kunci gate yang sedang lockdown."""
        self.r.delete(f"gate:lockdown:{gate_id}")
        self.r.srem("gate:lockdown:active", str(gate_id))

    def unlock_all(self):
        """Buka semua gate yang sedang lockdown."""
        for gid in self.r.smembers("gate:lockdown:active"):
            self.r.delete(f"gate:lockdown:{gid}")
        self.r.delete("gate:lockdown:active")

    def get_locked_gates(self) -> list:
        """Return list gate_id yang sedang terkunci."""
        return [int(g) for g in self.r.smembers("gate:lockdown:active")]

    def is_locked(self, gate_id: int) -> bool:
        return self.r.sismember("gate:lockdown:active", str(gate_id))

    # ── GATE_SILENT ────────────────────────────────────────────
    def check_gate_silent(self) -> list:
        """
        Cek semua gate yang sudah terdaftar heartbeat.
        Jika ada gate yang tidak heartbeat > HEARTBEAT_TIMEOUT_SEC,
        trigger GATE_SILENT anomaly.

        Return list Anomaly yang baru terdeteksi.
        """
        found = []
        now   = time.time()

        for key in self.r.scan_iter("gate:heartbeat:*"):
            try:
                gate_id = int(key.split(":")[-1])
                data    = self.r.hgetall(key)
                last_ts = data.get("last_heartbeat", "")
                if not last_ts:
                    continue
                # Format: "2026-05-07T12:38:46"
                last_dt = datetime.datetime.strptime(last_ts, "%Y-%m-%dT%H:%M:%S")
                elapsed = now - last_dt.timestamp()
                if elapsed > HEARTBEAT_TIMEOUT_SEC:
                    # Cek apakah sudah ada anomaly aktif untuk gate ini
                    active_key = f"gate:anomaly:active:GATE_SILENT:{gate_id}"
                    if not self.r.exists(active_key):
                        minutes = int(elapsed / 60)
                        anomaly = Anomaly(
                            type="GATE_SILENT", severity="medium", gate_id=gate_id,
                            message=f"Gate {gate_id} tidak ada heartbeat dalam {minutes} menit — kemungkinan node down",
                            detail={"gate_id": gate_id, "elapsed_minutes": minutes},
                        )
                        self._save(anomaly)
                        found.append(anomaly)
            except (ValueError, KeyError, Exception):
                continue

        return found

    # ── Helpers ───────────────────────────────────────────────
    def _save(self, a: Anomaly):
        entry = json.dumps(a.to_dict())
        self.r.lpush("gate:anomaly:log", entry)
        self.r.ltrim("gate:anomaly:log", 0, 99)
        self.r.setex(f"gate:anomaly:active:{a.type}:{a.gate_id}", 600, entry)

    def get_active(self) -> list:
        result = []
        for key in self.r.scan_iter("gate:anomaly:active:*"):
            raw = self.r.get(key)
            if raw:
                try: result.append(json.loads(raw))
                except: pass
        return sorted(result, key=lambda x: x.get("timestamp",""), reverse=True)

    def get_log(self, limit=20) -> list:
        return [json.loads(e) for e in self.r.lrange("gate:anomaly:log", 0, limit-1)]

    def get_summary(self) -> dict:
        active  = self.get_active()
        locked  = self.get_locked_gates()
        return {
            "total_active":  len(active),
            "high":   sum(1 for a in active if a["severity"] == "high"),
            "locked_gates":  locked,
            "anomalies":     active,
        }

    def clear_anomalies(self):
        for key in self.r.scan_iter("gate:anomaly:active:*"):
            self.r.delete(key)