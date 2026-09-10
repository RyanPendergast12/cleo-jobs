"""Application intake/outbox. No employer submission or AI generation in this module."""
import json
import math
import os
import sqlite3
from contextlib import closing
from dataclasses import asdict


def eligible(job):
    fit = job.fit or {}
    score = fit.get('score')
    return (isinstance(score, (int, float)) and not isinstance(score, bool)
            and math.isfinite(score) and 70 <= score <= 100
            and not any('BLOCKER:' in str(x) for x in
                        fit.get('eligibility', []) + fit.get('preferences', [])))


def run_pass(jobs, db_path, dry=False):
    # Opt-in: ordinary job alerts behave exactly as before until configured.
    endpoint = os.environ.get('JOB_APPLY_SERVICE_URL', '').rstrip('/')
    token = os.environ.get('JOB_APPLY_INGEST_TOKEN', '')
    if not endpoint or not token:
        return
    if not endpoint.startswith('https://'):
        raise ValueError('Application service must use HTTPS')
    if dry:
        print(f'[JOB-APPLY] would queue {sum(eligible(j) for j in jobs)} matches')
        return
    with closing(sqlite3.connect(db_path)) as con, con:
        con.execute('CREATE TABLE IF NOT EXISTS apply_outbox (id TEXT PRIMARY KEY, payload TEXT NOT NULL, delivered INTEGER DEFAULT 0)')
        for job in jobs:
            if eligible(job):
                payload = dict(id=job.fingerprint(), job=asdict(job))
                con.execute('INSERT OR IGNORE INTO apply_outbox(id,payload) VALUES (?,?)',
                            (payload['id'], json.dumps(payload)))
        con.commit()  # Retain candidates even if the service is offline.
        import requests
        for key, payload in con.execute('SELECT id,payload FROM apply_outbox WHERE delivered=0 LIMIT 50').fetchall():
            try:
                response = requests.post(endpoint + '/candidates', data=payload,
                    headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
                    timeout=15, allow_redirects=False)
                if response.status_code != 200:
                    print(f'[JOB-APPLY] intake HTTP {response.status_code}; retained for retry')
                    break
                con.execute('UPDATE apply_outbox SET delivered=1 WHERE id=?', (key,))
                con.commit()
            except requests.RequestException:
                print('[JOB-APPLY] service unavailable; retained for retry')
                break

