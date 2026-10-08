# Findings — irregularities the cross-language harness caught

Both of these were real defects in the first version of this deliverable, found
by running the shared vector file through both languages rather than trusting a
single implementation. Neither is a vulnerability in IMD Works; both are
exactly the class of bug a cross-language canonicalisation spec exists to
prevent.

---

## Finding 1 — `__proto__` silently mutated the prototype instead of being rejected

**Vector:** `v047` — *"prototype-pollution style `__proto__` key"*
**Expected:** reject with `unknown_field`
**Observed (JS, first version):** accepted, returned a hash

### Cause

The JavaScript transport decoder rebuilt input objects with plain assignment:

```js
const out = {};
for (const k of Object.keys(v)) out[k] = v[k];
```

`JSON.parse` creates `"__proto__"` as an **own enumerable property** when it
appears in the source text. But `out['__proto__'] = …` does not create an own
property — it invokes the `__proto__` **accessor** inherited from
`Object.prototype`, which sets the object's prototype. So the key vanished from
`Object.keys(out)` and the unknown-field check never saw it:

```
JSON.parse own keys:            [ '__proto__', 'a' ]   has own __proto__? true
rebuilt via assignment:         [ 'a' ]                has own __proto__? false
  -> prototype now polluted: {"x":1}
```

### Why it matters

Two failures in one:

1. **The rejection was lost.** A brief carrying a hostile `__proto__` field was
   hashed as if the field were absent, so `unknown_field` never fired.
2. **Prototype pollution.** `Object.prototype`-derived state (or, in a larger
   service, any object merged from the same decoder) is mutated by attacker
   controlled brief content. In a hashing library that is at best a
   correctness bug and at worst the entry point to a wider prototype-pollution
   chain.

### Fix

Build a null-prototype object and define keys explicitly so `__proto__` stays
an ordinary own property:

```js
const out = Object.create(null);
for (const k of keys) {
  Object.defineProperty(out, k, { value: dec(v[k]), enumerable: true, writable: true, configurable: true });
}
```

`Object.keys(out)` then includes `__proto__`, `unknown_field` fires, and the
prototype is untouched.

### Verification

`node -e` reproducer above; vector `v047` now passes in both languages.

---

## Finding 2 — negative numbers were rejected with the wrong reason

**Vector:** `v061` — *"reward negative"*
**Expected:** reject with `negative`
**Observed (JS, first version):** reject with `not_canonical_decimal`

### Cause

`intToDecimal` applied the canonical-decimal regex to the raw string before
examining the sign:

```js
if (!/^(0|[1-9][0-9]*)$/.test(s)) throw new CanonicalError('not_canonical_decimal', …);
```

`-1` fails that regex immediately, so the more specific `negative` branch below
it was unreachable. Python tested the sign first and returned `negative`, so the
two languages disagreed on the *reason* even though both rejected the input.

### Why it matters

The rejection kind is part of the contract. A client that maps
`negative -> "amount must be positive"` and `not_canonical_decimal -> "malformed
number"` shows the user a different message depending on which language
rendered the error. Divergent rejection taxonomies are how "the Python verifier
accepted it but the JS verifier rejected it" bug reports start.

### Fix

Check the sign before the shape:

```js
if (typeof value === 'bigint') { if (value < 0n) throw new CanonicalError('negative', field); … }
else if (typeof value === 'string') { if (value.startsWith('-')) throw new CanonicalError('negative', field); … }
```

### Verification

Vector `v061` now returns `negative` in both languages.

---

## Method note

Neither finding was reachable by reasoning about one implementation in
isolation. Both surfaced only because the harness:

1. feeds a **single frozen vector file** to both languages (neither computes its
   own expectations), and
2. compares raw canonical **bytes**, plus the rejection kind, rather than only
   the final hash.

A hash-only, single-language test suite passes with both bugs present.
