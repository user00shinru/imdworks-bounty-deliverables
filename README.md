# IMD Works — bounty deliverables

Reproducible, local-only deliverables for [imdworks.fun](https://imdworks.fun)
bounties on Robinhood Chain (4663). Each directory is self-contained: one
command, standard library only (plus one vendored keccak for the JS side), no
network access, no production infrastructure touched.

| Directory | Escrow id | Bounty |
|---|---|---|
| [`escrow1-invariant-harness/`](escrow1-invariant-harness/) | 1 | Build a stateful escrow accounting invariant harness |
| [`escrow2-reorg-indexer/`](escrow2-reorg-indexer/) | 2 | Implement a reorg-safe bounty event indexer |
| [`escrow5-canonical-brief-hashing/`](escrow5-canonical-brief-hashing/) | 5 | Design canonical brief hashing across two languages |
| [`escrow8-idempotent-submissions/`](escrow8-idempotent-submissions/) | 8 | Make bounty submissions idempotent under concurrency |
| [`escrow9-bytecode-provenance/`](escrow9-bytecode-provenance/) | 9 | Rebuild the published escrow source byte-exactly |

## escrow5 — canonical brief hashing

```
cd escrow5-canonical-brief-hashing && python run.py     # exit 0 = PASS
```

`imdworks-brief-v1`: a length-prefixed record stream over the six brief fields,
implemented independently in Python and JavaScript. 87 frozen vectors
(55 accept / 32 reject), all keccak256-checked, plus a **byte-level**
cross-language comparison — because a hash-only check would still pass if two
different byte streams collided.

* `SPEC.md` — normative rules, why JSON serializers are unusable, how a client
  tells byte equality from semantic similarity, why historical hashes stay
  verifiable.
* `FINDINGS.md` — **two real defects the harness caught in the first version**
  (a `__proto__` prototype-pollution bug in the JS decoder, and a divergent
  rejection taxonomy for negative numbers).

## escrow8 — idempotent submissions

```
cd escrow8-idempotent-submissions && python run.py      # exit 0 = PASS
```

SQLite-backed submission store, 120 concurrent requests across 10 wallets,
with response-loss and restart fault injection. 14/14 checks.

The invariant is enforced by the engine, not by application logic: a **partial
unique index** on `(wallet, bounty_id) WHERE reviewed = 0` closes the
read-then-write TOCTOU window, and a unique idempotency key makes a
commit-then-lost-response retry a no-op. `README.md` has the transaction
rationale and the assumptions list.

`naive.py` is a deliberately broken control. The harness asserts it *fails* —
measured **111 rows for 10 wallets** against the reference store's exactly 10.
Without that control the passing assertions would be unfalsifiable.

## escrow1 — escrow accounting invariant harness

```
cd escrow1-invariant-harness && python run.py     # exit 0 = PASS
```

Foundry invariant harness over the deployed escrow source: 3 creators, 5
workers, operator delegation, top-ups, time jumps, cancel/award/withdraw.

The assertions are **not** mirrored variables — the handler keeps a ghost ledger
derived from the economic specification and compares the contract against it.
`invariant_conservation_of_value` asserts
`totalLocked + totalClaimable + withdrawn == Σ created rewards`.

`src/BrokenEscrow.sol` is byte-identical except **one deleted line** in
`_credit`. Both contracts run through the same handler and the same assertions.

```
correct contract : 6 passed / 0 failed   (runs=1000, depth=100)
mutant  contract : 2 passed / 4 failed   (I1, I2, I3, I4 rejected)
```

## escrow2 — reorg-safe bounty event indexer

```
cd escrow2-reorg-indexer && python run.py         # exit 0 = PASS
```

Deterministic in-process chain fixture + SQLite. Three properties carry the
reorg safety:

* **order independence** — blocks are ingested by height and out-of-order blocks
  are buffered, so any permutation of the same block set yields identical state;
* **exactly-once effect** — derived tables are a pure function of a
  `(tx_hash, log_index)`-keyed journal, so recovery equals fresh replay *by
  construction*;
* **atomic hash-anchored checkpoint** — reorg detection walks block hashes, so a
  reorg that only removes *empty* blocks is still caught.

```
RESULT: PASS — 24/24 checks
```

Every scenario ends with `snapshot_after_recovery == snapshot_of_fresh_index`.

## Verification

Both were re-run from a clean state (no cached `report.json`, fresh database):

```
escrow5: RESULT PASS — 87/87 python, 87/87 javascript, 55/55 canonical bytes identical
escrow8: RESULT PASS — 14/14 checks
```

Seeds are fixed (`20261008`) so both are fully deterministic.
