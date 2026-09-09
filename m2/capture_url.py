#!/usr/bin/env python3
"""Capture a web page with Chrome and save timestamp/URL metadata."""

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("output", type=Path)
    parser.add_argument("--wait-ms", type=int, default=5000)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "google-chrome",
            "--headless",
            "--no-sandbox",
            "--disable-gpu",
            "--hide-scrollbars",
            "--window-size=1440,1000",
            f"--virtual-time-budget={args.wait_ms}",
            f"--screenshot={args.output.resolve()}",
            args.url,
        ],
        check=True,
    )
    metadata = {
        "url": args.url,
        "captured_utc": datetime.now(timezone.utc).isoformat(),
        "tool": "Google Chrome --headless",
        "image": args.output.name,
    }
    args.output.with_suffix(".json").write_text(json.dumps(metadata, indent=2) + "\n")


if __name__ == "__main__":
    main()
