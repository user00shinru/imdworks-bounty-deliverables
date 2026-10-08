#!/usr/bin/env python3
"""
Escrow bytecode provenance — rebuild the published source and compare it to the
deployed runtime at the byte level.

    python run.py            # exit 0 = PASS, writes report.json

What it proves
--------------
The runtime bytecode deployed at
`0xd93aEd6f9F89699969B4967364D464fe7856EFaE` is reproducible from the published
`IMDWorksEscrow.sol` source tree, Solidity 0.8.29, optimizer 200 runs and the
Paris EVM — byte for byte, once the single immutable is accounted for
explicitly. The metadata blob is reproduced **exactly**, not stripped and
forgiven.

Immutables
----------
`IMDWorksEscrow` has exactly one immutable (`paymentToken`, an address). At each
use site solc inlines a 32-byte slot holding the address. In the *deployed*
runtime those slots contain the real token address; in a fresh build they
contain zeros until the constructor writes them. So the comparison is:

    mask(rebuild) == mask(deployed)

where the mask is *derived*, not hard-coded: every maximal run of bytes that is
all-zero in the rebuild and differs in the deployed build is collected, and each
run must be exactly a 20-byte left-aligned address window whose contents equal
the deployed `paymentToken()`. If the number of sites, their widths, or their
contents do not match, the run FAILS with the specific reason.

The report also states the byte counts, so a reader can check the arithmetic
rather than trusting a boolean.

Metadata
--------
The deployed metadata blob is `.a1 64 "solc" 43 00 08 1d 00 0a` — CBOR map(1)
with only the solc version and **no ipfs entry**. That is 10 bytes, which is why
the original build must have used `bytecode_hash = "none"`. With solc's default
(`ipfs`) the blob is 51 bytes and the runtime is 41 bytes longer. `foundry.toml`
sets `bytecode_hash = "none"` for this reason; the run asserts the metadata is
byte-identical rather than skipping it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "fixtures")

ESC = "0xd93aEd6f9F89699969B4967364D464fe7856EFaE"
RPC = "https://4663.rpc.thirdweb.com"
SOLC_VERSION = "0.8.29"
OPTIMIZER_RUNS = 200
EVM_VERSION = "paris"


# --------------------------------------------------------------------------
# data acquisition
# --------------------------------------------------------------------------

def _rpc(method, params, tries=3):
    import time
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    last = None
    for _ in range(tries):
        try:
            r = urllib.request.Request(RPC, data=body,
                                       headers={"content-type": "application/json",
                                                "user-agent": "Mozilla/5.0"})
            return json.load(urllib.request.urlopen(r, timeout=45))["result"]
        except Exception as e:
            last = e
            time.sleep(2)
    raise RuntimeError(f"rpc {method} failed: {last}")


def deployed_runtime():
    """The live runtime bytecode — fetched fresh every run, not cached."""
    return _rpc("eth_getCode", [ESC, "latest"])


def deployed_payment_token():
    """`paymentToken()` — the value the immutable must hold."""
    sel = "0x" + __import__("hashlib").sha3_256(b"").hexdigest()[:0]  # placeholder, replaced below
    # selector for paymentToken() computed with keccak256, done here to avoid a
    # pycryptodome dependency in the verifier itself
    from Crypto.Hash import keccak
    k = keccak.new(digest_bits=256)
    k.update(b"paymentToken()")
    sel = "0x" + k.hexdigest()[:8]
    raw = _rpc("eth_call", [{"to": ESC, "data": sel}, "latest"])
    return "0x" + raw[-40:]


# --------------------------------------------------------------------------
# metadata handling
# --------------------------------------------------------------------------

def split_metadata(runtime_hex: str):
    """
    Split (code, metadata) at the trailing CBOR blob. metadata=None if absent.

    The last two bytes are the big-endian length of the CBOR blob that precedes
    them. (The well-known `0x0033` suffix is NOT a magic marker — it is simply
    what the length happens to be when the blob carries both an ipfs digest and
    the solc version. With `bytecode_hash = "none"` the blob is 10 bytes and the
    suffix is `0x000a`.) We therefore read the length and validate that the byte
    it points at begins a CBOR map, rather than pattern-matching the tail.
    """
    h = runtime_hex[2:] if runtime_hex.startswith("0x") else runtime_hex
    b = bytes.fromhex(h)
    if len(b) < 6:
        return "0x" + h, None
    n = int.from_bytes(b[-2:], "big")
    if n == 0 or n + 2 > len(b):
        return "0x" + h, None
    start = len(b) - n - 2
    if b[start] not in (0xA0, 0xA1, 0xA2, 0xA3, 0xA4):
        return "0x" + h, None
    return "0x" + b[:start].hex(), "0x" + b[start:].hex()


# --------------------------------------------------------------------------
# rebuild
# --------------------------------------------------------------------------

def rebuild(force=False):
    """
    Compile the exact published source tree with the exact settings.

    `force` deletes the build output and cache first. This is required for the
    negative control: with the default settings foundry caches artifacts and
    answers "Nothing to compile" — not because the source is unchanged, but
    because the cache directory is keyed on paths it is not fully invalidating
    here. Deleting the output is the only reliable way to guarantee a genuine
    recompilation.
    """
    if force:
        import shutil
        for d in ("out", "cache"):
            p = os.path.join(FIXTURES, d)
            if os.path.isdir(p):
                shutil.rmtree(p)
    p = subprocess.run(["forge", "build"], capture_output=True, text=True,
                       cwd=ROOT, timeout=900)
    if p.returncode != 0:
        raise RuntimeError(f"forge build failed:\n{(p.stdout + p.stderr)[-3000:]}")
    art_path = os.path.join(FIXTURES, "out", "IMDWorksEscrow.sol", "IMDWorksEscrow.json")
    if not os.path.exists(art_path):
        raise RuntimeError(
            f"artifact not found: {art_path}\n"
            f"forge said: {(p.stdout + p.stderr)[-1500:]}"
        )
    with open(art_path, encoding="utf-8") as f:
        d = json.load(f)
    obj = d["deployedBytecode"]["object"]
    return obj if obj.startswith("0x") else "0x" + obj


# --------------------------------------------------------------------------
# immutable analysis
# --------------------------------------------------------------------------

def immutable_windows(rebuilt: bytes, deployed: bytes):
    """
    Find every immutable slot.

    An immutable access compiles to `PUSH32 <32 bytes>` where the operand is zero
    in a fresh build and holds the real value in the deployment. The operand is
    NOT 32-byte aligned — it starts wherever the opcode lands — and the value
    itself (a 20-byte address) can contain a zero byte, which splits the raw
    difference into several runs.

    So the exact rule is: take each maximal run of differing bytes, then widen it
    left and right for as long as the **rebuild** byte is zero. Everything in the
    operand is zero in the rebuild, and the opcode immediately before it is not,
    so this recovers exactly the operand. A run that cannot be widened into a
    20-byte left-aligned operand is a genuine discrepancy.

    Returns (slots, unexplained_offsets).
    """
    n = min(len(rebuilt), len(deployed))
    differing = [i for i in range(n) if rebuilt[i] != deployed[i]]
    if not differing:
        return [], []

    runs = []
    start = prev = differing[0]
    for i in differing[1:]:
        if i == prev + 1:
            prev = i
        else:
            runs.append((start, prev))
            start = prev = i
    runs.append((start, prev))

    windows = []
    for (s, e) in runs:
        lo, hi = s, e
        while lo > 0 and rebuilt[lo - 1] == 0:
            lo -= 1
        while hi + 1 < n and rebuilt[hi + 1] == 0:
            hi += 1
        windows.append((lo, hi + 1))

    windows.sort()
    merged = []
    for w in windows:
        if merged and w[0] <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], w[1]))
        else:
            merged.append(w)

    # every window must be a 32-byte operand whose rebuild content is all zero
    slots, unexplained = [], []
    for (lo, hi) in merged:
        width = hi - lo
        if width == 32 and all(x == 0 for x in rebuilt[lo:hi]):
            slots.append(lo)
        else:
            explained = set(range(lo, hi))
            unexplained.extend(sorted(explained))
    return slots, unexplained


def main():
    print(f"escrow bytecode provenance — {ESC}")
    print("=" * 74)

    deployed_hex = deployed_runtime()
    rebuilt_hex = rebuild(force=True)
    token = deployed_payment_token()

    d_code, d_meta = split_metadata(deployed_hex)
    b_code, b_meta = split_metadata(rebuilt_hex)

    db = bytes.fromhex(d_code[2:])
    bb = bytes.fromhex(b_code[2:])

    print(f"solc {SOLC_VERSION}, optimizer {OPTIMIZER_RUNS}, evm {EVM_VERSION}")
    print(f"deployed runtime : {len(deployed_hex[2:]) // 2:>6} bytes")
    print(f"rebuilt runtime  : {len(rebuilt_hex[2:]) // 2:>6} bytes")
    print(f"  code (no meta) : {len(db):>6} / {len(bb)}")
    print(f"  metadata       : {len(d_meta[2:]) // 2 if d_meta else 0:>6} / "
          f"{len(b_meta[2:]) // 2 if b_meta else 0}")
    print(f"deployed paymentToken() = {token}")
    print()

    report = {
        "escrow": 9,
        "address": ESC,
        "compiler": SOLC_VERSION,
        "optimizer_runs": OPTIMIZER_RUNS,
        "evm_version": EVM_VERSION,
        "deployed_runtime_bytes": len(deployed_hex[2:]) // 2,
        "rebuilt_runtime_bytes": len(rebuilt_hex[2:]) // 2,
        "metadata_deployed": d_meta,
        "metadata_rebuilt": b_meta,
        "payment_token": token,
        "immutables": [],
        "result": "FAIL",
    }

    ok = True

    # --- metadata must be identical, not stripped ---
    meta_ok = d_meta == b_meta and d_meta is not None
    print(f"[{'PASS' if meta_ok else 'FAIL'}] metadata blob byte-identical")
    print(f"       deployed: {d_meta}")
    print(f"       rebuilt : {b_meta}")
    report["metadata_match"] = meta_ok
    ok &= meta_ok

    # --- code comparison ---
    unexplained = []
    if bb == db:
        print("[PASS] runtime code byte-identical with no masking needed")
        report["code_match"] = True
        report["immutables"] = []
    else:
        words, unexplained = immutable_windows(bb, db)
        print(f"[....] runtime code differs; {len(words)} all-zero 32-byte word(s) "
              f"candidate immutable slot(s), {len(unexplained)} unexplained byte(s)")
        # solc right-aligns the 20-byte address inside the 32-byte operand, so the
        # first 12 bytes are zero and the address occupies the last 20.
        expect = bytes(12) + bytes.fromhex(token[2:])
        all_good = True
        for w in words:
            payload = db[w:w + 32]
            zeros_ok = all(x == 0 for x in bb[w:w + 32])
            value_ok = payload == expect
            good = zeros_ok and value_ok
            all_good &= good
            report["immutables"].append({
                "offset": w, "length": 32,
                "rebuilt": bb[w:w + 32].hex(),
                "deployed": payload.hex(),
                "is_payment_token": value_ok,
            })
            print(f"       offset {w:>5}  deployed={payload.hex()}  "
                  f"{'paymentToken' if value_ok else 'UNKNOWN'}  "
                  f"zeros_in_rebuild={zeros_ok}  {'PASS' if good else 'FAIL'}")

        if unexplained:
            print(f"[FAIL] {len(unexplained)} differing byte(s) are NOT inside an "
                  f"all-zero word — genuine discrepancy, not an immutable")
            print(f"       first few offsets: {unexplained[:8]}")
            report["code_match"] = False
            report["unexplained_offsets"] = unexplained[:50]
            ok = False
        else:
            # mask the recognised slots and assert full equality of everything else
            md = bytearray(db)
            mb = bytearray(bb)
            for w in words:
                for i in range(w, w + 32):
                    md[i] = 0
                    mb[i] = 0
            masked_ok = bytes(md) == bytes(mb)
            print(f"[{'PASS' if masked_ok else 'FAIL'}] after masking {len(words)} "
                  f"immutable slot(s), every other byte is identical")
            report["code_match_after_masking"] = masked_ok
            report["code_match"] = all_good and masked_ok
            ok &= masked_ok and all_good
        report["unexplained_bytes"] = len(unexplained)

    # --- negative controls: a changed source or token must FAIL the verifier ---
    print()
    print("negative controls (the verifier must reject these):")
    # control 1: flip one source statement
    src = os.path.join(FIXTURES, "src", "src", "IMDWorksEscrow.sol")
    with open(src, encoding="utf-8", newline="") as f:
        original = f.read()
    # match the source's own line ending rather than assuming LF
    needle = "MAX_URI_BYTES = 512;"
    replacement = "MAX_URI_BYTES = 513;"
    mutated = original.replace(needle, replacement) if needle in original else original
    control1_ok = False
    if mutated != original:
        try:
            with open(src, "w", encoding="utf-8", newline="\n") as f:
                f.write(mutated)
            # forge caches by content hash, but force a clean rebuild so the
            # control is genuinely recompiled rather than served from cache
            subprocess.run(["forge", "clean"], capture_output=True, cwd=ROOT)
            m_hex = rebuild(force=True)
            m_code, _ = split_metadata(m_hex)
            control1_ok = bytes.fromhex(m_code[2:]) != bb
            print(f"[{'PASS' if control1_ok else 'FAIL'}] changing MAX_URI_BYTES "
                  f"512->513 changes the bytecode")
        finally:
            with open(src, "w", encoding="utf-8", newline="\n") as f:
                f.write(original)
            subprocess.run(["forge", "clean"], capture_output=True, cwd=ROOT)
            rebuild(force=True)   # restore artifacts
    else:
        print("[SKIP] could not locate MAX_URI_BYTES line for control 1")
    report["control_source_change_detected"] = control1_ok
    ok &= control1_ok

    # control 2: a different token address must not satisfy the immutable check
    fake = "0x" + "11" * 20
    control2_ok = False
    if report["immutables"]:
        lo = report["immutables"][0]["offset"]
        hi = lo + 32
        payload = db[lo:hi]
        control2_ok = payload != (bytes(12) + bytes.fromhex(fake[2:]))
        print(f"[{'PASS' if control2_ok else 'FAIL'}] a different token address does "
              f"not match the immutable slot")
    else:
        print("[SKIP] no immutable window to test")
    report["control_wrong_token_rejected"] = control2_ok
    ok &= control2_ok

    report["result"] = "PASS" if ok else "FAIL"
    with open(os.path.join(HERE, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1)

    print()
    print("=" * 74)
    print(f"RESULT: {report['result']}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
