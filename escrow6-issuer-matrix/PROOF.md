# PROOF — IMD Works escrow 6

**Bounty:** Model issuer restrictions without losing escrow credits (escrow id **6**)
**Reward:** 1 USDG · **Deadline:** 2026-10-14T09:54:30Z
**Contract under test:** `IMDWorksEscrow` **unmodified** (solc 0.8.29, optimizer 200, paris)

## Deliverable

A behaviour matrix with executable tests for six issuer-controlled ERC-20
personalities, plus a callback-reentrancy attempt and a conservation assertion.

```
src/mocks/IssuerTokens.sol   BaseToken + Pausable / SenderRestricted / RecipientBlocked
                             / Tax / FalseReturn / NoReturn + ReentrantCallbackToken
test/IssuerMatrix.t.sol      10 tests
run.py                       one-command runner -> report.json
```

## Result — 10/10 tests pass

| # | Token mode | Escrow support | What the test proves |
|---|---|---|---|
| 1 | exact transfer | supported | baseline deposit/award/withdraw + conservation |
| 2 | paused | partially supported | failed deposit leaves `totalLocked==0` and does **not** consume `nextBountyId`; award credits while paused; failed withdraw preserves `claimable` |
| 3 | paused + expiry | partially supported | `expire()` refunds to books while paused; `Status.Expired` (enum 4) |
| 4 | sender restricted | partially supported | directional block on creator and on the escrow; credit preserved |
| 5 | recipient blocklisted | partially supported | credit preserved; `withdraw(bob)` reroutes it |
| 6 | tax / fee-on-transfer | unsupported | `UnsupportedTransfer` on deposit |
| 7 | tax switched on after award | unsupported | withdraw rejected, credit intact |
| 8 | false-return | unsupported | `SafeERC20` converts `false` into a revert; no phantom credit |
| 9 | no-return (pre-standard) | supported | still pays exactly 1:1 |
| 10 | callback reentrancy | defended | exactly one payout, zero residual claimable |

## Key findings

1. **Bookkeeping is independent of transferability.** `award()` and `expire()` are
   pure ledger mutations and succeed even while the token blocks every movement —
   so a pause or blocklist **cannot** lose a credit, it only delays settlement.
2. **Credits survive every failed transfer.** `withdraw` zeroes `claimable` before
   the transfer and reverts the whole call if the token misbehaves, so the credit is
   restored atomically. Verified in 4 modes (pause, sender-block, recipient-block,
   false-return, tax).
3. **Deposits are delta-checked.** `_deposit` requires `afterBalance - beforeBalance
   == amount`; fee-on-transfer and false-return tokens are rejected outright.
4. **Reentrancy is defended twice over.** `nonReentrant` + credit zeroed before the
   external call ⇒ the re-entrant `withdraw` in the token callback cannot double-spend.
5. **Recovery paths that need the issuer:** unpause, unblock sender/recipient. The
   only thing needing *no* issuer action is routing a credit to another wallet
   (`withdraw(otherAddress)`).

## Supported vs unsupported (explicit boundary)

* **Supported** — exact-transfer ERC-20 (returns `true` or nothing), even under a
  temporary pause/blocklist; credits survive and settle after the issuer lifts it.
* **Unsupported by design** — rebasing, fee-on-transfer, or any token whose balance
  delta ≠ transferred value. This matches the contract's own documented limitation
  (`IMDWorksEscrow.sol:11`) and is the reason credits can't be silently drained.

## Conservation

`_assertConservation` is invoked in **8** cases and asserts:

```
liabilities() == totalLocked + totalClaimable
liabilities() <= token.balanceOf(escrow) + 1
token.balanceOf(escrow) <= deposited
```

The 1-unit tolerance covers the tax mock burning the fee (a token-side burn, never a
credit loss).

## Reproduce

```bash
cd escrow6-issuer-matrix
python run.py          # forge test + matrix + report.json  -> verdict PASS
```
