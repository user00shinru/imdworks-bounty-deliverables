# PROOF — IMD Works escrow 3

**Bounty:** Construct an adversarial wallet sign-in regression suite (escrow id **3**)
**Reward:** 1 USDG · **Deadline:** 2026-10-14T09:54:30Z

## Deliverable

A local sign-in service (nonce-bound `personal_sign`, EIP-4361 messages) plus a
black-box regression suite of **32 adversarial cases**. Wallets are generated
in-process; no network, no real credentials, no production infrastructure.

```
src/siwe.js         strict EIP-4361 parser + builder
src/service.js      nonce-bound sign-in service (the defended baseline)
src/vulnerable.js   two intentionally broken services the suite must detect
run.js              32-case suite -> report.json
report.json         committed verdict
vendor/node_modules/ethers 6.17 vendored -> `node run.js`, no npm install
```

## Result — PASS, 32/32 cases, deterministic

```
verdict: PASS  (32/32 cases, ~210 ms)
re-ran 4× consecutively -> PASS each time (race cases are deterministic)
```

## Criteria coverage

| Criterion | Where |
|---|---|
| **≥ 20 adversarial cases** | **32** cases |
| nonce replay | cases 2–3: replay of a completed verify; replay attempted after a failed signature |
| wrong signer | cases 7–8: another wallet signs; address swapped in the message body |
| substituted origin | cases 9–12: URI substitution, **lookalike domain**, Origin-header mismatch, foreign-origin challenge |
| expiration boundary | cases 15–18: exactly-at-deadline (valid), **+1 ms** (rejected), message Expiration in the past, Issued At in the future |
| malformed signature | case 20: 5 encodings (empty, short, 32-byte r‖s, non-hex, bad v) — all refused |
| **two concurrent verifications of one nonce** | cases 4–6: **exactly one succeeds** (also at 8-way parallelism) |
| **≥ 2 intentionally vulnerable implementations the suite rejects** | `ReplayableAuth` (case 30) and `RaceableAuth` (case 31) — each asserted *exploitable* |
| executable runner | `node run.js` → exit 0/1 + `report.json` |
| threat-model notes | README §Threat-model — in-scope vs out-of-scope, split explicitly |
| no real credentials / production attacks | wallets generated in-process; service is in-memory only |

## The concurrency guarantee, stated precisely

The nonce is consumed **before** the signature is checked:

```js
if (rec.used) throw new AuthError('NONCE_REPLAY', ...);
rec.used = true;                       // <- write happens first
...                                    // signature check follows
```

That ordering is what makes `Promise.allSettled([verify, verify])` resolve to exactly
one fulfilment — the losing call observes `used === true`. Case 4 asserts
`fulfilled === 1`; case 5 asserts only the winner's token resolves a session.

## The two vulnerable implementations

| Impl | Hole | Case asserts |
|---|---|---|
| `ReplayableAuth` | nonce never marked used; origin not compared | the **second** verify of the same message **succeeds** → replay confirmed |
| `RaceableAuth` | read → `await` → write (TOCTOU) | **both** concurrent verifies are fulfilled → race confirmed |

They are asserted as *exploitable*, not as *fixed*. A suite that merely says "my good
implementation is good" proves nothing; these two cases prove the suite can tell the
difference.

## Two bugs this suite found in itself (kept as evidence)

1. **A helper that could not fail.** `expectReject` returned a truthy `{wrong}` object
   when the rejection *code* differed, so `!!e` was always true — cases could pass while
   asserting the wrong code. Fixed to **throw** on any mismatch; several case
   expectations then had to be corrected (e.g. a 32-byte `r‖s` blob decodes and recovers
   to an unrelated address, so the correct code is `WRONG_SIGNER`, not `BAD_SIGNATURE`).
2. **A matrix that tested the wrong thing.** The malformed-signature matrix reused a
   single challenge; because a signature-level failure *burns the nonce by design*, the
   later encodings reported `NONCE_REPLAY` rather than their own fault. Each encoding
   now gets its own nonce.

Both are the failure modes a regression suite exists to catch: a green run that proves
nothing, and a case that asserts the wrong behaviour.

## Reproduce

```bash
cd escrow3-siwe-adversarial
node run.js            # -> verdict: PASS (32/32)
echo $?                # 0
```
