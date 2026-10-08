#!/usr/bin/env python3
"""
Local chain fixture with intentional reorgs.

A `Chain` is an ordered list of blocks. Each block has a number, a parent hash,
a hash, and a list of logs. `reorg(n)` replaces the top `n` blocks with a new,
longer or equal branch — exactly what a node does when a competing branch wins.

This is deterministic: hashes are derived from (number, parent, txs) via sha256,
so the same fixture always yields the same branch hashes and the same logs.

No network, no RPC, no real chain. Pure fixture.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable


# --------------------------------------------------------------------------
# Event encoding
# --------------------------------------------------------------------------

EVENT_TOPICS = {
    "BountyCreated": "0x" + "a1" * 32,
    "RewardAdded": "0x" + "a2" * 32,
    "WorkSubmitted": "0x" + "a3" * 32,
    "BountyAwarded": "0x" + "a4" * 32,
    "BountyRefunded": "0x" + "a5" * 32,
    "Withdrawn": "0x" + "a6" * 32,
    "OperatorSet": "0x" + "a7" * 32,
}

STATUS = {"Missing": 0, "Open": 1, "Awarded": 2, "Cancelled": 3, "Expired": 4}


def addr(n: int) -> str:
    """Deterministic 20-byte address from an integer."""
    return "0x" + hashlib.sha256(f"addr:{n}".encode()).hexdigest()[:40]


def log(event: str, **fields: Any) -> dict:
    """Construct a log line in the shape an RPC `eth_getLogs` would return."""
    return {
        "event": event,
        "topic0": EVENT_TOPICS[event],
        **fields,
    }


@dataclass
class Block:
    number: int
    parent_hash: str
    hash: str
    logs: list[dict] = field(default_factory=list)
    # fixture-only: a label so a reorg is human-readable in the report
    tag: str = ""
    # fixture-only: participates in the block hash so two branches that carry
    # no logs can still be distinct blocks (a real chain always differs by
    # coinbase/state root; the fixture needs an explicit stand-in).
    nonce: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _hash_block(number: int, parent_hash: str, logs: list[dict], nonce: str = "") -> str:
    payload = json.dumps(
        {"n": number, "p": parent_hash, "l": logs, "x": nonce},
        sort_keys=True, separators=(",", ":"),
    ).encode()
    return "0x" + hashlib.sha256(payload).hexdigest()


class Chain:
    """A linear chain that can be forked for a bounded number of blocks."""

    def __init__(self, genesis_hash: str = "0x" + "00" * 32) -> None:
        self.blocks: list[Block] = []
        self._genesis = genesis_hash
        self._next_number = 1
        # remember every block by hash so a reorg can reference the old branch
        self.by_hash: dict[str, Block] = {}

    # -- construction ------------------------------------------------------

    def _parent(self) -> str:
        return self.blocks[-1].hash if self.blocks else self._genesis

    def add_block(self, logs: Iterable[dict], tag: str = "", parent: str | None = None,
                  nonce: str = "") -> Block:
        p = parent if parent is not None else self._parent()
        n = self._next_number if parent is None else len(self.blocks) + 1
        logs = list(logs)
        h = _hash_block(n, p, logs, nonce)
        b = Block(number=n, parent_hash=p, hash=h, logs=logs, tag=tag, nonce=nonce)
        self.blocks.append(b)
        self.by_hash[h] = b
        self._next_number = max(self._next_number, n + 1)
        return b

    # -- reorg -------------------------------------------------------------

    def fork_from(self, height: int) -> "Chain":
        """Return a *view* of the chain truncated to `height` blocks.

        Used to build a competing branch: take the prefix, then add blocks that
        descend from that prefix's head. The old suffix remains addressable via
        `by_hash` so the indexer can be asked to roll it back.
        """
        view = Chain(self._genesis)
        view.blocks = list(self.blocks[:height])
        view.by_hash = dict(self.by_hash)
        view._next_number = max((b.number for b in view.blocks), default=0) + 1
        return view

    def reorg(self, depth: int, replacement: Iterable[Iterable[dict]], tags: Iterable[str] | None = None) -> "Chain":
        """Drop the top `depth` blocks and append the replacement branch.

        Mutates self in place and returns self, mirroring what the fixture
        harness needs: the node now serves a different canonical chain than the
        one the indexer last persisted. Orphaned blocks stay addressable through
        `by_hash`, so the indexer can be asked to roll them back.
        """
        if depth > len(self.blocks):
            raise ValueError("reorg deeper than the chain")
        replacement = [list(r) for r in replacement]
        tags = list(tags) if tags is not None else [f"reorg+{i}" for i in range(len(replacement))]
        if len(tags) != len(replacement):
            raise ValueError("tags length must match replacement length")

        # The orphaned blocks are no longer canonical but remain in by_hash.
        self.blocks = self.blocks[:-depth]
        self._next_number = max((b.number for b in self.blocks), default=0) + 1

        for i, logs in enumerate(replacement):
            # Distinct nonce per reorg generation so two branches that carry no
            # logs do not collide on hash (a real chain differs by state root).
            self.add_block(logs, tag=tags[i], nonce=f"{tags[i]}#{i}")
        return self

    # -- queries -----------------------------------------------------------

    def head(self) -> Block | None:
        return self.blocks[-1] if self.blocks else None

    def height(self) -> int:
        return len(self.blocks)

    def hash_at(self, number: int) -> str | None:
        for b in self.blocks:
            if b.number == number:
                return b.hash
        return None

    def logs_from(self, start_number: int, confirmations: int = 0) -> list[dict]:
        """Canonical logs from `start_number` up to head-minus-confirmations."""
        head_n = len(self.blocks) - confirmations
        out = []
        for b in self.blocks:
            if b.number >= start_number and b.number <= head_n:
                for j, lg in enumerate(b.logs):
                    out.append({
                        **lg,
                        "blockNumber": b.number,
                        "blockHash": b.hash,
                        "parentHash": b.parent_hash,
                        "txHash": "0x" + f"{b.number:08x}{j:056x}",
                        "logIndex": j,
                    })
        return out

    def block_records(self, start_number: int = 1) -> list[dict]:
        """Every canonical block from `start_number`, INCLUDING empty ones.

        The indexer needs the full block sequence, not just blocks that happen
        to carry logs: an empty block between two event blocks is still a
        canonical height, and gap detection must count it. This mirrors an
        indexer that polls `eth_getBlockByNumber` per height.
        """
        out = []
        for b in self.blocks:
            if b.number < start_number:
                continue
            logs = []
            for j, lg in enumerate(b.logs):
                logs.append({
                    **lg,
                    "blockNumber": b.number,
                    "blockHash": b.hash,
                    "parentHash": b.parent_hash,
                    "txHash": "0x" + f"{b.number:08x}{j:056x}",
                    "logIndex": j,
                })
            out.append({
                "number": b.number,
                "hash": b.hash,
                "parent_hash": b.parent_hash,
                "logs": logs,
            })
        return out

    def block_at(self, number: int) -> dict | None:
        for b in self.blocks:
            if b.number == number:
                return {"number": b.number, "hash": b.hash, "parent_hash": b.parent_hash,
                        "logs": self._block_logs(b)}
        return None

    def _block_logs(self, b: "Block") -> list[dict]:
        return [
            {**lg, "blockNumber": b.number, "blockHash": b.hash,
             "parentHash": b.parent_hash,
             "txHash": "0x" + f"{b.number:08x}{j:056x}", "logIndex": j}
            for j, lg in enumerate(b.logs)
        ]
