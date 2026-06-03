"""
dashboard.py v3 — Fix status label + chart per gate + timestamp ms
"""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, File, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import redis, json, asyncio, time, os, sys, csv, io
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "receiver"))
from anomaly import AnomalyDetector
from predictor import DensityPredictor

import os

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASS = os.getenv("REDIS_PASS", "")

app = FastAPI(title="Sat Set Tap Dashboard")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASS, decode_responses=True)
detector = AnomalyDetector(r)
predictor = DensityPredictor(r)


class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(data)
            except:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

manager = ConnectionManager()
last_log_len = 0


async def poll_redis():
    global last_log_len
    tick = 0
    while True:
        try:
            current_len = r.llen("gate:log")
            if current_len != last_log_len:
                new_count = max(0, current_len - last_log_len)
                if new_count > 0:
                    new_entries = r.lrange("gate:log", 0, new_count - 1)
                    for entry in reversed(new_entries):
                        parsed = json.loads(entry)
                        await manager.broadcast({"type": "new_event", "data": parsed})
                        # Proses anomali dari setiap event baru
                        anomalies = detector.process_event(parsed)
                        for a in anomalies:
                            await manager.broadcast({"type": "anomaly", "data": a.to_dict()})
                last_log_len = current_len

            await manager.broadcast({"type": "stats",     "data": get_stats_data()})
            if tick % 5 == 0:
                await manager.broadcast({"type": "analytics",  "data": get_analytics_data()})
                await manager.broadcast({"type": "loadbalance", "data": get_load_balance()})
                await manager.broadcast({"type": "forecast",   "data": predictor.get_forecast()})
                await manager.broadcast({"type": "network",    "data": get_network_data()})
            await manager.broadcast({"type": "anomaly_summary", "data": detector.get_summary()})
            # Cek gate silent tiap 30 detik
            if tick % 30 == 0:
                silent = detector.check_gate_silent()
                for a in silent:
                    await manager.broadcast({"type": "anomaly", "data": a.to_dict()})
            tick += 1
        except:
            pass
        await asyncio.sleep(1)


@app.on_event("startup")
async def startup():
    asyncio.create_task(poll_redis())


def get_stats_data() -> dict:
    total_reg   = r.scard("gate:registered:all") or 0
    total_entry = 0
    for uid in r.smembers("gate:registered:all"):
        data = r.hgetall(f"gate:registered:{uid}")
        if data and data.get("last_entry", "-") != "-":
            total_entry += 1

    gate_stats = {}
    for key in r.scan_iter("gate:stats:*"):
        gid  = key.split(":")[-1]
        data = r.hgetall(key)
        gate_stats[f"gate_{gid}"] = {
            "total_tap": int(data.get("total_tap", 0)),
            "last_seen": data.get("last_seen", "-"),
        }
    return {
        "total_registered":     total_reg,
        "total_entry":          total_entry,
        "total_unique_entries": total_entry,
        "total_tap":            sum(g["total_tap"] for g in gate_stats.values()),
        "gates":                gate_stats,
        "timestamp":            time.strftime("%H:%M:%S"),
    }


def get_analytics_data() -> dict:
    entries    = r.lrange("gate:log", 0, 999)
    # per_minute[gate_id][minute] = {allowed, denied}
    per_all    = defaultdict(lambda: {"allowed": 0, "denied": 0})
    per_gate   = defaultdict(lambda: defaultdict(lambda: {"allowed": 0, "denied": 0}))

    for raw in entries:
        try:
            e   = json.loads(raw)
            ts  = e.get("server_timestamp") or e.get("timestamp", "")
            if not ts or "T" not in ts: continue
            minute  = ts[:16].replace("T", " ")
            status  = e.get("status", "")
            gate_id = str(e.get("gate_id", "all"))
            allowed = status == "allowed"

            key = "allowed" if allowed else "denied"
            per_all[minute][key]           += 1
            per_gate[gate_id][minute][key] += 1
        except:
            continue

    sorted_minutes = sorted(per_all.keys())[-30:]

    # Data per gate
    gates_data = {}
    for gid, minutes in per_gate.items():
        gates_data[f"gate_{gid}"] = {
            "allowed": [minutes.get(m, {}).get("allowed", 0) for m in sorted_minutes],
            "denied":  [minutes.get(m, {}).get("denied",  0) for m in sorted_minutes],
        }

    return {
        "labels":      sorted_minutes,
        "all_allowed": [per_all[m]["allowed"] for m in sorted_minutes],
        "all_denied":  [per_all[m]["denied"]  for m in sorted_minutes],
        "gates":       gates_data,
    }


def get_load_balance() -> dict:
    gate_stats = {}
    for key in r.scan_iter("gate:stats:*"):
        gid  = key.split(":")[-1]
        data = r.hgetall(key)
        gate_stats[gid] = int(data.get("total_tap", 0))

    if not gate_stats:
        return {"status": "no_data", "suggestion": None, "gates": {}}

    total    = sum(gate_stats.values()) or 1
    busiest  = max(gate_stats, key=gate_stats.get)
    quietest = min(gate_stats, key=gate_stats.get)
    busy_pct = (gate_stats[busiest] / total) * 100
    quiet_pct= (gate_stats[quietest]/ total) * 100

    suggestion = None
    if len(gate_stats) > 1 and (busy_pct - quiet_pct) > 40:
        suggestion = {
            "from_gate": busiest,
            "to_gate":   quietest,
            "message":   f"Gate {busiest} terlalu ramai ({gate_stats[busiest]} tap). "
                         f"Arahkan ke Gate {quietest} ({gate_stats[quietest]} tap).",
            "severity":  "high" if (busy_pct - quiet_pct) > 60 else "medium",
        }
    return {"status":"ok","suggestion":suggestion,"gates":gate_stats,"busiest":busiest,"quietest":quietest}


def get_network_data() -> dict:
    """Return latency dan RSSI stats per gate."""
    gate_ids = set()
    for key in r.scan_iter("gate:latency:*"):
        gid = key.split(":")[-1]
        gate_ids.add(int(gid))

    result = {}
    for gid in sorted(gate_ids):
        latencies = r.lrange(f"gate:latency:{gid}", 0, -1)
        nums = [int(x) for x in latencies]
        # RSSI dari event log (last 10 per gate)
        rssi_values = []
        for raw in r.lrange("gate:log", 0, 199):
            try:
                e = json.loads(raw)
                if e.get("gate_id") == gid and "rssi" in e and e["rssi"] != 0:
                    rssi_values.append(e["rssi"])
            except:
                pass
        rssi_values = rssi_values[-20:]

        result[f"gate_{gid}"] = {
            "latency": {
                "current": nums[-1] if nums else 0,
                "min":     min(nums) if nums else 0,
                "max":     max(nums) if nums else 0,
                "avg":     round(sum(nums)/len(nums)) if nums else 0,
                "samples": len(nums),
                "history": nums[-20:],
            },
            "rssi": {
                "current": rssi_values[-1] if rssi_values else 0,
                "avg":     round(sum(rssi_values)/len(rssi_values)) if rssi_values else 0,
                "min":     min(rssi_values) if rssi_values else 0,
                "history": rssi_values[-20:],
            },
        }
    return result


@app.get("/api/stats")       
def api_stats():       
    return get_stats_data()

@app.get("/api/log")         
def api_log(limit: int = 100):
    return [json.loads(e) for e in r.lrange("gate:log", 0, limit - 1)]

@app.get("/api/analytics")   
def api_analytics():   
    return get_analytics_data()

@app.get("/api/loadbalance") 
def api_lb():          
    return get_load_balance()

@app.get("/api/forecast")
def api_forecast():
    return predictor.get_forecast()

@app.get("/api/forecast/{gate_id}")
def api_forecast_gate(gate_id: int):
    return predictor.predict(gate_id)

@app.get("/api/network")
def api_network():
    return get_network_data()

@app.get("/api/anomaly")
def api_anomaly():
    return detector.get_summary()

@app.get("/api/anomaly/log")
def api_anomaly_log(limit: int = 50):
    return detector.get_log(limit)

@app.post("/api/lockdown/{gate_id}")
def api_lockdown(gate_id: int):
    detector.lockdown_gate(gate_id, reason="MANUAL")
    return {"status": "locked", "gate_id": gate_id}

@app.post("/api/unlock/{gate_id}")
def api_unlock(gate_id: int):
    detector.unlock_gate(gate_id)
    return {"status": "unlocked", "gate_id": gate_id}

@app.post("/api/unlock/all")
def api_unlock_all():
    detector.unlock_all()
    return {"status": "all_unlocked"}

@app.delete("/api/anomaly/clear")
def api_anomaly_clear():
    """Clear semua anomali aktif."""
    for key in r.scan_iter("gate:anomaly:active:*"):
        r.delete(key)
    return {"status": "cleared"}

@app.delete("/api/flush")
def api_flush():
    keys = list(r.scan_iter("gate:*"))
    if keys: r.delete(*keys)
    return {"status": "flushed", "deleted": len(keys)}

@app.delete("/api/flush-event")
def api_flush_event():
    """Flush event saja, registrasi tetap."""
    for key in ["gate:log", "gate:uid:seen"]:
        r.delete(key)
    for key in list(r.scan_iter("gate:stats:*")) + list(r.scan_iter("gate:event:*")):
        r.delete(key)
    return {"status": "event flushed"}


@app.get("/api/log/export")
def api_log_export():
    """Export event log ke CSV."""
    entries = [json.loads(e) for e in r.lrange("gate:log", 0, -1)]
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["timestamp", "gate_id", "ktp_uid", "status", "name", "token_left", "token_max", "crc_valid", "rssi"])
    for e in reversed(entries):
        writer.writerow([
            e.get("server_timestamp") or e.get("timestamp", ""),
            e.get("gate_id", ""),
            e.get("ktp_uid", ""),
            e.get("status", ""),
            e.get("name", ""),
            e.get("token_left", ""),
            e.get("token_max", ""),
            e.get("crc_valid", ""),
            e.get("rssi", ""),
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=gate_log.csv"},
    )


@app.post("/api/register/import")
async def api_import_uids(upload: UploadFile = File(...)):
    """Import massal UID dari file CSV (kolom: uid, name, token_max)."""
    if not upload.filename or not upload.filename.endswith(".csv"):
        raise HTTPException(400, "Hanya file CSV yang didukung")
    content = await upload.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise HTTPException(400, "File CSV kosong atau header tidak ditemukan")
    if "uid" not in rows[0]:
        raise HTTPException(400, "Kolom 'uid' wajib ada di header CSV")

    from receiver.redis_handler import GateRedis
    db = GateRedis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASS)
    result = db.import_uids(rows)
    result["filename"] = upload.filename
    return result


@app.get("/api/register")
def api_register_list(limit: int = 200):
    """List semua UID terdaftar."""
    uids = list(r.smembers("gate:registered:all"))[:limit]
    result = []
    for uid in uids:
        data = r.hgetall(f"gate:registered:{uid}")
        if data:
            data["uid"] = uid
            result.append(data)
    return sorted(result, key=lambda x: x.get("registered_at", ""), reverse=True)


@app.delete("/api/register/{uid}")
def api_revoke_uid(uid: str):
    """Hapus satu UID dari registrasi."""
    uid = uid.upper()
    if len(uid) != 8 or not all(c in "0123456789ABCDEF" for c in uid):
        raise HTTPException(400, "Format UID tidak valid (8 char hex)")
    key = f"gate:registered:{uid}"
    if not r.hexists(key, "uid"):
        raise HTTPException(404, "UID tidak ditemukan")
    r.delete(key)
    r.delete(f"gate:event:{uid}")
    r.srem("gate:registered:all", uid)
    return {"status": "revoked", "uid": uid}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await manager.connect(ws)
    await ws.send_json({"type": "stats",       "data": get_stats_data()})
    await ws.send_json({"type": "analytics",   "data": get_analytics_data()})
    await ws.send_json({"type": "loadbalance", "data": get_load_balance()})
    await ws.send_json({"type": "history",        "data": api_log(50)})
    await ws.send_json({"type": "anomaly_summary", "data": detector.get_summary()})
    await ws.send_json({"type": "forecast", "data": predictor.get_forecast()})
    await ws.send_json({"type": "network",  "data": get_network_data()})
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTML


HTML = r"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sat Set Tap — Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Syne:wght@400;700;800&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root{--bg:#0f0f17;--surface:#1a1a24;--border:#2a2a3c;--accent:#40a02b;--warn:#fe640b;--info:#04a5e5;--deny:#d20f39;--muted:#585b70;--text:#cdd6f4;--dim:#6c6f85;--chart-a-bg:rgba(64,160,43,.15);--chart-a-brd:#40a02b;--chart-d-bg:rgba(210,15,57,.12);--chart-d-brd:#d20f39;--chart-leg:#6c6f85;--chart-tk:#6c6f85;--chart-gd:#2a2a3c;--chart-tlbg:#1a1a24;--chart-tlt:#cdd6f4;--chart-tlb:#6c6f85;--anomaly-bg:rgba(210,15,57,.08);--badge-abg:rgba(64,160,43,.15);--badge-dbg:rgba(210,15,57,.15);--badge-wbg:rgba(254,100,11,.15);--alert-bg:rgba(254,100,11,.12);--alert-ibg:rgba(4,165,229,.08);--tog-abg:rgba(64,160,43,.08);--td-bdr:rgba(42,42,60,.5);--tr-hbg:rgba(255,255,255,.02);--flash-bg:rgba(64,160,43,.1);--ah-bg:rgba(210,15,57,.18);--am-bg:rgba(254,100,11,.18);--ai-bdr:rgba(42,42,60,.5)}
:root.light{--bg:#e6e9ef;--surface:#ffffff;--border:#ccd0da;--accent:#1d6f09;--warn:#fe640b;--info:#04a5e5;--deny:#a3001f;--muted:#787b90;--text:#4c4f69;--dim:#5c5f79;--chart-a-bg:rgba(29,111,9,.12);--chart-a-brd:#1d6f09;--chart-d-bg:rgba(163,0,31,.1);--chart-d-brd:#a3001f;--chart-leg:#5c5f79;--chart-tk:#5c5f79;--chart-gd:#ccd0da;--chart-tlbg:#f5f6fa;--chart-tlt:#4c4f69;--chart-tlb:#5c5f79;--anomaly-bg:rgba(163,0,31,.06);--badge-abg:rgba(29,111,9,.12);--badge-dbg:rgba(163,0,31,.1);--badge-wbg:rgba(254,100,11,.1);--alert-bg:rgba(254,100,11,.08);--alert-ibg:rgba(4,165,229,.06);--tog-abg:rgba(29,111,9,.08);--td-bdr:rgba(204,208,218,.7);--tr-hbg:rgba(0,0,0,.02);--flash-bg:rgba(29,111,9,.08);--ah-bg:rgba(163,0,31,.12);--am-bg:rgba(254,100,11,.12);--ai-bdr:rgba(204,208,218,.7)}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:'Space Mono',monospace;min-height:100vh;transition:background .3s,color .3s}
body::before{content:'';position:fixed;inset:0;background-image:linear-gradient(var(--border) 1px,transparent 1px),linear-gradient(90deg,var(--border) 1px,transparent 1px);background-size:40px 40px;opacity:.3;pointer-events:none;z-index:0}
:root.light body::before{opacity:.12}
.wrap{position:relative;z-index:1;max-width:1300px;margin:0 auto;padding:2rem}
header{display:flex;align-items:center;justify-content:space-between;margin-bottom:2.5rem;padding-bottom:1.5rem;border-bottom:1px solid var(--border)}
.logo{font-family:'Syne',sans-serif;font-size:1.8rem;font-weight:800;letter-spacing:-.02em}
.logo span{color:var(--accent)}
.live-pill{display:flex;align-items:center;gap:.5rem;font-size:.7rem;color:var(--dim)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}
.dot.on{background:var(--accent);box-shadow:0 0 8px var(--accent);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:1rem;margin-bottom:1.5rem}
.card{background:var(--surface);border:1px solid var(--border);padding:1.5rem;position:relative;overflow:hidden;transition:background .3s,border-color .3s,box-shadow .3s}
:root.light .card,:root.light .chart-section,:root.light .gate-card,:root.light .anomaly-panel,:root.light .ticker-section{box-shadow:0 1px 4px rgba(0,0,0,.06)}
.card::after{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:var(--accent);transform:scaleX(0);transform-origin:left;transition:transform .4s}
.card.pop::after{transform:scaleX(1)}
.card-label{font-size:.6rem;color:var(--dim);letter-spacing:.15em;text-transform:uppercase;margin-bottom:.75rem}
.card-val{font-family:'Syne',sans-serif;font-size:2rem;font-weight:600;color:var(--accent);line-height:1}
.card-sub{font-size:.65rem;color:var(--dim);margin-top:.4rem}
.alert{display:none;background:var(--alert-bg);border:1px solid var(--warn);padding:1rem 1.25rem;margin-bottom:1.5rem;font-size:.8rem}
.alert.show{display:block}
.alert-title{color:var(--warn);font-weight:700;margin-bottom:.25rem;font-size:.7rem;letter-spacing:.1em;text-transform:uppercase}
.alert.medium{background:var(--alert-ibg);border-color:var(--info)}
.alert.medium .alert-title{color:var(--info)}
.section-title{font-size:.6rem;color:var(--dim);letter-spacing:.15em;text-transform:uppercase;margin-bottom:.75rem}
.gates{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:.75rem;margin-bottom:1.5rem}
.gate-card{background:var(--surface);border:1px solid var(--border);padding:1rem;transition:background .3s,border-color .3s}
.gate-name{font-size:.65rem;color:var(--dim);letter-spacing:.1em;text-transform:uppercase}
.gate-num{font-family:'Syne',sans-serif;font-size:2rem;font-weight:600;color:var(--text);margin:.2rem 0}
.gate-bar{height:3px;background:var(--border);margin:.5rem 0}
.gate-bar-fill{height:100%;background:var(--accent);transition:width .5s}
.gate-time{font-size:.6rem;color:var(--muted)}
.chart-section{background:var(--surface);border:1px solid var(--border);padding:1.5rem;margin-bottom:1.25rem;transition:background .3s,border-color .3s}
.chart-controls{display:flex;gap:.5rem;margin-bottom:1rem;flex-wrap:wrap}
.tog{padding:.3rem .7rem;font-family:'Space Mono',monospace;font-size:.6rem;letter-spacing:.08em;text-transform:uppercase;cursor:pointer;border:1px solid var(--border);background:transparent;color:var(--dim);transition:all .2s}
.tog.active{border-color:var(--accent);color:var(--accent);background:var(--tog-abg)}
.chart-wrap{position:relative;height:200px}
.log-header{display:flex;align-items:center;gap:.75rem;margin-bottom:1rem;flex-wrap:wrap}
.btn{padding:.45rem .9rem;font-family:'Space Mono',monospace;font-size:.65rem;letter-spacing:.08em;text-transform:uppercase;cursor:pointer;border:1px solid var(--border);background:var(--surface);color:var(--dim);transition:all .2s}
.btn:hover{border-color:var(--accent);color:var(--accent)}
.btn.danger:hover{border-color:var(--warn);color:var(--warn)}
table{width:100%;border-collapse:collapse;font-size:.72rem}
th{text-align:left;padding:.5rem .75rem;font-size:.58rem;letter-spacing:.12em;text-transform:uppercase;color:var(--dim);border-bottom:1px solid var(--border)}
td{padding:.55rem .75rem;border-bottom:1px solid var(--td-bdr)}
tr:hover td{background:var(--tr-hbg)}
.badge{display:inline-block;padding:.18rem .45rem;font-size:.58rem;letter-spacing:.06em;text-transform:uppercase;font-weight:700;white-space:nowrap}
.badge.allowed{background:var(--badge-abg);color:var(--accent)}
.badge.denied_unregistered{background:var(--badge-dbg);color:var(--deny)}
.badge.denied_token_empty{background:var(--badge-wbg);color:var(--warn)}
.uid{color:var(--dim);font-family:'Space Mono',monospace;letter-spacing:.04em}
.new-row{animation:flash 1.2s ease-out}
@keyframes flash{0%{background:var(--flash-bg)}100%{background:transparent}}
.ms{color:var(--muted);font-size:.65em}
.anomaly-panel{background:var(--surface);border:1px solid var(--border);margin-bottom:1.25rem;transition:background .3s,border-color .3s}
.anomaly-header{display:flex;align-items:center;justify-content:space-between;padding:1rem 1.25rem;border-bottom:1px solid var(--border)}
.anomaly-list{max-height:280px;overflow-y:auto}
.anomaly-item{padding:.75rem 1.25rem;border-bottom:1px solid var(--ai-bdr);display:flex;gap:.75rem;align-items:flex-start}
.anomaly-item:last-child{border:none}
.anomaly-badge{font-size:.55rem;letter-spacing:.08em;text-transform:uppercase;font-weight:700;padding:.2rem .5rem;white-space:nowrap;margin-top:.1rem}
.anomaly-badge.high{background:var(--ah-bg);color:var(--deny)}
.anomaly-badge.medium{background:var(--am-bg);color:var(--warn)}
.anomaly-msg{font-size:.72rem;color:var(--text);line-height:1.4}
.anomaly-ts{font-size:.6rem;color:var(--muted);margin-top:.2rem}
.no-anomaly{padding:2rem;text-align:center;color:var(--muted);font-size:.75rem}
.pulse-red{animation:pulseRed 2s infinite}
@keyframes pulseRed{0%,100%{box-shadow:0 0 0 0 rgba(210,15,57,.4)}50%{box-shadow:0 0 0 6px rgba(210,15,57,0)}}
.theme-btn{background:none;border:1px solid var(--border);color:var(--dim);cursor:pointer;padding:.35rem .5rem;font-size:1rem;line-height:1;border-radius:4px;transition:all .3s}
.theme-btn:hover{border-color:var(--accent);color:var(--accent)}
.divider{border:none;border-top:1px dashed var(--border);opacity:.3;margin:0 0 1.5rem 0}
.ticker-section{background:var(--surface);border:1px solid var(--border);padding:.45rem 1rem;margin-bottom:1.5rem;transition:background .3s,border-color .3s,box-shadow .3s}
.ticker-entries{overflow-x:auto;white-space:nowrap;font-family:'Space Mono',monospace;font-size:.65rem;color:var(--dim)}
.ticker-item{display:inline-block}
.ticker-item+.ticker-item::before{content:'|';color:var(--muted);margin:0 1rem}
.ticker-ts{color:var(--muted)}
.ticker-gate{color:var(--dim)}
.ticker-status{font-weight:700;letter-spacing:.06em}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo">THE<span>GATE</span></div>
    <div style="display:flex;align-items:center;gap:.75rem">
      <div style="background:rgba(254,100,11,.12);border:1px solid var(--warn);padding:.2rem .5rem;font-size:.55rem;letter-spacing:.08em;text-transform:uppercase;color:var(--warn)">AI Engine</div>
      <button class="theme-btn" id="theme-btn" onclick="toggleTheme()" title="Toggle theme">☀️</button>
      <div class="live-pill"><div class="dot" id="dot"></div><span id="ws-lbl">connecting...</span></div>
    </div>
  </header>

  <div class="stats">
    <div class="card" id="c-reg">
      <div class="card-label">Terdaftar</div>
      <div class="card-val" id="s-reg">0</div>
      <div class="card-sub">total peserta</div>
    </div>
    <div class="card" id="c-entry">
      <div class="card-label">Sudah Masuk</div>
      <div class="card-val" id="s-entry">0</div>
      <div class="card-sub">unique peserta</div>
    </div>
    <div class="card" id="c-total">
      <div class="card-label">Total Tap</div>
      <div class="card-val" id="s-total">0</div>
      <div class="card-sub">semua gate</div>
    </div>
    <div class="card" id="c-gates">
      <div class="card-label">Gate Aktif</div>
      <div class="card-val" id="s-gates">0</div>
      <div class="card-sub">dari total gate</div>
    </div>
    <div class="card">
      <div class="card-label">Last Update</div>
      <div class="card-val" id="s-time" style="font-size:1.3rem">--:--:--</div>
      <div class="card-sub">server time</div>
    </div>
  </div>

  <hr class="divider">

  <div class="alert" id="lb-alert">
    <div class="alert-title" id="lb-title">⚠ Gate Terlalu Ramai</div>
    <div class="alert-msg"  id="lb-msg"></div>
  </div>

  <hr class="divider" style="margin-top:0">

  <!-- Anomaly stat card tambahan -->
  <div id="anomaly-stat" style="display:none;background:var(--anomaly-bg);border:1px solid var(--deny);padding:1rem 1.25rem;margin-bottom:1.5rem;display:flex;align-items:center;gap:1rem">
    <div style="font-size:1.4rem" id="anomaly-icon">🔴</div>
    <div>
      <div style="font-size:.6rem;color:var(--deny);letter-spacing:.1em;text-transform:uppercase;font-weight:700">Anomali Terdeteksi</div>
      <div id="anomaly-stat-msg" style="font-size:.8rem;margin-top:.2rem"></div>
    </div>
    <button class="btn danger" onclick="clearAnomalies()" style="margin-left:auto">Clear</button>
  </div>

  <!-- Security Monitor (always visible) -->
  <div class="anomaly-panel">
    <div class="anomaly-header">
      <div class="section-title" style="margin:0">🛡 Security Monitor</div>
      <div style="display:flex;align-items:center;gap:.75rem">
        <div style="font-size:.65rem;color:var(--dim)" id="anomaly-count">0 anomali aktif</div>
        <button class="btn danger" style="font-size:.55rem;padding:.2rem .5rem" onclick="unlockAll()">Unlock Semua</button>
      </div>
    </div>
    <div class="anomaly-list" id="anomaly-list">
      <div class="no-anomaly">Tidak ada anomali — sistem aman ✓</div>
    </div>
  </div>

  <hr class="divider">

  <!-- 👤 PESERTA — Simple revoke -->
  <div class="chart-section" style="margin-bottom:1.25rem">
    <div class="section-title" style="margin-bottom:.75rem">Peserta</div>
    <div style="display:flex;gap:.75rem;align-items:center;flex-wrap:wrap">
      <input id="revoke-input" placeholder="UID (8 char hex)" style="background:var(--bg);border:1px solid var(--border);color:var(--text);padding:.5rem .75rem;font-family:'Space Mono',monospace;font-size:.72rem;width:180px;outline:none">
      <button class="btn danger" onclick="revokeUid()" id="revoke-btn">Revoke</button>
      <span id="revoke-msg" style="font-size:.65rem;color:var(--dim)"></span>
    </div>
  </div>

  <!-- ⚡ AI ENGINE (collapsible, default open) -->
  <details style="margin-bottom:1.25rem;background:var(--surface);border:1px solid var(--border);padding:1rem 1.25rem;transition:background .3s,border-color .3s" open>
    <summary style="font-size:.6rem;color:var(--dim);letter-spacing:.15em;text-transform:uppercase;cursor:pointer;font-weight:700;font-family:'Space Mono',monospace">⚡ AI Engine — Density Forecast &amp; Anomaly Detection</summary>
    <div style="margin-top:1rem;display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:.75rem" id="forecast-grid">
      <div style="color:var(--muted);font-size:.72rem">Menunggu data prediksi...</div>
    </div>
  </details>

  <!-- 📡 NETWORK STATUS (collapsible, default open) -->
  <details style="margin-bottom:1.25rem;background:var(--surface);border:1px solid var(--border);padding:1rem 1.25rem;transition:background .3s,border-color .3s" open>
    <summary style="font-size:.6rem;color:var(--dim);letter-spacing:.15em;text-transform:uppercase;cursor:pointer;font-weight:700;font-family:'Space Mono',monospace">📡 Network Status — Latency &amp; Signal</summary>
    <div style="margin-top:1rem;display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:.75rem" id="network-grid">
      <div style="color:var(--muted);font-size:.72rem">Menunggu data jaringan...</div>
    </div>
  </details>

  <hr class="divider">

  <div class="section-title">Status Per Gate</div>
  <div class="gates" id="gates-grid">
    <div class="gate-card" style="color:var(--muted);font-size:.75rem">Menunggu data...</div>
  </div>

  <div class="ticker-section" id="ticker-section">
    <div class="ticker-entries" id="ticker-entries">
      <div style="color:var(--muted)">Menunggu event...</div>
    </div>
  </div>

  <div class="chart-section">
    <div class="section-title" style="margin-bottom:.75rem">Frekuensi Tap (per menit)</div>
    <div class="chart-controls" id="gate-toggles">
      <button class="tog active" data-gate="all" onclick="setGateView('all',this)">Semua Gate</button>
    </div>
    <div class="chart-wrap"><canvas id="tapChart"></canvas></div>
  </div>

  <hr class="divider">

  <div class="log-header">
    <div class="section-title" style="margin:0">Event Log</div>
    <button class="btn" onclick="refreshLog()">Refresh</button>
    <button class="btn danger" onclick="flushEvent()">Flush Event</button>
    <button class="btn danger" onclick="flushAll()">Flush Semua</button>
  </div>
  <table>
    <thead><tr><th>Status</th><th>Gate</th><th>UID</th><th>Nama</th><th>Token</th><th>Waktu</th><th>CRC</th></tr></thead>
    <tbody id="log-body">
      <tr><td colspan="7" style="color:var(--muted);text-align:center;padding:2rem">Menunggu event...</td></tr>
    </tbody>
  </table>
</div>

<script>
// ── Chart ─────────────────────────────────────────────────────
const ctx = document.getElementById('tapChart').getContext('2d');
const tapChart = new Chart(ctx, {
  type: 'bar',
  data: { labels: [], datasets: [
    { label:'Masuk',   data:[], backgroundColor:'rgba(64,160,43,.15)', borderColor:'#40a02b', borderWidth:1 },
    { label:'Ditolak', data:[], backgroundColor:'rgba(210,15,57,.12)', borderColor:'#d20f39', borderWidth:1 },
  ]},
  options: {
    responsive:true, maintainAspectRatio:false,
    plugins:{
      legend:{labels:{color:'#6c6f85',font:{family:'Space Mono',size:11}}},
      tooltip:{backgroundColor:'#1a1a24',titleColor:'#cdd6f4',bodyColor:'#6c6f85'}
    },
    scales:{
      x:{ticks:{color:'#6c6f85',font:{size:10,family:'Space Mono'},maxRotation:45},grid:{color:'#2a2a3c'}},
      y:{ticks:{color:'#6c6f85',font:{size:10,family:'Space Mono'}},grid:{color:'#2a2a3c'},beginAtZero:true}
    }
  }
});
updateChartColors();

let analyticsData  = null;
let currentGateView = 'all';
let lockedGates     = [];
let tickerEvents    = [];
function updateTicker(){
  const c=document.getElementById('ticker-entries');
  if(!c)return;
  if(!tickerEvents.length){c.innerHTML='<span style="color:var(--muted)">Menunggu event...</span>';return;}
  c.innerHTML='<span style="color:var(--muted);margin-right:.5rem">&#10095;</span>'+tickerEvents.map(function(e){
    var ts=(e.server_timestamp||e.timestamp||'').includes('T')?(e.server_timestamp||e.timestamp||'').split('T')[1].slice(0,8):(e.server_timestamp||e.timestamp||'').slice(0,8);
    return '<span class="ticker-item"><span class="ticker-ts">'+ts+'</span> <span class="ticker-gate">G'+e.gate_id+'</span> <span class="ticker-status" style="color:'+(e.status==='allowed'?'var(--accent)':'var(--deny)')+'">'+(e.status==='allowed'?'ALLOW':'DENY')+'</span></span>';
  }).join('');
}

function setGateView(gate, btn) {
  currentGateView = gate;
  document.querySelectorAll('.tog').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  if (analyticsData) renderChart(analyticsData);
}

function renderChart(d) {
  analyticsData = d;
  tapChart.data.labels = d.labels.map(l => l.slice(-5));

  if (currentGateView === 'all') {
    tapChart.data.datasets[0].data = d.all_allowed;
    tapChart.data.datasets[1].data = d.all_denied;
    tapChart.data.datasets[0].label = 'Masuk (Semua)';
    tapChart.data.datasets[1].label = 'Ditolak (Semua)';
  } else {
    const gd = d.gates[currentGateView] || {allowed:[],denied:[]};
    tapChart.data.datasets[0].data = gd.allowed;
    tapChart.data.datasets[1].data = gd.denied;
    tapChart.data.datasets[0].label = `Masuk (${currentGateView.replace('_',' ')})`;
    tapChart.data.datasets[1].label = `Ditolak (${currentGateView.replace('_',' ')})`;
  }
  tapChart.update('none');

  // Update toggle buttons sesuai gate yang ada
  const toggles = document.getElementById('gate-toggles');
  const existing = new Set([...toggles.querySelectorAll('.tog')].map(b => b.dataset.gate));
  for (const gKey of Object.keys(d.gates)) {
    if (!existing.has(gKey)) {
      const b = document.createElement('button');
      b.className = 'tog';
      b.dataset.gate = gKey;
      b.textContent = gKey.replace('_',' ').toUpperCase();
      b.onclick = () => setGateView(gKey, b);
      toggles.appendChild(b);
    }
  }
}


// ── WebSocket ─────────────────────────────────────────────────
let ws;
function connect() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen  = () => { document.getElementById('dot').classList.add('on'); document.getElementById('ws-lbl').textContent='live'; };
  ws.onclose = () => { document.getElementById('dot').classList.remove('on'); document.getElementById('ws-lbl').textContent='reconnecting...'; setTimeout(connect,3000); };
  ws.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (msg.type==='stats')       updateStats(msg.data);
    if (msg.type==='analytics')   renderChart(msg.data);
    if (msg.type==='loadbalance') updateLB(msg.data);
    if (msg.type==='new_event')   {prependLog([msg.data]);tickerEvents.unshift(msg.data);if(tickerEvents.length>3)tickerEvents.length=3;updateTicker();}
    if (msg.type==='history')        {renderLog(msg.data);tickerEvents=msg.data.slice(-3).reverse();updateTicker();}
    if (msg.type==='anomaly_summary') updateAnomalySummary(msg.data);
    if (msg.type==='anomaly')         addAnomalyToast(msg.data);
    if (msg.type==='forecast')        renderForecast(msg.data);
    if (msg.type==='network')         renderNetwork(msg.data);
  };
}


// ── Stats ─────────────────────────────────────────────────────
function updateStats(d) {
  setVal('s-reg',   d.total_registered,  'c-reg');
  setVal('s-entry', d.total_entry,        'c-entry');
  setVal('s-total', d.total_tap,          'c-total');
  setVal('s-gates', Object.keys(d.gates).length, 'c-gates');
  document.getElementById('s-time').textContent = d.timestamp;

  if (!Object.keys(d.gates).length) return;
  const maxTap = Math.max(...Object.values(d.gates).map(g=>g.total_tap)) || 1;
  const grid   = document.getElementById('gates-grid');
  grid.innerHTML = '';
  for (const [name,info] of Object.entries(d.gates)) {
    const pct = Math.round((info.total_tap/maxTap)*100);
    const div = document.createElement('div');
    div.className = 'gate-card';
    const gidNum   = name.replace('gate_','');
    const locked   = (lockedGates||[]).includes(parseInt(gidNum));
    div.style.borderColor = locked ? 'var(--deny)' : '';
    div.innerHTML = `
      <div style="display:flex;align-items:center;justify-content:space-between">
        <div class="gate-name">${name.replace('_',' ')}</div>
        ${locked ? '<span style="font-size:.6rem;color:var(--deny);font-weight:700">🔒 LOCKED</span>' : ''}
      </div>
      <div class="gate-num" style="color:${locked?'var(--deny)':'var(--text)'}">${info.total_tap}</div>
      <div class="gate-bar"><div class="gate-bar-fill" style="width:${pct}%;background:${locked?'var(--deny)':'var(--accent)'}"></div></div>
      <div class="gate-time">${info.last_seen}</div>
      <div style="margin-top:.5rem;display:flex;gap:.4rem">
        ${locked
          ? `<button class="btn" style="font-size:.55rem;padding:.2rem .5rem;border-color:var(--accent);color:var(--accent)" onclick="unlockGate(${gidNum})">Unlock</button>`
          : `<button class="btn danger" style="font-size:.55rem;padding:.2rem .5rem" onclick="lockGate(${gidNum})">Lockdown</button>`
        }
      </div>`;
    grid.appendChild(div);
  }
  updateTicker();
}

function setVal(id,val,cardId) {
  const el=document.getElementById(id);
  if(parseInt(el.textContent)!==val){
    el.textContent=val;
    const c=document.getElementById(cardId);
    if(c){c.classList.remove('pop');void c.offsetWidth;c.classList.add('pop');}
  }
}


// ── Load balance ──────────────────────────────────────────────
function updateLB(d) {
  const alert=document.getElementById('lb-alert');
  if(!d.suggestion){alert.className='alert';return;}
  document.getElementById('lb-title').textContent=d.suggestion.severity==='high'?'⚠ Gate Terlalu Ramai':'ℹ Distribusi Tidak Merata';
  document.getElementById('lb-msg').textContent=d.suggestion.message;
  alert.className=`alert show ${d.suggestion.severity==='high'?'':'medium'}`;
}


// ── Log ───────────────────────────────────────────────────────
const STATUS_LABEL = {
  'allowed':             ['MASUK',       'allowed'],
  'denied_unregistered': ['TDK DAFTAR',  'denied_unregistered'],
  'denied_token_empty':  ['TOKEN HABIS', 'denied_token_empty'],
};

function formatTs(ts) {
  if (!ts) return '-';
  // Format: 2026-05-07T12:38:46.123 → 12:38:46.123
  const t = ts.includes('T') ? ts.split('T')[1] : ts;
  return `<span>${t.slice(0,8)}</span><span class="ms">${t.length>8?t.slice(8):''}</span>`;
}

function makeRow(e, animate) {
  const tr = document.createElement('tr');
  if (animate) tr.className = 'new-row';
  const status = e.status || 'allowed';
  const [label, cls] = STATUS_LABEL[status] || [status, 'allowed'];
  const tokenLeft = e.token_left !== undefined ? e.token_left : '-';
  const tokenMax  = e.token_max  !== undefined ? e.token_max  : '-';
  const ts = e.server_timestamp || e.timestamp || '-';

  tr.innerHTML = `
    <td><span class="badge ${cls}">${label}</span></td>
    <td>Gate ${e.gate_id}</td>
    <td class="uid">${e.ktp_uid}</td>
    <td style="color:var(--dim)">${e.name||'-'}</td>
    <td style="color:${status==='allowed'?'var(--accent)':'var(--warn)'}">
      ${tokenLeft}/${tokenMax}
    </td>
    <td>${formatTs(ts)}</td>
    <td style="color:${e.crc_valid?'var(--accent)':'var(--warn)'}">${e.crc_valid?'OK':'FAIL'}</td>`;
  return tr;
}

function renderLog(entries) {
  const tb=document.getElementById('log-body');
  tb.innerHTML='';
  entries.forEach(e=>tb.appendChild(makeRow(e,false)));
}

function prependLog(entries) {
  const tb=document.getElementById('log-body');
  if(tb.querySelector('td[colspan]'))tb.innerHTML='';
  entries.forEach(e=>tb.insertBefore(makeRow(e,true),tb.firstChild));
  while(tb.rows.length>100)tb.deleteRow(tb.rows.length-1);
}

async function refreshLog(){const res=await fetch('/api/log?limit=50');renderLog(await res.json());}

async function flushEvent(){
  if(!confirm('Flush event log? Data registrasi tetap ada.'))return;
  await fetch('/api/flush-event',{method:'DELETE'});
  document.getElementById('log-body').innerHTML='<tr><td colspan="7" style="color:var(--muted);text-align:center;padding:2rem">Event di-flush.</td></tr>';
}

async function flushAll(){
  if(!confirm('Flush SEMUA data termasuk registrasi?'))return;
  await fetch('/api/flush',{method:'DELETE'});
  document.getElementById('log-body').innerHTML='<tr><td colspan="7" style="color:var(--muted);text-align:center;padding:2rem">Semua data di-flush.</td></tr>';
}

// ── Forecast ──────────────────────────────────────────────────
const SEV_LABEL = {high:'HIGH',medium:'MED',low:'LOW'};
const CONF_LABEL = {high:'HIGH',low:'LOW',cold:'COLD'};
const CONF_COLOR = {high:'var(--accent)',low:'var(--warn)',cold:'var(--muted)'};
function renderForecast(d) {
  const grid = document.getElementById('forecast-grid');
  if (!d || !d.gates || !Object.keys(d.gates).length) {
    grid.innerHTML = '<div style="color:var(--muted);font-size:.72rem">Belum cukup data untuk prediksi...</div>';
    return;
  }

  // Status badge
  const header = grid.parentNode.querySelector('summary');
  const st = d.status === 'cold' ? '⏳ Mengumpulkan data' : d.status === 'warming' ? '⚡ Pemanasan (' + Object.keys(d.gates).length + ' gate)' : '✅ Siap';
  if (header) {
    let sb = header.querySelector('.forecast-status');
    if (!sb) { sb = document.createElement('span'); sb.className = 'forecast-status'; header.appendChild(sb); }
    sb.textContent = ' [' + st + ']';
    sb.style.cssText = 'color:var(--dim);font-weight:400;font-size:.55rem';
  }

  const entries = Object.entries(d.gates);
  grid.innerHTML = entries.map(([gid, g]) => `
    <div style="background:var(--bg);border:1px solid var(--border);padding:.75rem;${g.status === 'cold' ? 'opacity:.7' : ''}">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <span style="font-size:.55rem;color:var(--dim);letter-spacing:.1em;text-transform:uppercase">${gid.replace('_',' ')}</span>
        <span class="badge ${g.confidence === 'high' ? 'allowed' : g.confidence === 'low' ? 'denied_token_empty' : 'denied_unregistered'}" style="font-size:.45rem">${CONF_LABEL[g.confidence]}</span>
      </div>
      <div style="display:flex;align-items:baseline;gap:.75rem;margin:.4rem 0">
        <span style="font-size:.65rem;color:var(--muted)">Sekarang</span>
        <span style="font-family:'Syne',sans-serif;font-size:1.3rem;color:var(--text)">${g.current_tpm}</span>
        <span style="font-size:.55rem;color:var(--muted)">tpm</span>
      </div>
      <div style="display:flex;align-items:baseline;gap:.75rem;margin:.2rem 0">
        <span style="font-size:.65rem;color:var(--muted)">Prediksi</span>
        <span style="font-family:'Syne',sans-serif;font-size:1.8rem;color:${g.status === 'cold' ? 'var(--muted)' : g.severity==='high'?'var(--deny)':g.severity==='medium'?'var(--warn)':'var(--accent)'}">${g.predicted_tpm}</span>
        <span style="font-size:.55rem;color:var(--muted)">tpm</span>
      </div>
      <div style="display:flex;gap:.4rem;margin-top:.4rem">
        <span class="badge ${g.severity==='high'?'denied_unregistered':g.severity==='medium'?'denied_token_empty':'allowed'}" style="font-size:.5rem">${SEV_LABEL[g.severity]}</span>
        <span style="font-size:.55rem;color:var(--dim)">rank #${g.predicted_rank}</span>
      </div>
    </div>
  `).join('');

  // Recommendation
  const rc = document.getElementById('forecast-rec');
  if (d.recommendation) {
    if (!rc) {
      const r = document.createElement('div');
      r.id = 'forecast-rec';
      r.style.cssText = 'margin-top:.75rem;font-size:.65rem;color:var(--warn);background:var(--alert-bg);padding:.5rem .75rem;border:1px solid var(--warn)';
      r.textContent = '⏱ ' + d.recommendation;
      grid.parentNode.appendChild(r);
    } else {
      rc.textContent = '⏱ ' + d.recommendation;
      if (d.status === 'cold') rc.style.display = 'none';
      else rc.style.display = '';
    }
  }
}

// ── Network ──────────────────────────────────────────────────
function renderNetwork(d) {
  const grid = document.getElementById('network-grid');
  const entries = Object.entries(d);
  if (!entries.length) {
    grid.innerHTML = '<div style="color:var(--muted);font-size:.72rem">Menunggu data jaringan...</div>';
    return;
  }
  grid.innerHTML = entries.map(([gid, g]) => {
    const lat = g.latency;
    const rssi = g.rssi;
    // Signal bars: -30 to -120 dBm mapped to 0-4 bars
    const rssiBars = rssi.avg >= -60 ? 4 : rssi.avg >= -80 ? 3 : rssi.avg >= -100 ? 2 : rssi.avg >= -110 ? 1 : 0;
    const bars = '▮'.repeat(rssiBars) + '▯'.repeat(4 - rssiBars);
    const latColor = lat.current > 1000 ? 'var(--deny)' : lat.current > 500 ? 'var(--warn)' : 'var(--accent)';
    return `
    <div style="background:var(--bg);border:1px solid var(--border);padding:.75rem">
      <div style="font-size:.55rem;color:var(--dim);letter-spacing:.1em;text-transform:uppercase">${gid.replace('_',' ')}</div>
      <div style="display:flex;gap:1.5rem;margin-top:.5rem;flex-wrap:wrap">
        <div>
          <div style="font-size:.5rem;color:var(--muted);letter-spacing:.08em;text-transform:uppercase">Latency</div>
          <div style="font-size:1.2rem;color:${latColor}">${lat.current}<span style="font-size:.6rem;color:var(--dim)">ms</span></div>
          <div style="font-size:.55rem;color:var(--dim)">avg ${lat.avg} · max ${lat.max}</div>
        </div>
        <div>
          <div style="font-size:.5rem;color:var(--muted);letter-spacing:.08em;text-transform:uppercase">Signal</div>
          <div style="font-size:1rem;color:${rssi.avg >= -80 ? 'var(--accent)' : rssi.avg >= -100 ? 'var(--warn)' : 'var(--deny)'}">${bars}</div>
          <div style="font-size:.55rem;color:var(--dim)">${rssi.avg} dBm avg</div>
        </div>
      </div>
      <div style="margin-top:.5rem;font-size:.55rem;color:var(--muted)">${lat.samples} sample(s)</div>
    </div>`;
  }).join('');
}

// ── Anomaly ───────────────────────────────────────────────────
const ANOMALY_ICONS = {
  TAILGATING:       '👤',
  UNKNOWN_FLOOD:    '🚨',
  GATE_SILENT:      '😶',
  RAPID_CROSS_GATE: '🔄',
};

function updateAnomalySummary(d) {
  lockedGates = d.locked_gates || [];
  const stat  = document.getElementById('anomaly-stat');
  const count = document.getElementById('anomaly-count');
  const msg   = document.getElementById('anomaly-stat-msg');

  count.textContent = `${d.total_active} anomali aktif`;

  if (d.total_active === 0) {
    stat.style.display = 'none';
    renderAnomalyList([]);
    return;
  }

  stat.style.display = 'flex';
  const parts = [];
  if (d.high)   parts.push(`${d.high} HIGH`);
  if (d.medium) parts.push(`${d.medium} MEDIUM`);
  msg.textContent = parts.join(' · ');

  renderAnomalyList(d.anomalies || []);
}

function renderAnomalyList(anomalies) {
  const list = document.getElementById('anomaly-list');
  if (!anomalies.length) {
    list.innerHTML = '<div class="no-anomaly">Tidak ada anomali — sistem aman ✓</div>';
    return;
  }
  list.innerHTML = '';
  for (const a of anomalies) {
    const div = document.createElement('div');
    div.className = 'anomaly-item';
    div.innerHTML = `
      <span class="anomaly-badge ${a.severity}">${a.severity}</span>
      <div>
        <div class="anomaly-msg">${ANOMALY_ICONS[a.type]||'⚠'} ${a.message}</div>
        <div class="anomaly-ts">${a.timestamp} · Gate ${a.gate_id ?? 'semua'} · ${a.type}</div>
      </div>`;
    list.appendChild(div);
  }
}

function addAnomalyToast(a) {
  // Notifikasi popup singkat untuk anomali baru
  const toast = document.createElement('div');
  toast.style.cssText = `position:fixed;bottom:1.5rem;right:1.5rem;z-index:999;
    background:var(--surface);border:1px solid ${a.severity==='high'?'var(--deny)':'var(--warn)'};
    padding:.75rem 1rem;font-size:.72rem;max-width:320px;animation:flash .3s`;
  toast.innerHTML = `<strong>${ANOMALY_ICONS[a.type]||'⚠'} ${a.type}</strong><br>${a.message}`;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 5000);
}

async function lockGate(gateId) {
  if (!confirm(`Lockdown Gate ${gateId}? Semua tap akan ditolak.`)) return;
  await fetch(`/api/lockdown/${gateId}`, {method:'POST'});
}

async function unlockGate(gateId) {
  await fetch(`/api/unlock/${gateId}`, {method:'POST'});
}

async function unlockAll() {
  await fetch('/api/unlock/all', {method:'POST'});
}

async function revokeUid() {
  const input = document.getElementById('revoke-input');
  const msg = document.getElementById('revoke-msg');
  const btn = document.getElementById('revoke-btn');
  const uid = input.value.trim().toUpperCase();
  if (uid.length !== 8 || !/^[0-9A-F]+$/.test(uid)) {
    msg.textContent = 'Format: 8 char hex (0-9 A-F)';
    msg.style.color = 'var(--deny)';
    return;
  }
  if (!confirm(`Revoke UID ${uid}?\nData peserta akan dihapus permanen.`)) return;
  btn.textContent = '...';
  btn.disabled = true;
  try {
    const res = await fetch(`/api/register/${uid}`, {method:'DELETE'});
    const data = await res.json();
    if (res.ok) {
      msg.textContent = `UID ${uid} revoked`;
      msg.style.color = 'var(--accent)';
      input.value = '';
    } else {
      msg.textContent = data.detail || 'Gagal revoke';
      msg.style.color = 'var(--deny)';
    }
  } catch(e) {
    msg.textContent = 'Error: ' + e.message;
    msg.style.color = 'var(--deny)';
  }
  btn.textContent = 'Revoke';
  btn.disabled = false;
  setTimeout(() => {msg.textContent = '';}, 3000);
}

async function clearAnomalies() {
  await fetch('/api/anomaly/clear', {method:'DELETE'});
  updateAnomalySummary({total_active:0, high:0, medium:0, anomalies:[]});
}

function getCS(v){return getComputedStyle(document.documentElement).getPropertyValue(v).trim()}
function updateChartColors(){
  var ds=tapChart.data.datasets;ds[0].backgroundColor=getCS('--chart-a-bg');ds[0].borderColor=getCS('--chart-a-brd');ds[1].backgroundColor=getCS('--chart-d-bg');ds[1].borderColor=getCS('--chart-d-brd');
  var o=tapChart.options;o.plugins.legend.labels.color=getCS('--chart-leg');o.plugins.tooltip.backgroundColor=getCS('--chart-tlbg');o.plugins.tooltip.titleColor=getCS('--chart-tlt');o.plugins.tooltip.bodyColor=getCS('--chart-tlb');o.scales.x.ticks.color=getCS('--chart-tk');o.scales.y.ticks.color=getCS('--chart-tk');o.scales.x.grid.color=getCS('--chart-gd');o.scales.y.grid.color=getCS('--chart-gd');tapChart.update('none');
}
function setTheme(t){
  var l=t==='light';document.documentElement.classList.toggle('light',l);localStorage.setItem('theme',t);document.getElementById('theme-btn').textContent=l?'🌙':'☀️';updateChartColors();
}
function toggleTheme(){setTheme(document.documentElement.classList.contains('light')?'mocha':'light')}
var saved=localStorage.getItem('theme');if(saved==='light')setTheme('light');
connect();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080, reload=False)