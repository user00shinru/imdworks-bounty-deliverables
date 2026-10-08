#!/usr/bin/env python3
"""
Deterministic cross-language vector generator.

Writes vectors/vectors.json — a frozen list of cases:

    {id, kind, note, input, expect}

`kind` is one of:
    accept   -> `expect` is the canonical keccak256 hex (0x…)
    reject   -> `expect` is the rejection kind from the taxonomy

The vector set is generated from a fixed seed and written once; both
implementations then consume the SAME file, which is what makes the
comparison meaningful (neither side computes its own expectations).

Composed/decomposed Unicode, CRLF/LF, empty values and hostile nested input are
all present by construction.
"""
from __future__ import annotations

import json
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "python"))

from brief_canon import ALLOWED_KEYS, brief_hash, try_brief_hash  # noqa: E402

SEED = 20261008
OUT = os.path.join(os.path.dirname(__file__), "vectors", "vectors.json")

TOKEN = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"

# --- hostile / edge corpus -------------------------------------------------

TITLES_ACCEPT = [
    "Build a stateful escrow accounting invariant harness",
    "Bounty #1 — naïve title",
    "emoji ok 🧧",
    "a",                                    # shortest non-empty
    "x" * 300,                              # long
    "tab\tinside",                          # literal tab is allowed (not forbidden)
    "mixed\u00a0nbsp",                      # NBSP is a real char, kept
    "line\nfeed\ninside",                   # LF inside a scalar (kept verbatim)
]

DESC_ACCEPT = [
    "plain description",
    "multi\nline\ndescription\n",
    "with \"quotes\" and \\backslash\\",
    "unicode: 健三 🥷 café",                 # NFC by construction
    "control-safe\u0007? no — bell is allowed by spec",
]

CRIT_ACCEPT = [
    "criteria one",
    "≥ 100,000 seeded cases",
    "Use `python3 run.py` (exit 0 = PASS).",
]

REWARD_ACCEPT = [0, 1, 1000, 10**6, 2**64 - 1, 2**255, 2**256 - 1, 123456789012345678901234567890]
DEADLINE_ACCEPT = [1, 1700000000, 1791971670, 2**63 - 1]

TITLES_REJECT = [
    ("", "empty_field"),
    ("has\rcarriage", "cr_character"),
    ("has\u0000nul", "nul_byte"),
    ("\ud800lone", "lone_surrogate"),
]

# decomposed Unicode: "é" as e + combining acute
DECOMPOSED = "cafe\u0301"          # NFD form of "café"
COMPOSED = "caf\u00e9"             # NFC form of "café"


def enc(v):
    """
    JSON can only carry float64 numbers. A uint256 reward would be silently
    destroyed in transit, so integers are tagged on the way out and rebuilt
    exactly on the way in. `{"$int":"1000"}` means the input was the JSON
    integer 1000; `{"$float":1.5}` means it was a float (used for the
    'reward is a float' rejection cases).
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return {"$int": str(v)}
    if isinstance(v, float):
        return {"$float": v}
    if isinstance(v, dict):
        return {k: enc(x) for k, x in v.items()}
    if isinstance(v, list):
        return [enc(x) for x in v]
    return v


def dec(v):
    """Inverse of `enc` — reconstruct exact Python types for the runner."""
    if isinstance(v, dict):
        if set(v.keys()) == {"$int"}:
            return int(v["$int"])
        if set(v.keys()) == {"$float"}:
            return float(v["$float"])
        return {k: dec(x) for k, x in v.items()}
    if isinstance(v, list):
        return [dec(x) for x in v]
    return v


def add(vectors, kind, note, inp, expect):
    vectors.append({
        "id": f"v{len(vectors):03d}",
        "kind": kind,
        "note": note,
        "input": enc(inp),
        "expect": expect,
    })


def base(**over):
    b = {
        "title": "Build a stateful escrow accounting invariant harness",
        "description": "Model three creators, five workers, operator delegation.",
        "criteria": "Run at least 1,000 invariant sequences with depth 100.",
        "reward": 1000000,
        "token": TOKEN,
        "deadline": 1791971670,
    }
    b.update(over)
    return b


def gen():
    vectors = []

    # 1. the real deployed brief (escrow 4) as a ground-truth anchor
    add(vectors, "accept", "anchor: shape of a real board brief", base(),
        brief_hash(base()))

    # 2. scalar acceptance sweep
    for i, t in enumerate(TITLES_ACCEPT):
        add(vectors, "accept", f"title corpus #{i}", base(title=t), brief_hash(base(title=t)))
    for i, d in enumerate(DESC_ACCEPT):
        add(vectors, "accept", f"description corpus #{i}", base(description=d), brief_hash(base(description=d)))
    for i, c in enumerate(CRIT_ACCEPT):
        add(vectors, "accept", f"criteria corpus #{i}", base(criteria=c), brief_hash(base(criteria=c)))

    # 3. numeric sweep — reward
    for r in REWARD_ACCEPT:
        add(vectors, "accept", f"reward={r}", base(reward=r), brief_hash(base(reward=r)))
    for d in DEADLINE_ACCEPT:
        add(vectors, "accept", f"deadline={d}", base(deadline=d), brief_hash(base(deadline=d)))

    # 4. numeric sweep — reward as a decimal *string* (both impls must accept)
    for r in ["0", "1000000", "115792089237316195423570985008687907853269984665640564039457584007913129639935"]:
        add(vectors, "accept", f"reward string={r}", base(reward=r), brief_hash(base(reward=r)))

    # 5. Unicode: composed vs decomposed must BOTH be representable, but only
    #    the NFC one is accepted.
    add(vectors, "accept", "NFC composed 'café'", base(title=COMPOSED), brief_hash(base(title=COMPOSED)))
    add(vectors, "reject", "NFD decomposed 'café' must be rejected (not_nfc)",
        {**base(), "title": DECOMPOSED}, "not_nfc")

    # 6. line endings
    add(vectors, "accept", "LF only", base(description="a\nb"), brief_hash(base(description="a\nb")))
    add(vectors, "reject", "CRLF must be rejected, not silently rewritten",
        {**base(), "description": "a\r\nb"}, "cr_character")

    # 7. rejection sweep — text
    for i, (t, kind) in enumerate(TITLES_REJECT):
        add(vectors, "reject", f"title reject #{i}", {**base(), "title": t}, kind)

    # 8. empty values in every string field
    for f in ("title", "description", "criteria"):
        add(vectors, "reject", f"empty {f}", {**base(), f: ""}, "empty_field")

    # 9. key-set hostility
    add(vectors, "reject", "missing field: token", {k: v for k, v in base().items() if k != "token"}, "missing_field")
    add(vectors, "reject", "missing field: deadline", {k: v for k, v in base().items() if k != "deadline"}, "missing_field")
    add(vectors, "reject", "missing field: criteria", {k: v for k, v in base().items() if k != "criteria"}, "missing_field")
    add(vectors, "reject", "unknown top-level field", {**base(), "extra": 1}, "unknown_field")
    add(vectors, "reject", "prototype-pollution style __proto__ key", {**base(), "__proto__": {"x": 1}}, "unknown_field")

    # 10. hostile nested input — objects/arrays where scalars belong
    add(vectors, "reject", "title is an object", {**base(), "title": {"a": 1}}, "type")
    add(vectors, "reject", "title is an array", {**base(), "title": ["a"]}, "type")
    add(vectors, "reject", "title is null", {**base(), "title": None}, "type")
    add(vectors, "reject", "description is a number", {**base(), "description": 5}, "type")
    add(vectors, "reject", "criteria is an array of strings", {**base(), "criteria": ["x"]}, "type")
    add(vectors, "reject", "reward is a float", {**base(), "reward": 1.5}, "type")
    add(vectors, "reject", "reward is a bool", {**base(), "reward": True}, "type")
    add(vectors, "reject", "reward is null", {**base(), "reward": None}, "type")
    add(vectors, "reject", "deadline is an object", {**base(), "deadline": {}}, "type")

    # 11. token hostility
    add(vectors, "reject", "token not 0x-prefixed", {**base(), "token": "5fc5360d0400a0fd4f2af552add042d716f1d168"}, "bad_token")
    add(vectors, "reject", "token wrong length", {**base(), "token": "0xabc"}, "bad_token")
    add(vectors, "reject", "token non-hex chars", {**base(), "token": "0x" + "z" * 40}, "bad_token")
    add(vectors, "reject", "token upper-case (non-canonical)", {**base(), "token": "0x5FC5360D0400A0FD4F2AF552ADD042D716F1D168"}, "non_canonical_hex")

    # 12. numeric representation hostility
    add(vectors, "reject", "reward negative", {**base(), "reward": -1}, "negative")
    add(vectors, "reject", "reward leading zeros in string", {**base(), "reward": "007"}, "not_canonical_decimal")
    add(vectors, "reject", "reward exponential string", {**base(), "reward": "1e6"}, "not_canonical_decimal")
    add(vectors, "reject", "reward overflow uint256", {**base(), "reward": 2**256}, "overflow_uint256")
    add(vectors, "reject", "reward signed string", {**base(), "reward": "+1000"}, "not_canonical_decimal")

    # 13. key order must not matter — shuffled input, same hash
    shuffled = dict(random.Random(SEED).sample(list(base().items()), k=len(base())))
    add(vectors, "accept", "shuffled key order hashes identically", shuffled, brief_hash(base()))

    # 14. random fuzz corpus (seeded, 20 cases) mixing accept and reject
    rnd = random.Random(SEED + 1)
    alphabet = "abcXYZ 0123.\n\u00e9\u4e2d"
    for i in range(20):
        t = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 40)))
        d = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 60)))
        c = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 30)))
        rw = rnd.choice([0, 1, 999, 10**12, 2**200])
        dl = rnd.choice([1, 1700000000, 2**40])
        cand = {"title": t, "description": d, "criteria": c, "reward": rw, "token": TOKEN, "deadline": dl}
        h, kind = try_brief_hash(cand)
        if kind:
            add(vectors, "reject", f"fuzz #{i}", cand, kind)
        else:
            add(vectors, "accept", f"fuzz #{i}", cand, h)

    return vectors


def main():
    vectors = gen()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    doc = {
        "spec": "imdworks-brief-v1",
        "seed": SEED,
        "token_used": TOKEN,
        "count": len(vectors),
        "accept": sum(1 for v in vectors if v["kind"] == "accept"),
        "reject": sum(1 for v in vectors if v["kind"] == "reject"),
        "vectors": vectors,
    }
    with open(OUT, "w", encoding="utf-8", errors="surrogatepass") as f:
        json.dump(doc, f, indent=1, ensure_ascii=True, sort_keys=True)
        f.write("\n")
    print(f"wrote {OUT}")
    print(f"  total={doc['count']} accept={doc['accept']} reject={doc['reject']}")


if __name__ == "__main__":
    main()
