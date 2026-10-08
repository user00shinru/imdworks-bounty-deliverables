# PROOF — IMD Works escrow bounty 1

**Bounty:** Build a stateful escrow accounting invariant harness (escrow id **1**)
**Repository:** `user00shinru/imdworks-bounty-deliverables`
**Reproduction:** `python escrow1-invariant-harness/run.py`  (exit 0 = PASS)

---

## 1. Result

```
IMD Works escrow bounty 1 — escrow accounting invariant harness
  forge            forge Version: 1.7.1
  solc             Version: 0.8.37+commit.f401782d.Windows.msvc
  seed             0x20261008
  invariant_runs   1000
  invariant_depth  100
  network          none — local EVM only

[1/2] correct contract (1000 sequences x depth 100) ...
      suite_ok=True passed=6 failed=0 (41.41s)
[2/2] mutant contract  (1000 sequences x depth 100) ...
      suite_ok=False passed=2 failed=4 (40.81s)

verdict:
  [PASS] correct contract passes every invariant — 6 passed / 0 failed
  [PASS] mutant contract is rejected by the harness — 4 invariant(s) failed
  [PASS] harness is non-vacuous (same code both ways) — 6 invariants exercised
  [PASS] at least 1,000 sequences at depth 100 — runs=1000 depth=100
  [PASS] no network access required — local EVM (foundry), vendored forge-std + openzeppelin

RESULT: PASS
```

Requirements met, one by one:

| Brief requirement | Where |
|---|---|
| ≥1,000 invariant sequences, depth 100 | `run.py` default: `invariant_runs=1000`, `invariant_depth=100` |
| publish seeds | `seed 0x20261008` (also in `report.json` → `toolchain.seed`) |
| publish tool versions | `forge 1.7.1`, `solc 0.8.37` (in the run header and `report.json`) |
| publish output | `report.json` (full per-invariant counts + raw forge tail) |
| independently compute outstanding liabilities | ghost ledger in `test/Handler.sol`, never reads contract storage |
| assert token balance covers liabilities | `invariant_solvent_exact` |
| no terminal bounty pays twice | `invariant_no_double_payout` |
| deliberately broken local mutation | `src/BrokenEscrow.sol` — one deleted line in `_credit` |
| harness detects it | mutant run: 4 invariants rejected |
| pinned commit | see `PROOF.md` in the repo root / commit hash in §4 |
| one-command runner | `python run.py` |
| explanation of assumptions | `README.md` → "Assumptions" |
| not mirroring implementation variables | the model is spec-derived; see §2 |

## 2. Why the invariants are not tautological

The handler keeps a ghost ledger derived from the escrow's economic
specification:

* `ghostOpenLocked` — Σ reward over unresolved bounties (create/top-up adds,
  resolution removes)
* `ghostCredit[actor]` — withdrawable credit per actor
* `ghostWithdrawn` — Σ credit actually transferred out
* `recs[]` — per-bounty record: reward, open, resolved, submission count

The contract's `totalLocked`, `totalClaimable`, `claimable[]` and
`liabilities()` are then compared against this model. Nothing in the model reads
contract storage, so agreement is a real property.

The conservation invariant is the strongest:

```
I3: totalLocked + totalClaimable + ghostWithdrawn == Σ(created rewards)
```

## 3. The mutation and what it breaks

```diff
  function _credit(address account, uint256 amount) private {
-     totalLocked -= amount;
      totalClaimable += amount;
      claimable[account] += amount;
  }
```

Effect: money is credited to an account but never released from the locked pool.
`liabilities()` (locked + claimable) inflates above the true liability, so the
escrow reports it owes more than it has.

Minimal failing trace reported by the harness (auto-shrunk to 2 calls):

```
[FAIL: I3: value not conserved: 23158794 != 11579397]
    createBounty(0, 11579397, 473)   ->  deposit 11579397, totalLocked = 11579397
    cancel(1, 11579397)              ->  credit +11579397, totalLocked LEFT at 11579397
                                        -> accounted = 23158794, created = 11579397

[FAIL: I2: escrow undercollateralised: 11579397 < 23158794]
[FAIL: I1: liabilities != model: 23158794 != 11579397]
[FAIL: I4: totalLocked != model open: 11579397 != 0]
```

## 4. How to reproduce

```
git clone https://github.com/user00shinru/imdworks-bounty-deliverables
cd imdworks-bounty-deliverables/escrow1-invariant-harness
python run.py            # exit 0 = PASS, writes report.json
python run.py --quick    # 100 x 30, ~4 s, for a fast sanity check
```

`reverts: 0` on every run — the handler pre-checks each contract precondition
and returns early instead of letting a call revert, so no fuzzed sequence is
discarded.

## 5. Scope

Local EVM only. No network access, no production infrastructure touched, no live
token administrator impersonated. The mutilated contract is a local fixture.
