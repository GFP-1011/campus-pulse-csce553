#!/usr/bin/env python3
"""Seed the read-path table through your API. Stdlib only. Not a load test.

Copy into the app repo and adapt --path / --body to your M1 README.

Why: an empty list endpoint returns [] (tiny payload, almost no DB work).
The M2 read sweep must run against a stable, non-empty table. Seed once
before the first 60 s stage. Re-running --count N inserts N more rows.

Examples (replace URL, user, and JSON keys):

  # Social: fill GET /api/messages
  python seed_data.py --base https://YOUR.onrender.com \\
    --email "$M2_EMAIL" --password "$M2_PASSWORD" \\
    --path /api/messages --body "{\\"body\\":\\"seed {n}\\"}" --count 50

  # E-commerce: fill GET /api/products (manager account; not the cart)
  python seed_data.py --base https://YOUR.onrender.com \\
    --email "$M2_EMAIL" --password "$M2_PASSWORD" \\
    --path /api/products \\
    --body "{\\"name\\":\\"seed product {n}\\",\\"description\\":\\"m2 seed\\",\\"price\\":9.99}" \\
    --count 50

{n} is replaced by 1, 2, … --count. Sequential POSTs, one at a time.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def request(
    url: str,
    method: str,
    data: bytes | None,
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, str]:
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return int(e.code), body


def login(base: str, path: str, email: str, password: str, timeout: float) -> str:
    url = base + path
    payload = json.dumps({"email": email, "password": password}).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    code, body = request(url, "POST", payload, headers, timeout)
    if code >= 400:
        print(f"login failed HTTP {code}: {body[:500]}", file=sys.stderr)
        raise SystemExit(2)
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        print(f"login did not return JSON: {body[:500]}", file=sys.stderr)
        raise SystemExit(2)
    token = data.get("access_token") or data.get("token")
    if not token or not isinstance(token, str):
        print(f"no access_token/token in login JSON. Keys: {list(data)}", file=sys.stderr)
        raise SystemExit(2)
    return token


def main() -> int:
    p = argparse.ArgumentParser(description="CSCE 553 M2: seed via the write API")
    p.add_argument("--base", required=True, help="Origin, no trailing slash")
    p.add_argument("--email", default=os.environ.get("M2_EMAIL", ""))
    p.add_argument("--password", default=os.environ.get("M2_PASSWORD", ""))
    p.add_argument("--token", default=os.environ.get("M2_TOKEN", ""))
    p.add_argument("--login-path", default="/api/auth/login")
    p.add_argument("--path", required=True, help="POST path that creates a read-path row")
    p.add_argument("--body", required=True, help='JSON with {n} for a unique field')
    p.add_argument("--count", type=int, default=50, help="How many rows to insert")
    p.add_argument("--timeout", type=float, default=30.0)
    args = p.parse_args()

    if args.count < 1:
        print("--count must be >= 1", file=sys.stderr)
        return 2
    if "{n}" not in args.body:
        print("--body should include {n} so rows are unique (e.g. name or message text)", file=sys.stderr)
        return 2

    base = args.base.rstrip("/")
    path = args.path if args.path.startswith("/") else "/" + args.path
    url = base + path

    token = args.token
    if not token:
        if not args.email or not args.password:
            print("need --token or --email and --password", file=sys.stderr)
            return 2
        login_path = args.login_path if args.login_path.startswith("/") else "/" + args.login_path
        token = login(base, login_path, args.email, args.password, args.timeout)

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "csce553-m2-seed",
    }

    ok = 0
    for n in range(1, args.count + 1):
        payload = args.body.replace("{n}", str(n)).encode()
        code, body = request(url, "POST", payload, headers, args.timeout)
        if 200 <= code < 300:
            ok += 1
            if n == 1 or n % 10 == 0 or n == args.count:
                print(f"{n}/{args.count} HTTP {code}")
        else:
            print(f"{n}/{args.count} HTTP {code}: {body[:300]}", file=sys.stderr)
            print("stopping. Fix --path/--body (see your M1 README) and retry.", file=sys.stderr)
            return 3

    print(json.dumps({"posted": ok, "url": url}, indent=2))
    print("Next: curl the READ path and note size_download. Then start the M2 sweep.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
