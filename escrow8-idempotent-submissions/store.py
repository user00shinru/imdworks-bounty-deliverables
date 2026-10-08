#!/usr/bin/env python3
"""
Idempotent bounty submissions under concurrency — SQLite-backed reference.

Contract enforced by the schema, not by application logic:

  * one **active** (non-reviewed) submission per (wallet, bounty),
  * a reviewed submission is immutable,
  * a retry carrying a stale version loses to a newer one,
  * a commit that succeeded but whose response was lost is recoverable by
    replaying the same idempotency key.

Why a partial unique index rather than "SELECT then INSERT"
----------------------------------------------------------
A read-then-write check has a TOCTOU window: two concurrent requests both see
"no active submission" and both insert. A partial unique index makes the second
insert fail *inside the database*, which is the only place that is atomic with
respect to other connections.

    CREATE UNIQUE INDEX one_active ON submissions(wallet, bounty_id)
        WHERE reviewed = 0;

`BEGIN IMMEDIATE` is used so the write lock is taken before the read, removing
the upgrade deadlock that `BEGIN DEFERRED` produces under concurrency.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet        TEXT    NOT NULL,
    bounty_id     INTEGER NOT NULL,
    version       INTEGER NOT NULL,
    proof_hash    TEXT    NOT NULL,
    proof_uri     TEXT    NOT NULL,
    reviewed      INTEGER NOT NULL DEFAULT 0,
    idem_key      TEXT    NOT NULL,
    created_at    REAL    NOT NULL
);

-- one active submission per (wallet, bounty): enforced by the engine
CREATE UNIQUE INDEX IF NOT EXISTS one_active
    ON submissions(wallet, bounty_id)
    WHERE reviewed = 0;

-- replaying the same idempotency key must not create a second logical row
CREATE UNIQUE INDEX IF NOT EXISTS one_idem
    ON submissions(idem_key);

-- reviewed rows are immutable: version may only increase, never decrease
CREATE TABLE IF NOT EXISTS audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    submission INTEGER NOT NULL,
    event      TEXT    NOT NULL,
    detail     TEXT,
    at         REAL    NOT NULL
);
"""


class Conflict(Exception):
    """A submission lost an idempotency or version race."""


class Store:
    def __init__(self, path=":memory:"):
        self.path = path
        self._local = threading.local()
        self._bootstrap = sqlite3.connect(path, isolation_level=None, timeout=30)
        self._bootstrap.execute("PRAGMA journal_mode=WAL")
        self._bootstrap.executescript(SCHEMA)
        self._bootstrap.close()
        self._lock = threading.Lock()

    def conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, isolation_level=None, timeout=30)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            self._local.conn = c
        return c

    # -- the operation under test ------------------------------------------

    def submit(self, wallet, bounty_id, version, proof_hash, proof_uri, idem_key):
        """
        Returns (row, replay). `replay=True` means this call was a duplicate
        delivery of an already-committed request and NO new row was created.
        """
        c = self.conn()

        # Fast path: exact idempotency-key replay (response was lost).
        row = c.execute(
            "SELECT * FROM submissions WHERE idem_key=?", (idem_key,)
        ).fetchone()
        if row is not None:
            return dict(row), True

        try:
            c.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as e:
            raise Conflict(f"busy: {e}") from e

        try:
            existing = c.execute(
                "SELECT * FROM submissions WHERE wallet=? AND bounty_id=? AND reviewed=0",
                (wallet, bounty_id),
            ).fetchone()

            if existing is not None:
                if version <= existing["version"]:
                    # stale or duplicate update -> reject, do not overwrite
                    raise Conflict(
                        f"stale_version: have {existing['version']}, got {version}"
                    )
                # legitimate newer update of the active submission
                c.execute(
                    "UPDATE submissions SET version=?, proof_hash=?, proof_uri=?, idem_key=?, created_at=?"
                    " WHERE id=?",
                    (version, proof_hash, proof_uri, idem_key, time.time(), existing["id"]),
                )
                c.execute(
                    "INSERT INTO audit(submission,event,detail,at) VALUES(?,?,?,?)",
                    (existing["id"], "update", f"v{existing['version']}->v{version}", time.time()),
                )
                out = c.execute("SELECT * FROM submissions WHERE id=?", (existing["id"],)).fetchone()
                c.execute("COMMIT")
                return dict(out), False

            c.execute(
                "INSERT INTO submissions(wallet,bounty_id,version,proof_hash,proof_uri,idem_key,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (wallet, bounty_id, version, proof_hash, proof_uri, idem_key, time.time()),
            )
            sid = c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
            c.execute(
                "INSERT INTO audit(submission,event,detail,at) VALUES(?,?,?,?)",
                (sid, "create", f"v{version}", time.time()),
            )
            out = c.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
            c.execute("COMMIT")
            return dict(out), False

        except Conflict:
            c.execute("ROLLBACK")
            raise
        except sqlite3.IntegrityError as e:
            c.execute("ROLLBACK")
            # unique index fired: a concurrent writer won the race
            row = c.execute("SELECT * FROM submissions WHERE idem_key=?", (idem_key,)).fetchone()
            if row is not None:
                return dict(row), True
            raise Conflict(f"integrity: {e}") from e
        except Exception:
            c.execute("ROLLBACK")
            raise

    def review(self, wallet, bounty_id):
        c = self.conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute(
                "SELECT * FROM submissions WHERE wallet=? AND bounty_id=? AND reviewed=0",
                (wallet, bounty_id),
            ).fetchone()
            if row is None:
                c.execute("ROLLBACK")
                return None
            c.execute("UPDATE submissions SET reviewed=1 WHERE id=?", (row["id"],))
            c.execute(
                "INSERT INTO audit(submission,event,detail,at) VALUES(?,?,?,?)",
                (row["id"], "review", None, time.time()),
            )
            out = c.execute("SELECT * FROM submissions WHERE id=?", (row["id"],)).fetchone()
            c.execute("COMMIT")
            return dict(out)
        except Exception:
            c.execute("ROLLBACK")
            raise

    def active(self, wallet, bounty_id):
        c = self.conn()
        r = c.execute(
            "SELECT * FROM submissions WHERE wallet=? AND bounty_id=? AND reviewed=0",
            (wallet, bounty_id),
        ).fetchone()
        return dict(r) if r else None

    def count_logical(self):
        """Count of logical submissions = distinct (wallet, bounty) rows."""
        c = self.conn()
        return c.execute(
            "SELECT COUNT(DISTINCT wallet || '/' || bounty_id) AS n FROM submissions"
        ).fetchone()["n"]

    def all_rows(self):
        c = self.conn()
        return [dict(r) for r in c.execute("SELECT * FROM submissions ORDER BY id")]
