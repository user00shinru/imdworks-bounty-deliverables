// SPDX-License-Identifier: MIT
pragma solidity 0.8.29;

import {CommonBase} from "forge-std/Base.sol";
import {StdCheats} from "forge-std/StdCheats.sol";
import {StdUtils} from "forge-std/StdUtils.sol";

interface IEscrowLike {
    function createBounty(uint256 reward, uint64 deadline, bytes32 briefHash, string calldata briefURI) external returns (uint256);
    function addReward(uint256 id, uint256 amount) external;
    function setOperator(address operator, bool approved) external;
    function submitWork(uint256 id, bytes32 proofHash, string calldata proofURI) external;
    function submitWorkFor(uint256 id, address author, bytes32 proofHash, string calldata proofURI) external;
    function award(uint256 id, address winner) external;
    function cancel(uint256 id) external;
    function expire(uint256 id) external;
    function withdraw(address recipient) external;
    function totalLocked() external view returns (uint256);
    function totalClaimable() external view returns (uint256);
    function nextBountyId() external view returns (uint256);
    function claimable(address) external view returns (uint256);
    function operators(address, address) external view returns (bool);
    function bounties(uint256) external view returns (address creator, address winner, uint64 deadline, uint64 submissions, uint8 status, uint256 reward, bytes32 briefHash);
}

interface IMockToken {
    function mint(address to, uint256 amount) external;
    function approve(address spender, uint256 amount) external returns (bool);
    function balanceOf(address) external view returns (uint256);
}

/// @notice Stateful handler driving IMDWorksEscrow through its whole lifecycle
///         while maintaining an INDEPENDENT accounting ledger.
/// @dev The ledger is derived from the protocol's economic specification
///      (money in == money out), not by reading back the contract's own
///      `totalLocked` / `claimable` variables. The invariants then assert the
///      contract agrees with the specification-derived model. Mirrored-variable
///      assertions would be tautological; these are not.
contract Handler is CommonBase, StdCheats, StdUtils {
    IEscrowLike public escrow;
    IMockToken public token;

    address[3] public creators;
    address[5] public workers;
    address[] public actors;

    uint256 public constant MAX_REWARD = 1_000_000e6; // 1M USDG in 6 decimals
    uint256 public constant MIN_DEADLINE_OFFSET = 1 hours;
    uint256 public constant MAX_DEADLINE_OFFSET = 80 days;

    struct BountyRec {
        uint256 id;
        address creator;
        uint256 reward;
        bool open;      // still holding locked funds
        bool resolved;  // terminal: paid exactly once
        uint256 submissions;
    }

    BountyRec[] internal recs;                 // index == id - 1
    mapping(address => bool) public isActor;

    // ---- ghost ledger (specification-derived, independent of contract storage) ----
    uint256 public ghostOpenLocked;                              // Σ reward over unresolved bounties
    uint256 public ghostResolvedPaid;                            // Σ reward paid out exactly once
    uint256 public ghostWithdrawn;                               // Σ credit actually transferred out
    mapping(address => uint256) public ghostCredit;              // per-actor withdrawable credit
    uint256 public ghostCreditTotal;

    uint256 public callsCreate;
    uint256 public callsAddReward;
    uint256 public callsSubmit;
    uint256 public callsAward;
    uint256 public callsCancel;
    uint256 public callsExpire;
    uint256 public callsWithdraw;
    uint256 public callsWarp;

    constructor(address escrow_, address token_) {
        escrow = IEscrowLike(escrow_);
        token = IMockToken(token_);

        creators[0] = address(0xC0FFEE01);
        creators[1] = address(0xC0FFEE02);
        creators[2] = address(0xC0FFEE03);
        workers[0] = address(0xB0B001);
        workers[1] = address(0xB0B002);
        workers[2] = address(0xB0B003);
        workers[3] = address(0xB0B004);
        workers[4] = address(0xB0B005);

        for (uint256 i = 0; i < 3; i++) actorPush(creators[i]);
        for (uint256 i = 0; i < 5; i++) actorPush(workers[i]);

        // Fund every actor and pre-approve the escrow for exact-transfer deposits.
        for (uint256 i = 0; i < actors.length; i++) {
            token.mint(actors[i], 1e15); // 1e9 tokens in 6dp — far above any fuzz-drawn reward sum
            vm.prank(actors[i]);
            token.approve(address(escrow), type(uint256).max);
        }
    }

    function actorPush(address a) internal {
        actors.push(a);
        isActor[a] = true;
    }

    function actorCount() external view returns (uint256) { return actors.length; }

    // ------------------------------------------------------------------
    // Actions. Each pre-checks the exact contract precondition and returns
    // early when the call would revert, so a wasted fuzz call is a no-op
    // instead of a discarded run.
    // ------------------------------------------------------------------

    function createBounty(uint256 creatorSeed, uint256 rewardSeed, uint256 deadlineSeed) external {
        address creator = creators[creatorSeed % 3];
        uint256 reward = bound(rewardSeed, 1, MAX_REWARD);
        uint64 deadline = uint64(block.timestamp + bound(deadlineSeed, MIN_DEADLINE_OFFSET, MAX_DEADLINE_OFFSET));

        bytes32 briefHash = keccak256(abi.encode("brief", recs.length));
        vm.prank(creator);
        uint256 id = escrow.createBounty(reward, deadline, briefHash, "https://example.invalid/brief");

        recs.push(BountyRec(id, creator, reward, true, false, 0));
        ghostOpenLocked += reward;
        callsCreate++;
    }

    function addReward(uint256 recSeed, uint256 creatorSeed, uint256 amountSeed) external {
        if (recs.length == 0) return;
        uint256 idx = recSeed % recs.length;
        BountyRec storage r = recs[idx];
        if (!r.open) return;
        if (uint256(bountyDeadline(r.id)) <= block.timestamp) return;
        // only the original creator may top up
        if (creators[creatorSeed % 3] != r.creator) return;

        uint256 amount = bound(amountSeed, 1, MAX_REWARD);
        vm.prank(r.creator);
        escrow.addReward(r.id, amount);

        r.reward += amount;
        ghostOpenLocked += amount;
        callsAddReward++;
    }

    function submit(uint256 recSeed, uint256 workerSeed, uint256 opSeed, bool viaOperator) external {
        if (recs.length == 0) return;
        BountyRec storage r = recs[recSeed % recs.length];
        if (!r.open) return;
        if (uint256(bountyDeadline(r.id)) <= block.timestamp) return;

        address worker = workers[workerSeed % 5];
        if (worker == r.creator) return;
        if (escrow.operators(worker, worker) ) return; // never true; placeholder kept explicit

        bytes32 proof = keccak256(abi.encode("proof", r.id, worker, r.submissions));

        if (!viaOperator) {
            vm.prank(worker);
            escrow.submitWork(r.id, proof, "https://example.invalid/proof");
        } else {
            address op = workers[opSeed % 5];
            if (op == worker) return;
            if (!escrow.operators(worker, op)) return; // author must have approved op
            vm.prank(op);
            escrow.submitWorkFor(r.id, worker, proof, "https://example.invalid/proof");
        }

        r.submissions += 1;
        callsSubmit++;
    }

    function setOperator(uint256 ownerSeed, uint256 opSeed, bool approved) external {
        address owner = workers[ownerSeed % 5];
        address op = workers[opSeed % 5];
        if (op == owner) return;

        vm.prank(owner);
        escrow.setOperator(op, approved);
    }

    function award(uint256 recSeed, uint256 creatorSeed, uint256 winnerSeed) external {
        if (recs.length == 0) return;
        BountyRec storage r = recs[recSeed % recs.length];
        if (!r.open) return;
        if (r.submissions == 0) return;
        if (creators[creatorSeed % 3] != r.creator) return;
        uint64 dl = bountyDeadline(r.id);
        if (block.timestamp >= uint256(dl) + 7 days) return; // ReviewClosed

        // pick a worker that actually has a submission stored
        address winner = workers[winnerSeed % 5];
        if (!_hasSubmission(r.id, winner)) return;

        vm.prank(r.creator);
        escrow.award(r.id, winner);

        r.open = false;
        r.resolved = true;
        ghostOpenLocked -= r.reward;
        ghostCredit[winner] += r.reward;
        ghostCreditTotal += r.reward;
        ghostResolvedPaid += r.reward;
        callsAward++;
    }

    function cancel(uint256 recSeed, uint256 creatorSeed) external {
        if (recs.length == 0) return;
        BountyRec storage r = recs[recSeed % recs.length];
        if (!r.open) return;
        if (r.submissions != 0) return;
        if (creators[creatorSeed % 3] != r.creator) return;

        vm.prank(r.creator);
        escrow.cancel(r.id);

        r.open = false;
        r.resolved = true;
        ghostOpenLocked -= r.reward;
        ghostCredit[r.creator] += r.reward;
        ghostCreditTotal += r.reward;
        ghostResolvedPaid += r.reward;
        callsCancel++;
    }

    function expire(uint256 recSeed) external {
        if (recs.length == 0) return;
        BountyRec storage r = recs[recSeed % recs.length];
        if (!r.open) return;
        uint64 dl = bountyDeadline(r.id);
        uint256 unlockAt = uint256(dl) + (r.submissions == 0 ? 0 : 7 days);
        if (block.timestamp < unlockAt) return;

        escrow.expire(r.id);

        r.open = false;
        r.resolved = true;
        ghostOpenLocked -= r.reward;
        ghostCredit[r.creator] += r.reward;
        ghostCreditTotal += r.reward;
        ghostResolvedPaid += r.reward;
        callsExpire++;
    }

    function withdraw(uint256 actorSeed, uint256 recipientSeed) external {
        address actor = actors[actorSeed % actors.length];
        uint256 amount = ghostCredit[actor];
        if (amount == 0) return;

        address recipient = actors[recipientSeed % actors.length];

        vm.prank(actor);
        escrow.withdraw(recipient);

        ghostCredit[actor] = 0;
        ghostCreditTotal -= amount;
        ghostWithdrawn += amount;
        callsWithdraw++;
    }

    function warp(uint256 deltaSeed) external {
        uint256 delta = bound(deltaSeed, 0, 10 days);
        vm.warp(block.timestamp + delta);
        callsWarp++;
    }

    // ------------------------------------------------------------------

    function _hasSubmission(uint256 id, address author) internal view returns (bool) {
        (bytes32 ph, ) = _submission(id, author);
        return ph != bytes32(0);
    }

    function _submission(uint256 id, address author) internal view returns (bytes32, uint64) {
        (bool ok, bytes memory data) = address(escrow).staticcall(abi.encodeWithSignature("submissions(uint256,address)", id, author));
        if (!ok || data.length < 64) return (bytes32(0), 0);
        return abi.decode(data, (bytes32, uint64));
    }

    function bountyDeadline(uint256 id) internal view returns (uint64) {
        (, , uint64 dl, , , , ) = escrow.bounties(id);
        return dl;
    }

    function recCount() external view returns (uint256) { return recs.length; }

    function recAt(uint256 i) external view returns (uint256 id, address creator, uint256 reward, bool open, bool resolved, uint256 submissions) {
        BountyRec storage r = recs[i];
        return (r.id, r.creator, r.reward, r.open, r.resolved, r.submissions);
    }
}
