"""
predictor.py v2 — TheGate AI Density Forecaster

Metode:
  SMA (Simple Moving Average) dengan adaptive window.
  — Jika data >= 3 menit: SMA window=3
  — Jika data 1-2 menit: simple average (confidence LOW)
  — Jika 0 data: cold start — deteksi gate dari heartbeat/stats, return status "cold"

  Untuk production: cold start adalah normal (event dimulai, belum ada tap).
  Prediktor tetap return struktur gate list, dashboard render tanpa error.
  Confidence tag membantu operator menilai akurasi (LOW saat warming, HIGH setelah stabil).
"""

import json, time
from collections import defaultdict
from typing import Optional


class DensityPredictor:
    """
    DensityPredictor — prediksi kepadatan gate berbasis SMA.

    Data source: Redis `gate:log` (list of JSON event entries).
    Fallback source: Redis `gate:heartbeat:*` dan `gate:stats:*` (untuk cold start).

    Adaptive window:
      3+ menit data → SMA window=3 (confidence HIGH)
      1-2 menit      → simple average (confidence LOW)
      0 menit         → cold start, predicted_tpm=0 (confidence COLD)
    """

    SMA_WINDOW        = 3    # adaptive — window untuk data >= 3 menit
    GATE_CAPACITY     = 20   # tap/menit threshold gate penuh (skala demo)
    PREDICT_MINUTES   = 30   # prediksi horizon

    def __init__(self, redis_client):
        self.r = redis_client

    def _discover_gates(self) -> list[int]:
        """
        Cari semua gate yang terdaftar — dari heartbeat atau stats.
        Fallback saat gate:log masih kosong (cold start).
        """
        gate_ids = set()
        for key in self.r.scan_iter("gate:heartbeat:*"):
            try:
                gate_ids.add(int(key.split(":")[-1]))
            except (ValueError, IndexError):
                pass
        for key in self.r.scan_iter("gate:stats:*"):
            try:
                gate_ids.add(int(key.split(":")[-1]))
            except (ValueError, IndexError):
                pass
        return sorted(gate_ids)

    def predict_all(self) -> list[dict]:
        """
        Prediksi untuk semua gate.
        Cold start handling: jika gate:log kosong, discover gate dari heartbeat.
        Return: list of dict per gate.
        """
        raw = self._get_raw_data()
        if not raw:
            # Cold start — return semua gate yang dikenal dengan 0 prediction
            gates = self._discover_gates()
            if not gates:
                return []
            return [self._predict_cold(gid) for gid in gates]

        per_gate = self._aggregate_per_gate(raw)

        # Pastikan gate yang ada di heartbeat/stats muncul meski tanpa data tap
        discovered = self._discover_gates()
        for gid in discovered:
            if gid not in per_gate:
                per_gate[gid] = {}

        return [self._predict_gate(gid, data) for gid, data in per_gate.items()]

    def predict(self, gate_id: int) -> Optional[dict]:
        """Prediksi untuk satu gate tertentu."""
        raw = self._get_raw_data()
        if not raw:
            return self._predict_cold(gate_id)
        filtered = [e for e in raw if e.get("gate_id") == gate_id]
        per_gate = self._aggregate_per_gate(filtered)
        data = per_gate.get(gate_id)
        if not data:
            return self._predict_cold(gate_id)
        return self._predict_gate(gate_id, data)

    def get_forecast(self) -> dict:
        """
        Return ringkasan prediksi untuk dashboard.
        Format: {
          "timestamp": "...",
          "status": "ready" | "warming" | "cold",
          "gates": { ... },
          "recommendation": "..."
        }
        """
        all_predictions = self.predict_all()
        raw = self._get_raw_data()

        # Tentukan status sistem
        if not raw:
            status = "cold"
        elif len(raw) < self.SMA_WINDOW * 10:
            status = "warming"
        else:
            status = "ready"

        result = {
            "timestamp": time.strftime("%H:%M:%S"),
            "status":    status,
            "gates":     {},
            "recommendation": None,
        }

        for p in all_predictions:
            gid = f"gate_{p['gate_id']}"
            result["gates"][gid] = {
                "current_tpm":    p["current_tpm"],
                "predicted_tpm":  p["predicted_tpm"],
                "predicted_rank": p["predicted_rank"],
                "severity":       p["severity"],
                "message":        p["message"],
                "confidence":     p["confidence"],
                "status":         p["status"],
            }

        # Rekomendasi distribusi (hanya saat ready)
        ranked = sorted(all_predictions, key=lambda x: x["predicted_tpm"], reverse=True)
        if len(ranked) >= 2 and status != "cold":
            busiest  = ranked[0]
            quietest = ranked[-1]
            threshold_pct = 0.6 if status == "ready" else 0.8
            if busiest["predicted_tpm"] > self.GATE_CAPACITY * threshold_pct:
                result["recommendation"] = (
                    f"Prediksi 30 menit: Gate {busiest['gate_id']} berpotensi "
                    f"{busiest['predicted_tpm']} tap/menit. Arahkan peserta ke "
                    f"Gate {quietest['gate_id']} (~{quietest['predicted_tpm']} tap/menit)."
                )

        return result

    # ── Internal ────────────────────────────────────────────────

    def _get_raw_data(self) -> list[dict]:
        """Ambil 500 event terakhir dari gate:log."""
        try:
            raw = self.r.lrange("gate:log", 0, 499)
            return [json.loads(e) for e in raw]
        except Exception:
            return []

    def _aggregate_per_gate(self, entries: list) -> dict:
        """
        Agregasi per gate per menit.
        Return: { gate_id: { "minute": {"allowed":5, "denied":2}, ... } }
        """
        per_gate = defaultdict(lambda: defaultdict(lambda: {"allowed": 0, "denied": 0}))

        for e in entries:
            ts = e.get("server_timestamp") or e.get("timestamp", "")
            if not ts or "T" not in ts:
                continue
            status = e.get("status", "")
            gate_id = e.get("gate_id")
            if gate_id is None:
                continue

            try:
                minute = ts.split("T")[1][:5]
            except (IndexError, AttributeError):
                continue

            key = "allowed" if status == "allowed" else "denied"
            per_gate[gate_id][minute][key] += 1

        return dict(per_gate)

    def _predict_cold(self, gate_id: int) -> dict:
        """Cold start — belum ada data tap sama sekali."""
        return {
            "gate_id":        gate_id,
            "current_tpm":    0,
            "predicted_tpm":  0,
            "predicted_rank": 0,
            "severity":       "low",
            "confidence":     "cold",
            "status":         "cold",
            "message":        f"Gate {gate_id}: mengumpulkan data...",
        }

    def _sma(self, series: list[int]) -> tuple:
        """
        Adaptive SMA: pilih window berdasarkan jumlah data.
        Return: (predicted_tpm, confidence, sample_count)
        """
        n = len(series)
        if n == 0:
            return 0, "cold", 0
        if n >= self.SMA_WINDOW:
            window = self.SMA_WINDOW
            pred = int(sum(series[-window:]) / window)
            return pred, "high", n
        if n >= 2:
            pred = int(sum(series) / n)
            return pred, "low", n
        # n == 1
        return series[0], "low", 1

    def _predict_gate(self, gate_id: int, minute_data: dict) -> dict:
        """
        SMA prediction untuk satu gate.
        Adaptive window + confidence tag.
        """
        sorted_minutes = sorted(minute_data.keys())
        tpm_series = []
        for m in sorted_minutes:
            d = minute_data[m]
            tpm_series.append(d["allowed"] + d["denied"])

        current_tpm = tpm_series[-1] if tpm_series else 0
        predicted_tpm, confidence, n = self._sma(tpm_series)

        if confidence == "cold":
            return self._predict_cold(gate_id)

        # Severity dari predicted_tpm
        if predicted_tpm >= int(self.GATE_CAPACITY * 0.7):
            severity = "high"
        elif predicted_tpm >= int(self.GATE_CAPACITY * 0.4):
            severity = "medium"
        else:
            severity = "low"

        # Status: warming jika confidence low dan data masih sedikit
        forecast_status = "ready" if confidence == "high" else "warming"

        messages = {
            "high":   f"Gate {gate_id}: {predicted_tpm} tpm — perlu distribusi ulang",
            "medium": f"Gate {gate_id}: {predicted_tpm} tpm — pantau terus",
            "low":    f"Gate {gate_id}: {predicted_tpm} tpm — stabil",
        }

        # Rank
        all_data = self._aggregate_per_gate(self._get_raw_data())
        all_predicted = []
        for gid, data in all_data.items():
            series = []
            for m in sorted(data.keys()):
                d = data[m]
                series.append(d["allowed"] + d["denied"])
            ap, _, _ = self._sma(series)
            all_predicted.append((gid, ap))

        all_predicted.sort(key=lambda x: x[1], reverse=True)
        rank = 1
        for i, (g, _) in enumerate(all_predicted):
            if g == gate_id:
                rank = i + 1
                break

        return {
            "gate_id":        gate_id,
            "current_tpm":    current_tpm,
            "predicted_tpm":  predicted_tpm,
            "predicted_rank": rank,
            "severity":       severity,
            "confidence":     confidence,  # "high" | "low" | "cold"
            "status":         forecast_status,  # "ready" | "warming" | "cold"
            "message":        messages[severity],
        }
