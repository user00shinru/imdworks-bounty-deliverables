#!/usr/bin/env python3
"""
escrow 10 — independent ledger reconciler.

Rebuilds, from the ESCROW EVENT LOG ALONE, three things:

    locked[bounty]   — the reward currently held for each open bounty
    credit[wallet]   — the claimable balance of each wallet
    totals           — total locked, total claimable and total liabilities

then compares them field-by-field against the contract state recorded at a fixed
snapshot block. Token truth is a separate stream: an escrow-held token balance is
only meaningful when it equals (locked + claimable) + unsolicited donations.

Design decisions that matter
----------------------------
* **Events are the only input for the ledger.** State is used for comparison, never
  to derive values — that is what makes this an independent reconciliation and not
  a re-read of the contract.
* **Duplicates are dropped by LOG POSITION, not by content.** A real chain never
  repeats a log, but a re-org replay or a buggy indexer can deliver the same
  `(block, logIndex)` twice. Deduping on content instead would silently discard two
  legitimate identical withdrawals (same account, same amount, different time) and
  over-report credits. The identity is `(block, logIndex)`.
* **Unsolicited donations are surplus, not credit.** A `Transfer` into the escrow
  that was not caused by `createBounty`/`addReward` increases the token balance
  without creating a liability. It therefore shows up as
  `tokenBalance - liabilities` and is reported as surplus.
* **Failed withdrawals leave credit untouched.** The reconciler only reduces a
  credit when a `Withdrawn` event for that exact account/amount is present.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixture", "ledger_fixture.json")

# Status enum on the contract: Missing=0 Open=1 Awarded=2 Cancelled=3 Expired=4
OPEN = 1

REFUND_STATUS = {3, 4}  # Cancelled, Expired -> creator refunded, reward unlocked


class ReconError(Exception):
    pass


def replay(events: list[dict]) -> dict:
    """Rebuild locked + credits from the escrow event stream.

    Returns a dict with per-bounty locked values and per-wallet credits.
    """
    locked: dict[int, int] = defaultdict(int)
    credit: dict[str, int] = defaultdict(int)
    seen: set[tuple[int, int]] = set()
    stats = {"total": 0, "duplicates_dropped": 0, "unknown_events": []}

    for ev in events:
        kind = ev["kind"]
        data = ev["data"] if isinstance(ev["data"], dict) else json.loads(ev["data"])
        ident = (ev.get("block"), ev.get("logIndex"))
        stats["total"] += 1
        if ident in seen:
            stats["duplicates_dropped"] += 1
            continue
        seen.add(ident)

        if kind == "BountyCreated":
            locked[data["bountyId"]] += data["reward"]
        elif kind == "RewardAdded":
            locked[data["bountyId"]] += data["amount"]   # amount is the *additional* value
        elif kind == "WorkSubmitted":
            pass                                          # no value movement
        elif kind == "BountyAwarded":
            bid = data["bountyId"]
            amt = data["amount"]
            if locked[bid] < amt:
                raise ReconError(f"award {amt} > locked {locked[bid]} for bounty {bid}")
            locked[bid] -= amt
            credit[data["winner"]] += amt
        elif kind == "BountyRefunded":
            bid = data["bountyId"]
            amt = data["amount"]
            if data.get("status") not in REFUND_STATUS:
                raise ReconError(f"unknown refund status {data.get('status')} on bounty {bid}")
            if locked[bid] < amt:
                raise ReconError(f"refund {amt} > locked {locked[bid]} for bounty {bid}")
            locked[bid] -= amt
            credit[data["creator"]] += amt
        elif kind == "Withdrawn":
            acct = data["account"]
            amt = data["amount"]
            if credit[acct] < amt:
                raise ReconError(f"withdraw {amt} > credit {credit[acct]} for {acct}")
            credit[acct] -= amt
        else:
            stats["unknown_events"].append(kind)

    # prune zeros for readability
    locked = {k: v for k, v in locked.items() if v != 0}
    credit = {k: v for k, v in credit.items() if v != 0}
    return {"locked": locked, "credit": credit, "stats": stats}


def compare(fixture: dict) -> tuple[bool, list[dict], dict]:
    """Run the reconciliation and return (ok, checks, derived)."""
    events = fixture["events"]
    state = fixture["state"]
    wallets_expected = fixture.get("wallets", {})
    bounties_expected = fixture.get("bounties", [])
    token_events = fixture.get("tokenEvents", [])

    derived = replay(events)
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    # --- totals -----------------------------------------------------------------
    total_locked = sum(derived["locked"].values())
    total_claimable = sum(derived["credit"].values())
    liabilities = total_locked + total_claimable

    add("totalLocked matches chain",
        total_locked == state["totalLocked"],
        f"derived {total_locked} vs chain {state['totalLocked']}")
    add("totalClaimable matches chain",
        total_claimable == state["totalClaimable"],
        f"derived {total_claimable} vs chain {state['totalClaimable']}")
    add("liabilities matches chain",
        liabilities == state["liabilities"],
        f"derived {liabilities} vs chain {state['liabilities']}")

    # --- per-wallet credits ------------------------------------------------------
    mismatch_w = []
    for w, v in wallets_expected.items():
        got = derived["credit"].get(w, 0)
        if got != v:
            mismatch_w.append(f"{w}: derived {got} vs chain {v}")
    for w, v in derived["credit"].items():
        if wallets_expected.get(w, 0) != v:
            mismatch_w.append(f"{w}: derived {v} not in chain state")
    add("per-wallet credits match chain", not mismatch_w, "; ".join(mismatch_w[:4]))

    # --- per-bounty locked -------------------------------------------------------
    # chain per-bounty reward for an Open bounty == locked value
    mismatch_b = []
    for b in bounties_expected:
        bid = b["id"]
        if b["status"] == OPEN:
            got = derived["locked"].get(bid, 0)
            if got != b["reward"]:
                mismatch_b.append(f"bounty {bid}: derived locked {got} vs open reward {b['reward']}")
        else:
            if derived["locked"].get(bid, 0) != 0:
                mismatch_b.append(f"bounty {bid}: closed but derived locked {derived['locked'][bid]}")
    add("per-bounty locked matches chain", not mismatch_b, "; ".join(mismatch_b[:4]))

    # --- unsolicited donations are surplus --------------------------------------
    donations = sum(e["data"]["value"] for e in token_events
                    if isinstance(e.get("data"), dict) and e["data"].get("donation"))
    token_balance = state["escrowTokenBalance"]
    surplus = token_balance - liabilities
    add("donations explain the token surplus",
        surplus == donations,
        f"tokenBalance {token_balance} - liabilities {liabilities} = {surplus}; donations {donations}")
    add("token balance never below liabilities (solvent)", token_balance >= liabilities,
        f"{token_balance} >= {liabilities}")

    # --- duplicate handling was exercised ----------------------------------------
    add("duplicate events were dropped, not counted",
        derived["stats"]["duplicates_dropped"] > 0,
        f"{derived['stats']['duplicates_dropped']} duplicate(s) dropped")

    # --- no unknown event kinds --------------------------------------------------
    add("no unrecognised escrow events", not derived["stats"]["unknown_events"],
        ", ".join(sorted(set(derived["stats"]["unknown_events"]))))

    ok = all(c["ok"] for c in checks)
    return ok, checks, {**derived, "total_locked": total_locked,
                        "total_claimable": total_claimable, "liabilities": liabilities,
                        "donations": donations, "surplus": surplus}


# ---------------------------------------------------------------------------- corruption + idempotency

def corrupt_extra_created(events: list[dict]) -> list[dict]:
    """An EXTRA BountyCreated at a brand-new log position: a phantom reward.

    Distinct from a re-org replay (same position) — this one is a genuinely extra
    canonical event, so locked value must NOT match the chain any more.
    """
    out = list(events)
    maxpos = max((e.get("logIndex", 0) for e in events), default=0)
    for e in events:
        if e["kind"] == "BountyCreated":
            clone = dict(e)
            clone["block"] = e.get("block", 0) + 100000
            clone["logIndex"] = maxpos + 1
            out.append(clone)
            break
    return out


def corrupt_remove_created(events: list[dict]) -> list[dict]:
    """Drop a BountyCreated -> locked value under-reports."""
    for i, e in enumerate(events):
        if e["kind"] == "BountyCreated":
            return events[:i] + events[i + 1:]
    return events


def corrupt_remove_withdrawn(events: list[dict]) -> list[dict]:
    """Drop a Withdrawn event -> credits over-report."""
    for i, e in enumerate(events):
        if e["kind"] == "Withdrawn":
            return events[:i] + events[i + 1:]
    return events


def corrupt_flip_refund_status(events: list[dict]) -> list[dict]:
    """Swap a refund status so it no longer matches the contract enum."""
    out = []
    for e in events:
        if e["kind"] == "BountyRefunded":
            d = dict(e["data"])
            d["status"] = 1  # not a refund status
            out.append({**e, "data": d})
        else:
            out.append(e)
    return out


def replay_stream(events: list[dict]) -> list[dict]:
    """A full re-org replay: every event delivered a second time at the SAME position."""
    return list(events) + [dict(e) for e in events]


def run_corruption(fixture: dict) -> tuple[list[dict], list[dict]]:
    """Corrupted streams must FAIL LOUDLY. Replayed streams must stay idempotent."""
    corruptions = []
    for name, mutate, expect in [
        ("extra BountyCreated (new position)", corrupt_extra_created, "totals diverge"),
        ("removed BountyCreated", corrupt_remove_created, "locked under-reports"),
        ("removed Withdrawn event", corrupt_remove_withdrawn, "credits over-report"),
        ("refund status flipped", corrupt_flip_refund_status, "status rejected"),
    ]:
        bad = {**fixture, "events": mutate(fixture["events"])}
        try:
            ok, checks, _ = compare(bad)
            caught = not ok
            detail = next((c["detail"] for c in checks if not c["ok"]), "NO DIVERGENCE — MISSED")
        except ReconError as e:
            caught, detail = True, f"ReconError: {e}"
        corruptions.append({"tamper": name, "expected": expect,
                            "caught": caught, "detail": detail})

    idem = []
    bad = {**fixture, "events": replay_stream(fixture["events"])}
    ok, checks, derived = compare(bad)
    idem.append({
        "case": "full re-org replay (each event twice, same position)",
        "stayed_consistent": ok,
        "detail": f"duplicates dropped {derived['stats']['duplicates_dropped']}, "
                  f"liabilities {derived['liabilities']}",
    })
    return corruptions, idem


# ---------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", default=FIXTURE)
    ap.add_argument("--out", default=os.path.join(HERE, "report.json"))
    args = ap.parse_args()

    with open(args.fixture, encoding="utf-8") as f:
        fixture = json.load(f)

    ev_n = len(fixture["events"])
    print("=" * 74)
    print("escrow 10 — independent ledger reconciliation")
    print("=" * 74)
    print(f"fixture      : {os.path.relpath(args.fixture, HERE)}")
    print(f"seed         : {fixture['seed']}")
    print(f"snapshotBlock: {fixture['snapshotBlock']}")
    print(f"events       : {ev_n}  ({len(fixture.get('tokenEvents', []))} token events)")

    ok, checks, derived = compare(fixture)
    print(f"\nreplayed from events: locked {derived['total_locked']} · claimable "
          f"{derived['total_claimable']} · liabilities {derived['liabilities']}")
    print(f"donations (surplus):  {derived['donations']}  -> surplus {derived['surplus']}")
    print(f"duplicates dropped :  {derived['stats']['duplicates_dropped']}\n")

    for c in checks:
        print(f"{'✅' if c['ok'] else '❌'} {c['check']}" + (f" — {c['detail']}" if c['detail'] else ""))

    corr, idem = run_corruption(fixture)
    print("\n-- corrupted-stream detection (must diverge) --")
    for r in corr:
        print(f"{'✅' if r['caught'] else '❌'} {r['tamper']:<36} caught={r['caught']} — {r['detail'][:60]}")
    print("\n-- replayed-stream idempotency (must stay consistent) --")
    for r in idem:
        print(f"{'✅' if r['stayed_consistent'] else '❌'} {r['case']} — {r['detail']}")

    all_ok = ok and all(r["caught"] for r in corr) and all(r["stayed_consistent"] for r in idem)
    report = {
        "bounty": "imdworks escrow 10 — auditable bounty ledger reconciliation tool",
        "fixture": {
            "file": os.path.relpath(args.fixture, HERE).replace("\\", "/"),
            "seed": fixture["seed"], "snapshotBlock": fixture["snapshotBlock"],
            "escrow": fixture["escrow"], "token": fixture["token"],
            "events": ev_n, "tokenEvents": len(fixture.get("tokenEvents", [])),
        },
        "derived": {k: v for k, v in derived.items() if k != "stats"},
        "checks": checks,
        "corruption_modes": corr,
        "idempotency": idem,
        "verdict": "PASS" if all_ok else "FAIL",
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print("\n" + "=" * 74)
    print(f"verdict: {report['verdict']}  "
          f"({sum(1 for c in checks if c['ok'])}/{len(checks)} checks, "
          f"{sum(1 for r in corr if r['caught'])}/{len(corr)} corruptions caught, "
          f"{sum(1 for r in idem if r['stayed_consistent'])}/{len(idem)} replays idempotent)")
    print(f"report : {os.path.relpath(args.out, HERE)}")
    print("=" * 74)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
