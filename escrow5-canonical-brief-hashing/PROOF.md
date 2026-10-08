# PROOF — IMD Works escrow bounty 5

**Bounty:** Design canonical brief hashing across two languages
**Reward target:** IMD Works escrow bounty id **5**
**Repository (pinned):** `user00shinru/imdworks-bounty-deliverables`
**Commit:** `9410f22f727eea8cc2a645cfb20d88d4d1ff4200`
**Reproduction:** `cd escrow5-canonical-brief-hashing && python run.py` (exit 0 = PASS)

---

## 1. Result

```
vectors: 87 (accept=55 reject=32) seed=20261008

python   : 87/87 pass
javascript: 87/87 pass
cross-lang BYTES: 55/55 identical

RESULT: PASS
```

Re-run from a clean state (no cached `report.json`), exit code 0.

## 2. What was built

A specification, `imdworks-brief-v1`, for deterministically serialising a bounty
brief (`title, description, criteria, reward, token, deadline`) into bytes, plus
two independent implementations of it:

* `python/brief_canon.py` — reference, uses `pycryptodome`'s keccak256
* `js/brief_canon.js` — independent implementation, uses a vendored `js-sha3`

The canonical form is **not** `json.dumps` / `JSON.stringify`. It is a
length-prefixed record stream:

```
"imdworks-brief-v1\n"
  "title"       " " <byte length> ":" <utf-8 bytes>
  "description" " " <byte length> ":" <utf-8 bytes>
  "criteria"    " " <byte length> ":" <utf-8 bytes>
  "reward"      " " <byte length> ":" <ascii decimal digits>
  "token"       " " <byte length> ":" <0x + 40 lowercase hex>
  "deadline"    " " <byte length> ":" <ascii decimal digits>
```

joined with `\n`. `briefHash = keccak256(canonical_bytes(brief))`.

`SPEC.md` argues why the two languages' JSON serialisers cannot be used (they
disagree on non-ASCII escaping, float formatting, and big integers above 2^53).

## 3. Requirements → evidence

| Brief requirement | Where |
|---|---|
| ≥ 50 cross-language vectors | **87** frozen in `vectors/vectors.json` (seed `20261008`) |
| explicit rejection rules | `SPEC.md` R1–R5; 9-kind taxonomy; **32** reject vectors |
| matching keccak256 for all accepted vectors | `run.py` → **55/55** in both languages |
| composed/decomposed Unicode | `v0xx` `café` NFC accepted vs NFD rejected (`not_nfc`) |
| CRLF/LF | LF accepted; CRLF rejected as `cr_character` |
| empty values | `empty_field` for each of the three string fields |
| hostile nested input | objects/arrays/null where scalars belong → `type` |
| one-command test | `python run.py` |
| byte equality vs semantic similarity | `SPEC.md` §"How a client distinguishes…" |
| immutable historical briefs verifiable | `SPEC.md` §"How immutable historical briefs remain verifiable" |

**Beyond the brief:** the harness compares raw canonical **bytes** for every
accepted vector, not just the final hash. A hash-only comparison would still
pass if two different byte streams collided; byte comparison does not allow
that to hide.

## 4. Two real defects found (see `FINDINGS.md`)

Neither was reachable by reasoning about one implementation in isolation. Both
surfaced only because the harness feeds one frozen vector file to both
languages and compares bytes *and* rejection kinds.

### Finding 1 — `__proto__` mutated the prototype instead of being rejected

The JS transport decoder rebuilt objects with `out[k] = v[k]`. `JSON.parse`
creates `"__proto__"` as an own enumerable property, but assignment invokes the
inherited `__proto__` **setter**, so the key vanished from `Object.keys()` and
the `unknown_field` check never fired — while `Object.prototype`-derived state
was mutated by attacker-controlled brief content.

```
JSON.parse own keys:      [ '__proto__', 'a' ]   own __proto__? true
rebuilt via assignment:   [ 'a' ]                own __proto__? false
  -> prototype polluted: {"x":1}
```

Fixed with `Object.create(null)` + `Object.defineProperty`. Vector `v047` now
rejects correctly in both languages.

### Finding 2 — negative numbers rejected with the wrong reason

The JS version applied the canonical-decimal regex to the raw string before
examining the sign, so `-1` was rejected as `not_canonical_decimal` and the
more specific `negative` branch was unreachable. Python checked the sign first.
Both rejected the input, but with **different kinds** — and the rejection kind
is part of the contract. Fixed by checking the sign first. Vector `v061` now
returns `negative` in both languages.

## 5. Files

```
escrow5-canonical-brief-hashing/
├── SPEC.md              normative spec + rationale
├── FINDINGS.md          both defects, with reproducers and fixes
├── run.py               one-command cross-language runner
├── gen_vectors.py       deterministic vector generator (seed 20261008)
├── python/brief_canon.py
├── js/brief_canon.js
├── js/run_vectors.js
├── js/vendor/js-sha3.js vendored so no npm install is needed
└── vectors/vectors.json 87 frozen vectors, committed
```

## 6. Assumptions

1. `reward` and `deadline` must be representable exactly; a JSON integer is
   accepted, and a decimal string is accepted because JavaScript cannot carry a
   uint256 through JSON. Floats are rejected rather than converted.
2. The hash covers exactly the six fields, in the fixed order, under a versioned
   prefix. Adding a field requires a new prefix (`imdworks-brief-v2`) so old
   hashes stay computable.
3. Rejection is part of the contract: a brief rejected today is rejected
   forever, so no client can "fix" a brief and disagree with another client.

## 7. Environment

* Python 3.14.6, `pycryptodome`
* Node.js v24.11.1, vendored `js-sha3@0.9.3`
* No network access at test time. Deterministic: seed `20261008`.
