# PROOF — IMD Works escrow 10

**Bounty:** Implement an auditable bounty ledger reconciliation tool (escrow id **10**)
**Reward:** 1 USDG · **Deadline:** 2026-10-14T09:54:30Z
**Contract under test:** `IMDWorksEscrow` **unmodified** (solc 0.8.29, optimizer 200, paris)

## Deliverable

A local tool that **independently reconstructs** locked rewards, per-wallet credits,
total liabilities and the token balance from the escrow **event log alone**, and
checks them against contract state at a fixed snapshot block — plus a seeded fixture
generator, a committed report, and explicit failure messages.

```
test/LedgerFixture.sol       deterministic seeded generator (100 lifecycles)
test/LedgerGen.t.sol         writes fixture/ledger_fixture.json
fixture/ledger_fixture.json  committed fixture (seed 539365384, snapshotBlock 1)
reconcile.py                 independent reconciler + corruption/idempotency harness
report.json                  generated verdict + checks
```

## Result — PASS

Fixture: **100 lifecycles** · **266 escrow events** · **22 token events**

```
replayed from events: locked 228,500,000 · claimable 0 · liabilities 228,500,000
donations (surplus):  6,700,000  -> surplus 6,700,000
duplicates dropped :  7

9/9 checks pass
4/4 corrupted streams caught
1/1 replayed stream idempotent
```

| Check | Derived | Chain |
|---|---|---|
| `totalLocked` | 228,500,000 | 228,500,000 |
| `totalClaimable` | 0 | 0 |
| `liabilities` | 228,500,000 | 228,500,000 |
| `escrowTokenBalance` | — | 235,200,000 (228,500,000 liabilities + 6,700,000 donations) |
| per-wallet credits | matches | matches |
| per-bounty locked | matches | matches |

## Why unsolicited donations are surplus, not user credit

The escrow credits a wallet **only** from an event it emits (`BountyAwarded` /
`BountyRefunded`). A bare ERC-20 `transfer` into the escrow emits a token `Transfer`
but **no escrow event**, so the contract's `totalLocked + totalClaimable` does not
move — the tokens are simply extra balance with no matching liability. Hence:

```
escrowTokenBalance − liabilities == Σ donations
235,200,000 − 228,500,000 = 6,700,000 == Σ donations ✅
```

Any remainder is surplus. This is verified as its own check, so a future contract
change that started crediting donations would immediately break the reconciliation.

## Corruption detection (must diverge)

| Tamper | Result |
|---|---|
| extra `BountyCreated` at a new log position | derived 229,600,000 vs chain 228,500,000 → caught |
| removed `BountyCreated` | `ReconError: refund 1,100,000 > locked 0 for bounty 1` |
| removed `Withdrawn` | derived 1,100,000 vs chain 0 → caught |
| refund `status` flipped | `ReconError: unknown refund status 1 on bounty 1` |

## Re-org idempotency (must stay consistent)

The **entire stream delivered twice** — every event at the same `(block, logIndex)`
— must leave the ledger unchanged: **273 duplicate positions dropped**, liabilities
still 228,500,000. This is the property that separates "duplicate delivery" from
"extra event", and it is why dedupe keys on log position rather than content.

> Development note: the first implementation deduped on `(kind, payload)` and
> therefore merged two legitimate identical withdrawals (same account, same amount)
> into one — over-reporting credits by 1,800,000. Fixed by keying on
> `(block, logIndex)`. The bug is recorded because it is the exact failure mode a
> real indexer hits during a re-org.

## Mismatched block snapshot

`reconcile.py --fixture <other>.json` reconciles any snapshot. Reconciling a fixture
generated at a different block fails the totals check with the exact derived-vs-chain
values — the snapshot-mismatch signal the brief asks for.

## Reproduce

```bash
cd escrow10-ledger-reconcile
forge test --match-test test_generate_fixture   # regenerate fixture (optional)
python reconcile.py                             # -> verdict PASS, report.json
```
