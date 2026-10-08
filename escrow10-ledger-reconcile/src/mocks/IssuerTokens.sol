// SPDX-License-Identifier: MIT
pragma solidity 0.8.29;

/// @title Configurable ERC-20 behaviour mocks for the IMDWorksEscrow issuer matrix.
/// @notice Six token personalities that a real issuer could impose *without the escrow
///         code changing*: pause, sender restriction, recipient restriction, tax,
///         false-return and no-return. Each one is a faithful stand-in for a deployed
///         token an issuer controls — no live administrator is ever impersonated.

/// @dev Minimal 6-decimal token used as the common base. Exact 1:1 transfers.
contract BaseToken {
    string public name = "Mock USDG";
    string public symbol = "mUSDG";
    uint8 public constant decimals = 6;

    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);
    event Mint(address indexed to, uint256 value);

    function mint(address to, uint256 value) external virtual {
        totalSupply += value;
        balanceOf[to] += value;
        emit Mint(to, value);
    }

    function approve(address spender, uint256 value) external returns (bool) {
        allowance[msg.sender][spender] = value;
        emit Approval(msg.sender, spender, value);
        return true;
    }

    function _move(address from, address to, uint256 value) internal {
        balanceOf[from] -= value;
        balanceOf[to] += value;
        emit Transfer(from, to, value);
    }

    // Standard transfer: returns bool true.
    function transfer(address to, uint256 value) external virtual returns (bool) {
        _move(msg.sender, to, value);
        return true;
    }

    function transferFrom(address from, address to, uint256 value) external virtual returns (bool) {
        uint256 a = allowance[from][msg.sender];
        require(a >= value, "allowance");
        if (a != type(uint256).max) allowance[from][msg.sender] = a - value;
        _move(from, to, value);
        return true;
    }
}

/// @dev Issuer can freeze ALL movement. Deposits and withdrawals stop; bookkeeping does not.
contract PausableToken is BaseToken {
    bool public paused;
    function setPaused(bool p) external { paused = p; }
    function transfer(address to, uint256 value) external override returns (bool) {
        require(!paused, "paused");
        _move(msg.sender, to, value);
        return true;
    }
    function transferFrom(address from, address to, uint256 value) external override returns (bool) {
        require(!paused, "paused");
        uint256 a = allowance[from][msg.sender];
        require(a >= value, "allowance");
        if (a != type(uint256).max) allowance[from][msg.sender] = a - value;
        _move(from, to, value);
        return true;
    }
}

/// @dev Issuer blocks specific senders (e.g. the escrow contract itself). Directional.
contract SenderRestrictedToken is BaseToken {
    mapping(address => bool) public blockedSender;
    function blockSender(address a, bool b) external { blockedSender[a] = b; }
    function transfer(address to, uint256 value) external override returns (bool) {
        require(!blockedSender[msg.sender], "sender blocked");
        _move(msg.sender, to, value);
        return true;
    }
    function transferFrom(address from, address to, uint256 value) external override returns (bool) {
        require(!blockedSender[from], "sender blocked");
        uint256 a = allowance[from][msg.sender];
        require(a >= value, "allowance");
        if (a != type(uint256).max) allowance[from][msg.sender] = a - value;
        _move(from, to, value);
        return true;
    }
}

/// @dev Issuer blocklists a recipient (e.g. a winner wallet). Directional.
contract RecipientBlockedToken is BaseToken {
    mapping(address => bool) public blockedRecipient;
    function blockRecipient(address a, bool b) external { blockedRecipient[a] = b; }
    function transfer(address to, uint256 value) external override returns (bool) {
        require(!blockedRecipient[to], "recipient blocked");
        _move(msg.sender, to, value);
        return true;
    }
    function transferFrom(address from, address to, uint256 value) external override returns (bool) {
        require(!blockedRecipient[to], "recipient blocked");
        uint256 a = allowance[from][msg.sender];
        require(a >= value, "allowance");
        if (a != type(uint256).max) allowance[from][msg.sender] = a - value;
        _move(from, to, value);
        return true;
    }
}

/// @dev Issuer takes a cut on every hop. Recipient receives less than `value`.
///      The escrow's delta check must reject this as UnsupportedTransfer.
contract TaxToken is BaseToken {
    uint256 public feeBps; // e.g. 500 = 5%
    constructor(uint256 _feeBps) { feeBps = _feeBps; }
    /// @dev Issuer can turn the tax on/off at any time (simulates a fee switch).
    function setFee(uint256 _feeBps) external { feeBps = _feeBps; }
    function _taxedMove(address from, address to, uint256 value) internal {
        uint256 fee = (value * feeBps) / 10000;
        uint256 net = value - fee;
        balanceOf[from] -= value;
        balanceOf[to] += net;
        totalSupply -= fee; // burned
        emit Transfer(from, to, net);
    }
    function transfer(address to, uint256 value) external override returns (bool) {
        _taxedMove(msg.sender, to, value);
        return true;
    }
    function transferFrom(address from, address to, uint256 value) external override returns (bool) {
        uint256 a = allowance[from][msg.sender];
        require(a >= value, "allowance");
        if (a != type(uint256).max) allowance[from][msg.sender] = a - value;
        _taxedMove(from, to, value);
        return true;
    }
}

/// @dev Legacy token that returns `false` instead of reverting on a blocked move.
///      SafeERC20 turns the false into a revert -> escrow stays solvent.
contract FalseReturnToken is BaseToken {
    bool public failMoves;
    function setFailMoves(bool f) external { failMoves = f; }
    function transfer(address to, uint256 value) external override returns (bool) {
        if (failMoves) return false;
        _move(msg.sender, to, value);
        return true;
    }
    function transferFrom(address from, address to, uint256 value) external override returns (bool) {
        if (failMoves) return false;
        uint256 a = allowance[from][msg.sender];
        require(a >= value, "allowance");
        if (a != type(uint256).max) allowance[from][msg.sender] = a - value;
        _move(from, to, value);
        return true;
    }
}

/// @dev Pre-standard token: transfer/transferFrom return NOTHING.
///      OZ SafeERC20 tolerates this (returndata-length check) -> supported.
contract NoReturnToken {
    string public name = "Mock NoReturn USDG";
    string public symbol = "mNR";
    uint8 public constant decimals = 6;
    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    function mint(address to, uint256 value) external {
        totalSupply += value;
        balanceOf[to] += value;
    }

    function approve(address spender, uint256 value) external {
        allowance[msg.sender][spender] = value;
        emit Approval(msg.sender, spender, value);
    }

    function transfer(address to, uint256 value) external {
        balanceOf[msg.sender] -= value;
        balanceOf[to] += value;
        emit Transfer(msg.sender, to, value);
    }

    function transferFrom(address from, address to, uint256 value) external {
        uint256 a = allowance[from][msg.sender];
        require(a >= value, "allowance");
        if (a != type(uint256).max) allowance[from][msg.sender] = a - value;
        balanceOf[from] -= value;
        balanceOf[to] += value;
        emit Transfer(from, to, value);
    }
}

/// @dev Malicious token that re-enters the escrow on transfer. Used for the callback
///      reentrancy attempt (escrow is nonReentrant + credit is zeroed before transfer).
contract ReentrantCallbackToken is BaseToken {
    address public target;
    bytes public payload;
    bool public armed;

    function arm(address _target, bytes calldata _payload) external {
        target = _target;
        payload = _payload;
        armed = true;
    }

    function _fire() internal {
        if (!armed) return;
        armed = false; // one shot, avoid infinite recursion
        (bool ok, ) = target.call(payload);
        ok; // ignore: we only need the attempt; escrow must stay solvent
    }

    function transfer(address to, uint256 value) external override returns (bool) {
        _move(msg.sender, to, value);
        _fire();
        return true;
    }

    function transferFrom(address from, address to, uint256 value) external override returns (bool) {
        uint256 a = allowance[from][msg.sender];
        require(a >= value, "allowance");
        if (a != type(uint256).max) allowance[from][msg.sender] = a - value;
        _move(from, to, value);
        _fire();
        return true;
    }
}
