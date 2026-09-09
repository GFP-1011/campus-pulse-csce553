#!/usr/bin/env python3
"""Bounded M2 runner; uses the instructor's driver without changing the API."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
DRIVER = ROOT / 'm2/m2-tools/load_baseline.py'

def utc():
    return datetime.now(timezone.utc).isoformat()

def request(base, path, token='', body=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=90) as response:
        data = response.read()
        return json.loads(data), len(data)

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--series', choices=['H', 'L'], required=True)
    p.add_argument('--base', required=True)
    p.add_argument('--out', type=Path, default=ROOT / 'm2/results')
    p.add_argument('--token-file', type=Path)
    p.add_argument('--seed', action='store_true', help='Sequentially fill feed to 100 rows before reads')
    p.add_argument('--cold', action='store_true', help='Wait 16 minutes without requests, then measure health')
    args = p.parse_args()
    args.base = args.base.rstrip('/')
    args.out.mkdir(parents=True, exist_ok=True)
    if (args.out / f'{args.series}-manifest.json').exists():
        p.error('Series already exists; use a fresh --out directory to preserve evidence.')
    if not (ROOT / 'm2/results/prediction.md').exists():
        p.error('Write the pre-measurement prediction first.')
    token = os.environ.get('M2_TOKEN', '')
    if args.token_file:
        token = json.loads(args.token_file.read_text())['token']
    if not token and not (os.environ.get('M2_EMAIL') and os.environ.get('M2_PASSWORD')):
        p.error('Set M2_TOKEN or M2_EMAIL/M2_PASSWORD, or use --token-file.')
    manifest = {'series': args.series, 'base': args.base, 'started_utc': utc(),
                'api_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                'python': sys.version, 'driver_sha256': hashlib.sha256(DRIVER.read_bytes()).hexdigest(),
                'prediction_sha256': hashlib.sha256((ROOT/'m2/results/prediction.md').read_bytes()).hexdigest(),
                'warmup_s': 10, 'measurement_s': 60, 'recovery_s': 30}
    def save():
        (args.out / f'{args.series}-manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    def note(event, **values):
        with (args.out / 'timeline.jsonl').open('a') as f:
            f.write(json.dumps({'utc': utc(), 'series': args.series, 'event': event, **values})+'\n')
        print(event, values, flush=True)
    save()
    if args.cold:
        if args.series != 'H':
            p.error('--cold is hosted only')
        note('idle_started', planned_seconds=960)
        time.sleep(960)
        start = time.perf_counter()
        request(args.base, '/health')
        manifest['cold_start_ms'] = (time.perf_counter()-start)*1000
        manifest['cold_note'] = 'Runner sent no requests for 960 s; external traffic and platform sleep unverified.'
        save()
    request(args.base, '/health')
    if not token:
        data, _ = request(args.base, '/api/auth/login', body={'email':os.environ['M2_EMAIL'], 'password':os.environ['M2_PASSWORD']})
        token = data['access_token']
    feed, size = request(args.base, '/api/messages', token)
    if args.seed:
        for n in range(max(0, 100-len(feed))):
            request(args.base, '/api/messages', token, {'body': 'M2 baseline seed %03d '%n + 'x'*80})
        feed, size = request(args.base, '/api/messages', token)
    if len(feed) < 50:
        raise SystemExit('Feed too small: rerun into a fresh output directory with --seed.')
    manifest.update(feed_rows=len(feed), read_response_bytes=size)
    save()
    env = dict(os.environ, M2_TOKEN=token)
    def stage(kind, concurrency, repeat=False):
        label = f'{args.series}-{kind}-c{concurrency}' + ('-repeat' if repeat else '')
        cmd = [sys.executable, str(DRIVER), '--base', args.base, '--path', '/health' if kind=='health' else '/api/messages', '--concurrency',str(concurrency), '--duration','60','--warmup','10','--timeout','30','--raw','--label',label,'--out-dir',str(args.out)]
        if kind == 'health': cmd += ['--no-auth']
        if kind == 'write': cmd += ['--method','POST','--body','{"body":"m2 load"}']
        if kind == 'mix': cmd += ['--mix','0.8','--write-path','/api/messages','--write-body','{"body":"m2 load"}']
        note('stage_started', label=label)
        subprocess.run(cmd, env=env, check=True)
        with (args.out/'summary.csv').open() as f: row=list(csv.DictReader(f))[-1]
        stop = float(row['error_rate']) > .10 or float(row['p99_ms']) > 5000
        note('stage_finished', label=label, stop=stop)
        time.sleep(30)
        return row, stop
    _, stop = stage('health', 2)
    if stop: raise SystemExit('Control unhealthy; stop series.')
    healthy = None
    for concurrency in [1,4,8,16,32]:
        _, stop = stage('read', concurrency)
        if stop: break
        healthy = concurrency
    if healthy is not None:
        _, stop = stage('read', healthy, repeat=True)
        if not stop:
            _, stop = stage('write', healthy)
            if not stop: stage('mix', healthy)
    manifest['finished_utc'] = utc()
    manifest['last_healthy_read_concurrency'] = healthy
    save()

if __name__ == '__main__':
    main()
