"""SQLite store (data/jobs.db): one row per job id, processed once; `emails` remembers parsed messages.

Schema evolves through MIGRATIONS (PRAGMA user_version); a new version appends a script, never edits one.
Job statuses: candidate -> ready -> sent; skip_rule, skip_llm, dup, expired, failed are terminal.
Other tables: events (Upwork notifications), proposals + ledger (your Sent/Skip decisions and Connects),
tg_messages (Telegram message -> job/event/task), owner_tasks (your texts waiting for the LLM),
llm_calls (hourly budget), kv (small settings: balance, pause, summary day).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS = (
    """
    CREATE TABLE IF NOT EXISTS jobs(
      id TEXT PRIMARY KEY,
      title TEXT NOT NULL,
      url TEXT NOT NULL,
      snippet TEXT,
      first_seen TEXT NOT NULL,
      email_msgid TEXT,
      status TEXT NOT NULL,
      score REAL,
      verdict TEXT,
      sent_at TEXT,
      email_date TEXT,
      fields_json TEXT,
      fit INTEGER NOT NULL DEFAULT 0,
      fit_tags TEXT,
      fingerprint TEXT,
      reason TEXT,
      draft_json TEXT,
      attempts INTEGER NOT NULL DEFAULT 0,
      updated_at TEXT
    );
    CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
    CREATE INDEX IF NOT EXISTS jobs_fingerprint ON jobs(fingerprint);
    CREATE TABLE IF NOT EXISTS emails(
      msgid TEXT PRIMARY KEY,
      kind TEXT,
      email_date TEXT,
      subject TEXT,
      processed_at TEXT NOT NULL,
      parser_version INTEGER NOT NULL,
      n_jobs INTEGER NOT NULL,
      error TEXT
    );
    CREATE TABLE IF NOT EXISTS events(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      msgid TEXT UNIQUE,
      kind TEXT NOT NULL,
      subject TEXT,
      email_date TEXT,
      created TEXT NOT NULL,
      client TEXT,
      title TEXT,
      body TEXT,
      link TEXT,
      status TEXT NOT NULL,
      draft_json TEXT,
      note TEXT,
      updated_at TEXT
    );
    CREATE INDEX IF NOT EXISTS events_status ON events(status);
    CREATE INDEX IF NOT EXISTS events_kind ON events(kind);
    CREATE TABLE IF NOT EXISTS proposals(
      job_id TEXT PRIMARY KEY,
      title TEXT,
      status TEXT NOT NULL,
      connects INTEGER NOT NULL DEFAULT 0,
      bid_usd REAL,
      note TEXT,
      ts TEXT NOT NULL,
      source TEXT
    );
    CREATE TABLE IF NOT EXISTS ledger(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts TEXT NOT NULL,
      kind TEXT NOT NULL,
      delta INTEGER NOT NULL,
      balance INTEGER,
      ref TEXT,
      note TEXT
    );
    CREATE TABLE IF NOT EXISTS tg_messages(
      chat_id INTEGER NOT NULL,
      message_id INTEGER NOT NULL,
      kind TEXT NOT NULL,
      ref TEXT,
      ts TEXT NOT NULL,
      PRIMARY KEY(chat_id, message_id)
    );
    CREATE TABLE IF NOT EXISTS owner_tasks(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      chat_id INTEGER NOT NULL,
      message_id INTEGER,
      text TEXT NOT NULL,
      mode TEXT NOT NULL,
      context TEXT,
      status TEXT NOT NULL,
      result_json TEXT,
      note TEXT,
      created TEXT NOT NULL,
      updated_at TEXT
    );
    CREATE INDEX IF NOT EXISTS owner_tasks_status ON owner_tasks(status);
    CREATE TABLE IF NOT EXISTS llm_calls(
      ts REAL NOT NULL,
      purpose TEXT
    );
    CREATE INDEX IF NOT EXISTS llm_calls_ts ON llm_calls(ts);
    CREATE TABLE IF NOT EXISTS kv(
      key TEXT PRIMARY KEY,
      value TEXT
    );
    """,
)
MAX_SEND_ATTEMPTS = 5


def utc_iso(dt: datetime | None = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, path: Path | str, timeout: float = 10):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=timeout)
        self.conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for i in range(version, len(MIGRATIONS)):
            self.conn.executescript(MIGRATIONS[i])
            self.conn.execute(f"PRAGMA user_version = {i + 1}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ----- emails -----
    def email_done(self, msgid: str, parser_version: int) -> bool:
        row = self.conn.execute("SELECT parser_version FROM emails WHERE msgid=?", (msgid,)).fetchone()
        return row is not None and row[0] >= parser_version

    def mark_email(self, msgid: str, *, kind: str | None, email_date: str | None, subject: str, n_jobs: int,
                   parser_version: int, error: str | None = None, now: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO emails(msgid, kind, email_date, subject, processed_at, parser_version, n_jobs, error) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(msgid) DO UPDATE SET kind=excluded.kind, "
                "email_date=excluded.email_date, subject=excluded.subject, processed_at=excluded.processed_at, "
                "parser_version=excluded.parser_version, n_jobs=excluded.n_jobs, error=excluded.error",
                (msgid, kind, email_date, subject[:300], now or utc_iso(), parser_version, n_jobs, error))

    # ----- jobs -----
    def has_job(self, job_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone() is not None

    def find_fingerprint(self, fingerprint: str | None) -> str | None:
        if not fingerprint:
            return None
        row = self.conn.execute("SELECT id FROM jobs WHERE fingerprint=? LIMIT 1", (fingerprint,)).fetchone()
        return row[0] if row else None

    def add_job(self, job, *, msgid: str, email_date: str | None, status: str, reason: str | None = None,
                fit: int = 0, fit_tags: tuple[str, ...] | list[str] = (), fingerprint: str | None = None,
                now: str | None = None) -> bool:
        """Insert once (idempotent by id). Returns False when the id already exists."""
        now = now or utc_iso()
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO jobs(id, title, url, snippet, first_seen, email_msgid, status, email_date, "
                "fields_json, fit, fit_tags, fingerprint, reason, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job.id, job.title, job.url, job.snippet, now, msgid, status, email_date,
                 json.dumps(job.fields(), ensure_ascii=False), int(fit), ",".join(fit_tags), fingerprint, reason, now))
        return cur.rowcount == 1

    def get(self, job_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _row(row) if row else None

    def by_status(self, status: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM jobs WHERE status=? ORDER BY first_seen, id", (status,)).fetchall()
        return [_row(r) for r in rows]

    def next_candidates(self, limit: int) -> list[dict]:
        """Newest mail first, then better niche fit."""
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE status='candidate' ORDER BY email_date IS NULL, email_date DESC, fit DESC, "
            "first_seen, id LIMIT ?", (int(limit),)).fetchall()
        return [_row(r) for r in rows]

    def save_draft(self, job_id: str, draft: dict | None, status: str, reason: str | None = None,
                   now: str | None = None) -> None:
        d = draft or {}
        with self.conn:
            self.conn.execute(
                "UPDATE jobs SET draft_json=?, score=?, verdict=?, status=?, reason=?, updated_at=? WHERE id=?",
                (json.dumps(draft, ensure_ascii=False) if draft is not None else None, d.get("score"),
                 d.get("verdict"), status, reason, now or utc_iso(), job_id))

    def mark_sent(self, job_id: str, now: str | None = None) -> None:
        now = now or utc_iso()
        with self.conn:
            self.conn.execute("UPDATE jobs SET status='sent', sent_at=?, updated_at=? WHERE id=?", (now, now, job_id))

    def send_failed(self, job_id: str, max_attempts: int = MAX_SEND_ATTEMPTS, now: str | None = None) -> str:
        """Count a failed delivery; after max_attempts the job becomes 'failed'. Returns the new status."""
        with self.conn:
            self.conn.execute("UPDATE jobs SET attempts=attempts+1, updated_at=? WHERE id=?", (now or utc_iso(), job_id))
            self.conn.execute("UPDATE jobs SET status='failed' WHERE id=? AND status='ready' AND attempts>=?",
                              (job_id, max_attempts))
        row = self.conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        return row[0] if row else "missing"

    def expire_candidates(self, before_iso: str, now: str | None = None) -> int:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE jobs SET status='expired', updated_at=? WHERE status='candidate' "
                "AND COALESCE(email_date, first_seen) < ?", (now or utc_iso(), before_iso))
        return cur.rowcount

    def counts(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self.conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status")}

    def all_jobs(self) -> list[dict]:
        return [_row(r) for r in self.conn.execute("SELECT * FROM jobs ORDER BY first_seen, id").fetchall()]

    # ----- kv -----
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def _kv_put(self, key: str, value: str | None) -> None:          # caller holds the transaction
        self.conn.execute("INSERT INTO kv(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (key, value))

    def kv_set(self, key: str, value: str | None) -> None:
        with self.conn:
            self._kv_put(key, value)

    # ----- jobs: lookups for context and stats -----
    def find_job_by_title(self, title: str | None) -> dict | None:
        t = " ".join((title or "").split())
        if len(t) < 3:
            return None
        row = self.conn.execute("SELECT * FROM jobs WHERE lower(title)=lower(?) ORDER BY first_seen DESC LIMIT 1",
                                (t,)).fetchone()
        if row is None and len(t) >= 8:
            row = self.conn.execute(
                "SELECT * FROM jobs WHERE instr(lower(title), lower(?)) > 0 OR (length(title) >= 8 AND "
                "instr(lower(?), lower(title)) > 0) ORDER BY first_seen DESC LIMIT 1", (t, t)).fetchone()
        return _row(row) if row else None

    def count_jobs(self, *, status: str | None = None, since: str | None = None, until: str | None = None,
                   by: str = "first_seen") -> int:
        if by not in ("first_seen", "sent_at", "updated_at"):
            raise ValueError(by)
        q, args = "SELECT COUNT(*) FROM jobs WHERE 1=1", []
        if status:
            q, args = q + " AND status=?", args + [status]
        if since:
            q, args = q + f" AND {by} >= ?", args + [since]
        if until:
            q, args = q + f" AND {by} < ?", args + [until]
        return self.conn.execute(q, args).fetchone()[0]

    # ----- events -----
    def add_event(self, *, msgid: str, kind: str, subject: str, email_date: str | None, status: str,
                  client: str | None = None, title: str | None = None, body: str | None = None,
                  link: str | None = None, now: str | None = None) -> int | None:
        """Insert once per Message-ID; returns the new event id or None when already stored."""
        now = now or utc_iso()
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO events(msgid, kind, subject, email_date, created, client, title, body, link, "
                "status, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (msgid, kind, (subject or "")[:300], email_date, now, client, title, body, link, status, now))
        return cur.lastrowid if cur.rowcount == 1 else None

    def get_event(self, event_id: int | str) -> dict | None:
        row = self.conn.execute("SELECT * FROM events WHERE id=?", (int(event_id),)).fetchone()
        return _event_row(row) if row else None

    def events_by_status(self, status: str, limit: int = 50) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM events WHERE status=? ORDER BY id LIMIT ?", (status, int(limit)))
        return [_event_row(r) for r in rows.fetchall()]

    def set_event(self, event_id: int, *, status: str, draft: dict | None = None, note: str | None = None,
                  now: str | None = None) -> None:
        with self.conn:
            self.conn.execute("UPDATE events SET status=?, draft_json=COALESCE(?, draft_json), note=?, updated_at=? "
                              "WHERE id=?", (status, json.dumps(draft, ensure_ascii=False) if draft else None, note,
                                             now or utc_iso(), int(event_id)))

    def event_counts(self, since: str | None = None, until: str | None = None) -> dict[str, int]:
        q, args = "SELECT kind, COUNT(*) FROM events WHERE 1=1", []
        if since:
            q, args = q + " AND COALESCE(email_date, created) >= ?", args + [since]
        if until:
            q, args = q + " AND COALESCE(email_date, created) < ?", args + [until]
        return {r[0]: r[1] for r in self.conn.execute(q + " GROUP BY kind", args)}

    # ----- Telegram message -> job / event / task -----
    def add_tg_message(self, chat_id: int, message_id: int, kind: str, ref: str | int | None,
                       now: str | None = None) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO tg_messages(chat_id, message_id, kind, ref, ts) VALUES(?,?,?,?,?)",
                              (int(chat_id), int(message_id), kind, None if ref is None else str(ref), now or utc_iso()))

    def tg_message(self, chat_id: int, message_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM tg_messages WHERE chat_id=? AND message_id=?",
                                (int(chat_id), int(message_id))).fetchone()
        return dict(row) if row else None

    # ----- owner texts waiting for the LLM -----
    def add_owner_task(self, *, chat_id: int, message_id: int | None, text: str, mode: str,
                       context: str | None = None, now: str | None = None) -> int:
        now = now or utc_iso()
        with self.conn:
            cur = self.conn.execute("INSERT INTO owner_tasks(chat_id, message_id, text, mode, context, status, created, "
                                    "updated_at) VALUES(?,?,?,?,?,?,?,?)",
                                    (int(chat_id), message_id, text, mode, context, "queued", now, now))
        return cur.lastrowid

    def next_owner_task(self) -> dict | None:
        row = self.conn.execute("SELECT * FROM owner_tasks WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
        return _task_row(row) if row else None

    def get_owner_task(self, task_id: int | str) -> dict | None:
        row = self.conn.execute("SELECT * FROM owner_tasks WHERE id=?", (int(task_id),)).fetchone()
        return _task_row(row) if row else None

    def finish_owner_task(self, task_id: int, status: str, result: dict | None = None, note: str | None = None,
                          now: str | None = None) -> None:
        with self.conn:
            self.conn.execute("UPDATE owner_tasks SET status=?, result_json=?, note=?, updated_at=? WHERE id=?",
                              (status, json.dumps(result, ensure_ascii=False) if result else None, note,
                               now or utc_iso(), int(task_id)))

    def count_owner_tasks(self, status: str = "queued") -> int:
        return self.conn.execute("SELECT COUNT(*) FROM owner_tasks WHERE status=?", (status,)).fetchone()[0]

    def owner_tasks_by_status(self, status: str, limit: int = 20) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM owner_tasks WHERE status=? ORDER BY id LIMIT ?", (status, int(limit)))
        return [_task_row(r) for r in rows.fetchall()]

    # ----- LLM budget -----
    def record_llm_call(self, ts: float, purpose: str) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO llm_calls(ts, purpose) VALUES(?,?)", (float(ts), purpose))

    def llm_calls_between(self, start: float, end: float | None = None) -> int:
        if end is None:
            return self.conn.execute("SELECT COUNT(*) FROM llm_calls WHERE ts >= ?", (float(start),)).fetchone()[0]
        return self.conn.execute("SELECT COUNT(*) FROM llm_calls WHERE ts >= ? AND ts < ?",
                                 (float(start), float(end))).fetchone()[0]

    def oldest_llm_call_since(self, start: float) -> float | None:
        return self.conn.execute("SELECT MIN(ts) FROM llm_calls WHERE ts >= ?", (float(start),)).fetchone()[0]

    def prune_llm_calls(self, before: float) -> int:
        with self.conn:
            return self.conn.execute("DELETE FROM llm_calls WHERE ts < ?", (float(before),)).rowcount

    # ----- connects ledger and proposal decisions -----
    def balance(self) -> int | None:
        v = self.kv_get("connects_balance")
        return int(v) if v not in (None, "") else None

    def set_balance(self, n: int, *, ts: str | None = None, note: str | None = None) -> int:
        with self.conn:
            old = self.balance()
            self._kv_put("connects_balance", str(int(n)))
            self.conn.execute("INSERT INTO ledger(ts, kind, delta, balance, ref, note) VALUES(?,?,?,?,?,?)",
                              (ts or utc_iso(), "set", int(n) - (old or 0), int(n), None, note))
        return int(n)

    def get_proposal(self, job_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM proposals WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def find_proposal_by_title(self, title: str | None) -> dict | None:
        t = " ".join((title or "").split())
        if len(t) < 3:
            return None
        row = self.conn.execute("SELECT * FROM proposals WHERE lower(title)=lower(?) ORDER BY ts DESC LIMIT 1",
                                (t,)).fetchone()
        return dict(row) if row else None

    def record_decision(self, *, job_id: str, title: str | None, status: str, connects: int = 0,
                        bid_usd: float | None = None, note: str | None = None, ts: str | None = None,
                        source: str = "button") -> tuple[bool, int | None]:
        """One decision per job ('sent' | 'skipped'); 'sent' spends connects from a known balance, atomically.
        Returns (newly recorded, balance after)."""
        ts = ts or utc_iso()
        with self.conn:
            cur = self.conn.execute("INSERT OR IGNORE INTO proposals(job_id, title, status, connects, bid_usd, note, "
                                    "ts, source) VALUES(?,?,?,?,?,?,?,?)",
                                    (job_id, title, status, int(connects), bid_usd, note, ts, source))
            bal = self.balance()
            if cur.rowcount != 1:
                return False, bal
            if status == "sent" and connects:
                bal = None if bal is None else bal - int(connects)
                if bal is not None:
                    self._kv_put("connects_balance", str(bal))
                self.conn.execute("INSERT INTO ledger(ts, kind, delta, balance, ref, note) VALUES(?,?,?,?,?,?)",
                                  (ts, "proposal", -int(connects), bal, job_id, title))
        return True, bal

    def proposal_stats(self, since: str | None = None, until: str | None = None) -> dict[str, int]:
        q, args = "SELECT status, COUNT(*), COALESCE(SUM(connects), 0) FROM proposals WHERE 1=1", []
        if since:
            q, args = q + " AND ts >= ?", args + [since]
        if until:
            q, args = q + " AND ts < ?", args + [until]
        out = {"sent": 0, "skipped": 0, "connects": 0}
        for status, n, c in self.conn.execute(q + " GROUP BY status", args):
            out[status] = n
            if status == "sent":
                out["connects"] = c
        return out

    def ledger_rows(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM ledger ORDER BY id").fetchall()]


def _event_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["draft"] = json.loads(d.pop("draft_json")) if d.get("draft_json") else None
    return d


def _task_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["result"] = json.loads(d.pop("result_json")) if d.get("result_json") else None
    return d


def _row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["fields"] = json.loads(d.pop("fields_json") or "{}")
    d["draft"] = json.loads(d["draft_json"]) if d.get("draft_json") else None
    d.pop("draft_json", None)
    d["fit_tags"] = [t for t in (d.get("fit_tags") or "").split(",") if t]
    return d
