#!/usr/bin/env python3
"""Continuously log Supabase host metrics (CPU, disk) for both shards.

The /customer/v1/privileged/metrics endpoint refreshes about every 60 s, so we
poll every 5 s and keep a sample only when the counters changed. Output:
m6/results/dbhost-timeline.jsonl (one line per refresh per shard).
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from load_m6 import _metrics_targets, scrape  # noqa: E402

OUT = Path(__file__).resolve().parent / "results/dbhost-timeline.jsonl"


def main():
    targets = _metrics_targets(["shard_0", "shard_1"])
    last = {}
    while True:
        for s, (url, key) in targets.items():
            try:
                cpu, vals, n = scrape(url, key)
            except Exception as e:  # noqa: BLE001
                print(s, "scrape error", type(e).__name__, flush=True)
                continue
            tot = sum(cpu.values())
            if last.get(s) != tot:
                last[s] = tot
                with OUT.open("a") as f:
                    f.write(json.dumps({"t": time.time(), "shard": s, "ncpu": n, "cpu": cpu, **vals}) + "\n")
        time.sleep(5)


if __name__ == "__main__":
    main()
