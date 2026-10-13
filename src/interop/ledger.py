"""One-process SQLite outbox and separate source/destination evidence tables."""

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from .errors import InterfaceError
from .fhir import canonical, digest

SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS source_manifest (
    source_key TEXT PRIMARY KEY, payload_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY, source_key TEXT UNIQUE NOT NULL, payload_hash TEXT NOT NULL,
    payload TEXT NOT NULL, bundle TEXT NOT NULL, state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
    diagnostic TEXT, routes TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY, transaction_id INTEGER NOT NULL REFERENCES transactions(id),
    number INTEGER NOT NULL, outcome TEXT NOT NULL, diagnostic TEXT, occurred_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS expected_resources (
    transaction_id INTEGER NOT NULL REFERENCES transactions(id),
    path TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(transaction_id, path)
);
CREATE TABLE IF NOT EXISTS destination_snapshots (
    path TEXT PRIMARY KEY, digest TEXT NOT NULL, observed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rejections (
    id INTEGER PRIMARY KEY, payload_hash TEXT NOT NULL, diagnostic TEXT NOT NULL,
    occurred_at REAL NOT NULL
);
"""

RECONCILE_SQL = """
SELECT s.source_key, CASE
    WHEN t.id IS NULL THEN 'missing_integration'
    WHEN s.payload_hash != t.payload_hash THEN 'source_digest_mismatch'
    WHEN t.state != 'delivered' THEN t.state
    ELSE 'delivered' END AS outcome
FROM source_manifest s LEFT JOIN transactions t USING(source_key)
UNION ALL
SELECT t.source_key, 'missing_source'
FROM transactions t LEFT JOIN source_manifest s USING(source_key) WHERE s.source_key IS NULL
ORDER BY source_key
"""

RESOURCE_SQL = """
WITH latest AS (
    SELECT e.path, MAX(e.transaction_id) AS transaction_id
    FROM expected_resources e JOIN transactions t ON t.id=e.transaction_id
    WHERE t.state='delivered' GROUP BY e.path
)
SELECT e.path, CASE
    WHEN d.path IS NULL THEN 'missing_destination'
    WHEN d.digest != e.digest THEN 'destination_digest_mismatch'
    ELSE 'matched' END AS outcome
FROM latest l JOIN expected_resources e
    ON e.path=l.path AND e.transaction_id=l.transaction_id
LEFT JOIN destination_snapshots d ON d.path=e.path ORDER BY e.path
"""


def payload_digest(raw: str) -> str:
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


class Ledger:
    def __init__(self, path: str):
        if path != ":memory:":
            parent = Path(path).parent
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            os.chmod(path, 0o600)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def record_source(self, key: str, raw: str):
        with self.lock, self.db:
            existing = self.db.execute(
                "SELECT payload_hash FROM source_manifest WHERE source_key=?", (key,)
            ).fetchone()
            value = payload_digest(raw)
            if existing and existing[0] != value:
                raise InterfaceError("source.control-id-collision", ack="AR")
            self.db.execute("INSERT OR IGNORE INTO source_manifest VALUES (?, ?)", (key, value))

    def enqueue(self, key: str, raw: str, bundle: dict, routes: list[str] | None = None) -> int:
        with self.lock, self.db:
            value = payload_digest(raw)
            existing = self.db.execute(
                "SELECT id, payload_hash FROM transactions WHERE source_key=?", (key,)
            ).fetchone()
            if existing:
                if existing["payload_hash"] != value:
                    raise InterfaceError("source.control-id-collision", ack="AR")
                return existing["id"]
            row = self.db.execute(
                "INSERT INTO transactions(source_key,payload_hash,payload,bundle,state,routes,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    key,
                    value,
                    raw,
                    canonical(bundle),
                    "pending",
                    json.dumps(routes or []),
                    time.time(),
                ),
            )
            transaction_id = row.lastrowid
            for name in routes or [""]:
                for entry in bundle["entry"]:
                    path = entry["request"]["url"]
                    path = f"{name}|{path}" if name else path
                    self.db.execute(
                        "INSERT INTO expected_resources VALUES (?,?,?)",
                        (transaction_id, path, digest(entry["resource"])),
                    )
            return transaction_id

    def reject(self, raw: str, code: str):
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO rejections(payload_hash,diagnostic,occurred_at) VALUES (?,?,?)",
                (hashlib.sha256(raw.encode()).hexdigest(), code, time.time()),
            )

    def claim(self):
        with self.lock, self.db:
            # Preserve arrival order during backoff to avoid stale PUTs after corrections.
            row = self.db.execute(
                "SELECT * FROM transactions WHERE state IN ('pending','delivering') "
                "ORDER BY id LIMIT 1"
            ).fetchone()
            if not row or row["state"] == "delivering" or row["next_attempt"] > time.time():
                return None
            self.db.execute(
                "UPDATE transactions SET state='delivering', attempts=attempts+1 WHERE id=?",
                (row["id"],),
            )
            return dict(row)

    def finish(
        self,
        row: dict,
        snapshots: dict[str, dict] | None,
        error: InterfaceError | None,
        max_attempts: int,
        retry_base: float,
    ):
        attempt = row["attempts"] + 1
        if error is None:
            state, next_attempt, code = "delivered", 0, None
        elif error.retryable and attempt < max_attempts:
            state, next_attempt, code = (
                "pending",
                time.time() + retry_base * 2 ** (attempt - 1),
                error.code,
            )
        else:
            state, next_attempt, code = "exception", 0, error.code
        with self.lock, self.db:
            if snapshots is not None:
                for path, resource in snapshots.items():
                    self.db.execute(
                        "INSERT OR REPLACE INTO destination_snapshots VALUES (?,?,?)",
                        (path, digest(resource), time.time()),
                    )
            self.db.execute(
                "UPDATE transactions SET state=?,next_attempt=?,diagnostic=? WHERE id=?",
                (state, next_attempt, code, row["id"]),
            )
            self.db.execute(
                "INSERT INTO attempts(transaction_id,number,outcome,diagnostic,occurred_at) "
                "VALUES (?,?,?,?,?)",
                (row["id"], attempt, state, code, time.time()),
            )
        return state

    def requeue(self, transaction_id: int):
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT * FROM transactions WHERE id=? AND state='exception'", (transaction_id,)
            ).fetchone()
            if not row:
                raise InterfaceError("queue.not-in-exception")
            newer = self.db.execute(
                "SELECT 1 FROM expected_resources a JOIN expected_resources b ON a.path=b.path "
                "WHERE a.transaction_id=? AND b.transaction_id>? LIMIT 1",
                (transaction_id, transaction_id),
            ).fetchone()
            if newer:
                raise InterfaceError("queue.superseded")
            self.db.execute(
                "UPDATE transactions SET state='pending',attempts=0,next_attempt=0,"
                "diagnostic=NULL WHERE id=?",
                (transaction_id,),
            )

    def snapshots(self, resources: dict[str, dict]):
        with self.lock, self.db:
            self.db.execute("DELETE FROM destination_snapshots")
            for path, resource in resources.items():
                self.db.execute(
                    "INSERT INTO destination_snapshots VALUES (?,?,?)",
                    (path, digest(resource), time.time()),
                )

    def reconcile(self) -> dict:
        with self.lock:
            return {
                "transactions": [dict(r) for r in self.db.execute(RECONCILE_SQL)],
                "resources": [dict(r) for r in self.db.execute(RESOURCE_SQL)],
            }

    def health(self) -> dict:
        with self.lock:
            counts = dict(self.db.execute("SELECT state,COUNT(*) FROM transactions GROUP BY state"))
            oldest = self.db.execute(
                "SELECT MIN(created_at) FROM transactions WHERE state='pending'"
            ).fetchone()[0]
            rejected = self.db.execute("SELECT COUNT(*) FROM rejections").fetchone()[0]
            return {
                "states": counts,
                "rejections": rejected,
                "oldest_pending_seconds": max(0, time.time() - oldest) if oldest else 0,
            }

    def resource_paths(self) -> list[str]:
        with self.lock:
            return [r[0] for r in self.db.execute("SELECT DISTINCT path FROM expected_resources")]

    def exception_rows(self) -> list[dict]:
        with self.lock:
            return [
                dict(r)
                for r in self.db.execute(
                    "SELECT id,state,attempts,diagnostic FROM transactions WHERE state='exception'"
                )
            ]
