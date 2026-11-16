"""Case store for the unsubscribe agent: one case per mail in the collection mailbox (SQLite)."""

import hashlib
import json
import sqlite3
import threading
import time

STATES = ("new", "running", "unsubscribed", "not_possible", "unclear")


def case_key(message) -> str:
    """Message-ID of the mail in the collection mailbox; the UID changes when the mail is moved."""
    mid = str(message.get("Message-ID") or "").strip()
    if mid:
        return mid
    raw = "|".join(str(message.get(h, "")) for h in ("Date", "Subject", "From"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


class CaseStore:
    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS cases ("
                "key TEXT PRIMARY KEY, subject TEXT NOT NULL, links TEXT NOT NULL, address TEXT NOT NULL, "
                "state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '', "
                "evidence_url TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0, "
                "moved INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL, "
                "deferrals INTEGER NOT NULL DEFAULT 0)"
            )
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(cases)")}
            if "deferrals" not in columns:   # database from before deferrals were counted
                self._db.execute("ALTER TABLE cases ADD COLUMN deferrals INTEGER NOT NULL DEFAULT 0")
            self._db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")

    def _run(self, sql: str, args=()) -> int:
        with self._lock, self._db:
            return self._db.execute(sql, args).rowcount

    def _rows(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["links"] = json.loads(d["links"])
            out.append(d)
        return out

    def add(self, key: str, subject: str, links: list[str], address: str) -> bool:
        now = time.time()
        return self._run(
            "INSERT OR IGNORE INTO cases (key, subject, links, address, state, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'new', ?, ?)", (key, subject, json.dumps(links), address, now, now)) == 1

    def get(self, key: str) -> dict | None:
        rows = self._rows("SELECT * FROM cases WHERE key = ?", (key,))
        return rows[0] if rows else None

    def all(self) -> list[dict]:
        return self._rows("SELECT * FROM cases ORDER BY created_at DESC, key")

    def pending(self, limit: int) -> list[dict]:
        """Untried new cases; deferred ones behind the others, each group longest untouched first."""
        return self._rows(
            "SELECT * FROM cases WHERE state = 'new' AND attempts = 0 "
            "ORDER BY deferrals > 0, updated_at, rowid LIMIT ?", (limit,))

    def claim(self, key: str) -> None:
        self._run("UPDATE cases SET state = 'running', attempts = attempts + 1, updated_at = ? WHERE key = ?",
                  (time.time(), key))

    def release(self, key: str) -> None:
        """Give a claimed case back without counting the attempt (agent or model was unavailable)."""
        self._run("UPDATE cases SET state = 'new', attempts = MAX(attempts - 1, 0), updated_at = ? "
                  "WHERE key = ? AND state = 'running'", (time.time(), key))

    def defer(self, key: str) -> int:
        """Like release, and count the deferral (the model was unavailable for this case). Returns the new count."""
        with self._lock, self._db:
            self._db.execute("UPDATE cases SET state = 'new', attempts = MAX(attempts - 1, 0), "
                             "deferrals = deferrals + 1, updated_at = ? WHERE key = ? AND state = 'running'",
                             (time.time(), key))
            row = self._db.execute("SELECT deferrals FROM cases WHERE key = ?", (key,)).fetchone()
        return row["deferrals"] if row else 0

    def finish(self, key: str, state: str, reason: str = "", evidence: str = "", evidence_url: str = "") -> None:
        if state not in ("unsubscribed", "not_possible", "unclear"):
            raise ValueError(f"not a final state: {state}")
        self._run("UPDATE cases SET state = ?, reason = ?, evidence = ?, evidence_url = ?, updated_at = ? "
                  "WHERE key = ?", (state, reason, evidence, evidence_url, time.time(), key))

    def to_move(self) -> list[dict]:
        return self._rows("SELECT * FROM cases WHERE state = 'unsubscribed' AND moved = 0 ORDER BY created_at")

    def mark_moved(self, key: str) -> None:
        self._run("UPDATE cases SET moved = 1 WHERE key = ?", (key,))

    def retry(self, key: str) -> None:
        self._run("UPDATE cases SET state = 'new', attempts = 0, deferrals = 0, reason = '', evidence = '', "
                  "evidence_url = '', updated_at = ? WHERE key = ? AND state IN ('not_possible', 'unclear')",
                  (time.time(), key))

    def expired(self, days: int, now: float | None = None) -> list[dict]:
        limit = (now if now is not None else time.time()) - days * 86400
        return self._rows("SELECT * FROM cases WHERE state = 'unsubscribed' AND moved = 1 AND updated_at < ? "
                          "ORDER BY created_at, rowid", (limit,))

    def delete(self, key: str) -> None:
        self._run("DELETE FROM cases WHERE key = ?", (key,))

    def recover(self) -> int:
        """Cases left in 'running' by a restart count as a used attempt."""
        return self._run("UPDATE cases SET state = 'unclear', reason = 'interrupted by restart', updated_at = ? "
                         "WHERE state = 'running'", (time.time(),))

    def meta(self, k: str, default: str = "") -> str:
        with self._lock:
            row = self._db.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
        return row["v"] if row else default

    def set_meta(self, k: str, v: str) -> None:
        self._run("INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (k, v))
