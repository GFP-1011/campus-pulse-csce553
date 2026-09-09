#!/usr/bin/env python3
"""Closed-loop HTTP load driver for CSCE 553 M2. Stdlib only."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


def percentile(sorted_ms: list[float], p: float) -> float:
    if not sorted_ms:
        return float("nan")
    k = (len(sorted_ms) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_ms) - 1)
    if f == c:
        return sorted_ms[f]
    return sorted_ms[f] + (sorted_ms[c] - sorted_ms[f]) * (k - f)


def once(
    url: str,
    method: str,
    data: bytes | None,
    headers: dict[str, str],
    timeout: float,
) -> tuple[float, int, str]:
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
            ms = (time.perf_counter() - t0) * 1000.0
            return ms, int(resp.status), ""
    except urllib.error.HTTPError as e:
        try:
            e.read()
        except Exception:
            pass
        ms = (time.perf_counter() - t0) * 1000.0
        return ms, int(e.code), "http"
    except Exception as e:
        ms = (time.perf_counter() - t0) * 1000.0
        return ms, 0, type(e).__name__


def worker_loop(
    stop_at: float,
    url: str,
    method: str,
    data: bytes | None,
    headers: dict[str, str],
    timeout: float,
    samples: list[tuple[float, int, str]],
    lock: threading.Lock,
) -> None:
    while time.perf_counter() < stop_at:
        s = once(url, method, data, headers, timeout)
        with lock:
            samples.append(s)


def mix_loop(
    stop_at: float,
    read_url: str,
    write_url: str,
    write_body: bytes | None,
    headers: dict[str, str],
    timeout: float,
    read_frac: float,
    samples: list[tuple[float, int, str]],
    lock: threading.Lock,
) -> None:
    while time.perf_counter() < stop_at:
        if random.random() < read_frac:
            s = once(read_url, "GET", None, headers, timeout)
        else:
            s = once(write_url, "POST", write_body, headers, timeout)
        with lock:
            samples.append(s)


def main() -> int:
    p = argparse.ArgumentParser(description="CSCE 553 M2 closed-loop load driver")
    p.add_argument("--base", required=True, help="Origin, no trailing slash")
    p.add_argument("--path", default="/health")
    p.add_argument("--method", default="GET")
    p.add_argument("--body", default="")
    p.add_argument("--token", default=os.environ.get("M2_TOKEN", ""))
    p.add_argument("--no-auth", action="store_true")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--duration", type=float, default=60.0, help="Measure window seconds")
    p.add_argument("--warmup", type=float, default=10.0)
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--label", default="run")
    p.add_argument("--out-dir", default="m2/results")
    p.add_argument("--raw", action="store_true", help="Write per-request CSV")
    p.add_argument("--mix", type=float, default=0.0, help="Read fraction; 0 disables mix")
    p.add_argument("--write-path", default="")
    p.add_argument("--write-body", default="")
    args = p.parse_args()
    if not 1 <= args.concurrency <= 32:
        p.error("concurrency must be between 1 and 32")
    if not 0 < args.duration <= 60 or not 0 <= args.warmup <= 10:
        p.error("measurement must be (0,60] seconds; warmup must be [0,10]")
    if not 0 <= args.mix <= 1 or not 0 < args.timeout <= 90:
        p.error("invalid mix or timeout")
    if not args.no_auth and not args.token:
        p.error("set M2_TOKEN for authenticated workloads")

    base = args.base.rstrip("/")
    headers = {"Accept": "application/json", "User-Agent": "csce553-m2"}
    if args.body or args.write_body:
        headers["Content-Type"] = "application/json"
    if args.token and not args.no_auth:
        headers["Authorization"] = f"Bearer {args.token}"

    read_url = base + (args.path if args.path.startswith("/") else "/" + args.path)
    body = args.body.encode() if args.body else None
    write_url = ""
    write_body = args.write_body.encode() if args.write_body else None
    if args.mix > 0:
        wp = args.write_path or args.path
        write_url = base + (wp if wp.startswith("/") else "/" + wp)
        if write_body is None:
            print("mix requires --write-body", file=sys.stderr)
            return 2

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    def run_window(seconds: float) -> list[tuple[float, int, str]]:
        samples: list[tuple[float, int, str]] = []
        lock = threading.Lock()
        stop_at = time.perf_counter() + seconds
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futs = []
            for _ in range(args.concurrency):
                if args.mix > 0:
                    futs.append(
                        pool.submit(
                            mix_loop,
                            stop_at,
                            read_url,
                            write_url,
                            write_body,
                            headers,
                            args.timeout,
                            args.mix,
                            samples,
                            lock,
                        )
                    )
                else:
                    futs.append(
                        pool.submit(
                            worker_loop,
                            stop_at,
                            read_url,
                            args.method.upper(),
                            body,
                            headers,
                            args.timeout,
                            samples,
                            lock,
                        )
                    )
            for f in as_completed(futs):
                f.result()
        return samples

    print(f"warmup {args.warmup}s  measure {args.duration}s  c={args.concurrency}  {read_url}")
    run_window(args.warmup)
    started_utc = datetime.now(timezone.utc).isoformat()
    t_wall0 = time.perf_counter()
    samples = run_window(args.duration)
    wall = time.perf_counter() - t_wall0

    lat = [s[0] for s in samples]
    oks = [s for s in samples if 200 <= s[1] < 400]
    errs = len(samples) - len(oks)
    lat.sort()
    row: dict[str, Any] = {
        "label": args.label,
        "started_utc": started_utc,
        "ended_utc": datetime.now(timezone.utc).isoformat(),
        "mean_ms": round(sum(lat) / len(lat), 3) if lat else float("nan"),
        "env_note": base,
        "concurrency": args.concurrency,
        "duration_s": round(wall, 3),
        "attempts": len(samples),
        "successes": len(oks),
        "errors": errs,
        "rps_success": round(len(oks) / wall, 3) if wall else 0,
        "p50_ms": round(percentile(lat, 50), 2),
        "p95_ms": round(percentile(lat, 95), 2),
        "p99_ms": round(percentile(lat, 99), 2),
        "max_ms": round(lat[-1], 2) if lat else float("nan"),
        "error_rate": round(errs / len(samples), 4) if samples else 1.0,
    }
    summary_path = out / "summary.csv"
    write_header = not summary_path.exists()
    with summary_path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if write_header:
            w.writeheader()
        w.writerow(row)

    if args.raw:
        raw_path = out / f"{args.label}.requests.csv"
        with raw_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["latency_ms", "status", "error"])
            w.writerows(samples)

    print(json.dumps(row, indent=2))
    if row["error_rate"] > 0.10:
        print("STOP RULE: error_rate > 10%", file=sys.stderr)
    if row["p99_ms"] == row["p99_ms"] and row["p99_ms"] > 5000:
        print("STOP RULE: p99_ms > 5000", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
    