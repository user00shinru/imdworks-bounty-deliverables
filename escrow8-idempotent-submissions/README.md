# Idempotent bounty submissions under concurrency

A local SQLite-backed submission API harness proving that one active submission
per wallet per bounty survives retries, response loss, restarts and stale
updates — and a deliberately broken control that does not.

```
python run.py        # exit 0 = PASS, writes report.json
```

No dependencies beyond the Python 3 standard library. Deterministic: seed
`20261008`.

## What the brief asked for, and where it is

| Requirement | Where |
|---|---|
| ≥ 100 concurrent requests across 10 wallets | `run.py` → **120 requests / 10 wallets** |
| response loss injected | `scenario_2_response_loss` (commit succeeds, response throws) |
| restart faults injected | `scenario_3_restart` (drop connections mid-run, reopen) |
| no duplicate logical submissions | `S1 exactly one logical submission per wallet` |
| no overwritten reviewed submission | `S4 reviewed submission not overwritten` |
| deterministic handling of stale versions | `S4 stale version rejected` |
| failing naive implementation | `naive.py` + `control_test_report_structure` |
| schema constraints | `store.py` → two `UNIQUE INDEX`es, one partial |
| transaction rationale | module docstring + "Why" below |
| request traces | `report.json.checks[].detail` |
| one-command replay | `python run.py` |
| recovery after commit-but-lost-response | `scenario 2` |

## Why these two constraints, and not application logic

The interesting failure is not "two requests at once". It is that a
read-then-write check is *not atomic with respect to another connection*:

```python
row = SELECT * FROM submissions WHERE wallet=? AND bounty_id=?   # A reads: nothing
                                                                 # B reads: nothing
if row is None: INSERT                                            # A inserts
                                                                 # B inserts -> 2 rows
```

No amount of Python-level locking fixes this while the service is allowed to run
more than one process. So the invariant is pushed into the engine:

```sql
CREATE UNIQUE INDEX one_active ON submissions(wallet, bounty_id) WHERE reviewed = 0;
CREATE UNIQUE INDEX one_idem   ON submissions(idem_key);
```

The first is **partial**: reviewed rows are exempt, because a wallet may
legitimately submit again to the same bounty after its previous attempt was
reviewed (and, at the contract level, a reviewed submission is a distinct row;
the escrow's own `submissions[bounty][author]` map is what caps "one proof per
author per bounty", while this service models the *review lifecycle* around it).

The second makes a retry with the same idempotency key a no-op **even if the
original response was lost**, which is the only way to recover from
commit-then-die.

`BEGIN IMMEDIATE` (not `DEFERRED`) is used so the write lock is taken before
the read. A deferred transaction that upgrades a read lock to a write lock under
concurrency produces `SQLITE_BUSY` deadlocks between two writers; immediate
serialises them at the start.

## The control is the point

`control_test_report_structure` runs the *same* 120-request workload against
`naive.py`, which:

* opens a fresh connection per request,
* reads and writes outside any transaction,
* has no unique index, and
* overwrites reviewed rows.

Measured on this machine: **111 rows for 10 wallets** (vs the reference store's
exactly 10). Without this control the passing assertions would be unfalsifiable —
they would pass against a store with no concurrency protection at all if the
workload never actually raced. The harness asserts the control *fails*, so a
future change that accidentally weakens the workload is caught.

## Results

```
scenario 1 — 120 concurrent requests / 10 wallets     PASS   10 rows / 10 wallets
scenario 2 — response loss + idempotent retry         PASS   replay=True, 1 row
scenario 3 — restart between commit and checkpoint    PASS   1 -> 1, replay=True
scenario 4 — stale version + reviewed immutability    PASS   5 kept, v7 kept, reviewed row untouched
control    — naive store must fail                    PASS   111 rows / 10 wallets
RESULT: PASS (14/14)
```

## Assumptions

1. **Idempotency key is client-supplied and stable across retries.** A client
   that generates a new key for each attempt gets no protection; that is the
   client's contract to honour, and is why the key is stored and unique rather
   than derived from the payload.
2. **Version numbers are client-supplied, monotonic per submission.** The store
   rejects `version <= current`; it does not invent versions.
3. **One writer process per database file is *not* assumed.** The harness runs
   threads, but every guarantee above is enforced by the engine (indexes +
   `BEGIN IMMEDIATE`), so it holds for multiple processes on the same file too.
   `journal_mode=WAL` is set for that reason.
4. **"Logical submission" means a distinct `(wallet, bounty_id)` row**, not a
   distinct request. Retries are expected and are not counted as submissions.
