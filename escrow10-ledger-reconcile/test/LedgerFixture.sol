// SPDX-License-Identifier: MIT
pragma solidity 0.8.29;

import {IMDWorksEscrow} from "../src/IMDWorksEscrow.sol";
import {BaseToken} from "../src/mocks/IssuerTokens.sol";

/// @dev Emits Transfer events; used to observe the direct donation (not via createBounty).
contract DonorToken is BaseToken {
    function donate(address to, uint256 value) external {
        _move(msg.sender, to, value);
    }
}

/// @title Deterministic ledger fixture generator for escrow 10
/// @notice Deterministic (seeded LCG, fixed actors, no block-hash fuzz) so the committed
///         fixture and report reproduce byte-for-byte. Every escrow call is issued **as the
///         correct actor** via the HEVM `prank` cheatcode, so creator/operator authorisation
///         is genuinely exercised.
///         Emits: escrow event log (kind + JSON payload), token donation log, per-bounty
///         state, per-wallet claimable, aggregate state, and the fixed snapshot block.
contract LedgerFixture {
    address internal constant HEVM = 0x7109709ECfa91a80626fF3989D68f67F5b1DD12D;

    uint256 internal seed;
    uint256 internal rng;
    uint256 internal logCounter;

    address[] internal creators;
    address[] internal workers;

    DonorToken internal token;
    IMDWorksEscrow internal esc;

    struct Ev {
        uint256 block;
        uint256 logIndex;
        string kind;
        string payload;
        bool orphan;
    }
    Ev[] internal evs;

    struct TEv {
        uint256 block;
        uint256 logIndex;
        string payload;
    }
    TEv[] internal tevs;

    // -------------------------------------------------------------- cheatcodes

    function _prank(address who) internal {
        (bool ok,) = HEVM.call(abi.encodeWithSignature("prank(address)", who));
        require(ok, "prank");
    }

    function _warp(uint256 t) internal {
        (bool ok,) = HEVM.call(abi.encodeWithSignature("warp(uint256)", t));
        require(ok, "warp");
    }

    // -------------------------------------------------------------- rng

    function _rand(uint256 mod) internal returns (uint256) {
        rng = (rng * 1103515245 + 12345) & 0x7fffffff;
        return rng % mod;
    }

    // -------------------------------------------------------------- json utils

    function _u(uint256 v) internal pure returns (string memory) {
        if (v == 0) return "0";
        uint256 t = v;
        uint256 d;
        while (t != 0) { d++; t /= 10; }
        bytes memory b = new bytes(d);
        while (v != 0) { b[--d] = bytes1(uint8(48 + v % 10)); v /= 10; }
        return string(b);
    }

    function _a(address x) internal pure returns (string memory) {
        bytes memory alpha = "0123456789abcdef";
        bytes memory b = new bytes(42);
        b[0] = "0"; b[1] = "x";
        for (uint256 i = 0; i < 20; i++) {
            uint8 byteVal = uint8(uint160(x) >> (8 * (19 - i)));
            b[2 + i * 2] = alpha[byteVal >> 4];
            b[3 + i * 2] = alpha[byteVal & 0x0f];
        }
        return string(b);
    }

    function _ev(uint256 blk, string memory kind, string memory payload) internal {
        evs.push(Ev(blk, logCounter++, kind, payload, false));
    }

    function _evOrphan(uint256 blk, string memory kind, string memory payload) internal {
        evs.push(Ev(blk, logCounter++, kind, payload, true));
    }

    function _evT(uint256 blk, string memory payload) internal {
        tevs.push(TEv(blk, logCounter++, payload));
    }

    // -------------------------------------------------------------- generate

    function generate(uint256 _seed, uint256 lifecycles, uint256 orphanDuplicates)
        external returns (string memory)
    {
        seed = _seed;
        rng = _seed;
        token = new DonorToken();
        esc = new IMDWorksEscrow(address(token));

        for (uint256 i = 0; i < 6; i++) {
            creators.push(address(uint160(0xC000 + i)));
            workers.push(address(uint160(0xA000 + i)));
        }
        for (uint256 i = 0; i < creators.length; i++) {
            token.mint(creators[i], 1_000_000_000); // 1000 USDG headroom each
            _prank(creators[i]);
            token.approve(address(esc), type(uint256).max);
        }
        // The fixture contract itself needs a balance to make direct donations later.
        token.mint(address(this), 100_000_000);

        for (uint256 i = 0; i < lifecycles; i++) {
            _lifecycle(i, i + 1); // block N+1 for lifecycle N
        }

        // Exact duplicates of already-recorded escrow events: the SAME (block, logIndex)
        // delivered twice, as a re-org replay would. A reconciler must dedupe on the log
        // position, not on the content — two legitimate identical withdraws are NOT dups.
        for (uint256 k = 0; k < orphanDuplicates; k++) {
            if (evs.length == 0) break;
            Ev memory e = evs[_rand(evs.length)];
            evs.push(Ev(e.block, e.logIndex, e.kind, e.payload, false));
        }

        // One genuinely orphaned bounty (created but dropped from the canonical branch).
        _orphanBranch();

        return _render();
    }

    // -------------------------------------------------------------- lifecycles

    function _lifecycle(uint256 idx, uint256 blk) internal {
        uint256 roll = _rand(100);
        address creator = creators[_rand(creators.length)];
        address worker = workers[_rand(workers.length)];
        uint256 reward = (1 + _rand(50)) * 100_000; // 0.1..5.0 USDG
        uint64 deadline = uint64(block.timestamp + 2 days);

        _prank(creator);
        (bool ok, bytes memory ret) = address(esc).call(abi.encodeWithSignature(
            "createBounty(uint256,uint64,bytes32,string)",
            reward, deadline, keccak256(abi.encodePacked("brief", idx)), "ipfs://brief"));
        if (!ok) return;
        uint256 id = abi.decode(ret, (uint256));
        _ev(blk, "BountyCreated", string.concat(
            "{\"bountyId\":", _u(id), ",\"creator\":\"", _a(creator), "\",\"reward\":", _u(reward), "}"));

        if (_rand(100) < 40) {
            uint256 extra = (1 + _rand(10)) * 100_000;
            _prank(creator);
            (bool ok2,) = address(esc).call(abi.encodeWithSignature("addReward(uint256,uint256)", id, extra));
            if (ok2) {
                _ev(blk, "RewardAdded", string.concat(
                    "{\"bountyId\":", _u(id), ",\"amount\":", _u(extra), ",\"reward\":", _u(reward + extra), "}"));
                reward += extra;
            }
        }

        address terminator = creator;
        bool hasSub = _rand(100) < 70;
        if (hasSub) {
            _prank(worker);
            (bool ok3,) = address(esc).call(abi.encodeWithSignature(
                "submitWork(uint256,bytes32,string)", id,
                keccak256(abi.encodePacked("proof", idx, worker)), "ipfs://proof"));
            if (ok3) {
                _ev(blk, "WorkSubmitted", string.concat(
                    "{\"bountyId\":", _u(id), ",\"author\":\"", _a(worker), "\"}"));
            } else {
                hasSub = false;
            }
        }

        if (roll < 35) {
            if (hasSub) {
                _prank(creator);
                (bool ok4,) = address(esc).call(abi.encodeWithSignature("award(uint256,address)", id, worker));
                if (ok4) _ev(blk, "BountyAwarded", string.concat(
                    "{\"bountyId\":", _u(id), ",\"winner\":\"", _a(worker), "\",\"amount\":", _u(reward), "}"));
            }
        } else if (roll < 50 && !hasSub) {
            _prank(creator);
            (bool ok5,) = address(esc).call(abi.encodeWithSignature("cancel(uint256)", id));
            if (ok5) _ev(blk, "BountyRefunded", string.concat(
                "{\"bountyId\":", _u(id), ",\"creator\":\"", _a(creator), "\",\"amount\":", _u(reward), ",\"status\":3}"));
        } else if (roll < 65) {
            _warp(block.timestamp + (hasSub ? 7 days : 0) + 1);
            (bool ok6,) = address(esc).call(abi.encodeWithSignature("expire(uint256)", id));
            if (ok6) _ev(blk, "BountyRefunded", string.concat(
                "{\"bountyId\":", _u(id), ",\"creator\":\"", _a(creator), "\",\"amount\":", _u(reward), ",\"status\":4}"));
        }
        terminator; // silence

        _maybeWithdraw(worker, blk);
        _maybeWithdraw(creator, blk);

        // Direct token donation (NOT via createBounty): must be treated as surplus.
        if (_rand(100) < 20) {
            uint256 amt = (1 + _rand(5)) * 100_000;
            token.donate(address(esc), amt);
            _evT(blk, string.concat("{\"from\":\"", _a(address(this)), "\",\"to\":\"", _a(address(esc)),
                "\",\"value\":", _u(amt), ",\"donation\":true}"));
        }
    }

    function _maybeWithdraw(address who, uint256 blk) internal {
        uint256 credit = esc.claimable(who);
        if (credit == 0) return;
        _prank(who);
        (bool ok,) = address(esc).call(abi.encodeWithSignature("withdraw(address)", who));
        if (ok) _ev(blk, "Withdrawn", string.concat(
            "{\"account\":\"", _a(who), "\",\"recipient\":\"", _a(who), "\",\"amount\":", _u(credit), "}"));
    }

    function _orphanBranch() internal {
        address creator = creators[0];
        _prank(creator);
        (bool ok, bytes memory ret) = address(esc).call(abi.encodeWithSignature(
            "createBounty(uint256,uint64,bytes32,string)",
            100_000, uint64(block.timestamp + 2 days), keccak256("orphan"), "ipfs://orphan"));
        if (!ok) return;
        uint256 id = abi.decode(ret, (uint256));
        _evOrphan(block.number, "BountyCreated", string.concat(
            "{\"bountyId\":", _u(id), ",\"creator\":\"", _a(creator), "\",\"reward\":100000}"));
    }

    // -------------------------------------------------------------- render

    function _render() internal view returns (string memory s) {
        s = string.concat("{\n  \"seed\": ", _u(seed), ",\n  \"snapshotBlock\": ", _u(block.number), ",\n");
        s = string.concat(s, "  \"escrow\": \"", _a(address(esc)), "\",\n");
        s = string.concat(s, "  \"token\": \"", _a(address(token)), "\",\n");

        s = string.concat(s, "  \"events\": [\n");
        for (uint256 i = 0; i < evs.length; i++) {
            s = string.concat(s, "    {\"seq\":", _u(i), ",\"block\":", _u(evs[i].block),
                ",\"logIndex\":", _u(evs[i].logIndex),
                ",\"kind\":\"", evs[i].kind, "\",\"data\":", evs[i].payload,
                ",\"orphan\":", evs[i].orphan ? "true" : "false", "}");
            s = string.concat(s, i + 1 < evs.length ? ",\n" : "\n");
        }
        s = string.concat(s, "  ],\n");

        s = string.concat(s, "  \"tokenEvents\": [\n");
        for (uint256 i = 0; i < tevs.length; i++) {
            s = string.concat(s, "    {\"seq\":", _u(i), ",\"block\":", _u(tevs[i].block),
                ",\"logIndex\":", _u(tevs[i].logIndex), ",\"data\":", tevs[i].payload, "}");
            s = string.concat(s, i + 1 < tevs.length ? ",\n" : "\n");
        }
        s = string.concat(s, "  ],\n");

        s = string.concat(s, "  \"state\": {\n");
        s = string.concat(s, "    \"totalLocked\": ", _u(esc.totalLocked()), ",\n");
        s = string.concat(s, "    \"totalClaimable\": ", _u(esc.totalClaimable()), ",\n");
        s = string.concat(s, "    \"liabilities\": ", _u(esc.liabilities()), ",\n");
        s = string.concat(s, "    \"escrowTokenBalance\": ", _u(token.balanceOf(address(esc))), ",\n");
        s = string.concat(s, "    \"nextBountyId\": ", _u(esc.nextBountyId()), "\n");
        s = string.concat(s, "  },\n");

        s = string.concat(s, "  \"wallets\": {");
        address[] memory all = _allAddresses();
        bool first = true;
        for (uint256 i = 0; i < all.length; i++) {
            uint256 cl = esc.claimable(all[i]);
            if (cl == 0) continue;
            s = string.concat(s, first ? "" : ",", "\"", _a(all[i]), "\":", _u(cl));
            first = false;
        }
        s = string.concat(s, "},\n");

        s = string.concat(s, "  \"bounties\": [");
        for (uint256 id = 1; id < esc.nextBountyId(); id++) {
            (address c, address w, uint64 dl, uint64 subs, IMDWorksEscrow.Status st, uint256 rw, bytes32 bh) = esc.bounties(id);
            s = string.concat(s, id > 1 ? "," : "", "{\"id\":", _u(id), ",\"creator\":\"", _a(c),
                "\",\"winner\":\"", _a(w), "\",\"status\":", _u(uint256(st)), ",\"reward\":", _u(rw),
                ",\"submissions\":", _u(subs), "}");
            bh; dl;
        }
        s = string.concat(s, "]\n}\n");
    }

    function _allAddresses() internal view returns (address[] memory a) {
        a = new address[](creators.length * 2);
        for (uint256 i = 0; i < creators.length; i++) {
            a[i] = creators[i];
            a[creators.length + i] = workers[i];
        }
    }
}
