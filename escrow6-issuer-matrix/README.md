# escrow 6 — model issuer restrictions without losing escrow credits

Behaviour matrix for the **unmodified** `IMDWorksEscrow` driven against six
issuer-controlled ERC-20 personalities. The question the bounty asks is narrow and
answerable: *when a token issuer pauses, restricts, taxes or lies, does the escrow
lose user credits?* The answer is **no** — bookkeeping is independent of token
transferability — and this suite proves it case by case.

## Design invariant under test

> `claimable`, `totalClaimable` and `totalLocked` are mutated **before** any token
> effect, and a failed transfer **reverts the whole call**, so credits can never be
> silently destroyed. Conversely, awarding and refunding are pure bookkeeping and
> succeed even while the token blocks all movement.

Two contract mechanics make this hold:

1. `_deposit` / `withdraw` measure `balanceOf` **before and after** the transfer and
   revert with `UnsupportedTransfer` unless the delta is exactly `amount`, in the
   right direction (`IMDWorksEscrow.sol:156-161`, `:189-192`).
2. `withdraw` zeroes `claimable[msg.sender]` *before* calling the token
   (`:154`), and every money path is `nonReentrant`.

## Behaviour matrix

| Token mode | Escrow support | What happens | Recovery requires issuer? |
|---|---|---|---|
| exact transfer (baseline) | **supported** | normal deposit / award / withdraw | no |
| pause (all moves frozen) | **partially supported** | deposit reverts; award + refund bookkeeping still succeed; withdraw reverts, credit preserved | **yes** — unpause |
| sender restricted | **partially supported** | deposit reverts; withdraw reverts when the escrow is the blocked sender; credit preserved | **yes** — unblock |
| recipient blocklisted | **partially supported** | withdraw to the blocked wallet reverts; credit preserved; winner may route to another wallet | maybe — or reroute |
| transfer tax / fee-on-transfer | **unsupported** | `UnsupportedTransfer` on deposit **and** on withdraw; credit preserved | no — token is simply incompatible |
| false-return on failure | **unsupported** | `SafeERC20` converts `false` into a revert; no silent loss | no |
| no-return (pre-standard) | **supported** | `SafeERC20` tolerates empty returndata; exact 1:1 still enforced | no |
| callback reentrancy | **defended** | re-entrant `withdraw` from the token fails; exactly one payout | no |

## Evidence per row

| Test | Proves |
|---|---|
| `test_base_exactTransfer_supported` | baseline flow + conservation |
| `test_pause_blocksDeposit_butBookkeepingUnaffected` | failed deposit leaves `totalLocked==0` and does **not** consume `nextBountyId`; award credits while paused; failed withdraw preserves `claimable` |
| `test_pausedToken_doesNotBlockExpireBookkeeping` | `expire()` refunds to books while paused; `Status.Expired` (enum = 4) |
| `test_senderRestricted_depositReverts_withdrawRecoverable` | directional sender block on creator *and* on escrow |
| `test_recipientBlocked_withdrawReverts_creditStays` | blocked recipient cannot destroy credit; `withdraw(bob)` reroutes it |
| `test_taxToken_rejected_unsupportedTransfer` | fee-on-transfer deposit rejected |
| `test_taxToken_onWithdraw_rejectsAndPreservesCredit` | issuer flips the fee on *after* award → withdraw rejected, credit intact |
| `test_falseReturn_revertsViaSafeERC20_noSilentLoss` | false-return never produces a phantom credit |
| `test_noReturnToken_supported_exactTransfer` | pre-standard token still pays exactly |
| `test_callbackReentrancy_attempt_cannotDoubleSpend` | exactly one payout, zero residual claimable |

## Conservation

`_assertConservation(esc, token, deposited)` is invoked in **8** cases and asserts:

```
liabilities() == totalLocked + totalClaimable
liabilities() <= token.balanceOf(escrow) + 1     # solvent (1 unit for int rounding)
token.balanceOf(escrow) <= deposited             # never holds more than was deposited
```

The 1-unit tolerance is intentional: the tax mock burns the fee, so a fully-drained
escrow can sit 1 unit under its book value — a token-side burn, never a credit loss.

## Run it

```bash
python run.py          # full: forge test + matrix + report.json
python run.py --quick  # same tests, skips the report write
```

Requires `forge` (Foundry) on PATH. Everything else — `forge-std` and
`openzeppelin-contracts` — is vendored under `lib/`, so there is nothing to install.

## Scope boundary — supported vs unsupported

**Supported:** exact-transfer ERC-20s, whether they return `true` or nothing, and
even when the issuer *temporarily* pauses or blocklists (credits survive; the user
waits, or the issuer unfreezes).

**Unsupported by design:** any token that is rebasing, fee-on-transfer, or changes
balances by any amount other than exactly the transferred value. The contract's own
header states this (`IMDWorksEscrow.sol:11`), and  `_deposit`/`withdraw` enforce it.
This is a *documented* limitation, not a bug: it is the precise reason credits
cannot be silently drained by a lying token.

## Files

```
src/IMDWorksEscrow.sol        unmodified contract (verbatim, solc 0.8.29)
src/mocks/IssuerTokens.sol    BaseToken + 6 issuer personalities + reentrancy mock
test/IssuerMatrix.t.sol       10 tests
run.py                        one-command runner -> report.json
report.json                   generated matrix + checks (committed)
```
