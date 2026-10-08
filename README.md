# IMD Works — bounty deliverables

Reproducible, local-only deliverables for [imdworks.fun](https://imdworks.fun)
bounties on Robinhood Chain (4663). Each directory is self-contained: one
command, standard library only (plus one vendored keccak for the JS side), no
network access, no production infrastructure touched.

| Directory | Escrow id | Bounty |
|---|---|---|
| [`escrow5-canonical-brief-hashing/`](escrow5-canonical-brief-hashing/) | 5 | Design canonical brief hashing across two languages |
| [`escrow8-idempotent-submissions/`](escrow8-idempotent-submissions/) | 8 | Make bounty submissions idempotent under concurrency |

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

## Verification

Both were re-run from a clean state (no cached `report.json`, fresh database):

```
escrow5: RESULT PASS — 87/87 python, 87/87 javascript, 55/55 canonical bytes identical
escrow8: RESULT PASS — 14/14 checks
```

Seeds are fixed (`20261008`) so both are fully deterministic.
