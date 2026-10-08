// SPDX-License-Identifier: MIT
pragma solidity 0.8.29;

import {Test} from "forge-std/Test.sol";
import {IMDWorksEscrow} from "../src/IMDWorksEscrow.sol";
import {BaseToken} from "../src/mocks/IssuerTokens.sol";
import {LedgerFixture} from "./LedgerFixture.sol";

/// @title escrow 10 — fixture generator + event dump for the ledger reconciler
/// @notice Runs a deterministic, seeded set of bounty lifecycles against the unmodified
///         escrow and emits a JSON blob (via `vm.writeFile`) containing:
///           - the escrow's own event log (the ONLY input the reconciler is allowed)
///           - token Transfer/Mint events (for the token-truth cross-check)
///           - the fixed snapshot block + the contract state at that block
///           - the scenario manifest (what each lifecycle did, including orphan/dup cases)
///         The Python reconciler then reconstructs locked rewards and credits **from the
///         events alone** and must agree with the state block.
contract LedgerGenTest is Test {
    uint256 internal constant HARDHAT = 31337;

    function test_generate_fixture() public {
        LedgerFixture f = new LedgerFixture();
        string memory json = f.generate(
            /*seed*/ 0x20261008,
            /*lifecycles*/ 100,
            /*orphanDuplicates*/ 7
        );
        vm.writeFile("fixture/ledger_fixture.json", json);
        emit log_string("wrote fixture/ledger_fixture.json");
        emit log_named_uint("bytes", bytes(json).length);
    }
}
