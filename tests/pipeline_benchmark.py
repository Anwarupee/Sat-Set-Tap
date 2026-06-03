"""
pipeline_benchmark.py - Mengukur throughput full pipeline receiver
(parse -> store_tap -> response) dengan Redis dimock di memori.
Berjalan tanpa Redis asli, cocok untuk Windows.

Usage:
  python tests\pipeline_benchmark.py --gates 4 --duration 5 --uids 100
"""

import sys, os, time, threading, random, statistics, json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "raspi", "receiver"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "raspi"))

from packet import build_hex_packet, parse_hex_packet, Command
from redis_handler import GateRedis
from anomaly import AnomalyDetector


class MockRedisConnection:
    """Mimik Redis data structure di memori - tanpa server Redis."""

    def __init__(self):
        self.data = {}  # key -> value (string, list, set, hash)

    def ping(self):
        return True

    # -- String ops ------------------------------------
    def get(self, key):
        val = self.data.get(key)
        if isinstance(val, str):
            return val
        return None

    def setex(self, key, ttl, value):
        self.data[key] = value
        return True

    def exists(self, key):
        return 1 if key in self.data else 0

    def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self.data:
                del self.data[k]
                n += 1
        return n

    def expire(self, key, ttl):
        return 1 if key in self.data else 0

    # -- Hash ops --------------------------------------
    def hgetall(self, key):
        val = self.data.get(key)
        if isinstance(val, dict):
            return val.copy()
        return {}

    def hget(self, key, field):
        val = self.data.get(key)
        if isinstance(val, dict):
            return val.get(field)
        return None

    def hset(self, key, field, value):
        if key not in self.data or not isinstance(self.data[key], dict):
            self.data[key] = {}
        self.data[key][field] = value
        return 1

    def hincrby(self, key, field, amount=1):
        if key not in self.data or not isinstance(self.data[key], dict):
            self.data[key] = {}
        old = int(self.data[key].get(field, 0))
        new = old + amount
        self.data[key][field] = str(new)
        return new

    def hexists(self, key, field):
        val = self.data.get(key)
        return 1 if isinstance(val, dict) and field in val else 0

    # -- List ops --------------------------------------
    def lpush(self, key, value):
        if key not in self.data or not isinstance(self.data[key], list):
            self.data[key] = []
        self.data[key].insert(0, value)
        return len(self.data[key])

    def ltrim(self, key, start, stop):
        val = self.data.get(key)
        if isinstance(val, list):
            if start >= len(val):
                self.data[key] = []
            else:
                self.data[key] = val[start:stop+1]
        return True

    def lrange(self, key, start, stop):
        val = self.data.get(key)
        if isinstance(val, list):
            return val[start:stop+1] if stop >= 0 else val[start:]
        return []

    # -- Set ops ---------------------------------------
    def sadd(self, key, member):
        if key not in self.data or not isinstance(self.data[key], set):
            self.data[key] = set()
        self.data[key].add(member)
        return 1

    def srem(self, key, member):
        val = self.data.get(key)
        if isinstance(val, set) and member in val:
            val.remove(member)
            return 1
        return 0

    def smembers(self, key):
        val = self.data.get(key)
        if isinstance(val, set):
            return val.copy()
        return set()

    def scard(self, key):
        val = self.data.get(key)
        if isinstance(val, set):
            return len(val)
        return 0

    def sismember(self, key, member):
        val = self.data.get(key)
        return 1 if isinstance(val, set) and member in val else 0

    # -- Scan ------------------------------------------
    def scan_iter(self, pattern):
        import fnmatch
        for key in list(self.data.keys()):
            if fnmatch.fnmatch(key, pattern):
                yield key


class PipelineBenchmark:
    """Mengukur throughput full receiver pipeline dengan mock Redis."""

    def __init__(self, num_gates=2, num_uids=50, duration=5, token_enabled=True):
        self.num_gates = num_gates
        self.num_uids = num_uids
        self.duration = duration
        self.token_enabled = token_enabled
        self.lock = threading.Lock()
        self.results = []
        self.errors = 0
        self.stop = threading.Event()

        # Mock Redis + GateRedis asli
        self.mock_conn = MockRedisConnection()
        self.db = GateRedis.__new__(GateRedis)
        self.db.r = self.mock_conn
        self.db.token_enabled = token_enabled

        # Mock anomaly detector (tidak dipanggil di real-time path,
        # tapi kita simpan untuk referensi)
        self.detector = AnomalyDetector(self.mock_conn)

    def register_uids(self):
        """Daftarkan UID di mock Redis seperti real registration."""
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        count = 0
        for i in range(self.num_uids):
            uid = f"{i:08X}"
            key = f"gate:registered:{uid}"
            if self.mock_conn.hexists(key, "uid"):
                continue
            self.mock_conn.hset(key, "uid", uid)
            self.mock_conn.hset(key, "name", f"Peserta_{i+1}")
            self.mock_conn.hset(key, "token_max", 99)
            self.mock_conn.hset(key, "token_left", 99)
            self.mock_conn.hset(key, "registered_at", now)
            self.mock_conn.hset(key, "last_entry", "-")
            self.mock_conn.sadd("gate:registered:all", uid)
            count += 1
        return count

    def gate_worker(self, gate_id):
        """Simulasi satu gate yang mengirim TAP_REQUEST terus menerus."""
        uid_pool = [f"{i:08X}" for i in range(self.num_uids)]
        start = time.time()
        while not self.stop.is_set():
            uid = random.choice(uid_pool)
            ts = int((time.time() - start) * 1000) % 65535
            rssi = random.randint(-90, -40)

            # Bangun hex packet (seperti gate_node.ino)
            hex_str = build_hex_packet(Command.TAP_REQUEST, gate_id, uid, timestamp=ts)

            # -- Pipeline lengkap: parse -> store_tap -> response --
            t0 = time.perf_counter()
            try:
                # Step 1: Parse (sama seperti handle_packet)
                packet = parse_hex_packet(hex_str)

                # Step 2: Store tap (gate:registered check, token, log, stats)
                result = self.db.store_tap(packet, rssi=rssi)

                # Step 3: Hitung latency
                lat = (time.perf_counter() - t0) * 1000

                with self.lock:
                    self.results.append({
                        "gate": gate_id,
                        "uid": uid,
                        "status": result["status"],
                        "latency_ms": round(lat, 3),
                    })
            except Exception as e:
                with self.lock:
                    self.errors += 1

            elapsed = time.time() - start
            if elapsed >= self.duration:
                break

    def run(self):
        print(f"\n{'='*65}")
        print(f"  PIPELINE BENCHMARK - Sat Set Tap")
        print(f"  Gate: {self.num_gates} | UID: {self.num_uids} | Durasi: {self.duration}s")
        print(f"  Token: {'ON' if self.token_enabled else 'OFF'}")
        print(f"  Pipeline: parse_hex -> store_tap (mock Redis) -> response")
        print(f"{'='*65}\n")

        regged = self.register_uids()
        print(f"  [+] {regged} UID terdaftar di mock Redis\n")

        threads = []
        for gid in range(1, self.num_gates + 1):
            t = threading.Thread(target=self.gate_worker, args=(gid,), daemon=True)
            threads.append(t)

        # Flush event log
        for key in ["gate:log", "gate:uid:seen"]:
            self.mock_conn.delete(key)
        for key in list(self.mock_conn.scan_iter("gate:stats:*")):
            self.mock_conn.delete(key)
        for key in list(self.mock_conn.scan_iter("gate:event:*")):
            self.mock_conn.delete(key)

        real_start = time.time()
        for t in threads:
            t.start()

        # Progress indicator
        while time.time() - real_start < self.duration:
            remaining = self.duration - (time.time() - real_start)
            if remaining <= 0:
                break
            with self.lock:
                done = len(self.results)
            if done and done % 1000 < 100:
                rate = done / (time.time() - real_start) if (time.time() - real_start) > 0 else 0
                print(f"  >> {max(0, int(remaining))}s sisa | {done} tap | {rate:.0f} tps", end="\r")
            time.sleep(0.1)

        self.stop.set()
        for t in threads:
            t.join()

        real_elapsed = time.time() - real_start
        self.report(real_elapsed)
        return self.results

    def report(self, elapsed):
        allowed = [r for r in self.results if r["status"] == "allowed"]
        denied = [r for r in self.results if r["status"] != "allowed"]
        latencies = [r["latency_ms"] for r in self.results]

        if not latencies:
            print("\n  [!] Tidak ada data - benchmark gagal?")
            return

        p50 = statistics.median(latencies)
        sorted_lat = sorted(latencies)
        p95 = sorted_lat[int(len(sorted_lat) * 0.95)]
        p99 = sorted_lat[int(len(sorted_lat) * 0.99)]

        print(f"\n{'='*65}")
        print(f"  HASIL PIPELINE BENCHMARK")
        print(f"{'='*65}")
        print(f"  Durasi:              {elapsed:.2f}s")
        print(f"  Total tap:           {len(self.results)}")
        print(f"  Allowed:             {len(allowed)}")
        print(f"  Denied:              {len(denied)}")
        print(f"  Errors:              {self.errors}")
        print(f"  Throughput:          {len(self.results)/elapsed:.0f} tps")
        print(f"{'-'*65}")
        print(f"  Latency per tap (ms)")
        print(f"    Min:   {min(latencies):.3f}")
        print(f"    Max:   {max(latencies):.3f}")
        print(f"    Avg:   {statistics.mean(latencies):.3f}")
        print(f"    P50:   {p50:.3f}")
        print(f"    P95:   {p95:.3f}")
        print(f"    P99:   {p99:.3f}")
        print(f"{'='*65}\n")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Pipeline Benchmark - Sat Set Tap")
    parser.add_argument("--gates",    type=int, default=4,   help="Jumlah gate simulasi")
    parser.add_argument("--uids",     type=int, default=100, help="Jumlah UID di pool")
    parser.add_argument("--duration", type=int, default=5,   help="Durasi (detik)")
    parser.add_argument("--no-token", action="store_true",   help="Nonaktifkan token")
    args = parser.parse_args()

    test = PipelineBenchmark(
        num_gates=args.gates,
        num_uids=args.uids,
        duration=args.duration,
        token_enabled=not args.no_token,
    )
    test.run()

    # -- Perbandingan: throughput fisik gate --
    print(f"\n  Perbandingan untuk proposal:")
    print(f"  {'-'*65}")
    print(f"  Pipeline throughput:  ~{len(test.results)/max(test.duration, 0.1):.0f} tps (server-side)")
    print(f"  Gate fisik (real):    ~0.3-0.5 tps/gate (servo + orang lewat)")
    print(f"  {'-'*65}")
    print(f"  Server bukan bottleneck - {'%.0f' % (len(test.results)/max(test.duration, 0.1) / (0.5 * args.gates))}x lebih cepat",
          f"dari batas fisik gate.\n")


if __name__ == "__main__":
    main()
