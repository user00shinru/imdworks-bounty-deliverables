#!/usr/bin/env python3
"""
One-command cross-language vector runner.

    python run.py            # exit 0 = PASS, exit 1 = FAIL

Loads vectors/vectors.json (frozen, shared by both implementations), then:

  1. Python impl  → must reproduce every `expect`.
  2. JS impl      → must reproduce every `expect` (invoked as a child process).
  3. Cross-language: for every accepted vector the raw canonical BYTES must be
     identical, not merely the final hash. Two different byte streams that
     collide would still pass a hash-only check; this does not let that hide.

Writes report.json.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "python"))
sys.path.insert(0, HERE)

from brief_canon import CanonicalError, canonical_bytes, try_brief_hash  # noqa: E402
from gen_vectors import dec  # noqa: E402

VECTORS = os.path.join(HERE, "vectors", "vectors.json")
JS_RUNNER = os.path.join(HERE, "js", "run_vectors.js")


def run_python(vectors):
    """Return (results, failures)."""
    results, failures = [], []
    for v in vectors:
        h, kind = try_brief_hash(dec(v["input"]))
        got = h if h else kind
        ok = got == v["expect"]
        results.append({"id": v["id"], "got": got, "ok": ok})
        if not ok:
            failures.append({
                "id": v["id"], "note": v["note"],
                "expect": v["expect"], "got": got,
            })
    return results, failures


def run_js(vectors):
    """
    Hand the whole vector file to a node process and read back per-vector
    results. One process for the whole set, so the runner stays one-command.
    """
    proc = subprocess.run(
        ["node", JS_RUNNER, VECTORS],
        capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"node runner failed rc={proc.returncode}\n{proc.stderr[:2000]}")
    out = json.loads(proc.stdout)
    return out["results"], out["failures"]


def cross_language_bytes(vectors):
    """
    For every ACCEPTED vector compare canonical bytes Python vs JS.
    The JS side returns hex of its canonical bytes.
    """
    accepted = [v for v in vectors if v["kind"] == "accept"]
    payload = {"vectors": [{"id": v["id"], "input": v["input"]} for v in accepted]}
    proc = subprocess.run(
        ["node", JS_RUNNER, "--bytes"],
        input=json.dumps(payload), capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"node byte-runner failed rc={proc.returncode}\n{proc.stderr[:2000]}")
    js_bytes = {r["id"]: r["bytes"] for r in json.loads(proc.stdout)["results"]}

    mismatches = []
    for v in accepted:
        py = canonical_bytes(dec(v["input"])).hex()
        js = js_bytes.get(v["id"])
        if py != js:
            mismatches.append({"id": v["id"], "note": v["note"], "python": py, "js": js})
    return len(accepted), mismatches


def main():
    with open(VECTORS, encoding="utf-8") as f:
        doc = json.load(f)
    vectors = doc["vectors"]

    print(f"vectors: {doc['count']} (accept={doc['accept']} reject={doc['reject']}) seed={doc['seed']}")
    print()

    py_res, py_fail = run_python(vectors)
    print(f"python   : {len(vectors) - len(py_fail)}/{len(vectors)} pass")
    for f in py_fail:
        print(f"   FAIL {f['id']} {f['note']}: expect={f['expect']} got={f['got']}")

    try:
        js_res, js_fail = run_js(vectors)
    except Exception as e:
        print(f"javascript: ERROR {e}")
        js_fail = [{"id": "?", "note": str(e), "expect": "", "got": ""}]
        js_res = []
    print(f"javascript: {len(vectors) - len(js_fail)}/{len(vectors)} pass")
    for f in js_fail:
        print(f"   FAIL {f['id']} {f['note']}: expect={f['expect']} got={f['got']}")

    try:
        n_acc, byte_mismatch = cross_language_bytes(vectors)
    except Exception as e:
        print(f"cross-language BYTES: ERROR {e}")
        n_acc, byte_mismatch = 0, [{"id": "?", "note": str(e)}]
    print(f"cross-lang BYTES: {n_acc - len(byte_mismatch)}/{n_acc} identical")
    for m in byte_mismatch[:5]:
        print(f"   MISMATCH {m['id']} {m['note']}\n     py={str(m.get('python'))[:80]}\n     js={str(m.get('js'))[:80]}")

    ok = not py_fail and not js_fail and not byte_mismatch
    report = {
        "vectors_total": len(vectors),
        "python": {"pass": len(vectors) - len(py_fail), "fail": len(py_fail), "failures": py_fail},
        "javascript": {"pass": len(vectors) - len(js_fail), "fail": len(js_fail), "failures": js_fail},
        "cross_language_bytes": {"checked": n_acc, "mismatch": len(byte_mismatch), "details": byte_mismatch},
        "result": "PASS" if ok else "FAIL",
    }
    with open(os.path.join(HERE, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1)
    print()
    print("RESULT:", report["result"])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
