"""
dashboard.py v3 — Fix status label + chart per gate + timestamp ms
"""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import redis, json, asyncio, time, os, sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "receiver"))

REDIS_HOST = "localhost"
REDIS_PORT = 6379
import os
REDIS_PASS = os.getenv("REDIS_PASS", "")

app = FastAPI(title="TheGate Dashboard")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASS, decode_responses=True)


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
                        await manager.broadcast({"type": "new_event", "data": json.loads(entry)})
                last_log_len = current_len

            await manager.broadcast({"type": "stats",     "data": get_stats_data()})
            if tick % 5 == 0:
                await manager.broadcast({"type": "analytics", "data": get_analytics_data()})
                await manager.broadcast({"type": "loadbalance","data": get_load_balance()})
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


BUSY_WINDOW_SEC   = 120   # window pemantauan: 2 menit
BUSY_MAX_GAP_SEC  = 30    # maksimum jeda antar tap agar dianggap "terus-menerus"
BUSY_MIN_TAPS     = 3     # minimal jumlah tap dalam window agar dievaluasi


def _get_continuous_busy_gates() -> dict:
    """
    Mengembalikan dict {gate_id: {"tap_count": int, "max_gap_sec": float, "is_busy": bool}}
    untuk setiap gate yang memiliki tap dalam BUSY_WINDOW_SEC terakhir.

    Sebuah gate dinyatakan "ramai" jika:
      1. Ada minimal BUSY_MIN_TAPS tap dalam 2 menit terakhir, DAN
      2. Tidak ada jeda antar tap yang melebihi BUSY_MAX_GAP_SEC
         (artinya tap datang secara terus-menerus tanpa henti selama window tersebut).
    """
    now     = time.time()
    cutoff  = now - BUSY_WINDOW_SEC

    # Kumpulkan timestamp tap per gate dari gate:log
    gate_taps: dict[str, list[float]] = defaultdict(list)

    for raw in r.lrange("gate:log", 0, 999):
        try:
            e  = json.loads(raw)
            ts = e.get("server_timestamp") or e.get("timestamp", "")
            if not ts or "T" not in ts:
                continue

            # Parse ISO timestamp → epoch float
            # Format: 2026-05-07T12:38:46.123  atau  2026-05-07T12:38:46
            ts_clean = ts[:26]  # potong trailing karakter ekstra jika ada
            try:
                import datetime as _dt
                if "." in ts_clean:
                    dt = _dt.datetime.strptime(ts_clean[:23], "%Y-%m-%dT%H:%M:%S.%f")
                else:
                    dt = _dt.datetime.strptime(ts_clean[:19], "%Y-%m-%dT%H:%M:%S")
                epoch = dt.timestamp()
            except Exception:
                continue

            if epoch < cutoff:
                continue

            gate_id = str(e.get("gate_id", "unknown"))
            gate_taps[gate_id].append(epoch)
        except Exception:
            continue

    result = {}
    for gid, timestamps in gate_taps.items():
        if len(timestamps) < BUSY_MIN_TAPS:
            result[gid] = {"tap_count": len(timestamps), "max_gap_sec": None, "is_busy": False}
            continue

        sorted_ts = sorted(timestamps)
        gaps = [sorted_ts[i+1] - sorted_ts[i] for i in range(len(sorted_ts) - 1)]
        max_gap = max(gaps) if gaps else 0

        # Busy = tap terus-menerus tanpa jeda > BUSY_MAX_GAP_SEC
        is_busy = max_gap <= BUSY_MAX_GAP_SEC

        result[gid] = {
            "tap_count":   len(sorted_ts),
            "max_gap_sec": round(max_gap, 1),
            "is_busy":     is_busy,
        }

    return result


def get_load_balance() -> dict:
    busy_info = _get_continuous_busy_gates()

    if not busy_info:
        return {"status": "no_data", "suggestion": None, "gates": {}}

    busy_gates  = [gid for gid, info in busy_info.items() if info["is_busy"]]
    quiet_gates = [gid for gid, info in busy_info.items() if not info["is_busy"]]

    # Ambil total_tap dari gate:stats untuk info gate sepi (tujuan redirect)
    gate_stats = {}
    for key in r.scan_iter("gate:stats:*"):
        gid  = key.split(":")[-1]
        data = r.hgetall(key)
        gate_stats[gid] = int(data.get("total_tap", 0))

    suggestion = None
    if busy_gates:
        # Pilih gate paling sibuk (tap terbanyak dalam window)
        from_gate = max(busy_gates, key=lambda g: busy_info[g]["tap_count"])
        info      = busy_info[from_gate]

        if quiet_gates:
            # Arahkan ke gate paling sepi (total_tap terendah)
            to_gate = min(quiet_gates, key=lambda g: gate_stats.get(g, 0))
            msg = (
                f"Gate {from_gate} menerima tap terus-menerus "
                f"({info['tap_count']} tap dalam 2 menit, jeda maks {info['max_gap_sec']}s). "
                f"Arahkan ke Gate {to_gate} ({gate_stats.get(to_gate, 0)} tap)."
            )
        else:
            to_gate = None
            msg = (
                f"Gate {from_gate} menerima tap terus-menerus "
                f"({info['tap_count']} tap dalam 2 menit, jeda maks {info['max_gap_sec']}s). "
                f"Semua gate sedang sibuk."
            )

        suggestion = {
            "from_gate": from_gate,
            "to_gate":   to_gate,
            "message":   msg,
            "severity":  "high",
            "busy_gates": busy_gates,
        }

    return {
        "status":     "ok",
        "suggestion": suggestion,
        "gates":      gate_stats,
        "busy_info":  busy_info,
    }


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


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await manager.connect(ws)
    await ws.send_json({"type": "stats",       "data": get_stats_data()})
    await ws.send_json({"type": "analytics",   "data": get_analytics_data()})
    await ws.send_json({"type": "loadbalance", "data": get_load_balance()})
    await ws.send_json({"type": "history",     "data": api_log(50)})
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
<title>TheGate — Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Syne:wght@400;700;800&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root{--bg:#0a0a0f;--surface:#111118;--border:#1e1e2e;--accent:#00ff88;--warn:#ff6b35;--info:#38bdf8;--deny:#ff4466;--muted:#444466;--text:#e0e0f0;--dim:#666688}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:'Space Mono',monospace;min-height:100vh}
body::before{content:'';position:fixed;inset:0;background-image:linear-gradient(var(--border) 1px,transparent 1px),linear-gradient(90deg,var(--border) 1px,transparent 1px);background-size:40px 40px;opacity:.3;pointer-events:none;z-index:0}
.wrap{position:relative;z-index:1;max-width:1300px;margin:0 auto;padding:2rem}
header{display:flex;align-items:center;justify-content:space-between;margin-bottom:2.5rem;padding-bottom:1.5rem;border-bottom:1px solid var(--border)}
.logo{font-family:'Syne',sans-serif;font-size:1.8rem;font-weight:800;letter-spacing:-.02em}
.logo span{color:var(--accent)}
.live-pill{display:flex;align-items:center;gap:.5rem;font-size:.7rem;color:var(--dim)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}
.dot.on{background:var(--accent);box-shadow:0 0 8px var(--accent);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:1rem;margin-bottom:2rem}
.card{background:var(--surface);border:1px solid var(--border);padding:1.5rem;position:relative;overflow:hidden}
.card::after{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:var(--accent);transform:scaleX(0);transform-origin:left;transition:transform .4s}
.card.pop::after{transform:scaleX(1)}
.card-label{font-size:.6rem;color:var(--dim);letter-spacing:.15em;text-transform:uppercase;margin-bottom:.75rem}
.card-val{font-family:'Syne',sans-serif;font-size:2.2rem;font-weight:800;color:var(--accent);line-height:1}
.card-sub{font-size:.65rem;color:var(--dim);margin-top:.4rem}
.alert{display:none;background:rgba(255,107,53,.12);border:1px solid var(--warn);padding:1rem 1.25rem;margin-bottom:1.5rem;font-size:.8rem}
.alert.show{display:block}
.alert-title{color:var(--warn);font-weight:700;margin-bottom:.25rem;font-size:.7rem;letter-spacing:.1em;text-transform:uppercase}
.alert.medium{background:rgba(56,189,248,.08);border-color:var(--info)}
.alert.medium .alert-title{color:var(--info)}
.section-title{font-size:.6rem;color:var(--dim);letter-spacing:.15em;text-transform:uppercase;margin-bottom:1rem}
.gates{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:.75rem;margin-bottom:2rem}
.gate-card{background:var(--surface);border:1px solid var(--border);padding:1rem}
.gate-name{font-size:.65rem;color:var(--dim);letter-spacing:.1em;text-transform:uppercase}
.gate-num{font-family:'Syne',sans-serif;font-size:2rem;font-weight:800;color:var(--text);margin:.2rem 0}
.gate-bar{height:3px;background:var(--border);margin:.5rem 0}
.gate-bar-fill{height:100%;background:var(--accent);transition:width .5s}
.gate-time{font-size:.6rem;color:var(--muted)}
.chart-section{background:var(--surface);border:1px solid var(--border);padding:1.5rem;margin-bottom:2rem}
.chart-controls{display:flex;gap:.5rem;margin-bottom:1rem;flex-wrap:wrap}
.tog{padding:.3rem .7rem;font-family:'Space Mono',monospace;font-size:.6rem;letter-spacing:.08em;text-transform:uppercase;cursor:pointer;border:1px solid var(--border);background:transparent;color:var(--dim);transition:all .2s}
.tog.active{border-color:var(--accent);color:var(--accent);background:rgba(0,255,136,.08)}
.chart-wrap{position:relative;height:200px}
.log-header{display:flex;align-items:center;gap:.75rem;margin-bottom:1rem;flex-wrap:wrap}
.btn{padding:.45rem .9rem;font-family:'Space Mono',monospace;font-size:.65rem;letter-spacing:.08em;text-transform:uppercase;cursor:pointer;border:1px solid var(--border);background:var(--surface);color:var(--dim);transition:all .2s}
.btn:hover{border-color:var(--accent);color:var(--accent)}
.btn.danger:hover{border-color:var(--warn);color:var(--warn)}
table{width:100%;border-collapse:collapse;font-size:.72rem}
th{text-align:left;padding:.5rem .75rem;font-size:.58rem;letter-spacing:.12em;text-transform:uppercase;color:var(--dim);border-bottom:1px solid var(--border)}
td{padding:.55rem .75rem;border-bottom:1px solid rgba(30,30,46,.5)}
tr:hover td{background:rgba(255,255,255,.02)}
.badge{display:inline-block;padding:.18rem .45rem;font-size:.58rem;letter-spacing:.06em;text-transform:uppercase;font-weight:700;white-space:nowrap}
.badge.allowed{background:rgba(0,255,136,.15);color:var(--accent)}
.badge.denied_unregistered{background:rgba(255,68,102,.15);color:var(--deny)}
.badge.denied_token_empty{background:rgba(255,107,53,.15);color:var(--warn)}
.uid{color:var(--dim);font-family:'Space Mono',monospace;letter-spacing:.04em}
.new-row{animation:flash 1.2s ease-out}
@keyframes flash{0%{background:rgba(0,255,136,.1)}100%{background:transparent}}
.ms{color:var(--muted);font-size:.65em}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo">THE<span>GATE</span></div>
    <div class="live-pill"><div class="dot" id="dot"></div><span id="ws-lbl">connecting...</span></div>
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

  <div class="alert" id="lb-alert">
    <div class="alert-title" id="lb-title">⚠ Gate Terlalu Ramai</div>
    <div class="alert-msg"  id="lb-msg"></div>
  </div>

  <div class="section-title">Status Per Gate</div>
  <div class="gates" id="gates-grid">
    <div class="gate-card" style="color:var(--muted);font-size:.75rem">Menunggu data...</div>
  </div>

  <div class="chart-section">
    <div class="section-title" style="margin-bottom:.75rem">Frekuensi Tap (per menit)</div>
    <div class="chart-controls" id="gate-toggles">
      <button class="tog active" data-gate="all" onclick="setGateView('all',this)">Semua Gate</button>
    </div>
    <div class="chart-wrap"><canvas id="tapChart"></canvas></div>
  </div>

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
    { label:'Masuk',   data:[], backgroundColor:'rgba(0,255,136,.5)', borderColor:'#00ff88', borderWidth:1 },
    { label:'Ditolak', data:[], backgroundColor:'rgba(255,68,102,.3)', borderColor:'#ff4466', borderWidth:1 },
  ]},
  options: {
    responsive:true, maintainAspectRatio:false,
    plugins:{
      legend:{labels:{color:'#666688',font:{family:'Space Mono',size:11}}},
      tooltip:{backgroundColor:'#111118',titleColor:'#e0e0f0',bodyColor:'#666688'}
    },
    scales:{
      x:{ticks:{color:'#444466',font:{size:10,family:'Space Mono'},maxRotation:45},grid:{color:'#1e1e2e'}},
      y:{ticks:{color:'#444466',font:{size:10,family:'Space Mono'}},grid:{color:'#1e1e2e'},beginAtZero:true}
    }
  }
});

let analyticsData  = null;
let currentGateView = 'all';

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
    if (msg.type==='new_event')   prependLog([msg.data]);
    if (msg.type==='history')     renderLog(msg.data);
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
    div.innerHTML = `<div class="gate-name">${name.replace('_',' ')}</div>
      <div class="gate-num">${info.total_tap}</div>
      <div class="gate-bar"><div class="gate-bar-fill" style="width:${pct}%"></div></div>
      <div class="gate-time">${info.last_seen}</div>`;
    grid.appendChild(div);
  }
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

connect();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080, reload=False)