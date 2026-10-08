#!/usr/bin/env python3
"""
Reorg-safe bounty event indexer.

Reconstructs bounty state (BountyCreated / RewardAdded / WorkSubmitted /
BountyAwarded / BountyRefunded / Withdrawn) from canonical blocks and persists
a checkpoint that ties a block number to its *hash*.

Design properties that matter for this bounty:

1. ORDER INDEPENDENCE. The indexer ingests *blocks by height*, not log batches.
   A block that arrives before its predecessor is buffered; it is applied only
   once every lower height is present. Consequence: any permutation of the same
   block set produces byte-identical derived state. Blocks with no logs still
   count as heights, so gaps are real gaps.

2. EXACTLY-ONCE EFFECT. Every applied log is journalled under a UNIQUE
   `(tx_hash, log_index)`. Redelivery is a no-op. A block whose hash is already
   recorded at its height is a no-op too.

3. ATOMIC CHECKPOINT. The checkpoint `(number, hash, log_count)` is written in
   the SAME transaction as the state it describes, so a crash can never leave
   effects committed while the checkpoint lags (or vice versa).

4. ONE RECOVERY PATH. Derived tables are a pure function of the journal. A
   reorg is handled by deleting journal rows above the fork point and replaying
   — the identical code path used for a fresh index. Nothing can drift.

Recovery is therefore provably equal to fresh canonical replay: both are
`reduce(apply_event, journal)`.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS journal (
    tx_hash      TEXT NOT NULL,
    log_index    INTEGER NOT NULL,
    block_number INTEGER NOT NULL,
    block_hash   TEXT NOT NULL,
    payload      TEXT NOT NULL,
    PRIMARY KEY (tx_hash, log_index)
);
CREATE INDEX IF NOT EXISTS journal_by_block ON journal(block_number);

CREATE TABLE IF NOT EXISTS blocks (
    number      INTEGER PRIMARY KEY,
    hash        TEXT NOT NULL,
    parent_hash TEXT NOT NULL
);

-- buffered blocks whose height is ahead of the contiguous head
CREATE TABLE IF NOT EXISTS pending_blocks (
    number      INTEGER PRIMARY KEY,
    hash        TEXT NOT NULL,
    parent_hash TEXT NOT NULL,
    payload     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checkpoint (
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    number    INTEGER NOT NULL,
    hash      TEXT NOT NULL,
    log_count INTEGER NOT NULL
);

-- derived --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bounties (
    id          INTEGER PRIMARY KEY,
    creator     TEXT NOT NULL,
    reward      INTEGER NOT NULL,
    deadline    INTEGER NOT NULL,
    submissions INTEGER NOT NULL DEFAULT 0,
    status      INTEGER NOT NULL DEFAULT 1,
    winner      TEXT,
    brief_hash  TEXT
);

CREATE TABLE IF NOT EXISTS submission_authors (
    bounty_id  INTEGER NOT NULL,
    author     TEXT NOT NULL,
    proof_hash TEXT NOT NULL,
    operator   TEXT,
    PRIMARY KEY (bounty_id, author)
);

CREATE TABLE IF NOT EXISTS credits (
    account TEXT PRIMARY KEY,
    amount  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS withdrawals (
    tx_hash   TEXT NOT NULL,
    log_index INTEGER NOT NULL,
    account   TEXT NOT NULL,
    recipient TEXT NOT NULL,
    amount    INTEGER NOT NULL,
    PRIMARY KEY (tx_hash, log_index)
);
"""

GENESIS = "0x" + "00" * 32


@dataclass
class ApplyResult:
    applied_blocks: int = 0
    applied_logs: int = 0
    skipped_duplicates: int = 0
    buffered: int = 0
    dropped_stale: int = 0


class Indexer:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        fresh = not os.path.exists(db_path)
        self.db = sqlite3.connect(db_path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript(SCHEMA)
        if fresh:
            self.db.execute(
                "INSERT INTO checkpoint (id, number, hash, log_count) VALUES (1, 0, ?, 0)",
                (GENESIS,),
            )

    # ------------------------------------------------------------ txn glue

    def _begin(self) -> None:
        self.db.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self.db.execute("COMMIT")

    def _rollback(self) -> None:
        self.db.execute("ROLLBACK")

    # ----------------------------------------------------------- checkpoint

    def checkpoint(self) -> tuple[int, str, int]:
        n, h, c = self.db.execute(
            "SELECT number, hash, log_count FROM checkpoint WHERE id=1"
        ).fetchone()
        return int(n), str(h), int(c)

    def _set_checkpoint(self, number: int, hash_: str, log_count: int) -> None:
        self.db.execute(
            "UPDATE checkpoint SET number=?, hash=?, log_count=? WHERE id=1",
            (number, hash_, log_count),
        )

    def canonical_hash(self, number: int) -> str:
        if number <= 0:
            return GENESIS
        row = self.db.execute("SELECT hash FROM blocks WHERE number=?", (number,)).fetchone()
        return str(row[0]) if row else GENESIS

    def height(self) -> int:
        row = self.db.execute("SELECT MAX(number) FROM blocks").fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def _log_count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM journal").fetchone()[0])

    # --------------------------------------------------------------- ingest

    def ingest_block(self, block: dict) -> ApplyResult:
        """Ingest one canonical block record: {number, hash, parent_hash, logs}.

        A block is applied immediately when its height is exactly one past the
        contiguous head; otherwise it is buffered. After each application the
        buffer is drained for as long as the next contiguous height is present.
        This is what makes ingestion order-independent.
        """
        res = ApplyResult()
        self._begin()
        try:
            n = int(block["number"])
            h = str(block["hash"])

            if n <= self.height():
                # Already have this height. Same hash → duplicate; different →
                # the caller must call sync() to roll back. Ignore here.
                if self.canonical_hash(n) == h:
                    res.skipped_duplicates += 1
                else:
                    res.dropped_stale += 1
                self._commit()
                return res

            if n == self.height() + 1:
                self._apply_block(block)
                res.applied_blocks += 1
                res.applied_logs += len(block.get("logs", []))
                res = self._drain_buffer(res)
            else:
                # Out of order: buffer until the gap fills.
                self.db.execute(
                    "INSERT OR REPLACE INTO pending_blocks(number, hash, parent_hash, payload) VALUES (?,?,?,?)",
                    (n, h, str(block.get("parent_hash", GENESIS)), json.dumps(block, sort_keys=True)),
                )
                res.buffered += 1

            head = self.db.execute(
                "SELECT number, hash FROM blocks ORDER BY number DESC LIMIT 1"
            ).fetchone()
            if head:
                self._set_checkpoint(int(head[0]), str(head[1]), self._log_count())
            self._commit()
        except Exception:
            self._rollback()
            raise
        return res

    def _drain_buffer(self, res: ApplyResult) -> ApplyResult:
        while True:
            nxt = self.height() + 1
            row = self.db.execute(
                "SELECT payload FROM pending_blocks WHERE number=?", (nxt,)
            ).fetchone()
            if not row:
                break
            blk = json.loads(row[0])
            self._apply_block(blk)
            self.db.execute("DELETE FROM pending_blocks WHERE number=?", (nxt,))
            res.applied_blocks += 1
            res.applied_logs += len(blk.get("logs", []))
        return res

    def ingest_sequence(self, blocks: Iterable[dict]) -> ApplyResult:
        total = ApplyResult()
        for b in blocks:
            r = self.ingest_block(b)
            total.applied_blocks += r.applied_blocks
            total.applied_logs += r.applied_logs
            total.skipped_duplicates += r.skipped_duplicates
            total.buffered += r.buffered
            total.dropped_stale += r.dropped_stale
        return total

    def _apply_block(self, block: dict) -> None:
        n, h = int(block["number"]), str(block["hash"])
        ph = str(block.get("parent_hash", GENESIS))
        self.db.execute(
            "INSERT INTO blocks(number, hash, parent_hash) VALUES (?,?,?) "
            "ON CONFLICT(number) DO UPDATE SET hash=excluded.hash, parent_hash=excluded.parent_hash",
            (n, h, ph),
        )
        for lg in block.get("logs", []):
            tx, li = str(lg["txHash"]), int(lg["logIndex"])
            dup = self.db.execute(
                "SELECT 1 FROM journal WHERE tx_hash=? AND log_index=?", (tx, li)
            ).fetchone()
            if dup:
                continue
            self.db.execute(
                "INSERT INTO journal(tx_hash, log_index, block_number, block_hash, payload) VALUES (?,?,?,?,?)",
                (tx, li, n, h, json.dumps({**lg, "blockNumber": n, "blockHash": h}, sort_keys=True)),
            )
            self._apply_event({**lg, "blockNumber": n, "blockHash": h})

    # ------------------------------------------------------------- recovery

    def journal_rows(self) -> list[dict]:
        return [
            {"block_number": r[0], "block_hash": r[1], "tx_hash": r[2],
             "log_index": r[3], "payload": json.loads(r[4])}
            for r in self.db.execute(
                "SELECT block_number, block_hash, tx_hash, log_index, payload "
                "FROM journal ORDER BY block_number, log_index"
            ).fetchall()
        ]

    def _rebuild_derived(self) -> None:
        self.db.execute("DELETE FROM bounties")
        self.db.execute("DELETE FROM submission_authors")
        self.db.execute("DELETE FROM credits")
        self.db.execute("DELETE FROM withdrawals")
        for row in self.journal_rows():
            self._apply_event(row["payload"])

    def rollback_to(self, height: int) -> int:
        """Drop everything above `height` (blocks, journal, buffer) and rebuild."""
        self._begin()
        try:
            removed = int(self.db.execute(
                "SELECT COUNT(*) FROM blocks WHERE number > ?", (height,)
            ).fetchone()[0])
            self.db.execute("DELETE FROM blocks WHERE number > ?", (height,))
            self.db.execute("DELETE FROM journal WHERE block_number > ?", (height,))
            self.db.execute("DELETE FROM pending_blocks WHERE number > ?", (height,))
            self._rebuild_derived()
            self._set_checkpoint(height, self.canonical_hash(height), self._log_count())
            self._commit()
        except Exception:
            self._rollback()
            raise
        return removed

    def sync(self, chain) -> dict:
        """Reconcile with `chain`: detect a reorg, roll back, then catch up.

        A reorg is detected by comparing the stored hash at every height that
        both sides share. Any mismatch means the stored suffix is orphaned.
        Crucially, this walks block hashes — so a reorg that only removes EMPTY
        blocks is detected too, which a log-only comparison would miss.
        """
        stored_max = self.height()
        chain_blocks = chain.block_records(1)

        reorged = False
        common = 0
        for blk in chain_blocks:
            n, h = int(blk["number"]), str(blk["hash"])
            if n > stored_max:
                break
            if self.canonical_hash(n) != h:
                reorged = True
                break
            common = n

        if reorged:
            self.rollback_to(common)

        # Catch up on everything above the (possibly rolled back) head.
        start = self.height() + 1
        pending = [b for b in chain_blocks if int(b["number"]) >= start]
        res = self.ingest_sequence(pending)
        return {
            "reorg_detected": reorged,
            "common_ancestor": common,
            "applied_blocks": res.applied_blocks,
            "applied_logs": res.applied_logs,
            "duplicates_skipped": res.skipped_duplicates,
            "buffered": res.buffered,
        }

    # -------------------------------------------------------------- events

    def _apply_event(self, lg: dict) -> None:
        ev = lg["event"]
        if ev == "BountyCreated":
            self.db.execute(
                "INSERT OR REPLACE INTO bounties(id, creator, reward, deadline, submissions, status, winner, brief_hash) "
                "VALUES (?,?,?,?,0,1,NULL,?)",
                (lg["bountyId"], lg["creator"], lg["reward"], lg["deadline"], lg.get("briefHash")),
            )
        elif ev == "RewardAdded":
            self.db.execute("UPDATE bounties SET reward = reward + ? WHERE id = ?",
                            (lg["amount"], lg["bountyId"]))
        elif ev == "WorkSubmitted":
            self.db.execute(
                "INSERT INTO submission_authors(bounty_id, author, proof_hash, operator) VALUES (?,?,?,?) "
                "ON CONFLICT(bounty_id, author) DO UPDATE SET proof_hash=excluded.proof_hash, operator=excluded.operator",
                (lg["bountyId"], lg["author"], lg["proofHash"], lg.get("operator")),
            )
            self.db.execute(
                "UPDATE bounties SET submissions=(SELECT COUNT(*) FROM submission_authors WHERE bounty_id=?) WHERE id=?",
                (lg["bountyId"], lg["bountyId"]),
            )
        elif ev == "BountyAwarded":
            self.db.execute("UPDATE bounties SET status=2, winner=? WHERE id=?", (lg["winner"], lg["bountyId"]))
            self.db.execute(
                "INSERT INTO credits(account, amount) VALUES (?,?) "
                "ON CONFLICT(account) DO UPDATE SET amount = amount + excluded.amount",
                (lg["winner"], lg["amount"]),
            )
        elif ev == "BountyRefunded":
            self.db.execute("UPDATE bounties SET status=? WHERE id=?", (lg["status"], lg["bountyId"]))
            self.db.execute(
                "INSERT INTO credits(account, amount) VALUES (?,?) "
                "ON CONFLICT(account) DO UPDATE SET amount = amount + excluded.amount",
                (lg["creator"], lg["amount"]),
            )
        elif ev == "Withdrawn":
            self.db.execute(
                "INSERT OR IGNORE INTO withdrawals(tx_hash, log_index, account, recipient, amount) VALUES (?,?,?,?,?)",
                (lg["txHash"], int(lg["logIndex"]), lg["account"], lg["recipient"], lg["amount"]),
            )
            self.db.execute("UPDATE credits SET amount = amount - ? WHERE account = ?",
                            (lg["amount"], lg["account"]))
        elif ev == "OperatorSet":
            pass
        else:
            raise ValueError(f"unknown event {ev}")

    # ------------------------------------------------------------ snapshot

    def snapshot(self, drop_checkpoint: bool = False) -> dict:
        bounties = [
            {"id": r[0], "creator": r[1], "reward": r[2], "deadline": r[3],
             "submissions": r[4], "status": r[5], "winner": r[6]}
            for r in self.db.execute(
                "SELECT id, creator, reward, deadline, submissions, status, winner FROM bounties ORDER BY id"
            ).fetchall()
        ]
        credits = {r[0]: r[1] for r in self.db.execute("SELECT account, amount FROM credits ORDER BY account")}
        withdrawals = [
            {"account": r[0], "recipient": r[1], "amount": r[2]}
            for r in self.db.execute("SELECT account, recipient, amount FROM withdrawals ORDER BY account, amount")
        ]
        out: dict[str, Any] = {"bounties": bounties, "credits": credits, "withdrawals": withdrawals}
        if not drop_checkpoint:
            n, h, c = self.checkpoint()
            out["checkpoint"] = {"number": n, "hash": h, "log_count": c}
        return out

    def close(self) -> None:
        self.db.close()
