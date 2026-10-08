# escrow 2 — reorg-safe bounty event indexer

Indexes `BountyCreated`, `WorkSubmitted`, `BountyAwarded`, `BountyRefunded` and
`Withdrawn` from canonical blocks, persists a hash-anchored checkpoint, and
reconstructs bounty state from canonical logs after arbitrary disruption.

```
python run.py            # exit 0 = PASS, writes report.json
```

Everything is local: a deterministic in-process chain fixture (`fixture.py`)
plus SQLite. No RPC, no network, no real chain, no private wallets.

## The three properties that make it reorg-safe

### 1. Order independence — blocks are ingested by height, not by batch

A block delivered before its predecessor is **buffered**, not applied. It is
applied only once every lower height is present. After each application the
buffer is drained for as long as the next contiguous height exists.

Consequence: any permutation of the same block set yields byte-identical derived
state. This is what makes "out-of-order batches" safe rather than merely
tolerated — a naive log-appender cannot satisfy it, because `RewardAdded` before
`BountyCreated` silently updates zero rows.

Blocks with no logs still count as heights, so gaps are real gaps.

### 2. Exactly-once effect — the journal is the source of truth

Every applied log is written to `journal` under a UNIQUE `(tx_hash, log_index)`.
Redelivering a block is a no-op. Derived tables (`bounties`, `credits`,
`withdrawals`) are a **pure function of the journal** — `reduce(apply_event, ...)`.

This single property collapses several bounty requirements into one:

* duplicate delivery cannot double-count,
* recovery equals fresh replay *by construction*, not by test,
* a reorg is just "delete journal rows above the fork point, replay".

### 3. Atomic checkpoint — hash-anchored, same transaction as the effects

The checkpoint is `(number, hash, log_count)`, written in the **same
transaction** as the state it describes. A crash can therefore never leave
effects committed while the checkpoint lags. If the process dies between a log
write and the checkpoint write, recovery *replays* instead of skipping.

Reorg detection compares the stored hash at every shared height, walking
**block hashes, not logs** — so a reorg that only removes empty blocks is
detected too, which a log-only comparison would miss.

## Scenarios

| # | Scenario | Key assertion |
|---|---|---|
| S1 | duplicate block + log delivery | first pass 20/20; redelivery applies 0 |
| S2 | out-of-order blocks (tail first) | tail buffered, gap fill drains it, re-sent tail is a no-op |
| S3 | crash between log write and checkpoint | lost suffix is *replayed*, state == fresh replay |
| S4 | process restart mid-batch | two halves converge to fresh replay |
| S5 | **five-block reorg** | detected, common ancestor found, state == fresh canonical replay, new branch indexed |
| S6 | reorg that orphans a payout | orphaned credit reverts to 0; orphaned withdrawal removed |
| S7 | deep then shallow reorg | both detected; converges |
| S8 | restart after reorg, then continue | post-reorg blocks indexed |

Every scenario ends with the same decisive comparison:

```python
snap   = indexer_after_recovery.snapshot(drop_checkpoint=True)
expect = fresh_indexer_over_canonical_chain.snapshot(drop_checkpoint=True)
assert snap == expect
```

The checkpoint itself is excluded from the comparison so the test proves the
*state* matches, not merely that two counters agree.

## Measured result

```
RESULT: PASS — 24/24 checks (2.48s)
```

## Assumptions

* `block_records()` models an indexer polling `eth_getBlockByNumber` per height
  (which is why empty blocks are visible). A log-only poll would need the same
  height bookkeeping from the provider.
* Block/log identity is `(tx_hash, log_index)`; the fixture derives `tx_hash`
  deterministically from `(block number, log index)`.
* Reorg depth is bounded by the block history the indexer has already stored;
  a reorg deeper than the stored history is handled by the same rollback to
  height 0.
* SQLite is the store. The transaction boundaries are `BEGIN IMMEDIATE` around
  each block application and each rollback+rebuild, so a crash cannot leave a
  half-applied block.

## Reproduce

```
cd escrow2-reorg-indexer
python run.py            # writes report.json, exit 0 on PASS
```

Python 3.14 stdlib only. Pinned commit recorded in `PROOF.md`.
