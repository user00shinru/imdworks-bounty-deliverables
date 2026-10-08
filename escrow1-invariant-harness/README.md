# escrow 1 — stateful escrow accounting invariant harness

Foundry invariant harness for the deployed `IMDWorksEscrow` source. Models
**three creators, five workers, operator delegation, top-ups, time jumps,
cancellation, awards and withdrawals** on a local EVM.

```
python run.py            # 1000 sequences x depth 100  → exit 0 = PASS
python run.py --quick    #  100 sequences x depth  30
```

## Why the assertions are not tautologies

The brief warns: *"Tests that only mirror implementation variables do not
qualify."* So the handler maintains a **ghost ledger** derived from the economic
specification, never from the contract's own storage:

| Model variable | Meaning | Source |
|---|---|---|
| `ghostOpenLocked` | Σ reward over unresolved bounties | incremented on create/top-up, decremented on award/cancel/expire |
| `ghostCredit` | per-actor withdrawable credit | credited to winner or creator when a bounty resolves |
| `ghostWithdrawn` | Σ credit actually transferred out | incremented in `withdraw` |
| `recs[]` | per-bounty record (reward, open, resolved, submissions) | the model's own bookkeeping |

The contract's `totalLocked`, `totalClaimable`, `claimable[]` and `liabilities()`
are then **compared against the model**. Agreement is a property of the
implementation, not an identity.

## Invariants

| # | Invariant | Property |
|---|---|---|
| I1 | `invariant_liabilities_match_model` | `liabilities() == ghostOpenLocked + ghostCreditTotal` |
| I2 | `invariant_solvent_exact` | token balance `>=` liabilities **and** `==` liabilities (no surplus, no deficit) |
| I3 | `invariant_conservation_of_value` | `totalLocked + totalClaimable + ghostWithdrawn == Σ created rewards` |
| I4 | `invariant_split_matches_model` | `totalLocked == ghostOpenLocked` **and** `totalClaimable == ghostCreditTotal` |
| I5 | `invariant_no_double_payout` | every resolved bounty has left `Status.Open` and reached a terminal status; every unresolved one is still `Open` |
| I6 | `invariant_locked_never_exceeds_created` | locked and claimable never exceed everything ever created |

I3 is the strongest: it asserts money is neither created nor destroyed across
the whole lifetime, using an independently computed "total ever deposited".

## The deliberately broken control

`src/BrokenEscrow.sol` is byte-identical to the deployed escrow **except one
deleted line** in `_credit`:

```diff
  function _credit(address account, uint256 amount) private {
-     totalLocked -= amount;
      totalClaimable += amount;
      claimable[account] += amount;
  }
```

Both contracts are driven by the **same handler, same ghost ledger, same
assertions**. Only the contract under test differs. Without this control a green
run would prove nothing; with it, a green run means the suite is sensitive.

### Measured result

```
correct contract : 6 passed / 0 failed     (runs=1000, depth=100)
mutant  contract : 2 passed / 4 failed     (I1, I2, I3, I4 rejected)
```

The mutant's failure traces are minimal and readable, e.g.

```
[FAIL: I3: value not conserved: 23158794 != 11579397]
    createBounty(0, 11579397, 473)   →  deposit 11579397, totalLocked = 11579397
    cancel(1, 11579397)              ->  credit +11579397, totalLocked LEFT at 11579397
                                        -> accounted = 23158794, created = 11579397
```

## Assumptions

* The escrow's documented asset class: standard, non-rebasing ERC-20 with exact
  transfers (6 decimals in the fixture). Fee-on-transfer, rebasing and
  non-standard-return tokens are out of scope here — bounty 6 covers those.
* Time is advanced only through the handler's `warp`, bounded to 10 days per
  call. Deadline arithmetic uses the fixture's own `block.timestamp`.
* Operators are worker wallets; the escrow forbids self- and contract-operators.
* `reverts: 0` in every run: the handler pre-checks each contract precondition
  and returns early rather than letting a call revert, so no fuzzed sequence is
  discarded. That number is itself a signal — a non-zero revert count would mean
  the handler was probing illegal states and masking coverage.

## Reproduce

```
cd escrow1-invariant-harness
python run.py            # writes report.json, exit 0 on PASS
```

Vendored: `forge-std` 1.16.2, OpenZeppelin 5.4.0 (matching the deployed build).
No network access. Pinned commit recorded in `PROOF.md`.
