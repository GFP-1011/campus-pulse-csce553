#!/usr/bin/env python3
"""Generate the three figures required by CSCE 553 M2."""

import csv
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt


RESULTS = Path(__file__).parent / "results"


def load_rows():
    with (RESULTS / "summary.csv").open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    return [row for row in rows if "-read-c" in row["label"] and "repeat" not in row["label"]]


def series(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["label"].split("-", 1)[0]].append(row)
    for values in grouped.values():
        values.sort(key=lambda row: int(row["concurrency"]))
    return grouped


def finish(name, ylabel):
    plt.xlabel("Offered load (concurrent clients)")
    plt.ylabel(ylabel)
    plt.xticks([1, 4, 8, 16, 32])
    plt.grid(alpha=0.25)
    plt.legend()
    plt.tight_layout()
    plt.savefig(RESULTS / name, dpi=180)
    plt.close()


def main():
    grouped = series(load_rows())

    display = {"H": "H: hosted + PostgreSQL", "L": "L: local API + same PostgreSQL"}

    for environment, rows in grouped.items():
        x = [int(row["concurrency"]) for row in rows]
        plt.plot(x, [float(row["rps_success"]) for row in rows], marker="o", label=display.get(environment, environment))
    plt.title("Authenticated GET /api/messages: throughput")
    finish("throughput-vs-load.png", "Successful responses/s")

    for environment, rows in grouped.items():
        x = [int(row["concurrency"]) for row in rows]
        name = display.get(environment, environment)
        plt.plot(x, [float(row["p50_ms"]) for row in rows], marker="o", label=f"{name} p50")
        plt.plot(x, [float(row["p99_ms"]) for row in rows], marker="s", label=f"{name} p99")
    plt.title("Authenticated GET /api/messages: latency")
    finish("latency-vs-load.png", "Latency (ms)")

    for environment, rows in grouped.items():
        x = [int(row["concurrency"]) for row in rows]
        plt.plot(x, [100 * float(row["error_rate"]) for row in rows], marker="o", label=display.get(environment, environment))
    plt.title("Authenticated GET /api/messages: error rate")
    finish("error-rate-vs-load.png", "Errors (%)")


if __name__ == "__main__":
    main()
