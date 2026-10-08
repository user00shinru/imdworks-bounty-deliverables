# Canonical brief hashing — specification (imdworks-brief-v1)

## Problem

A bounty brief is stored off-chain, but its integrity is anchored on-chain as
`bytes32 briefHash` inside `IMDWorksEscrow.Bounty`. Two independent clients — one
in JavaScript, one in Python — must be able to take the *same* brief, serialize
it, and arrive at the **same 32 bytes**. If they disagree, a brief that a
verifier considers valid is one a contract considers tampered with.

The hard part is not hashing. It is deciding exactly which inputs are
canonical, and refusing the rest loudly instead of "helpfully" normalising them.

## Canonical form

```
"imdworks-brief-v1\n"
  "title"       " " <decimal byte length> ":" <utf-8 bytes>
  "description" " " <decimal byte length> ":" <utf-8 bytes>
  "criteria"    " " <decimal byte length> ":" <utf-8 bytes>
  "reward"      " " <decimal byte length> ":" <ascii decimal digits>
  "token"       " " <decimal byte length> ":" <0x + 40 lowercase hex>
  "deadline"    " " <decimal byte length> ":" <ascii decimal digits>
```

joined with a single `\n` (0x0A) between records.

`briefHash = keccak256(canonical_bytes(brief))`.

### Why not `JSON.stringify` / `json.dumps`

`json.dumps` and `JSON.stringify` disagree in observable ways:

| Case | Python `json.dumps` | JS `JSON.stringify` |
|---|---|---|
| non-ASCII | `"\u00e9"` (escaped) | `"é"` (raw) |
| `/` | `"/"` | `"/"` (but `<\/` in some serialisers) |
| key order | insertion order | insertion order |
| big int | exact | destroyed above 2^53 |
| floats | `1.0` | `1` |

Any of these would make the two languages produce different bytes for the same
logical brief. A length-prefixed record stream removes every one of those
choices: there is no escaping, no key order, no float, and no serializer
involved at all.

## Rules (normative)

### R1 — String fields are byte-exact, never normalised
`title`, `description` and `criteria` are taken **exactly as stored**. No NFC/NFD
folding, no whitespace trimming, no line-ending rewriting.

* `\n` inside a string is preserved verbatim (it is a byte like any other).
* `\r` is **rejected** (`cr_character`), because CR is the classic source of
  "looks identical, hashes differently" between a Windows and a Unix editor.
  Rejecting is safer than choosing a winner.
* `\x00` is **rejected** (`nul_byte`).

The zero-width-joiner test vector `U+200D` is **accepted** and hashes as its own
distinct brief — we do not silently strip it, because stripping would mean a
brief containing an invisible character hashes identically to one without it,
which defeats the purpose of an integrity hash.

### R2 — Unicode must already be NFC
A string whose NFC form differs from itself is rejected (`not_nfc`). Example:
`"café"` written as `caf` + `U+0301` (NFD) is rejected; `caf` + `U+00E9` (NFC)
is accepted.

Rationale: `U+00E9` and `U+0065 U+0301` render identically. If both were
accepted they would hash differently, and no human looking at the two briefs
could tell why. Rejecting the decomposed form makes the ambiguity a hard error
at authoring time instead of a silent hash divergence at verification time.

### R3 — Every field is required; unknown fields are rejected
Exactly the six keys. A missing key is `missing_field`; any extra key is
`unknown_field`. This includes `__proto__` and `constructor`, which are
ordinary strings in Python but dangerous in JavaScript (see Finding 1).

### R4 — Numeric fields are canonical unsigned decimals
`reward` and `deadline` accept either:

* a JSON integer, or
* a **decimal string** (`"1000000"`).

A JSON integer is required to fit in the transport exactly. Because JSON
numbers are float64, a uint256 cannot survive a JSON round-trip in JavaScript —
so a string form exists, and **both** implementations accept it.

Rejected: negative (`negative`), leading zeros (`not_canonical_decimal`),
exponent (`not_canonical_decimal`), fraction (`type` if a JSON float,
`not_canonical_decimal` if a string), overflow above `2^256-1`
(`overflow_uint256`).

Floats are rejected outright rather than converted. `0.1 + 0.2` is not `0.3`,
and a reward that silently becomes a different integer is worse than an error.

### R5 — `token` is a lowercase `0x`-prefixed 20-byte address
Upper-case hex is rejected (`non_canonical_hex`) rather than lower-cased, so
that the stored form is the only accepted form.

## How a client distinguishes byte equality from semantic similarity

This is the core question the bounty brief asks.

* **Byte equality** is what the hash proves. Two briefs with the same hash are
  byte-identical by construction (collision resistance aside). There is no
  path through `canonical_bytes` that produces the same bytes from different
  inputs, because the length prefixes and the fixed key order remove every
  free choice.
* **Semantic similarity** is *not* provable by the hash and must **never** be
  inferred from it. `"Run 1000 cases"` and `"Run 1,000 cases"` are semantically
  the same instruction and hash differently. `"café"` in NFC and NFD are
  semantically the same word; one is rejected.
* A client that wants to know "is this the same *meaning*" must use a
  different mechanism (e.g. a human-readable diff). A client that wants to know
  "is this the same *brief*" compares hashes. Conflating the two is the
  vulnerability: an implementation that normalises before hashing silently
  upgrades a semantic question into a byte-equality claim.

## How immutable historical briefs remain verifiable

The hash covers only the six fields, in a fixed order, with a versioned prefix
(`imdworks-brief-v1`). That yields three properties:

1. **No re-serialisation drift.** A brief authored today and verified in five
   years produces the same bytes, because the algorithm is not
   serializer-dependent. Upgrading a runtime's JSON library cannot change a
   historical hash.
2. **Explicit versioning.** If the spec ever changes, the prefix changes
   (`imdworks-brief-v2`), and old hashes remain computable by keeping the v1
   function. A historical brief is never re-hashed under a new rule.
3. **Rejection is stable.** A brief that is rejected today (`not_nfc`) is
   rejected forever. Rejection is part of the contract, not an implementation
   detail, so a verifier cannot "fix" a brief and end up with a hash that a
   different client disagrees with.

## Cross-language verification

`python run.py` runs **both** implementations against the *same* frozen vector
file and, additionally, compares the raw canonical **bytes** for every accepted
vector — not just the final hash. Hash-only comparison would still pass if two
different byte streams collided; byte comparison does not allow that to hide.
