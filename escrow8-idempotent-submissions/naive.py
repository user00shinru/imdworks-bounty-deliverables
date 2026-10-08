#!/usr/bin/env python3
"""
Naive (broken) implementation — kept as the control the harness must reject.

This is the shape most people write first:

    row = SELECT * FROM submissions WHERE wallet=? AND bounty_id=?
    if row is None:
        INSERT ...
    else:
        UPDATE ...

It opens a fresh connection per request (the usual request-scoped session),
runs the read and the write **outside** any transaction, has **no unique
index**, and overwrites reviewed rows. The workload in run.py drives it into
those failure modes so the tests can prove the *reference* store does better
rather than just asserting it on faith.

This file exists only to be wrong in a controlled way.
"""
from __future__ import annotations

import sqlite3
import time

NAIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet     TEXT    NOT NULL,
    bounty_id  INTEGER NOT NULL,
    version    INTEGER NOT NULL,
    proof_hash TEXT    NOT NULL,
    proof_uri  TEXT    NOT NULL,
    reviewed   INTEGER NOT NULL DEFAULT 0,
    idem_key   TEXT    NOT NULL,
    created_at REAL    NOT NULL
);
"""


class NaiveStore:
    def __init__(self, path=":memory:"):
        self.path = path
        c = sqlite3.connect(path, isolation_level=None, timeout=30)
        c.executescript(NAIVE_SCHEMA)
        c.close()

    def _conn(self):
        """A fresh connection per request, as an unpooled service would open."""
        c = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    def submit(self, wallet, bounty_id, version, proof_hash, proof_uri, idem_key):
        c = self._conn()
        try:
            # deliberately no transaction and no unique index
            row = c.execute(
                "SELECT * FROM submissions WHERE wallet=? AND bounty_id=? AND reviewed=0",
                (wallet, bounty_id),
            ).fetchone()
            time.sleep(0.0005)  # widen the TOCTOU window, as real RPC latency does
            if row is None:
                c.execute(
                    "INSERT INTO submissions(wallet,bounty_id,version,proof_hash,proof_uri,idem_key,created_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (wallet, bounty_id, version, proof_hash, proof_uri, idem_key, time.time()),
                )
                sid = c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
            else:
                sid = row["id"]
                # blindly overwrites — including a reviewed submission
                c.execute(
                    "UPDATE submissions SET version=?, proof_hash=?, proof_uri=? WHERE id=?",
                    (version, proof_hash, proof_uri, sid),
                )
            out = c.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
            return dict(out), False
        finally:
            c.close()

    def review(self, wallet, bounty_id):
        c = self._conn()
        try:
            row = c.execute(
                "SELECT * FROM submissions WHERE wallet=? AND bounty_id=?",
                (wallet, bounty_id),
            ).fetchone()
            if row is None:
                return None
            c.execute("UPDATE submissions SET reviewed=1 WHERE id=?", (row["id"],))
            return dict(c.execute("SELECT * FROM submissions WHERE id=?", (row["id"],)).fetchone())
        finally:
            c.close()

    def all_rows(self):
        c = self._conn()
        try:
            return [dict(r) for r in c.execute("SELECT * FROM submissions ORDER BY id")]
        finally:
            c.close()
