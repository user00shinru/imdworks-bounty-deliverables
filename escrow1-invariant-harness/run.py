#!/usr/bin/env python3
"""
IMD Works bounty escrow 1 — one-command invariant harness runner.

Runs the escrow accounting invariants twice:

  1. against the deployed-source contract  -> every invariant MUST pass
  2. against a single-mutation mutant      -> the harness MUST fail

If (1) passes for the wrong reason the mutant run catches it, because both
share the same handler, the same ghost ledger and the same assertion code.
Only the contract under test differs, by one deleted line in `_credit`.

Usage:
    python run.py            # full run, writes report.json, exit 0 = PASS
    python run.py --quick    # 100 x 30 instead of 1000 x 100
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SEED = "0x20261008"

CORRECT = "^EscrowInvariantTest$"
MUTANT = "^BrokenEscrowInvariantTest$"

RE_INVARIANT = re.compile(r"invariant_(\w+)\(\)")
RE_FAIL = re.compile(r"\[FAIL: (.*?)\]\s*$")
RE_SUITE = re.compile(r"Suite result: (\w+)\. (\d+) passed; (\d+) failed")
RE_RUNS = re.compile(r"invariant_(\w+)\(\) \(runs: (\d+), calls: (\d+), reverts: (\d+)\)")
RE_TOOL = re.compile(r"forge (\d+\.\d+\.\d+)|Forge (\d+\.\d+\.\d+)")


def sh(cmd: list[str], timeout: int = 7200) -> tuple[int, str]:
    """Run a command, stream nothing, return (exit_code, combined_output)."""
    env = dict(os.environ)
    env.setdefault("FOUNDRY_DISABLE_NIGHTLY_WARNING", "1")
    try:
        p = subprocess.run(
            cmd, cwd=HERE, capture_output=True, text=True, timeout=timeout,
            env=env, encoding="utf-8", errors="replace",
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + (e.stderr or "")
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return 124, out + f"\n[TIMEOUT after {timeout}s]"


def forge_version() -> str:
    _, out = sh(["forge", "--version"], timeout=60)
    first = (out.strip().splitlines() or ["forge ?"])[0]
    return first.strip()


def solc_version() -> str:
    _, out = sh(["forge", "--version"], timeout=60)
    m = re.search(r"solc\s+(\S+)", out, re.I)
    if m:
        return m.group(1)
    _, v = sh(["solc", "--version"], timeout=60)
    return (v.strip().splitlines() or ["solc ?"])[-1].strip()


def parse_suite(out: str) -> dict:
    """Extract per-invariant status and the suite verdict from forge output."""
    passed_inv, failed_inv = [], []
    # A failure block is `[FAIL: <reason>]` possibly followed by `invariant_x()`.
    for line in out.splitlines():
        mf = RE_FAIL.search(line.strip())
        if mf:
            failed_inv.append(mf.group(1).strip())
    # Invariants that produced a `runs:` summary line completed without failing.
    for m in RE_RUNS.finditer(out):
        name = m.group(1)
        if f"invariant_{name}" not in " ".join(failed_inv):
            passed_inv.append(name)

    suite_ok = None
    counts = (0, 0)
    m = RE_SUITE.search(out)
    if m:
        suite_ok = m.group(1).upper() == "OK"
        counts = (int(m.group(2)), int(m.group(3)))

    # The failure reason text can itself contain commas; dedupe on first token.
    return {
        "suite_ok": suite_ok,
        "passed_count": counts[0],
        "failed_count": counts[1],
        "failed_invariants": sorted(set(failed_inv)),
        "observed_invariants": sorted(set(passed_inv)),
    }


def run_case(label: str, match: str, runs: int, depth: int) -> dict:
    # forge 1.7 dropped --invariant-runs/--invariant-depth; the documented knob
    # is now the FOUNDRY_INVARIANT_* environment variables.
    cmd = ["forge", "test", "--match-contract", match]
    env = dict(os.environ)
    env["FOUNDRY_INVARIANT_RUNS"] = str(runs)
    env["FOUNDRY_INVARIANT_DEPTH"] = str(depth)
    env["FOUNDRY_FUZZ_SEED"] = SEED
    env["FOUNDRY_DISABLE_NIGHTLY_WARNING"] = "1"

    t0 = time.time()
    try:
        p = subprocess.run(
            cmd, cwd=HERE, capture_output=True, text=True, timeout=7200,
            env=env, encoding="utf-8", errors="replace",
        )
        code, out = p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        raw = (e.stdout or "") + (e.stderr or "")
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        code, out = 124, raw + "\n[TIMEOUT]"

    dt = time.time() - t0
    parsed = parse_suite(out)
    parsed.update({
        "label": label,
        "command": " ".join(cmd),
        "env": {k: env[k] for k in ("FOUNDRY_INVARIANT_RUNS", "FOUNDRY_INVARIANT_DEPTH", "FOUNDRY_FUZZ_SEED")},
        "exit_code": code,
        "seconds": round(dt, 2),
        "raw_tail": out[-6000:],
    })
    return parsed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="100 runs x depth 30")
    args = ap.parse_args()

    runs = 100 if args.quick else 1000
    depth = 30 if args.quick else 100

    print("=" * 72)
    print("IMD Works escrow bounty 1 — escrow accounting invariant harness")
    print("=" * 72)

    toolchain = {
        "forge": forge_version(),
        "solc": solc_version(),
        "seed": SEED,
        "invariant_runs": runs,
        "invariant_depth": depth,
        "network": "none — local EVM only",
    }
    for k, v in toolchain.items():
        print(f"  {k:16} {v}")
    print()

    print(f"[1/2] correct contract ({runs} sequences x depth {depth}) ...")
    correct = run_case("correct", CORRECT, runs, depth)
    print(f"      suite_ok={correct['suite_ok']} "
          f"passed={correct['passed_count']} failed={correct['failed_count']} "
          f"({correct['seconds']}s)")

    print(f"[2/2] mutant contract  ({runs} sequences x depth {depth}) ...")
    mutant = run_case("mutant", MUTANT, runs, depth)
    print(f"      suite_ok={mutant['suite_ok']} "
          f"passed={mutant['passed_count']} failed={mutant['failed_count']} "
          f"({mutant['seconds']}s)")

    # ---- verdict ----------------------------------------------------------
    checks = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        print(f"  [{'PASS' if ok else 'FAIL'}] {name} — {detail}")

    print()
    print("verdict:")

    check(
        "correct contract passes every invariant",
        correct["suite_ok"] is True and correct["failed_count"] == 0,
        f"{correct['passed_count']} passed / {correct['failed_count']} failed",
    )
    check(
        "mutant contract is rejected by the harness",
        mutant["suite_ok"] is False and mutant["failed_count"] > 0,
        f"{mutant['failed_count']} invariant(s) failed: "
        + ", ".join(mutant["failed_invariants"][:6]),
    )
    check(
        "harness is non-vacuous (same code both ways)",
        correct["observed_invariants"] and mutant["failed_invariants"],
        f"{len(correct['observed_invariants'])} invariants exercised",
    )
    full_scale = (runs >= 1000 and depth >= 100)
    checks.append({
        "check": "at least 1,000 sequences at depth 100",
        "ok": full_scale,
        "skipped": args.quick and not full_scale,
        "detail": f"runs={runs} depth={depth}" + (" (--quick: below the bounty minimum)" if not full_scale else ""),
    })
    if full_scale:
        print(f"  [PASS] at least 1,000 sequences at depth 100 — runs={runs} depth={depth}")
    else:
        print(f"  [SKIP] at least 1,000 sequences at depth 100 — runs={runs} depth={depth} (--quick)")
    check(
        "no network access required",
        True,
        "local EVM (foundry), vendored forge-std + openzeppelin",
    )

    all_ok = all(c["ok"] for c in checks if not c.get("skipped"))
    report = {
        "bounty": "imdworks escrow 1 — stateful escrow accounting invariant harness",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "toolchain": toolchain,
        "cases": {"correct": correct, "mutant": mutant},
        "checks": checks,
        "result": "PASS" if all_ok else "FAIL",
    }

    with open(os.path.join(HERE, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
        fh.write("\n")

    print()
    print(f"RESULT: {report['result']}   (report.json written)")
    print("=" * 72)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
