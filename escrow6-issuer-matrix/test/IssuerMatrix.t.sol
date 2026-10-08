// SPDX-License-Identifier: MIT
pragma solidity 0.8.29;

import {Test} from "forge-std/Test.sol";
import {IMDWorksEscrow} from "../src/IMDWorksEscrow.sol";
import {
    BaseToken,
    PausableToken,
    SenderRestrictedToken,
    RecipientBlockedToken,
    TaxToken,
    FalseReturnToken,
    NoReturnToken,
    ReentrantCallbackToken
} from "../src/mocks/IssuerTokens.sol";

/// @title Issuer-restriction behaviour matrix for IMDWorksEscrow
/// @notice Every case drives the UNMODIFIED escrow against an issuer-controlled token and
///         records what the contract does. The escrow's design invariant is:
///         **bookkeeping (claimable/liabilities) is independent of token transferability** —
///         a blocked transfer must revert the whole call and leave credits untouched.
contract IssuerMatrixTest is Test {
    uint256 internal constant REWARD = 1_000_000; // 1 USDG (6dp), as on the live board
    uint64 internal constant DURATION = 2 days;

    address internal creator = address(0xC0FFEE);
    address internal alice = address(0xA11CE);
    address internal bob = address(0xB0B);
    address internal op = address(0x0BE0);

    bytes32 internal constant HASH = keccak256("proof");
    string internal constant URI = "https://example.invalid/proof.md";

    // ---------------------------------------------------------------- helpers

    function _newBounty(IMDWorksEscrow esc, address _creator) internal returns (uint256 id) {
        vm.prank(_creator);
        id = esc.createBounty(REWARD, uint64(block.timestamp) + DURATION, HASH, URI);
    }

    /// Deploy escrow bound to `token`, fund creator + alice, and assert the escrow
    /// constructor's own balance probe worked (base tokens support balanceOf).
    function _setup(BaseToken token, uint256 creatorBal) internal returns (IMDWorksEscrow esc) {
        esc = new IMDWorksEscrow(address(token));
        token.mint(creator, creatorBal);
        vm.prank(creator);
        token.approve(address(esc), type(uint256).max);
    }

    /// Conservation: escrow-held tokens + total outstanding credits must never exceed
    /// what was actually deposited, and liabilities() must equal locked+claimable.
    function _assertConservation(IMDWorksEscrow esc, BaseToken token, uint256 deposited) internal view {
        uint256 held = token.balanceOf(address(esc));
        uint256 liab = esc.liabilities();
        assertEq(esc.totalLocked() + esc.totalClaimable(), liab, "liabilities() != locked+claimable");
        assertLe(liab, held + 1, "credits exceed tokens held (insolvent)");
        assertLe(held, deposited, "escrow holds more than was ever deposited");
    }

    // ================================================================ BASE (happy path)

    function test_base_exactTransfer_supported() public {
        BaseToken token = new BaseToken();
        IMDWorksEscrow esc = _setup(token, REWARD);
        uint256 id = _newBounty(esc, creator);
        assertEq(esc.totalLocked(), REWARD, "reward locked");

        vm.prank(alice);
        esc.submitWork(id, HASH, URI);
        vm.prank(creator);
        esc.award(id, alice);

        assertEq(esc.claimable(alice), REWARD, "alice credited");
        vm.prank(alice);
        esc.withdraw(alice);
        assertEq(token.balanceOf(alice), REWARD, "alice paid");
        assertEq(esc.claimable(alice), 0, "credit zeroed");
        _assertConservation(esc, token, REWARD);
    }

    // ================================================================ PAUSE

    function test_pause_blocksDeposit_butBookkeepingUnaffected() public {
        PausableToken token = new PausableToken();
        IMDWorksEscrow esc = _setup(token, REWARD);

        token.setPaused(true);
        // Deposit fails while paused -> createBounty reverts atomically, nothing locked.
        vm.prank(creator);
        vm.expectRevert();
        esc.createBounty(REWARD, uint64(block.timestamp) + DURATION, HASH, URI);
        assertEq(esc.totalLocked(), 0, "no phantom lock on failed deposit");
        assertEq(esc.nextBountyId(), 1, "id not consumed on revert");

        // Unpause, create + award, then pause again.
        token.setPaused(false);
        uint256 id = _newBounty(esc, creator);
        vm.prank(alice);
        esc.submitWork(id, HASH, URI);
        vm.prank(creator);
        esc.award(id, alice); // award is pure bookkeeping: must succeed while paused
        token.setPaused(true);
        assertEq(esc.claimable(alice), REWARD, "award credited despite pause");

        // Withdrawal blocked while paused, credit PRESERVED (not lost).
        vm.prank(alice);
        vm.expectRevert();
        esc.withdraw(alice);
        assertEq(esc.claimable(alice), REWARD, "failed withdraw preserves claimable");
        assertEq(esc.totalClaimable(), REWARD, "totalClaimable intact");

        // Issuer unpauses -> recovery path is issuer action.
        token.setPaused(false);
        vm.prank(alice);
        esc.withdraw(alice);
        assertEq(token.balanceOf(alice), REWARD, "recovered after unpause");
        _assertConservation(esc, token, REWARD);
    }

    // ================================================================ SENDER RESTRICTION

    function test_senderRestricted_depositReverts_withdrawRecoverable() public {
        SenderRestrictedToken token = new SenderRestrictedToken();
        IMDWorksEscrow esc = _setup(token, REWARD);

        // Creator itself blocked as sender -> deposit reverts.
        token.blockSender(creator, true);
        vm.prank(creator);
        vm.expectRevert();
        esc.createBounty(REWARD, uint64(block.timestamp) + DURATION, HASH, URI);
        token.blockSender(creator, false);

        uint256 id = _newBounty(esc, creator);
        vm.prank(alice);
        esc.submitWork(id, HASH, URI);
        vm.prank(creator);
        esc.award(id, alice);

        // Escrow blocked as sender -> withdrawal reverts, credit preserved.
        token.blockSender(address(esc), true);
        vm.prank(alice);
        vm.expectRevert();
        esc.withdraw(alice);
        assertEq(esc.claimable(alice), REWARD, "credit preserved on blocked sender");

        token.blockSender(address(esc), false);
        vm.prank(alice);
        esc.withdraw(alice);
        assertEq(token.balanceOf(alice), REWARD, "recovers when unblocked");
        _assertConservation(esc, token, REWARD);
    }

    // ================================================================ RECIPIENT BLOCKLIST

    function test_recipientBlocked_withdrawReverts_creditStays() public {
        RecipientBlockedToken token = new RecipientBlockedToken();
        IMDWorksEscrow esc = _setup(token, REWARD);

        uint256 id = _newBounty(esc, creator);
        vm.prank(alice);
        esc.submitWork(id, HASH, URI);
        vm.prank(creator);
        esc.award(id, alice);

        token.blockRecipient(alice, true);
        vm.prank(alice);
        vm.expectRevert();
        esc.withdraw(alice);
        assertEq(esc.claimable(alice), REWARD, "credit preserved when recipient blocked");

        // Alice can route the credit to a non-blocked wallet instead.
        token.blockRecipient(alice, false);
        vm.prank(alice);
        esc.withdraw(bob);
        assertEq(token.balanceOf(bob), REWARD, "credit routed to bob");
        _assertConservation(esc, token, REWARD);
    }

    // ================================================================ TAX

    function test_taxToken_rejected_unsupportedTransfer() public {
        TaxToken token = new TaxToken(500); // 5%
        IMDWorksEscrow esc = _setup(token, REWARD);

        // Deposit: escrow receives < reward -> delta check rejects.
        vm.prank(creator);
        vm.expectRevert(IMDWorksEscrow.UnsupportedTransfer.selector);
        esc.createBounty(REWARD, uint64(block.timestamp) + DURATION, HASH, URI);
        assertEq(esc.totalLocked(), 0, "no lock after rejected deposit");
    }

    function test_taxToken_onWithdraw_rejectsAndPreservesCredit() public {
        // Zero-fee at deposit, then the issuer switches the tax on before withdrawal.
        TaxToken token = new TaxToken(0);
        IMDWorksEscrow esc = _setup(token, REWARD);
        uint256 id = _newBounty(esc, creator);
        vm.prank(alice);
        esc.submitWork(id, HASH, URI);
        vm.prank(creator);
        esc.award(id, alice);

        token.setFee(500); // simulate issuer turning the tax on later
        vm.prank(alice);
        vm.expectRevert(IMDWorksEscrow.UnsupportedTransfer.selector);
        esc.withdraw(alice);
        assertEq(esc.claimable(alice), REWARD, "credit preserved when tax makes transfer unsupported");
    }

    // ================================================================ FALSE RETURN

    function test_falseReturn_revertsViaSafeERC20_noSilentLoss() public {
        FalseReturnToken token = new FalseReturnToken();
        IMDWorksEscrow esc = _setup(token, REWARD);

        token.setFailMoves(true);
        vm.prank(creator);
        vm.expectRevert(); // SafeERC20 turns `false` into a revert
        esc.createBounty(REWARD, uint64(block.timestamp) + DURATION, HASH, URI);

        token.setFailMoves(false);
        uint256 id = _newBounty(esc, creator);
        vm.prank(alice);
        esc.submitWork(id, HASH, URI);
        vm.prank(creator);
        esc.award(id, alice);

        token.setFailMoves(true);
        vm.prank(alice);
        vm.expectRevert();
        esc.withdraw(alice);
        assertEq(esc.claimable(alice), REWARD, "credit preserved on false-return withdraw");
        _assertConservation(esc, token, REWARD);
    }

    // ================================================================ NO RETURN (supported)

    function test_noReturnToken_supported_exactTransfer() public {
        NoReturnToken token = new NoReturnToken();
        IMDWorksEscrow esc = new IMDWorksEscrow(address(token));
        token.mint(creator, REWARD);
        vm.prank(creator);
        token.approve(address(esc), type(uint256).max);

        vm.prank(creator);
        uint256 id = esc.createBounty(REWARD, uint64(block.timestamp) + DURATION, HASH, URI);
        vm.prank(alice);
        esc.submitWork(id, HASH, URI);
        vm.prank(creator);
        esc.award(id, alice);
        vm.prank(alice);
        esc.withdraw(alice);
        assertEq(token.balanceOf(alice), REWARD, "no-return token still pays exactly");
    }

    // ================================================================ CALLBACK REENTRANCY

    function test_callbackReentrancy_attempt_cannotDoubleSpend() public {
        ReentrantCallbackToken token = new ReentrantCallbackToken();
        IMDWorksEscrow esc = _setup(token, REWARD * 2);
        uint256 id = _newBounty(esc, creator);
        vm.prank(alice);
        esc.submitWork(id, HASH, URI);
        vm.prank(creator);
        esc.award(id, alice);

        // Arm the token so that the escrow->token transfer calls back into withdraw(alice).
        // The escrow zeroes claimable BEFORE the transfer and is nonReentrant, so the
        // re-entrant call must fail and the second attempt cannot mint a second payout.
        bytes memory payload = abi.encodeWithSelector(IMDWorksEscrow.withdraw.selector, alice);
        token.arm(address(esc), payload);

        vm.prank(alice);
        esc.withdraw(alice); // outer call succeeds
        assertEq(token.balanceOf(alice), REWARD, "exactly one payout, no double spend");
        assertEq(esc.claimable(alice), 0, "credit zeroed once");
        assertEq(esc.totalClaimable(), 0, "no residual claimable");
        _assertConservation(esc, token, REWARD); // only the 1 reward was ever locked
    }

    // ================================================================ EXPIRED / REFUND BOOKKEEPING

    function test_pausedToken_doesNotBlockExpireBookkeeping() public {
        PausableToken token = new PausableToken();
        IMDWorksEscrow esc = _setup(token, REWARD);
        uint256 id = _newBounty(esc, creator);

        // Pause immediately; nobody submits; expiry is pure bookkeeping.
        token.setPaused(true);
        vm.warp(block.timestamp + DURATION + 1);
        esc.expire(id); // anyone can release
        assertEq(esc.claimable(creator), REWARD, "creator refunded in books despite pause");
        assertEq(esc.totalLocked(), 0, "lock released");
        (address _c, address _w, uint64 _d, uint64 _s, IMDWorksEscrow.Status status, uint256 _r, bytes32 _b) = esc.bounties(id);
        assertEq(uint8(status), uint8(IMDWorksEscrow.Status.Expired), "Expired");
        _c; _w; _d; _s; _r; _b;

        // Creator cannot withdraw while paused (issuer action needed), credit preserved.
        vm.prank(creator);
        vm.expectRevert();
        esc.withdraw(creator);
        assertEq(esc.claimable(creator), REWARD, "refund credit preserved");

        token.setPaused(false);
        vm.prank(creator);
        esc.withdraw(creator);
        assertEq(token.balanceOf(creator), REWARD, "creator recovers after unpause");
        _assertConservation(esc, token, REWARD);
    }
}
