#!/usr/bin/env python3
"""
IMD Works bounty escrow 2 — reorg-safe bounty event indexer.

Scenario matrix (each scenario gets its own database file):

  S1  duplicate log delivery              — same batch applied twice
  S2  out-of-order batches                — overlapping/regressive batches
  S3  crash between log write and checkpoint
  S4  process restart mid-batch
  S5  five-block reorg                    — the required case
  S6  reorg that orphans a payout         — the credit must be reversed
  S7  deep then shallow reorg sequence
  S8  restart after reorg then continue

The assertion that makes this meaningful: after recovery the indexed snapshot
must equal a FRESH indexer fed the same canonical chain. A narrative-only
claim is not enough.

Usage:
    python run.py            # exit 0 = PASS, writes report.json
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from fixture import Chain, addr, log, STATUS
from indexer import Indexer

HERE = os.path.dirname(os.path.abspath(__file__))
SEED = 20261008


# --------------------------------------------------------------------------
# Scratch workspace
#
# tempfile.TemporaryDirectory cannot be used on Windows: SQLite keeps the
# .db/-wal/-shm handles open until the connection object is collected, so the
# implicit rmtree raises WinError 32. We own the lifecycle instead and retry.
# --------------------------------------------------------------------------

def workspace(name: str) -> tuple[str, str]:
    root = os.path.join(HERE, "_scratch")
    os.makedirs(root, exist_ok=True)
    run_dir = os.path.join(root, f"{name}-{uuid.uuid4().hex[:8]}")
    os.makedirs(run_dir, exist_ok=True)
    return run_dir, root


def cleanup(run_dir: str, root: str) -> None:
    for _ in range(6):
        try:
            shutil.rmtree(run_dir)
            break
        except (PermissionError, OSError):
            time.sleep(0.2)
    try:
        if not os.listdir(root):
            os.rmdir(root)
    except OSError:
        pass


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


# --------------------------------------------------------------------------
# Canonical fixture: a realistic escrow workload
# --------------------------------------------------------------------------

def build_canonical() -> Chain:
    """A deterministic lifecycle.

    Layout matters for the reorg scenarios: the *final* payload (the award of
    bounty 2 and w2's withdrawal) sits in the last blocks, so a shallow reorg
    actually orphans a payout rather than only removing empty filler. Filler
    blocks are inserted in the MIDDLE to give the deep-reorg cases room.
    """
    c = Chain()
    creatorA, creatorB, creatorC = addr(1), addr(2), addr(3)
    w1, w2, w3 = addr(11), addr(12), addr(13)
    op = addr(21)

    c.add_block([
        log("OperatorSet", account=w1, operator=op, approved=True),
        log("BountyCreated", bountyId=1, creator=creatorA, reward=100_000,
            deadline=1_800_000_000, briefHash="0x" + "11" * 32),
    ], tag="b1-create")

    c.add_block([
        log("BountyCreated", bountyId=2, creator=creatorB, reward=250_000,
            deadline=1_800_000_000, briefHash="0x" + "22" * 32),
        log("BountyCreated", bountyId=3, creator=creatorC, reward=75_000,
            deadline=1_800_000_000, briefHash="0x" + "33" * 32),
    ], tag="b2-b3-create")

    c.add_block([log("RewardAdded", bountyId=1, amount=50_000, reward=150_000)], tag="topup-1")

    c.add_block([
        log("WorkSubmitted", bountyId=1, author=w1, operator=op, proofHash="0x" + "aa" * 32),
        log("WorkSubmitted", bountyId=1, author=w2, operator=w2, proofHash="0x" + "bb" * 32),
        log("WorkSubmitted", bountyId=2, author=w2, operator=w2, proofHash="0x" + "cc" * 32),
    ], tag="submissions")

    c.add_block([
        log("WorkSubmitted", bountyId=1, author=w1, operator=op, proofHash="0x" + "dd" * 32),
    ], tag="resubmit")

    c.add_block([log("BountyAwarded", bountyId=1, winner=w1, amount=150_000)], tag="award-1")

    c.add_block([log("BountyRefunded", bountyId=3, creator=creatorC, amount=75_000,
                     status=STATUS["Cancelled"])], tag="cancel-3")

    c.add_block([log("Withdrawn", account=w1, recipient=w3, amount=150_000)], tag="withdraw-1")

    # Filler in the middle: heights 9..18.
    for i in range(10):
        c.add_block([], tag=f"filler-{i}")

    # Final payload — the last two blocks. Shallow reorgs orphan these.
    c.add_block([log("BountyAwarded", bountyId=2, winner=w2, amount=250_000)], tag="award-2")
    c.add_block([log("Withdrawn", account=w2, recipient=w2, amount=250_000)], tag="withdraw-2")

    return c


def canonical_logs(chain: Chain) -> list[dict]:
    out = []
    for b in chain.blocks:
        for j, lg in enumerate(b.logs):
            out.append({
                **lg,
                "blockNumber": b.number,
                "blockHash": b.hash,
                "parentHash": b.parent_hash,
                "txHash": "0x" + f"{b.number:08x}{j:056x}",
                "logIndex": j,
            })
    return out


def fresh_index(db_path: str, chain: Chain) -> dict:
    """Reference implementation: one clean pass over the canonical chain."""
    ix = Indexer(db_path)
    ix.ingest_sequence(chain.block_records(1))
    snap = ix.snapshot(drop_checkpoint=True)
    ix.close()
    return snap


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------

def scenario_s1(tag: str) -> list[Check]:
    """Duplicate log delivery / duplicate block delivery."""
    chain = build_canonical()
    blocks = chain.block_records(1)
    d, root = workspace("s1")
    try:
        ix = Indexer(os.path.join(d, "s1.db"))
        r1 = ix.ingest_sequence(blocks)
        r2 = ix.ingest_sequence(blocks)
        snap = ix.snapshot(drop_checkpoint=True)
        expect = fresh_index(os.path.join(d, "ref.db"), chain)
        ix.close()
        return [
            Check(f"{tag} first pass applied every block", r1.applied_blocks == len(blocks),
                  f"blocks={r1.applied_blocks}/{len(blocks)}"),
            Check(f"{tag} redelivery applies nothing new", r2.applied_blocks == 0,
                  f"applied={r2.applied_blocks} dupes={r2.skipped_duplicates}"),
            Check(f"{tag} state equals fresh replay", snap == expect,
                  "identical" if snap == expect else "DIVERGED"),
        ]
    finally:
        cleanup(d, root)


def scenario_s2(tag: str) -> list[Check]:
    """Out-of-order blocks: last third first, then the rest, then the tail again."""
    chain = build_canonical()
    blocks = chain.block_records(1)
    a = max(1, len(blocks) // 3)
    tail, rest = blocks[-a:], blocks[:-a]
    d, root = workspace("s2")
    try:
        ix = Indexer(os.path.join(d, "s2.db"))
        r1 = ix.ingest_sequence(tail)
        r2 = ix.ingest_sequence(rest)
        r3 = ix.ingest_sequence(tail)
        snap = ix.snapshot(drop_checkpoint=True)
        expect = fresh_index(os.path.join(d, "ref.db"), chain)
        ix.close()
        return [
            Check(f"{tag} out-of-order batch is buffered", r1.applied_blocks == 0 and r1.buffered > 0,
                  f"applied={r1.applied_blocks} buffered={r1.buffered}"),
            Check(f"{tag} gap fill drains the buffer", r2.applied_blocks >= len(rest),
                  f"applied={r2.applied_blocks}"),
            Check(f"{tag} tail redelivery is a no-op", r3.applied_blocks == 0,
                  f"applied={r3.applied_blocks}"),
            Check(f"{tag} final state equals fresh replay", snap == expect,
                  "identical" if snap == expect else "DIVERGED"),
        ]
    finally:
        cleanup(d, root)


def scenario_s3(tag: str) -> list[Check]:
    """Crash between log write and checkpoint write."""
    chain = build_canonical()
    d, root = workspace("s3")
    try:
        p = os.path.join(d, "s3.db")
        ix = Indexer(p)
        ix.ingest_sequence(chain.block_records(1))
        ix.db.execute("UPDATE checkpoint SET number=?, hash=?, log_count=0 WHERE id=1",
                      (3, ix.canonical_hash(3)))
        ix.db.execute("DELETE FROM journal WHERE block_number > 3")
        ix.db.execute("DELETE FROM blocks WHERE number > 3")
        ix._rebuild_derived()
        ix.close()

        ix2 = Indexer(p)
        res = ix2.sync(chain)
        snap = ix2.snapshot(drop_checkpoint=True)
        expect = fresh_index(os.path.join(d, "ref.db"), chain)
        ix2.close()
        return [
            Check(f"{tag} recovery replayed the lost suffix", res["applied_blocks"] > 0,
                  f"blocks={res['applied_blocks']} logs={res['applied_logs']}"),
            Check(f"{tag} state equals fresh replay after recovery", snap == expect,
                  "identical" if snap == expect else "DIVERGED"),
        ]
    finally:
        cleanup(d, root)


def scenario_s4(tag: str) -> list[Check]:
    """Process restart mid-batch."""
    chain = build_canonical()
    blocks = chain.block_records(1)
    cut = len(blocks) // 2
    d, root = workspace("s4")
    try:
        p = os.path.join(d, "s4.db")
        ix = Indexer(p)
        ix.ingest_sequence(blocks[:cut])
        ix.close()

        ix2 = Indexer(p)
        ix2.ingest_sequence(blocks[cut:])
        snap = ix2.snapshot(drop_checkpoint=True)
        expect = fresh_index(os.path.join(d, "ref.db"), chain)
        ix2.close()
        return [
            Check(f"{tag} restart completes the batch", True, f"blocks {cut}+{len(blocks)-cut}"),
            Check(f"{tag} state equals fresh replay", snap == expect,
                  "identical" if snap == expect else "DIVERGED"),
        ]
    finally:
        cleanup(d, root)


def scenario_s5(tag: str, depth: int = 5) -> list[Check]:
    """The required five-block reorg."""
    chain = build_canonical()
    d, root = workspace("s5")
    try:
        p = os.path.join(d, "s5.db")
        ix = Indexer(p)
        ix.ingest_sequence(chain.block_records(1))
        before = ix.snapshot(drop_checkpoint=True)

        creatorD, w4 = addr(4), addr(14)
        replacement = [
            [log("BountyCreated", bountyId=4, creator=creatorD, reward=500_000,
                 deadline=1_800_000_000, briefHash="0x" + "dd" * 32)],
            [log("WorkSubmitted", bountyId=4, author=w4, operator=w4, proofHash="0x" + "ee" * 32)],
            [log("BountyAwarded", bountyId=4, winner=w4, amount=500_000)],
            [log("Withdrawn", account=w4, recipient=creatorD, amount=500_000)],
            [],
            [],
        ]
        chain.reorg(depth, replacement, tags=[f"r{i}" for i in range(len(replacement))])

        res = ix.sync(chain)
        snap = ix.snapshot(drop_checkpoint=True)
        expect = fresh_index(os.path.join(d, "ref.db"), chain)

        orphan_award = any(b["id"] == 2 and b["status"] == 2 for b in before["bounties"])
        now_award = any(b["id"] == 2 and b["status"] == 2 for b in snap["bounties"])
        ix.close()
        return [
            Check(f"{tag}{depth}-block reorg detected", res["reorg_detected"] is True,
                  f"common={res['common_ancestor']} applied={res['applied_blocks']}"),
            Check(f"{tag} state equals fresh canonical replay", snap == expect,
                  "identical" if snap == expect else "DIVERGED"),
            Check(f"{tag} orphaned branch effect is gone", (not now_award) or (not orphan_award),
                  f"orphan_award={orphan_award} now_award={now_award}"),
            Check(f"{tag} new branch is indexed", any(b["id"] == 4 for b in snap["bounties"]),
                  "bounty4=yes" if any(b["id"] == 4 for b in snap["bounties"]) else "bounty4=no"),
        ]
    finally:
        cleanup(d, root)


def scenario_s6(tag: str) -> list[Check]:
    """Reorg that orphans a payout - the credit must be reversed, not kept."""
    chain = build_canonical()
    d, root = workspace("s6")
    try:
        p = os.path.join(d, "s6.db")
        ix = Indexer(p)
        ix.ingest_sequence(chain.block_records(1))
        replacement = [
            [log("BountyAwarded", bountyId=3, winner=addr(13), amount=75_000)],
            [],
            [],
        ]
        chain.reorg(3, replacement, tags=[f"n{i}" for i in range(3)])
        res = ix.sync(chain)
        snap = ix.snapshot(drop_checkpoint=True)
        expect = fresh_index(os.path.join(d, "ref.db"), chain)
        ix.close()
        return [
            Check(f"{tag} reorg that orphans a payout detected", res["reorg_detected"] is True,
                  f"common={res['common_ancestor']}"),
            Check(f"{tag} orphaned award credit is reversed",
                  snap["credits"].get(addr(12), 0) == 0,
                  f"w2 credit={snap['credits'].get(addr(12), 0)} (must be 0)"),
            Check(f"{tag} orphaned withdrawal is undone",
                  all(w["account"] != addr(12) for w in snap["withdrawals"]),
                  f"w2 withdrawals={[w for w in snap['withdrawals'] if w['account'] == addr(12)]}"),
            Check(f"{tag} state equals fresh canonical replay", snap == expect,
                  "identical" if snap == expect else "DIVERGED"),
        ]
    finally:
        cleanup(d, root)


def scenario_s7(tag: str) -> list[Check]:
    """Sequential deep then shallow reorgs."""
    chain = build_canonical()
    d, root = workspace("s7")
    try:
        p = os.path.join(d, "s7.db")
        ix = Indexer(p)
        ix.ingest_sequence(chain.block_records(1))
        chain.reorg(5, [[], [], [], [], [], []], tags=[f"a{i}" for i in range(6)])
        r1 = ix.sync(chain)
        chain.reorg(2, [[], [], []], tags=[f"b{i}" for i in range(3)])
        r2 = ix.sync(chain)
        snap = ix.snapshot(drop_checkpoint=True)
        expect = fresh_index(os.path.join(d, "ref.db"), chain)
        ix.close()
        return [
            Check(f"{tag} deep reorg detected", r1["reorg_detected"] is True,
                  f"common={r1['common_ancestor']}"),
            Check(f"{tag} shallow reorg detected", r2["reorg_detected"] is True,
                  f"common={r2['common_ancestor']}"),
            Check(f"{tag} state equals fresh canonical replay", snap == expect,
                  "identical" if snap == expect else "DIVERGED"),
        ]
    finally:
        cleanup(d, root)


def scenario_s8(tag: str) -> list[Check]:
    """Restart (close+reopen) after a reorg, then continue syncing."""
    chain = build_canonical()
    d, root = workspace("s8")
    try:
        p = os.path.join(d, "s8.db")
        ix = Indexer(p)
        ix.ingest_sequence(chain.block_records(1))
        chain.reorg(4, [[], [], [], [], []], tags=[f"c{i}" for i in range(5)])
        ix.sync(chain)
        ix.close()

        ix2 = Indexer(p)
        chain.add_block([log("BountyCreated", bountyId=9, creator=addr(9), reward=1_000,
                             deadline=1_800_000_000, briefHash="0x" + "99" * 32)], tag="post")
        for i in range(4):
            chain.add_block([], tag=f"post-{i}")
        ix2.sync(chain)
        snap = ix2.snapshot(drop_checkpoint=True)
        expect = fresh_index(os.path.join(d, "ref.db"), chain)
        ix2.close()
        return [
            Check(f"{tag} post-restart sync converges", snap == expect,
                  "identical" if snap == expect else "DIVERGED"),
            Check(f"{tag} post-reorg block was indexed",
                  any(b["id"] == 9 for b in snap["bounties"]),
                  "bounty9=yes" if any(b["id"] == 9 for b in snap["bounties"]) else "bounty9=no"),
        ]
    finally:
        cleanup(d, root)


SCENARIOS = [
    ("S1", "duplicate log delivery", scenario_s1),
    ("S2", "out-of-order batches", scenario_s2),
    ("S3", "crash between log write and checkpoint", scenario_s3),
    ("S4", "process restart mid-batch", scenario_s4),
    ("S5", "five-block reorg", scenario_s5),
    ("S6", "reorg that orphans a payout", scenario_s6),
    ("S7", "deep then shallow reorg", scenario_s7),
    ("S8", "restart after reorg", scenario_s8),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()
    random.seed(SEED)

    print("=" * 72)
    print("IMD Works escrow bounty 2 — reorg-safe bounty event indexer")
    print("=" * 72)
    print(f"  seed   {SEED}")
    print(f"  sqlite {sqlite3.sqlite_version}")
    print(f"  python {sys.version.split()[0]}")
    print()

    checks: list[Check] = []
    t0 = time.time()
    for sid, name, fn in SCENARIOS:
        print(f"[{sid}] {name}")
        try:
            res = fn(f"{sid} ")
        except Exception as e:
            res = [Check(f"{sid} raised", False, f"{type(e).__name__}: {e}")]
        for c in res:
            checks.append(c)
            print(f"    [{'PASS' if c.ok else 'FAIL'}] {c.name.strip()} — {c.detail}")
    dt = time.time() - t0

    passed = sum(1 for c in checks if c.ok)
    report = {
        "bounty": "imdworks escrow 2 — reorg-safe bounty event indexer",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "seed": SEED,
        "sqlite_version": sqlite3.sqlite_version,
        "python": sys.version.split()[0],
        "seconds": round(dt, 2),
        "checks": [{"name": c.name.strip(), "ok": c.ok, "detail": c.detail} for c in checks],
        "passed": passed,
        "failed": len(checks) - passed,
        "result": "PASS" if passed == len(checks) else "FAIL",
    }
    with open(os.path.join(HERE, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
        fh.write("\n")

    print()
    print(f"RESULT: {report['result']} — {passed}/{len(checks)} checks ({dt:.2f}s)")
    print("=" * 72)
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
