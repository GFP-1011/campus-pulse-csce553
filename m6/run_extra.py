#!/usr/bin/env python3
"""Extra publish stages between the doubling steps (to locate each series' peak).

  python m6/run_extra.py --shards 2 --steps 96,160,192 --repeat 256          # layout already seeded
  python m6/run_extra.py --shards 1 --steps 48,96 --reseed                     # rebuild 1-db layout first
Same stage protocol as run_series.py (5 s warmup, 30 s measured, truncate, 15 s recovery).
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import shardctl  # noqa: E402
from run_series import ROOT, cron, note, stage, start_api, stop_api  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shards", type=int, choices=[1, 2], required=True)
    ap.add_argument("--steps", default="")
    ap.add_argument("--repeat", default="")
    ap.add_argument("--reseed", action="store_true")
    a = ap.parse_args()
    n, out = a.shards, ROOT / "m6/results"
    tag = f"S{n}"
    note(out, "extra_start", series=tag, steps=a.steps, repeat=a.repeat)
    cron(False)
    api = None
    try:
        if a.reseed:
            shardctl.setup(n)
            shardctl.seed(n, users=400, follows=40)
        shardctl.reset_activity(n)
        api = start_api(n, out)
        time.sleep(3)
        plan = [(int(c), f"{tag}-post-c{c}") for c in a.steps.split(",") if c]
        plan += [(int(c), f"{tag}-post-c{c}-repeat") for c in a.repeat.split(",") if c]
        for c, label in plan:
            stage(n, out, label, "post", c, api.pid)
            shardctl.reset_activity(n)
            time.sleep(15)
    finally:
        if api:
            stop_api(api)
        note(out, "m5_cron_restored", jobs=cron(True))
        note(out, "extra_end", series=tag)


if __name__ == "__main__":
    main()
