"""Persistent store for Max's tasks, test results, standup notes and briefing.

Phase 3.5 (2026-10-23): replaces the in-memory lists in server.py so nothing
is lost when max-brain restarts. One local SQLite file, gitignored
(default: <repo>/data/max.db, override with MAX_DB_PATH). The morning Cowork
test run reaches it through max-brain's HTTP endpoints, never the file.
"""
from __future__ import annotations

import datetime
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

_DEFAULT = Path(__file__).resolve().parent.parent / "data" / "max.db"
_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = Path(os.getenv("MAX_DB_PATH") or _DEFAULT)
        path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(path), check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript("""
            CREATE TABLE IF NOT EXISTS records (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                kind       TEXT NOT NULL,            -- task | result | note
                day        TEXT NOT NULL,            -- YYYY-MM-DD (local) it was created
                ticket_id  TEXT,
                data       TEXT NOT NULL             -- JSON
            );
            CREATE INDEX IF NOT EXISTS records_kind_day ON records(kind, day);
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
        """)
        _conn.commit()
    return _conn


def now_label() -> str:
    return time.strftime("%Y-%m-%d %H:%M IST")


def today() -> str:
    return datetime.date.today().isoformat()


def previous_working_day(d: Optional[datetime.date] = None) -> str:
    d = d or datetime.date.today()
    d -= datetime.timedelta(days=1)
    while d.weekday() >= 5:          # Sat/Sun -> Friday
        d -= datetime.timedelta(days=1)
    return d.isoformat()


def _add(kind: str, item: dict, ticket_id: Optional[str] = None) -> dict:
    with _lock:
        cur = _db().execute(
            "INSERT INTO records(kind, day, ticket_id, data) VALUES (?,?,?,?)",
            (kind, today(), ticket_id, json.dumps(item)))
        _db().commit()
    return {**item, "id": cur.lastrowid}


def _list(kind: str, since_day: Optional[str] = None, day: Optional[str] = None) -> list[dict]:
    q, args = "SELECT id, data FROM records WHERE kind=?", [kind]
    if day:
        q += " AND day=?"; args.append(day)
    elif since_day:
        q += " AND day>=?"; args.append(since_day)
    with _lock:
        rows = _db().execute(q + " ORDER BY id", args).fetchall()
    return [{**json.loads(d), "id": i} for i, d in rows]


# ── Tasks (tickets Max logged for testing) ────────────────────────────────────

def add_task(task: dict) -> dict:
    task = dict(task)
    task.setdefault("logged_at", now_label())
    task.setdefault("status", "pending")
    return _add("task", task, task.get("ticket_id"))


def list_tasks(status: Optional[str] = None) -> list[dict]:
    tasks = _list("task")
    return [t for t in tasks if not status or t.get("status") == status]


def mark_task_done(ticket_id: str) -> bool:
    """Mark the oldest pending task for this ticket as done."""
    with _lock:
        rows = _db().execute(
            "SELECT id, data FROM records WHERE kind='task' AND ticket_id=? ORDER BY id",
            (ticket_id,)).fetchall()
        for rid, data in rows:
            t = json.loads(data)
            if t.get("status") == "pending":
                t["status"] = "done"
                _db().execute("UPDATE records SET data=? WHERE id=?", (json.dumps(t), rid))
                _db().commit()
                return True
    return False


# ── Test results (posted by the morning Cowork test run) ──────────────────────

def add_result(result: dict) -> dict:
    result = dict(result)
    result.setdefault("posted_at", now_label())
    saved = _add("result", result, result.get("ticket_id"))
    if result.get("ticket_id"):
        mark_task_done(result["ticket_id"])
    return saved


def list_results(since_day: Optional[str] = None) -> list[dict]:
    return _list("result", since_day=since_day)


def delete_results(ticket_id: str) -> int:
    with _lock:
        cur = _db().execute(
            "DELETE FROM records WHERE kind='result' AND ticket_id=?", (ticket_id,))
        _db().commit()
    return cur.rowcount


# ── Standup notes (one set per day) ───────────────────────────────────────────

def add_note(note: dict) -> dict:
    return _add("note", dict(note))


def list_notes(day: Optional[str] = None) -> list[dict]:
    return _list("note", day=day or today())


def clear_notes(day: Optional[str] = None) -> int:
    with _lock:
        cur = _db().execute(
            "DELETE FROM records WHERE kind='note' AND day=?", (day or today(),))
        _db().commit()
    return cur.rowcount


# ── Briefing text ─────────────────────────────────────────────────────────────

def get_kv(key: str) -> str:
    with _lock:
        row = _db().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row[0] if row else ""


def set_kv(key: str, value: str) -> None:
    with _lock:
        _db().execute("INSERT INTO kv(key, value) VALUES(?, ?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        _db().commit()


def get_briefing() -> str:
    return get_kv("briefing")


def set_briefing(text: str) -> None:
    set_kv("briefing", text)
