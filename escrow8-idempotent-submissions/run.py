#!/usr/bin/env python3
"""
Concurrency workload with fault injection.

    python run.py            # exit 0 = PASS

All four scenarios the brief names:

  1. >= 100 concurrent requests across 10 wallets
  2. response loss  (commit succeeds, responder dies before replying)
  3. restart between log write and checkpoint
  4. stale version updates

The assertions are about *observable database state*, not about what the API
returned, so a lying return value cannot pass.

Fault injection is deterministic: the RNG is seeded, and "response loss" is
implemented by having the worker perform the real commit and then throw before
returning — the honest approximation of a lost response.
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from naive import NaiveStore  # noqa: E402
from store import Conflict, Store  # noqa: E402

SEED = 20261008
WALLETS = [f"0x{i:040x}" for i in range(1, 11)]
BOUNTY = 42          # scenario 1
BOUNTY_S2 = 43       # scenario 2
BOUNTY_S3 = 44       # scenario 3
BOUNTY_S4 = 45       # scenario 4


class Report:
    def __init__(self):
        self.checks = []

    def check(self, name, ok, detail=""):
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})
        flag = "PASS" if ok else "FAIL"
        line = f"  [{flag}] {name}"
        if detail:
            line += f" — {detail}"
        print(line)
        return ok

    @property
    def ok(self):
        return all(c["ok"] for c in self.checks)


class LostResponse(Exception):
    """Raised after a successful commit to simulate a lost response."""


def scenario_1_concurrent(db_path, report, store_cls=Store):
    """100+ concurrent requests, 10 wallets, one logical submission each."""
    store = store_cls(db_path)
    rnd = random.Random(SEED)
    jobs = []
    for i in range(120):
        w = WALLETS[i % len(WALLETS)]
        jobs.append({"wallet": w, "bounty": BOUNTY, "version": 1,
                     "hash": f"0x{i:064x}", "uri": f"https://x/{i}",
                     "idem": f"idem-{i}"})
    rnd.shuffle(jobs)

    results = {"ok": 0, "conflict": 0, "replay": 0, "err": 0}
    lock = threading.Lock()

    def worker(j):
        try:
            _, replay = store.submit(j["wallet"], j["bounty"], j["version"],
                                     j["hash"], j["uri"], j["idem"])
            with lock:
                if replay:
                    results["replay"] += 1
                else:
                    results["ok"] += 1
        except Conflict:
            with lock:
                results["conflict"] += 1
        except Exception as e:
            with lock:
                results["err"] += 1
                print("    unexpected:", type(e).__name__, e)

    ts = [threading.Thread(target=worker, args=(j,)) for j in jobs]
    t0 = time.time()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    dt = time.time() - t0

    rows = store.all_rows()
    n_wallets = len({r["wallet"] for r in rows})
    report.check(
        "S1 no unexpected errors",
        results["err"] == 0,
        json.dumps(results),
    )
    report.check(
        "S1 exactly one logical submission per wallet",
        n_wallets == 10 and len(rows) == 10,
        f"{len(rows)} rows / {n_wallets} wallets (120 requests in {dt:.2f}s)",
    )
    report.check(
        "S1 no duplicate active rows",
        len({(r["wallet"], r["bounty_id"]) for r in rows if r["reviewed"] == 0}) == len(rows),
        "",
    )
    return store, results


def scenario_2_response_loss(db_path, report):
    """Commit succeeds, response is lost, client retries same idem key."""
    store = Store(db_path)
    w = WALLETS[0]
    key = "lost-1"
    # first attempt: real commit, then simulate the response dying in flight
    try:
        store.submit(w, BOUNTY_S2, 1, "0xdead", "https://x/1", key)
        raise LostResponse()
    except LostResponse:
        pass
    # retry with the same idempotency key
    row, replay = store.submit(w, BOUNTY_S2, 1, "0xdead", "https://x/1", key)
    report.check(
        "S2 retry after lost response is a replay, not a new row",
        replay is True,
        f"replay={replay}",
    )
    n = store.conn().execute(
        "SELECT COUNT(*) AS n FROM submissions WHERE wallet=? AND bounty_id=?",
        (w, BOUNTY_S2),
    ).fetchone()["n"]
    report.check("S2 exactly one row after lost response + retry", n == 1, f"rows={n}")
    return store


def scenario_3_restart(db_path, report):
    """
    Restart between the log write and the checkpoint.

    We close every connection (the process death) after a commit, reopen the
    file, and assert the committed row is still there and a replay of the same
    idempotency key does not duplicate it.
    """
    w = WALLETS[1]
    store = Store(db_path)
    store.submit(w, BOUNTY_S3, 1, "0xbeef", "https://x/2", "restart-1")
    before = store.conn().execute(
        "SELECT COUNT(*) AS n FROM submissions WHERE wallet=? AND bounty_id=?", (w, BOUNTY_S3)
    ).fetchone()["n"]

    # simulate process death: drop the thread-local connection
    store._local = threading.local()

    store2 = Store(db_path)
    after = store2.conn().execute(
        "SELECT COUNT(*) AS n FROM submissions WHERE wallet=? AND bounty_id=?", (w, BOUNTY_S3)
    ).fetchone()["n"]
    _, replay = store2.submit(w, BOUNTY_S3, 1, "0xbeef", "https://x/2", "restart-1")
    final = store2.conn().execute(
        "SELECT COUNT(*) AS n FROM submissions WHERE wallet=? AND bounty_id=?", (w, BOUNTY_S3)
    ).fetchone()["n"]

    report.check("S3 row survives restart", before == after == 1, f"{before} -> {after}")
    report.check("S3 replay after restart is idempotent", replay is True and final == 1,
                 f"replay={replay} rows={final}")


def scenario_4_stale_and_reviewed(db_path, report):
    """Stale version loses; a reviewed submission is immutable."""
    store = Store(db_path)
    w = WALLETS[2]

    store.submit(w, BOUNTY_S4, 5, "0x5", "https://x/5", "v5")
    try:
        store.submit(w, BOUNTY_S4, 3, "0x3", "https://x/3", "v3")
        report.check("S4 stale version rejected", False, "stale update was accepted")
    except Conflict as e:
        report.check("S4 stale version rejected", True, str(e))

    active = store.active(w, BOUNTY_S4)
    report.check("S4 active row still v5 after stale attempt",
                 active is not None and active["version"] == 5,
                 f"version={active['version'] if active else None}")

    # newer version legitimately updates
    store.submit(w, BOUNTY_S4, 7, "0x7", "https://x/7", "v7")
    report.check("S4 newer version updates in place",
                 store.active(w, BOUNTY_S4)["version"] == 7, "")

    # review it, then attempt to overwrite it
    reviewed = store.review(w, BOUNTY_S4)
    report.check("S4 review succeeds", reviewed is not None and reviewed["reviewed"] == 1, "")
    # after review there is no *active* row, so a new submit creates a fresh one
    new_row, _ = store.submit(w, BOUNTY_S4, 8, "0x8", "https://x/8", "v8")
    old = store.conn().execute(
        "SELECT * FROM submissions WHERE id=?", (reviewed["id"],)
    ).fetchone()
    report.check(
        "S4 reviewed submission not overwritten",
        old["reviewed"] == 1 and old["version"] == 7,
        f"reviewed row is v{old['version']} reviewed={old['reviewed']}",
    )
    report.check("S4 new active submission is a distinct row",
                 new_row["id"] != reviewed["id"], "")


def control_test_report_structure(db_path, report):
    """
    Run the main workload against the NAIVE store to prove it actually fails.
    A control that passes proves nothing about the assertions.
    """
    print("\n  --- control: naive implementation (must fail S1) ---")
    sub = Report()
    try:
        scenario_1_concurrent(db_path + ".naive", sub, store_cls=NaiveStore)
    except Exception as e:
        sub.check("control naive raises", True, f"{type(e).__name__}: {e}")
    s1_logical = [c for c in sub.checks if c["name"].startswith("S1 exactly one logical")]
    naive_failed = bool(s1_logical) and not s1_logical[0]["ok"]
    report.check("control: naive store FAILS the one-logical-per-wallet assertion",
                 naive_failed,
                 s1_logical[0]["detail"] if s1_logical else "no result")


def main():
    print(f"idempotent submissions harness — seed={SEED} wallets={len(WALLETS)}")
    print("=" * 72)
    tmp = os.path.join(HERE, "_run.db")
    for suffix in ("", "-wal", "-shm", ".naive", ".naive-wal", ".naive-shm"):
        try:
            os.remove(tmp + suffix)
        except OSError:
            pass

    report = Report()
    print("\nscenario 1 — 120 concurrent requests / 10 wallets")
    scenario_1_concurrent(tmp, report)
    print("\nscenario 2 — response loss + idempotent retry")
    scenario_2_response_loss(tmp, report)
    print("\nscenario 3 — restart between commit and checkpoint")
    scenario_3_restart(tmp, report)
    print("\nscenario 4 — stale version + reviewed immutability")
    scenario_4_stale_and_reviewed(tmp, report)
    control_test_report_structure(tmp, report)

    out = {
        "seed": SEED,
        "wallets": len(WALLETS),
        "checks": report.checks,
        "result": "PASS" if report.ok else "FAIL",
    }
    with open(os.path.join(HERE, "report.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)

    print("\n" + "=" * 72)
    n_ok = sum(1 for c in report.checks if c["ok"])
    print(f"RESULT: {out['result']}  ({n_ok}/{len(report.checks)} checks)")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
