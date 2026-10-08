# escrow 10 — auditable bounty ledger reconciliation tool

Reconstructs the escrow's **locked rewards**, **per-wallet credits**, **total
liabilities** and **token balance** from the **event log alone**, then checks the
result against contract state read at a fixed snapshot block. Events are the only
input to the ledger; state is only ever used for comparison. That separation is what
makes this an independent reconciliation rather than a re-read of the contract.

## What it does

```
forge test --match-test test_generate_fixture     # writes fixture/ledger_fixture.json
python reconcile.py                               # replays it -> report.json
```

The Solidity generator drives the **unmodified** escrow through a deterministic,
seeded set of 100 bounty lifecycles and emits:

| Stream | Contents |
|---|---|
| `events` | `BountyCreated`, `RewardAdded`, `WorkSubmitted`, `BountyAwarded`, `BountyRefunded`, `Withdrawn` — each with `block` + `logIndex` |
| `tokenEvents` | direct token donations (a `Transfer` into the escrow that is **not** a deposit) |
| `state` | `totalLocked`, `totalClaimable`, `liabilities`, `escrowTokenBalance`, `nextBountyId` at the snapshot |
| `wallets` / `bounties` | per-wallet `claimable`, per-bounty `status` + `reward` |

## The three rules that make it correct

1. **Dedupe on log position, never on content.** A re-org replay delivers the same
   `(block, logIndex)` twice and must be idempotent. Deduping on `(kind, payload)`
   instead *silently merges two legitimate identical withdrawals* (same account,
   same amount, different time) and over-reports credits. This bug was hit and fixed
   during development — see `replay()`.
2. **Unsolicited donations are surplus, not credit.** A token transfer into the
   escrow that came from a plain `transfer` creates **no liability** — the contract
   only credits from events it emits. Therefore
   `escrowTokenBalance − liabilities == Σ donations`, and any remainder is surplus.
   This is exactly why the tool reports donations separately.
3. **A failed withdrawal preserves credit.** Credit is only reduced on a
   `Withdrawn` event for that exact account and amount. A revert produces no event,
   so the balance survives — verified by the escrow's own `UnsupportedTransfer`
   delta check.

## Corruption detection (must diverge)

| Tamper | Result |
|---|---|
| extra `BountyCreated` at a **new** log position | totals diverge → caught |
| removed `BountyCreated` | next refund exceeds locked → `ReconError` |
| removed `Withdrawn` | credits over-report → caught |
| refund `status` flipped to a non-refund value | `ReconError: unknown refund status` |

## Re-org idempotency (must stay consistent)

The whole stream delivered twice (every event at the same position) must leave the
ledger **identical** — 273 duplicate positions dropped, liabilities unchanged.

## Snapshot mismatch

`reconcile.py --fixture <other.json>` reconciles any snapshot. Point it at a fixture
generated at a different block and the totals check fails with the exact derived vs
chain values, which is the "mismatched block snapshot" signal the brief asks for.

## Run it

```bash
forge test --match-test test_generate_fixture   # regenerate the fixture (needs forge)
python reconcile.py                             # reconcile -> report.json
```

Only Python stdlib is needed for the reconciler. Foundry + the vendored
`forge-std`/`openzeppelin-contracts` under `lib/` are needed to regenerate the
fixture; the committed fixture means `reconcile.py` runs on its own.

## Files

```
src/IMDWorksEscrow.sol       unmodified contract (verbatim, solc 0.8.29)
src/mocks/IssuerTokens.sol   BaseToken + DonorToken (donation helper)
test/LedgerFixture.sol       deterministic seeded generator (100 lifecycles)
test/LedgerGen.t.sol         writes fixture/ledger_fixture.json
fixture/ledger_fixture.json  committed fixture (seed 539365384)
reconcile.py                 independent reconciler + corruption/idempotency harness
report.json                  generated verdict + checks (committed)
```
