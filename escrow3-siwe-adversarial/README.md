# escrow 3 — adversarial wallet sign-in regression suite

A local sign-in service and a black-box regression suite that attacks it with
**32 adversarial cases** and asserts it refuses every one. Wallets are generated
in-process; nothing touches a network, a real key, or any production service.

```bash
node run.js          # -> verdict PASS/FAIL, report.json
```

No `npm install` — `ethers` (6.17) is vendored under `vendor/node_modules/`.

## What the service does

`src/service.js` implements the nonce-bound `personal_sign` flow a dapp uses:

```
challenge(address) -> { nonce, message, expiresAt }     # single-use nonce, EIP-4361 message
verify({message, signature}) -> { token, address }      # checks, then issues a session
session(token) -> { address }
```

It is written the way a careful implementation *should* be — that is the baseline the
suite attacks. Four properties carry the security weight:

| Property | Mechanism |
|---|---|
| nonce is single-use | `rec.used = true` is set **before** the signature check, so a failure cannot be retried |
| exactly one concurrent winner | the same early write closes the TOCTOU window — the loser sees `NONCE_REPLAY` |
| origin/chain/address are bound | the parsed message fields are compared to the challenge and to the live config |
| expiry is exact | `t > expiresAt` rejects past-deadline; `Issued At` in the future is rejected |

## Coverage — 32 cases

| Group | Cases | Examples |
|---|---|---|
| happy path | 1 | valid sign-in + session resolves |
| nonce replay | 2 | replay a completed verify; replay after a bad signature |
| concurrency | 3 | 2-parallel and 8-parallel same-nonce → **exactly one** wins; only one session minted |
| wrong signer | 2 | other wallet signs; address swapped inside the message |
| origin / URI | 4 | URI substituted; **lookalike** domain (`dapp.local.evil.example`); Origin header mismatch; foreign-origin challenge |
| chain id | 2 | chain substituted in message; challenge for another chain |
| expiry boundaries | 4 | exactly at deadline (valid); +1 ms (rejected); message Expiration in the past; Issued At in the future |
| malformed input | 3 | **15-variant** message matrix; 5 signature encodings; 5 request shapes |
| nonce binding | 3 | A's nonce used by B; unknown nonce; 200 nonces all distinct |
| session | 3 | TTL expiry; unknown token; two wallets stay isolated |
| **vulnerable impls** | 2 | both intentionally-broken services are **detected as exploitable** |
| self-consistency | 1 | parser round-trips statement + resources |

## The two intentionally vulnerable implementations

`src/vulnerable.js` ships two services the suite must prove are broken — otherwise the
suite is just confirming a good implementation is good:

1. **`ReplayableAuth`** — validates the nonce but never marks it used, and skips the
   origin check. Exploit: submit the same signed message twice → two sessions.
   Case 30 asserts the second verify **succeeds**, i.e. the hole is real.
2. **`RaceableAuth`** — reads the nonce, awaits (as a real handler awaiting a DB read
   would), *then* writes. Exploit: two concurrent verifies both pass the check.
   Case 31 asserts **both** verifies are fulfilled.

If either assertion fails, the suite has stopped being a regression test — that is why
they are asserted as *exploitable* rather than *fixed*.

## Two bugs the suite found in itself (recorded on purpose)

1. `expectReject` originally returned a truthy `{wrong}` object on a code mismatch, so
   `!!e` was always true and cases passed while asserting the wrong rejection code.
   Hardened to **throw** on any mismatch.
2. The malformed-signature matrix reused one challenge; the first bad signature burned
   the nonce, so later cases reported `NONCE_REPLAY` instead of the encoding fault they
   were meant to test. Each encoding now gets its own nonce.

Both are the exact failure modes a suite is supposed to catch in *itself*: a green run
that proves nothing, and a case that tests the wrong thing.

## Threat-model notes

**In scope (defended):** nonce replay (sequential and concurrent), wrong signer,
signature-malleability/encoding faults, origin and URI substitution including lookalike
domains, chain-id confusion, expiry-boundary confusion, nonce/address confusion,
malformed EIP-4361 messages, session fixation by token guessing (24 random bytes),
cross-wallet session leakage.

**Out of scope (documented, not defended by this local service):** transport security
(TLS), browser extension/wallet UI spoofing, phishing, DNS/BGP hijack of the origin,
supply-chain risk in the vendored `ethers`, and any protection of real credentials —
the service never sees a real key.

## Files

```
src/siwe.js         strict EIP-4361 parser + builder
src/service.js      the careful sign-in service (baseline)
src/vulnerable.js   ReplayableAuth + RaceableAuth (intentionally broken)
src/vendor.js       re-export of the vendored ethers
run.js              32-case suite -> report.json
report.json         generated verdict (committed)
vendor/node_modules/ vendored ethers 6.17 + @noble + tslib + ws (off-line)
```
