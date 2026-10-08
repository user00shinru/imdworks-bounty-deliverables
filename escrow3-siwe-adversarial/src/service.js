/**
 * Local wallet sign-in service, modelled on a nonce-bound personal_sign flow.
 *
 * This is a FIXTURE, not a production server: it holds all state in memory, never
 * touches a network and never sees a real key. Every wallet is generated inside the
 * test process.
 *
 * The service is intentionally written the way a careful implementation would be —
 * single-use nonces, exact origin/chain checks, expiring messages — so the suite can
 * then hand it hostile inputs and watch it refuse.
 */
import crypto from 'node:crypto';
import { ethers } from './vendor.js';
import { parseSiwe, normAddress, iso, SiweError } from './siwe.js';

export const DEFAULTS = {
  origin: 'https://dapp.local',
  chainId: 4663,
  nonceTtlMs: 5 * 60 * 1000,   // challenge validity
  sessionTtlMs: 30 * 60 * 1000,
  clockSkewMs: 60 * 1000,
};

export class AuthError extends Error {
  constructor(code, message, status = 401) {
    super(message);
    this.name = 'AuthError';
    this.code = code;    // machine-readable
    this.status = status;
  }
}

export class SignInService {
  constructor(opts = {}) {
    this.cfg = { ...DEFAULTS, ...opts };
    this.nonces = new Map();     // nonce -> {address, issuedAt, expiresAt, used}
    this.sessions = new Map();   // token -> {address, expiresAt, nonce}
    this.verifications = 0;
    this.now = opts.now || (() => Date.now());
  }

  /** Step 1: issue a nonce bound to an address. Always fresh, never reused. */
  challenge(address, { chainId, origin } = {}) {
    let addr;
    try { addr = normAddress(address); }
    catch { throw new AuthError('BAD_ADDRESS', 'address is not a 20-byte hex value', 400); }
    const cid = chainId ?? this.cfg.chainId;
    if (cid !== this.cfg.chainId) throw new AuthError('BAD_CHAIN', 'unsupported chain', 400);
    if (origin && origin !== this.cfg.origin) throw new AuthError('BAD_ORIGIN', 'origin not allowed', 403);

    const nonce = crypto.randomBytes(16).toString('hex');
    const t = this.now();
    const message = [
      `${this.cfg.origin} wants you to sign in with your Ethereum account:`,
      addr,
      '',
      'Sign in to the local test service. This request will not trigger a blockchain transaction or cost any gas.',
      '',
      `URI: ${this.cfg.origin}`,
      'Version: 1',
      `Chain ID: ${this.cfg.chainId}`,
      `Nonce: ${nonce}`,
      `Issued At: ${iso(t)}`,
      `Expiration Time: ${iso(t + this.cfg.nonceTtlMs)}`,
    ].join('\n');
    this.nonces.set(nonce, { address: addr, issuedAt: t, expiresAt: t + this.cfg.nonceTtlMs, used: false });
    return { nonce, message, expiresAt: t + this.cfg.nonceTtlMs };
  }

  /**
   * Step 2: verify a signature over the challenge message and issue a session.
   *
   * Everything a login endpoint must check, in order:
   *   - the nonce exists and belongs to this address
   *   - the nonce has never been used           (replay)
   *   - the nonce has not expired               (expiry)
   *   - the message parses, and its own fields agree with the challenge
   *     (origin, chain id, address, nonce)
   *   - the signature recovers to the claimed address
   * The nonce is consumed *before* the signature check succeeds or fails, so a
   * concurrent second attempt for the same nonce can never both win.
   */
  async verify({ message, signature }, { origin } = {}) {
    if (origin && origin !== this.cfg.origin) throw new AuthError('BAD_ORIGIN', 'origin not allowed', 403);
    if (typeof message !== 'string' || typeof signature !== 'string') {
      throw new AuthError('MALFORMED_REQUEST', 'message and signature are required', 400);
    }

    let parsed;
    try { parsed = parseSiwe(message); }
    catch (e) { throw new AuthError('BAD_MESSAGE', e.message, 400); }

    const nonce = parsed.nonce;
    const rec = this.nonces.get(nonce);
    if (!rec) throw new AuthError('UNKNOWN_NONCE', 'nonce was not issued by this service');

    // Consume immediately: single-use, and a losing concurrent call must not be able
    // to retry the same nonce after a signature failure.
    if (rec.used) throw new AuthError('NONCE_REPLAY', 'nonce has already been used');
    rec.used = true;

    const t = this.now();
    if (t > rec.expiresAt) throw new AuthError('NONCE_EXPIRED', 'nonce has expired');

    if (normAddress(parsed.address) !== rec.address) {
      throw new AuthError('ADDRESS_MISMATCH', 'message address does not match the nonce owner');
    }
    if (parsed.uri !== this.cfg.origin) {
      throw new AuthError('URI_MISMATCH', 'message URI does not match the service origin');
    }
    if (parsed.chainId !== this.cfg.chainId) {
      throw new AuthError('CHAIN_MISMATCH', 'message chain does not match the service chain');
    }
    if (parsed.expirationTime && Date.parse(parsed.expirationTime) < t - this.cfg.clockSkewMs) {
      throw new AuthError('MESSAGE_EXPIRED', 'message expiration is in the past');
    }
    if (parsed.issuedAt && Date.parse(parsed.issuedAt) > t + this.cfg.clockSkewMs) {
      throw new AuthError('ISSUED_IN_FUTURE', 'Issued At is in the future');
    }

    let recovered;
    try { recovered = ethers.verifyMessage(message, signature); }
    catch { throw new AuthError('BAD_SIGNATURE', 'signature could not be recovered'); }
    if (normAddress(recovered) !== rec.address) {
      throw new AuthError('WRONG_SIGNER', 'signature does not match the claimed address');
    }

    const token = crypto.randomBytes(24).toString('hex');
    this.sessions.set(token, { address: rec.address, expiresAt: t + this.cfg.sessionTtlMs, nonce });
    this.verifications++;
    return { token, address: rec.address, expiresAt: t + this.cfg.sessionTtlMs };
  }

  /** Validate a session token. */
  session(token) {
    const s = this.sessions.get(token);
    if (!s) throw new AuthError('NO_SESSION', 'unknown session token');
    if (this.now() > s.expiresAt) { this.sessions.delete(token); throw new AuthError('SESSION_EXPIRED', 'session expired'); }
    return { address: s.address };
  }
}

export { SiweError };
