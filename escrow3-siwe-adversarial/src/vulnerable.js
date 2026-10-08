/**
 * Two intentionally vulnerable sign-in implementations.
 *
 * They exist so the regression suite proves it can actually DETECT the classic
 * mistakes, not merely confirm that a careful implementation refuses. If either of
 * these ever passes the suite, the suite has stopped being a regression test.
 */
import { ethers } from './vendor.js';
import { parseSiwe, normAddress } from './siwe.js';

/** Shared: recover the signer, or null. */
function recover(message, signature) {
  try { return normAddress(ethers.verifyMessage(message, signature)); }
  catch { return null; }
}

/**
 * VULN #1 — nonce is not single-use, and replay is not checked.
 *
 * Classic replay hole: the nonce is validated once and then left usable, so the same
 * signed message can be submitted again to mint a second session. Also skips origin.
 */
export class ReplayableAuth {
  constructor({ chainId = 4663 } = {}) {
    this.chainId = chainId;
    this.nonces = new Map();
    this.sessions = new Map();
  }

  challenge(address) {
    const addr = normAddress(address);
    const nonce = 'n' + Math.random().toString(16).slice(2, 18);
    const message = [
      `https://dapp.local wants you to sign in with your Ethereum account:`,
      addr, '', 'Sign in.', '',
      `URI: https://dapp.local`, 'Version: 1', `Chain ID: ${this.chainId}`,
      `Nonce: ${nonce}`, `Issued At: ${new Date().toISOString().replace(/\.\d{3}Z$/, 'Z')}`,
    ].join('\n');
    this.nonces.set(nonce, { address: addr });
    return { nonce, message };
  }

  async verify({ message, signature }) {
    const p = parseSiwe(message);
    const rec = this.nonces.get(p.nonce);
    if (!rec) throw new Error('unknown nonce');
    // BUG: no `used` flag. Replay succeeds.
    const signer = recover(message, signature);
    if (!signer || signer !== rec.address) throw new Error('wrong signer');
    // BUG: origin/URI not compared at all.
    const token = 's' + Math.random().toString(16).slice(2, 18);
    this.sessions.set(token, { address: signer });
    return { token, address: signer };
  }
}

/** @deprecated alias kept so either name can be used in the runner. */
export const ReplayAuth = ReplayableAuth;

/**
 * VULN #2 — nonce validity is looked up but never marked used, and there is no
 * concurrency guard, so two simultaneous verifications of one nonce both succeed.
 *
 * The oracle-style hole: the check runs, the mutation happens after, so the window
 * between them is exploitable. `verify` also deliberately yields to the event loop
 * (matching a real handler that awaits a DB read) to make the race reachable.
 */
export class RaceableAuth {
  constructor({ chainId = 4663 } = {}) {
    this.chainId = chainId;
    this.nonces = new Map();
    this.sessions = new Map();
  }

  challenge(address) {
    const addr = normAddress(address);
    const nonce = 'r' + Math.random().toString(16).slice(2, 18);
    const message = [
      `https://dapp.local wants you to sign in with your Ethereum account:`,
      addr, '', 'Sign in.', '',
      `URI: https://dapp.local`, 'Version: 1', `Chain ID: ${this.chainId}`,
      `Nonce: ${nonce}`, `Issued At: ${new Date().toISOString().replace(/\.\d{3}Z$/, 'Z')}`,
    ].join('\n');
    this.nonces.set(nonce, { address: addr });
    return { nonce, message };
  }

  async verify({ message, signature }) {
    const p = parseSiwe(message);
    const rec = this.nonces.get(p.nonce);
    if (!rec) throw new Error('unknown nonce');
    // BUG: read, then await, then write — the classic TOCTOU window.
    await new Promise((r) => setImmediate(r));
    const signer = recover(message, signature);
    if (!signer || signer !== rec.address) throw new Error('wrong signer');
    const token = 'c' + Math.random().toString(16).slice(2, 18);
    this.sessions.set(token, { address: signer });
    return { token, address: signer };
  }
}
