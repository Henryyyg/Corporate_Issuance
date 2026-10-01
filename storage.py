"""Transactional SQLite event history, retry-safe filing ledger and cautious linking."""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from filing_parser import STAGES


class Store:
    def __init__(self, path='data/issuance.sqlite3'):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS filings(accession TEXT PRIMARY KEY, processed_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, accession TEXT NOT NULL,
                  deal_id TEXT NOT NULL, cik INTEGER NOT NULL, stage TEXT NOT NULL,
                  fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
                  UNIQUE(deal_id, stage, fingerprint));
            ''')

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def seen(self, accession):
        with self.connect() as db:
            return db.execute('SELECT 1 FROM filings WHERE accession=?', (accession,)).fetchone() is not None

    def events(self):
        with self.connect() as db:
            rows = db.execute('SELECT payload FROM events ORDER BY id DESC').fetchall()
        return [json.loads(row[0]) for row in rows]

    def save(self, accession, events):
        inserted = 0
        with self.connect() as db:
            if db.execute('SELECT 1 FROM filings WHERE accession=?', (accession,)).fetchone():
                return 0
            for event in events:
                event = dict(event)
                terms = {k: event[k] for k in ('amount', 'currency')}
                terms['tranches'] = [{k: t[k] for k in ('currency', 'amount', 'coupon', 'maturity', 'floating')}
                                     for t in event['tranches']]
                fingerprint = hashlib.sha256(json.dumps(terms, sort_keys=True).encode()).hexdigest()
                # Unknown terms cannot reliably identify a transaction. Keep separate.
                signature = {(t['currency'], t['maturity']) for t in event['tranches']}
                candidates = []
                old_rows = db.execute('SELECT payload FROM events WHERE cik=? ORDER BY id DESC', (event['cik'],)).fetchall()
                for row in old_rows:
                    old = json.loads(row[0])
                    if old['deal_id'] in {c['deal_id'] for c in candidates}:
                        continue
                    days = abs((datetime.fromisoformat(event['filed_date']) - datetime.fromisoformat(old['filed_date'])).days)
                    same = signature and signature == {(t['currency'], t['maturity']) for t in old['tranches']}
                    if same and days <= 7 and STAGES[event['stage']] >= STAGES[old['stage']] and (
                        event['amount'] == old['amount'] or old['amount'] is None):
                        candidates.append(old)
                event['deal_id'] = candidates[0]['deal_id'] if len(candidates) == 1 else f'{event["cik"]}:{accession}:{fingerprint[:12]}'
                event['observed_at'] = datetime.now(timezone.utc).isoformat()
                cur = db.execute('INSERT OR IGNORE INTO events(accession,deal_id,cik,stage,fingerprint,payload) VALUES(?,?,?,?,?,?)',
                    (accession, event['deal_id'], event['cik'], event['stage'], fingerprint, json.dumps(event)))
                inserted += cur.rowcount
            # Called only after every required document has been read successfully.
            db.execute('INSERT INTO filings VALUES(?,?)', (accession, datetime.now(timezone.utc).isoformat()))
        return inserted
