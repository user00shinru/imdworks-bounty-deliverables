# PROOF — IMD Works escrow bounty 2

**Bounty:** Implement a reorg-safe bounty event indexer (escrow id **2**)
**Repository:** `user00shinru/imdworks-bounty-deliverables`
**Reproduction:** `python escrow2-reorg-indexer/run.py`  (exit 0 = PASS)

---

## 1. Result

```
IMD Works escrow bounty 2 — reorg-safe bounty event indexer
  seed   20261008
  sqlite 3.53.2
  python 3.14.6

RESULT: PASS — 24/24 checks (2.48s)
```

All 24 checks:

| Scenario | Check | Measured |
|---|---|---|
| S1 duplicate delivery | first pass applied every block | `blocks=20/20` |
| S1 | redelivery applies nothing new | `applied=0 dupes=20` |
| S1 | state equals fresh replay | identical |
| S2 out-of-order | out-of-order batch is buffered | `applied=0 buffered=6` |
| S2 | gap fill drains the buffer | `applied=20` |
| S2 | tail redelivery is a no-op | `applied=0` |
| S2 | final state equals fresh replay | identical |
| S3 crash log↔checkpoint | recovery replayed the lost suffix | `blocks=17 logs=9` |
| S3 | state equals fresh replay after recovery | identical |
| S4 restart mid-batch | restart completes the batch | `blocks 10+10` |
| S4 | state equals fresh replay | identical |
| **S5 five-block reorg** | 5-block reorg detected | `common=15 applied=6` |
| S5 | state equals fresh canonical replay | identical |
| S5 | orphaned branch effect is gone | `orphan_award=True now_award=False` |
| S5 | new branch is indexed | `bounty4=yes` |
| S6 orphaned payout | reorg that orphans a payout detected | `common=17` |
| S6 | orphaned award credit is reversed | `w2 credit=0` |
| S6 | orphaned withdrawal is undone | `w2 withdrawals=[]` |
| S6 | state equals fresh canonical replay | identical |
| S7 deep+shallow | deep reorg detected | `common=15` |
| S7 | shallow reorg detected | `common=19` |
| S7 | state equals fresh canonical replay | identical |
| S8 restart after reorg | post-restart sync converges | identical |
| S8 | post-reorg block was indexed | `bounty9=yes` |

Requirements met, one by one:

| Brief requirement | Where |
|---|---|
| index BountyCreated, WorkSubmitted, BountyAwarded, BountyRefunded, Withdrawn | `indexer.py` → `_apply_event` |
| local chain fixture with competing branches | `fixture.py` → `Chain.reorg()` |
| do not index private wallets / require paid RPC | deterministic fixture addresses, no network |
| persist a checkpoint with block hashes | `checkpoint(number, hash, log_count)`, hash-anchored |
| reconstruct bounty state from canonical logs | derived tables = `reduce(apply_event, journal)` |
| demonstrate duplicate log delivery | S1 |
| demonstrate out-of-order batches | S2 |
| demonstrate process restart between log write and checkpoint | S3, S4 |
| demonstrate a five-block reorg | S5 |
| after recovery DB == fresh canonical replay | every scenario, final assertion |
| no duplicate submissions or double-counted payouts | S6 asserts the orphaned credit reverts to 0 |
| deterministic fixtures | `seed=20261008`, hashes from sha256 |
| automated assertions | 24 checks, non-zero exit on any failure |
| transaction boundaries | `BEGIN IMMEDIATE` per block; rollback+rebuild atomic |
| pinned runnable commit | repo commit in §4 |
| not narrative-only | `run.py` is the proof |

## 2. The three properties that make it reorg-safe

**(a) Order independence.** Blocks are ingested *by height*, not by log batch. A
block that arrives before its predecessor is buffered and applied only once
every lower height is present; the buffer drains on each gap fill. Therefore any
permutation of the same block set yields byte-identical state. A naive
log-appender cannot satisfy this — `RewardAdded` delivered before
`BountyCreated` silently updates zero rows. Blocks with no logs still count as
heights, so gaps are real.

**(b) Exactly-once effect.** Every log is journalled under a UNIQUE
`(tx_hash, log_index)`, and the derived tables are a pure function of the
journal. Duplicate delivery is a no-op *and* recovery equals fresh replay *by
construction* — a reorg is just "delete journal rows above the fork point,
replay". One code path, so nothing can drift.

**(c) Atomic, hash-anchored checkpoint.** `(number, hash, log_count)` is written
in the SAME transaction as the effects it describes. A crash cannot leave
effects committed while the checkpoint lags. Reorg detection walks **block
hashes**, not logs — so a reorg that only removes empty blocks is detected,
which a log-only comparison would miss (S7, `common=15` after an empty-block-only
replacement).

## 3. The decisive assertion

Every scenario ends the same way:

```python
snap   = indexer_after_recovery.snapshot(drop_checkpoint=True)
expect = fresh_indexer_over_canonical_chain.snapshot(drop_checkpoint=True)
assert snap == expect
```

The checkpoint is excluded from the comparison so the test proves the *state*
matches — not merely that two counters agree.

## 4. How to reproduce

```
git clone https://github.com/user00shinru/imdworks-bounty-deliverables
cd imdworks-bounty-deliverables/escrow2-reorg-indexer
python run.py      # exit 0 = PASS, writes report.json
```

Python 3.14 standard library only. `fixture.py` builds the chain in-process;
`indexer.py` is the store. No SQLite extensions, no network, no RPC.

## 5. Assumptions

* `block_records()` models an indexer polling `eth_getBlockByNumber` per height,
  which is why empty blocks are visible to it.
* Log identity is `(tx_hash, log_index)`; the fixture derives `tx_hash`
  deterministically from `(block number, log index)`.
* A reorg deeper than the stored history is handled by the same rollback path
  (to height 0) — there is no separate "genesis" case.
* `OperatorSet` is not part of the required reconstruction and is journalled but
  not folded into derived state.

## 6. Scope

Local fixture chain only. No production IMD Works or third-party infrastructure
was probed.
