# One-shot login; prints the token field if the JSON looks like M1.
import json
import urllib.request
import sys

base, email, password = sys.argv[1], sys.argv[2], sys.argv[3]
req = urllib.request.Request(
    base.rstrip("/") + "/api/auth/login",
    data=json.dumps({"email": email, "password": password}).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=30) as r:
    body = json.loads(r.read().decode())
print(body.get("access_token") or body.get("token") or body)
