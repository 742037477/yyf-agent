"""Append-only, hash-chained event ledger (SQLite).

Every fact the kernel relies on is an event here. Nothing is updated or deleted.
Budget usage is *derived* from events, so it can never be "reset".
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import locks

GENESIS = "0" * 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id   TEXT NOT NULL,
    kind      TEXT NOT NULL,
    op_id     TEXT,
    payload   TEXT NOT NULL,
    ts        TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_task ON events(task_id, seq);
CREATE UNIQUE INDEX IF NOT EXISTS events_op ON events(task_id, kind, op_id) WHERE op_id IS NOT NULL;
"""


class LedgerError(RuntimeError):
    pass


@dataclass(frozen=True)
class Event:
    seq: int
    task_id: str
    kind: str
    op_id: str | None
    payload: dict[str, Any]
    ts: str
    prev_hash: str
    hash: str


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def event_hash(prev_hash: str, task_id: str, kind: str, op_id: str | None, payload: str, ts: str) -> str:
    body = _canonical([prev_hash, task_id, kind, op_id, payload, ts])
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


_event_hash = event_hash


class Ledger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), isolation_level=None, timeout=120)   # many concurrent runs (T90)
        self._db.executescript(SCHEMA)
        self._ledger_id = None

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc) -> None:
        self.close()   # Windows cannot delete or move an SQLite file that is still open

    # ---- write -------------------------------------------------------------
    def append(self, task_id: str, kind: str, payload: dict[str, Any] | None = None,
               op_id: str | None = None, *, guard: Callable[[], str | None] | None = None) -> Event:
        """Check an optional refusal guard under the write lock, rolling back on rejection."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            with self._db:
                if guard is not None and (reason := guard()) is not None:
                    raise LedgerError(reason)
                return self._append(task_id, kind, payload, op_id)
        except sqlite3.IntegrityError as e:
            raise LedgerError(f"duplicate operation {task_id}/{kind}/{op_id}") from e

    def append_then(self, task_id, kind, payload, then, *, guard=None):
        """Commit the event, then call then(event); both under one lock beside the ledger, so concurrent m2k runs
        call then (the OBS witness) in ledger order (T90). A crash in between leaves an event without a witness,
        never a witness of an event that was rolled back."""
        with nullcontext() if str(self.path) == ":memory:" else locks.mutex(self.path, timeout_s=None):
            event = self.append(task_id, kind, payload, guard=guard)
            return event, then(event)

    def _append(self, task_id: str, kind: str, payload: dict[str, Any] | None = None,
                op_id: str | None = None) -> Event:
        """Insert in the caller's transaction, using the same hash chain for every event."""
        payload_s = _canonical(payload or {})
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        cur = self._db.cursor()
        row = cur.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        prev = row[0] if row else GENESIS
        h = _event_hash(prev, task_id, kind, op_id, payload_s, ts)
        cur.execute(
            "INSERT INTO events(task_id, kind, op_id, payload, ts, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
            (task_id, kind, op_id, payload_s, ts, prev, h))
        return Event(cur.lastrowid, task_id, kind, op_id, json.loads(payload_s), ts, prev, h)

    def reserve(self, parent_id: str, child_id: str, amounts: dict[str, int],
                limits: dict[str, int | None]) -> bool:
        self._db.execute("BEGIN IMMEDIATE")
        with self._db:
            events = self.events(parent_id)
            reserved = {e.payload["child"]: e.payload["amounts"] for e in events if e.kind == "budget.reserved"}
            if child_id in reserved:
                return False
            released = {e.payload["child"] for e in events if e.kind == "budget.released"}
            committed = self.usage(parent_id)
            for child, budget in reserved.items():
                used = self.usage(child) if child in released else budget
                for key in limits:
                    committed[key] = committed.get(key, 0) + used.get(key, 0)
            short = {key: committed.get(key, 0) + amounts.get(key, 0) - limit
                     for key, limit in limits.items()
                     if limit is not None and committed.get(key, 0) + amounts.get(key, 0) > limit}
            self._append(parent_id, "budget.refused" if short else "budget.reserved",
                         {"child": child_id, "amounts": amounts, **({"short": short} if short else {})})
            return not short

    def release(self, parent_id: str, child_id: str) -> None:
        self._db.execute("BEGIN IMMEDIATE")
        with self._db:
            kinds = {e.kind for e in self.events(parent_id) if e.payload.get("child") == child_id}
            if "budget.reserved" in kinds and "budget.released" not in kinds:
                self._append(parent_id, "budget.released", {"child": child_id})

    # ---- read --------------------------------------------------------------
    @property
    def ledger_id(self) -> str | None:
        if self._ledger_id is None:
            row = self._db.execute("SELECT hash FROM events ORDER BY seq LIMIT 1").fetchone()
            if row:
                self._ledger_id = row[0]
        return self._ledger_id

    def events(self, task_id: str | None = None, kind: str | None = None) -> list[Event]:
        sql, args = "SELECT seq, task_id, kind, op_id, payload, ts, prev_hash, hash FROM events", []
        where = []
        if task_id is not None:
            where.append("task_id = ?"); args.append(task_id)
        if kind is not None:
            where.append("kind = ?"); args.append(kind)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY seq"
        return [Event(r[0], r[1], r[2], r[3], json.loads(r[4]), r[5], r[6], r[7])
                for r in self._db.execute(sql, args)]

    def last(self, task_id: str, kind: str) -> Event | None:
        evs = self.events(task_id, kind)
        return evs[-1] if evs else None

    def event(self, seq: int) -> Event | None:
        row = self._db.execute("SELECT seq, task_id, kind, op_id, payload, ts, prev_hash, hash "
                               "FROM events WHERE seq = ?", (seq,)).fetchone()
        return Event(row[0], row[1], row[2], row[3], json.loads(row[4]), row[5], row[6], row[7]) if row else None

    def task_ids(self) -> list[str]:
        return [r[0] for r in self._db.execute(
            "SELECT task_id FROM events GROUP BY task_id ORDER BY MIN(seq)")]

    def verify_chain(self) -> tuple[bool, int | None]:
        """Recompute every hash. Returns (ok, first_bad_seq)."""
        prev = GENESIS
        for r in self._db.execute(
                "SELECT seq, task_id, kind, op_id, payload, ts, prev_hash, hash FROM events ORDER BY seq"):
            seq, task_id, kind, op_id, payload, ts, prev_hash, h = r
            if prev_hash != prev or _event_hash(prev, task_id, kind, op_id, payload, ts) != h:
                return False, seq
            prev = h
        return True, None

    # ---- derived state -----------------------------------------------------
    def usage(self, task_id: str) -> dict[str, Any]:
        """Budget consumption derived from events. Unknown token usage is counted, never assumed zero."""
        u = {"rounds": 0, "model_calls": 0, "tokens": 0, "tokens_unknown_calls": 0, "commands": 0}
        for e in self.events(task_id):
            if e.kind == "model.called":
                u["model_calls"] += 1
                if e.payload.get("role") == "worker":
                    u["rounds"] += 1
                tin, tout = e.payload.get("input_tokens"), e.payload.get("output_tokens")
                if tin is None or tout is None:
                    u["tokens_unknown_calls"] += 1
                u["tokens_unknown_calls"] += e.payload.get("lost_attempts", 0)
                u["tokens"] += (tin or 0) + (tout or 0)
            elif e.kind == "effect.intent" and e.payload.get("tool") == "run_command":
                u["commands"] += 1   # counted at intent time: an attempted command is consumption
        return u
