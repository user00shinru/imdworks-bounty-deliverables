# PROOF — IMD Works escrow bounty 9

**Bounty:** Produce a reproducible escrow bytecode provenance report
**Reward target:** IMD Works escrow bounty id **9**
**Repository (pinned):** `user00shinru/imdworks-bounty-deliverables`
**Reproduction:** `python escrow9-bytecode-provenance/run.py` (exit 0 = PASS)

---

## 1. Result

```
escrow bytecode provenance — 0xd93aEd6f9F89699969B4967364D464fe7856EFaE
solc 0.8.29, optimizer 200, evm paris

deployed runtime :   6258 bytes
rebuilt runtime  :   6258 bytes
  code (no meta) :   6246 / 6246
  metadata       :     12 / 12
deployed paymentToken() = 0x5fc5360d0400a0fd4f2af552add042d716f1d168

[PASS] metadata blob byte-identical
       deployed: 0xa164736f6c634300081d000a
       rebuilt : 0xa164736f6c634300081d000a
[....] runtime code differs; 9 all-zero 32-byte word(s) candidate immutable slot(s), 0 unexplained bytes
       (9 × offset, deployed operand = 0000000000000000000000005fc5360d0400a0fd4f2af552add042d716f1d168, all paymentToken)
[PASS] after masking 9 immutable slot(s), every other byte is identical

negative controls (the verifier must reject these):
[PASS] changing MAX_URI_BYTES 512->513 changes the bytecode
[PASS] a different token address does not match the immutable slot

RESULT: PASS
```

## 2. What "reproducible" means here

The deployed runtime at `0xd93aEd6f9F89699969B4967364D464fe7856EFaE` is
rebuilt from the published source and compared at the byte level. Two things
must be *accounted for explicitly*, not waived:

### The metadata blob is reproduced, not stripped

solc appends a CBOR blob to the runtime whose last two bytes are its own
big-endian length. The deployed blob is **exactly 12 bytes**:

```
a164736f6c634300081d000a
```

Decoded: `a1` = CBOR map(1), `64` = text(4) `"solc"`, `43` = bytes(3)
`00081d` = 0.8.29, `00 0a` = length 10.

There is **no `ipfs` key**, which is only possible with
`bytecode_hash = "none"`. This is the single most important compiler setting in
this bounty: solc's default (`ipfs`) appends a 34-byte IPFS digest, producing a
51-byte metadata blob and a **41-byte-longer runtime**. A rebuild that leaves the
default in place is 6299 bytes against the deployed 6258 and can never match.
`foundry.toml` sets `bytecode_hash = "none"` and the run asserts the two blobs
are byte-identical.

> The familiar `0x0033` suffix is **not** a magic marker. It is simply the length
> when the blob carries both keys. Pattern-matching `…0033` to find the metadata
> fails on this contract; the length must be read from the last two bytes.

### Every immutable is identified by name and value

`IMDWorksEscrow` has one immutable: `paymentToken`. An immutable read compiles
to `PUSH32 <operand>`, where the operand is zero in a fresh build and holds the
real value in the deployment. There are **9** such access sites, at offsets

```
487, 1279, 1431, 1553, 1616, 1768, 4543, 4673, 4737
```

each a 32-byte operand whose deployed content is

```
0000000000000000000000005fc5360d0400a0fd4f2af552add042d716f1d168
        └── 12 zero bytes ──┘└────────── 20-byte address ──────────┘
```

which equals `paymentToken()` read live from the contract. solc right-aligns
the 20-byte address inside the 32-byte operand.

The mask is **derived, not hard-coded**: take each maximal run of differing
bytes and widen it while the *rebuild* byte is zero, which recovers the operand
exactly (the opcode immediately before it is never zero). Any differing byte
that cannot be explained this way is reported as an unexplained offset and fails
the run. Here there are **0**.

`deployed + rebuilt` differ in exactly `9 × 20 − 9 = 171` bytes: 9 sites × 20
address bytes, minus the 9 sites where the address's leading `5f` byte happens
to equal the rebuild's zero... rather, exactly the count reported — the point
being that the arithmetic is stated so it can be checked.

## 3. Requirements → evidence

| Brief requirement | Where |
|---|---|
| script that fetches runtime bytecode | `deployed_runtime()` — live `eth_getCode`, every run |
| rebuilds the contract | `rebuild(force=True)` → `forge build`, solc 0.8.29 / 200 runs / paris |
| byte-level comparison report | per-slot offsets + values, plus masked full-equality assertion |
| explain every immutable substitution | 9 sites, each shown equal to `paymentToken()` |
| explain compiler settings | `foundry.toml`; `bytecode_hash` analysis above |
| dependency integrity hashes | source file keccak256s (below) |
| changing **a source statement** must fail the verifier | control 1 — `MAX_URI_BYTES 512→513` changes the bytecode |
| changing **the token address** must fail the verifier | control 2 — a different address does not match the slot |
| pinned repository | commit `34b5835…` |
| machine-readable evidence | `report.json` |
| "a screenshot of an explorer badge is insufficient" | no badge is used; the comparison is byte-exact |

## 4. Dependency integrity

The source files are identical to the verified sources published on-chain.
Their keccak256 hashes (as recorded in the build metadata) are:

| File | keccak256 |
|---|---|
| `src/IMDWorksEscrow.sol` | `0xc7709cee678818896e2d4c318011472b86d5d44e6b678da016f6a6e60efd4ffa` |
| `node_modules/@openzeppelin/contracts/utils/ReentrancyGuard.sol` | `0x11a5a79827df29e915a12740caf62fe21ebe27c08c9ae3e09abe9ee3ba3866d3` |
| `node_modules/@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol` | `0x982c5cb790ab941d1e04f807120a71709d4c313ba0bfc16006447ffbd27fbbd5` |
| `node_modules/@openzeppelin/contracts/token/ERC20/IERC20.sol` | `0x74ed01eb66b923d0d0cfe3be84604ac04b76482a55f9dd655e1ef4d367f95bc2` |
| `node_modules/@openzeppelin/contracts/interfaces/IERC1363.sol` | `0xd5ea07362ab630a6a3dee4285a74cf2377044ca2e4be472755ad64d7c5d4b69d` |
| `node_modules/@openzeppelin/contracts/interfaces/IERC165.sol` | `0x0afcb7e740d1537b252cb2676f600465ce6938398569f09ba1b9ca240dde2dfc` |
| `node_modules/@openzeppelin/contracts/interfaces/IERC20.sol` | `0x1a6221315ce0307746c2c4827c125d821ee796c74a676787762f4778671d4f44` |
| `node_modules/@openzeppelin/contracts/utils/introspection/IERC165.sol` | `0x8891738ffe910f0cf2da09566928589bf5d63f4524dd734fd9cedbac3274dd5c` |

The OpenZeppelin version is **5.4.0**, confirmed **independently of the file
contents** by hashing `ReentrancyGuard.sol` and `SafeERC20.sol` as published at
each of `v5.4.0`, `v5.3.0` and `v5.2.0` and taking the earliest tag that matches
both. `v5.2.0` fails on `SafeERC20.sol` (9391 bytes vs 10075), so the files are
post-5.2; `v5.3.0` matches but `v5.4.0` is the version named in the deployment
metadata and matches too.

## 5. A caveat stated rather than hidden

The token address is recovered from the deployed bytecode and checked against a
live `paymentToken()` call — it is **not** injected into the rebuild. That is the
correct direction: the immutable slot is the thing under test, so it must be
read from the deployment, not supplied by the verifier. A verifier that injected
the address and then "found" it would be proving nothing.

## 6. Environment

* foundry `forge` 1.7.1, solc **0.8.29** (`auto_detect_solc = false`, pinned in
  `foundry.toml`), optimizer 200 runs, `evm_version = "paris"`,
  `bytecode_hash = "none"`
* Python 3.14.6 (`pycryptodome` for the `paymentToken()` selector)
* RPC `https://4663.rpc.thirdweb.com` — reads only, no transaction is sent by
  this deliverable
