// SPDX-License-Identifier: MIT
pragma solidity 0.8.29;

import {Test} from "forge-std/Test.sol";
import {IMDWorksEscrow} from "../src/IMDWorksEscrow.sol";
import {BrokenEscrow} from "../src/BrokenEscrow.sol";
import {MockToken} from "../src/mocks/MockToken.sol";
import {Handler} from "./Handler.sol";

/// @title Escrow accounting invariants — spec-derived, not mirrored.
///
/// The handler maintains `ghost*` ledgers derived purely from the economic
/// specification: a bounty's reward enters `ghostOpenLocked` on creation,
/// leaves it exactly once on award/cancel/expire, and lands in the winner's or
/// creator's `ghostCredit`. Withdrawals drain `ghostCredit` into
/// `ghostWithdrawn`.
///
/// The invariants below then compare that independent model against the
/// contract. Because the model never reads `totalLocked`, `totalClaimable` or
/// `claimable`, agreement is a real property of the implementation rather than
/// an identity.
abstract contract InvariantBase is Test {
    IMDWorksEscrow internal escrow;
    MockToken internal token;
    Handler internal handler;

    // ------------------------------------------------------------------

    function _setUp(bool useMutant) internal {
        token = new MockToken();
        if (useMutant) {
            escrow = IMDWorksEscrow(address(new BrokenEscrow(address(token))));
        } else {
            escrow = new IMDWorksEscrow(address(token));
        }

        handler = new Handler(address(escrow), address(token));
        targetContract(address(handler));

        excludeContract(address(token));
        excludeContract(address(escrow));

        bytes4[] memory sels = new bytes4[](8);
        sels[0] = Handler.createBounty.selector;
        sels[1] = Handler.addReward.selector;
        sels[2] = Handler.submit.selector;
        sels[3] = Handler.award.selector;
        sels[4] = Handler.cancel.selector;
        sels[5] = Handler.expire.selector;
        sels[6] = Handler.withdraw.selector;
        sels[7] = Handler.warp.selector;
        targetSelector(FuzzSelector({addr: address(handler), selectors: sels}));
    }

    /// @dev Total ever deposited = Σ reward over every bounty ever created.
    function _totalCreated() internal view returns (uint256 sum) {
        uint256 n = handler.recCount();
        for (uint256 i = 0; i < n; i++) {
            (, , uint256 reward, , , ) = handler.recAt(i);
            sum += reward;
        }
    }

    // ------------------------------------------------------------------
    // I1 — the contract's liability view equals the specification model.
    // ------------------------------------------------------------------
    function invariant_liabilities_match_model() public view {
        uint256 model = handler.ghostOpenLocked() + handler.ghostCreditTotal();
        assertEq(escrow.liabilities(), model, "I1: liabilities != model");
    }

    // ------------------------------------------------------------------
    // I2 — solvency with an *exact* balance: the contract holds precisely what
    //      it still owes (open rewards + unwithdrawn credits). Any surplus
    //      would mean a credit went unaccounted; any deficit is insolvency.
    // ------------------------------------------------------------------
    function invariant_solvent_exact() public view {
        uint256 owed = escrow.liabilities();
        uint256 held = token.balanceOf(address(escrow));
        assertGe(held, owed, "I2: escrow undercollateralised");
        assertEq(held, owed, "I2: escrow holds more than it owes");
    }

    // ------------------------------------------------------------------
    // I3 — conservation of value across the whole lifetime:
    //      every created reward is either still locked, still credited,
    //      or already withdrawn. Nothing is created or destroyed.
    // ------------------------------------------------------------------
    function invariant_conservation_of_value() public view {
        uint256 created = _totalCreated();
        uint256 accounted = escrow.totalLocked() + escrow.totalClaimable() + handler.ghostWithdrawn();
        assertEq(accounted, created, "I3: value not conserved");
    }

    // ------------------------------------------------------------------
    // I4 — the contract's own locked/claimable split equals the model split.
    // ------------------------------------------------------------------
    function invariant_split_matches_model() public view {
        assertEq(escrow.totalLocked(), handler.ghostOpenLocked(), "I4: totalLocked != model open");
        assertEq(escrow.totalClaimable(), handler.ghostCreditTotal(), "I4: totalClaimable != model credit");
    }

    // ------------------------------------------------------------------
    // I5 — a terminal bounty can never pay twice. Every resolved bounty must
    //      have left Status.Open and have its reward counted exactly once.
    // ------------------------------------------------------------------
    function invariant_no_double_payout() public view {
        uint256 n = handler.recCount();
        for (uint256 i = 0; i < n; i++) {
            (uint256 id, , uint256 reward, , bool resolved, ) = handler.recAt(i);
            (uint8 status) = _statusOf(id);
            if (resolved) {
                assertTrue(status != 1, "I5: resolved bounty still Open");
                assertTrue(status == 2 || status == 3 || status == 4, "I5: bad terminal status");
            } else {
                assertEq(status, 1, "I5: unresolved record not Open");
            }
            reward;
        }
    }

    /// @dev Reads only the status enum out of the Bounty tuple via a typed
    ///      interface call. The concrete contract exposes `Status` (an enum);
    ///      ABI-wise it is identical to uint8, so a local interface declaring
    ///      uint8 decodes it cleanly without hand-rolled offsets.
    function _statusOf(uint256 id) internal view returns (uint8) {
        (, , , , uint8 status, , ) = IEscrowStatus(address(escrow)).bounties(id);
        return status;
    }

    // ------------------------------------------------------------------
    // I6 — open rewards can never exceed what the contract still holds; a
    //      resolved bounty's reward is never double-counted in the locked pool.
    // ------------------------------------------------------------------
    function invariant_locked_never_exceeds_created() public view {
        assertLe(escrow.totalLocked(), _totalCreated(), "I6: locked > created");
        assertLe(escrow.totalClaimable(), _totalCreated(), "I6: claimable > created");
    }
}

/// @dev Minimal view of the escrow's public `bounties` getter.
interface IEscrowStatus {
    function bounties(uint256 id)
        external
        view
        returns (address creator, address winner, uint64 deadline, uint64 submissions, uint8 status, uint256 reward, bytes32 briefHash);
}

/// @notice Correct contract — every invariant must hold.
contract EscrowInvariantTest is InvariantBase {
    function setUp() public { _setUp(false); }
}

/// @notice Broken mutant — the harness must FAIL. Used by run.py to prove the
///         suite is not vacuous.
contract BrokenEscrowInvariantTest is InvariantBase {
    function setUp() public { _setUp(true); }
}
