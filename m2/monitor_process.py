#!/usr/bin/env python3
"""Sample API process CPU and RSS with UTC timestamps."""

import argparse
import csv
from datetime import datetime, timezone
import time

import psutil


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pid", type=int)
    parser.add_argument("output")
    parser.add_argument("--duration", type=int, default=900)
    args = parser.parse_args()
    process = psutil.Process(args.pid)
    process.cpu_percent(None)
    with open(args.output, "w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["utc", "api_cpu_percent_one_core", "api_rss_mb", "threads"])
        for _ in range(args.duration // 5):
            time.sleep(5)
            writer.writerow([
                datetime.now(timezone.utc).isoformat(),
                process.cpu_percent(None),
                round(process.memory_info().rss / 1024 / 1024, 2),
                process.num_threads(),
            ])
            stream.flush()


if __name__ == "__main__":
    main()
