#!/usr/bin/env python3
"""
stress_test.py - Stress testing untuk Sat Set Tap.
Simulasi concurrent tap dari multiple gate dengan rate tertentu.
Mengukur throughput, latency, dan error rate.

Usage:
  python tests/stress_test.py --gates 4 --rate 10 --duration 30 --uids 100
  python tests/stress_test.py --no-token --gates 2 --rate 5 --duration 10
"""

import argparse
import sys, os, time, threading, random, statistics

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "raspi", "receiver"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "raspi"))

from packet import build_hex_packet, parse_hex_packet, Command
from redis_handler import GateRedis


class StressTest:
    def __init__(self, num_gates=2, num_uids=50, rate_tps=5, duration=30,
                 token_enabled=True, redis_host="localhost", redis_port=6379):
        self.num_gates = num_gates
        self.num_uids = num_uids
        self.rate_tps = rate_tps
        self.duration = duration
        self.lock = threading.Lock()
        self.results = []
        self.errors = 0
        self.stop = threading.Event()
        self.db = GateRedis(host=redis_host, port=redis_port,
                            token_enabled=token_enabled)

    def register_uids(self):
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        count = 0
        for i in range(self.num_uids):
            uid = f"{i:08X}"
            key = f"gate:registered:{uid}"
            if self.db.r.hexists(key, "uid"):
                continue
            self.db.r.hset(key, "uid", uid)
            self.db.r.hset(key, "name", f"StressTest_{i}")
            self.db.r.hset(key, "token_max", 99)
            self.db.r.hset(key, "token_left", 99)
            self.db.r.hset(key, "registered_at", now)
            self.db.r.hset(key, "last_entry", "-")
            self.db.r.sadd("gate:registered:all", uid)
            count += 1
        return count

    def gate_worker(self, gate_id):
        uid_pool = [f"{i:08X}" for i in range(self.num_uids)]
        start = time.time()
        while not self.stop.is_set():
            uid = random.choice(uid_pool)
            ts = int((time.time() - start) * 1000) % 65535
            rssi = random.randint(-90, -40)

            hex_str = build_hex_packet(Command.TAP_REQUEST, gate_id, uid, timestamp=ts)
            packet = parse_hex_packet(hex_str)

            t0 = time.perf_counter()
            try:
                result = self.db.store_tap(packet, rssi=rssi)
                lat = (time.perf_counter() - t0) * 1000
                with self.lock:
                    self.results.append({
                        "gate": gate_id,
                        "uid": uid,
                        "status": result["status"],
                        "latency_ms": round(lat, 2),
                    })
            except Exception as e:
                with self.lock:
                    self.errors += 1

            elapsed = time.time() - start
            if elapsed >= self.duration:
                break

    def run(self):
        print(f"\n{'='*60}")
        print(f"  STRESS TEST - Sat Set Tap")
        print(f"  Gate: {self.num_gates} | UID: {self.num_uids} | Rate: {self.rate_tps} tps")
        print(f"  Duration: {self.duration}s | Token: {'enabled' if self.db.token_enabled else 'disabled'}")
        print(f"{'='*60}\n")

        if not self.db.ping():
            print("[FAIL] Redis tidak jalan!")
            sys.exit(1)
        print("[OK] Redis OK")

        regged = self.register_uids()
        print(f"[OK] {regged} UID siap\n")

        self.db.flush_event_only()
        threads = []
        for gid in range(1, self.num_gates + 1):
            t = threading.Thread(target=self.gate_worker, args=(gid,), daemon=True)
            threads.append(t)

        real_start = time.time()
        for t in threads:
            t.start()

        elapsed = 0
        while elapsed < self.duration:
            time.sleep(0.5)
            elapsed = time.time() - real_start
            with self.lock:
                done = len(self.results)
            if done:
                elapsed_ = time.time() - real_start
                rate = done / elapsed_
                print(f"  >> {elapsed:.0f}s | {done} tap | {rate:.1f} tps", end="\r")

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

        p50 = statistics.median(latencies) if latencies else 0
        sorted_lat = sorted(latencies)
        p95 = sorted_lat[int(len(sorted_lat) * 0.95)] if sorted_lat else 0
        p99 = sorted_lat[int(len(sorted_lat) * 0.99)] if sorted_lat else 0

        print(f"\n{'='*60}")
        print(f"  HASIL STRESS TEST")
        print(f"{'='*60}")
        print(f"  Durasi:              {elapsed:.1f}s")
        print(f"  Total tap:           {len(self.results)}")
        print(f"  Allowed:             {len(allowed)}")
        print(f"  Denied:              {len(denied)}")
        print(f"  Errors:              {self.errors}")
        print(f"  Throughput:          {len(self.results)/elapsed:.1f} tps")
        print(f"  Throughput (ok):     {len(allowed)/elapsed:.1f} tps")
        print(f"{'-'*60}")
        print(f"  Latency (ms)")
        print(f"    Min:   {min(latencies):.1f}" if latencies else "    Min:   -")
        print(f"    Max:   {max(latencies):.1f}" if latencies else "    Max:   -")
        print(f"    Avg:   {statistics.mean(latencies):.1f}" if latencies else "    Avg:   -")
        print(f"    P50:   {p50:.1f}")
        print(f"    P95:   {p95:.1f}")
        print(f"    P99:   {p99:.1f}")
        print(f"{'='*60}\n")


def main():
    parser = argparse.ArgumentParser(description="Stress Test - Sat Set Tap")
    parser.add_argument("--gates",    type=int, default=2,   help="Jumlah gate simulasi")
    parser.add_argument("--uids",     type=int, default=50,  help="Jumlah UID di pool")
    parser.add_argument("--rate",     type=int, default=5,   help="Target tap/detik per gate")
    parser.add_argument("--duration", type=int, default=30,  help="Durasi test (detik)")
    parser.add_argument("--no-token", action="store_true",   help="Nonaktifkan token")
    parser.add_argument("--redis-host", default="localhost")
    parser.add_argument("--redis-port", type=int, default=6379)
    args = parser.parse_args()

    test = StressTest(
        num_gates=args.gates,
        num_uids=args.uids,
        rate_tps=args.rate,
        duration=args.duration,
        token_enabled=not args.no_token,
        redis_host=args.redis_host,
        redis_port=args.redis_port,
    )
    test.run()


if __name__ == "__main__":
    main()
