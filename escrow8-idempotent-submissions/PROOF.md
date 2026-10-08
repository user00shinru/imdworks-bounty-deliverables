# PROOF — IMD Works escrow bounty 8

**Bounty:** Make bounty submissions idempotent under concurrency
**Reward target:** IMD Works escrow bounty id **8**
**Repository (pinned):** `user00shinru/imdworks-bounty-deliverables`
**Commit:** `9410f22f727eea8cc2a645cfb20d88d4d1ff4200`
**Reproduction:** `cd escrow8-idempotent-submissions && python run.py` (exit 0 = PASS)

---

## 1. Result

```
idempotent submissions harness — seed=20261008 wallets=10
========================================================================
scenario 1 — 120 concurrent requests / 10 wallets        PASS
scenario 2 — response loss + idempotent retry             PASS
scenario 3 — restart between commit and checkpoint        PASS
scenario 4 — stale version + reviewed immutability        PASS
control    — naive store must fail                        PASS

RESULT: PASS (14/14 checks)
```

Re-run from a clean state (fresh `_run.db`, no cached `report.json`), exit 0.

## 2. The invariant, and why it is in the schema

The interesting failure is not "two requests at once" — it is that
read-then-write is **not atomic with respect to another connection**:

```python
row = SELECT * FROM submissions WHERE wallet=? AND bounty_id=?   # A reads: nothing
                                                                 # B reads: nothing
if row is None: INSERT                                            # A inserts
                                                                 # B inserts -> 2 rows
```

No Python-level lock fixes this across processes, so the constraint is pushed
into the engine:

```sql
CREATE UNIQUE INDEX one_active ON submissions(wallet, bounty_id) WHERE reviewed = 0;
CREATE UNIQUE INDEX one_idem   ON submissions(idem_key);
```

* `one_active` is **partial** — reviewed rows are exempt, so a wallet may
  submit again to the same bounty after review.
* `one_idem` makes a retry with the same idempotency key a no-op **even when the
  original response was lost**, which is the only recovery path from
  commit-then-die.

`BEGIN IMMEDIATE` (never `DEFERRED`) takes the write lock before the read.
A deferred transaction that upgrades a read lock to a write lock under
concurrency produces `SQLITE_BUSY` deadlocks between two writers; immediate
serialises them at the start.

## 3. Requirements → evidence

| Brief requirement | Where |
|---|---|
| ≥ 100 concurrent requests across 10 wallets | `scenario_1` — **120 requests / 10 wallets** |
| response loss injected | `scenario_2` — commit succeeds, then the responder raises |
| restart faults injected | `scenario_3` — connections dropped mid-run, store reopened |
| stale versions handled deterministically | `S4 stale version rejected` (`have 5, got 3`) |
| no duplicate logical submissions | `S1 exactly one logical submission per wallet` |
| no overwritten reviewed submission | `S4 reviewed submission not overwritten` |
| failing naive implementation | `naive.py` + control check |
| schema constraints | two `UNIQUE INDEX`es, one partial |
| transaction rationale | `README.md` §"Why these two constraints" |
| request traces | `report.json.checks[].detail` |
| one-command replay | `python run.py` |
| recovery after commit + lost response | `scenario_2` — `replay=True`, 1 row |

## 4. The control is the point

`control_test_report_structure` runs the **same** 120-request workload against
`naive.py`, which opens a fresh connection per request, reads and writes outside
any transaction, has no unique index, and overwrites reviewed rows.

Measured on this machine:

```
reference store :   10 rows / 10 wallets
naive store     :  111 rows / 10 wallets
```

Without this control the passing assertions would be unfalsifiable — they would
also pass against a store with no concurrency protection if the workload never
actually raced. The harness asserts the control **fails**, so a future change
that weakens the workload is caught.

## 5. Full check list (from `report.json`)

```
[PASS] S1 no unexpected errors — {"ok": 10, "conflict": 110, "replay": 0, "err": 0}
[PASS] S1 exactly one logical submission per wallet — 10 rows / 10 wallets
[PASS] S1 no duplicate active rows
[PASS] S2 retry after lost response is a replay, not a new row — replay=True
[PASS] S2 exactly one row after lost response + retry — rows=1
[PASS] S3 row survives restart — 1 -> 1
[PASS] S3 replay after restart is idempotent — replay=True rows=1
[PASS] S4 stale version rejected — stale_version: have 5, got 3
[PASS] S4 active row still v5 after stale attempt — version=5
[PASS] S4 newer version updates in place
[PASS] S4 review succeeds
[PASS] S4 reviewed submission not overwritten — reviewed row is v7 reviewed=1
[PASS] S4 new active submission is a distinct row
[PASS] control: naive store FAILS the one-logical-per-wallet assertion — 111 rows / 10 wallets
```

## 6. Assumptions

1. **The idempotency key is client-supplied and stable across retries.** A
   client that generates a fresh key per attempt gets no protection; that is the
   client's contract to honour, and is why the key is stored and unique rather
   than derived from the payload.
2. **Version numbers are client-supplied and monotonic per submission.** The
   store rejects `version <= current`; it does not invent versions.
3. **Multiple writer processes are supported, not assumed away.** The harness
   runs threads, but every guarantee is enforced by the engine (indexes +
   `BEGIN IMMEDIATE`), so it holds for several processes on one file.
   `journal_mode=WAL` is set for that reason.
4. **"Logical submission" = a distinct `(wallet, bounty_id)` row**, not a
   distinct request. Retries are expected and are not counted as submissions.

## 7. Environment

* Python 3.14.6, standard library only (`sqlite3`)
* No network access at test time. Deterministic: seed `20261008`.
