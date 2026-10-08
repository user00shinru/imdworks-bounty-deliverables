/**
 * Canonical brief hashing — JavaScript reference implementation.
 *
 * Must produce byte-identical canonical bytes to python/brief_canon.py.
 *
 * Numeric representation is the sharp edge here: JavaScript `number` is a
 * float64 and loses integer precision above 2^53. A bounty reward or deadline
 * is a uint256/uint64 on chain, so this implementation **refuses** bare
 * numbers for `reward` and `deadline` and requires a decimal string or BigInt.
 * Passing `1e21` as a number would silently become 1000000000000000000000 —
 * which happens to print the same, but `Number.MAX_SAFE_INTEGER + 2` does not.
 * Rejecting is the only honest option.
 */
'use strict';

const fs = require('fs');
const path = require('path');

// js-sha3 is vendored so the repo is self-contained (no npm install needed)
const VENDOR = path.join(__dirname, 'vendor', 'js-sha3.js');
const { keccak256: _keccak } = require(fs.existsSync(VENDOR) ? VENDOR : 'js-sha3');

const ALLOWED_KEYS = ['title', 'description', 'criteria', 'reward', 'token', 'deadline'];
const PREFIX = 'imdworks-brief-v1\n';

class CanonicalError extends Error {
  constructor(kind, detail = '') {
    super(detail ? `${kind}: ${detail}` : kind);
    this.kind = kind;
    this.detail = detail;
  }
}

function isPlainObject(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v);
}

function assertString(field, v) {
  if (typeof v !== 'string') {
    throw new CanonicalError('type', `${field} must be a string, got ${typeof v}`);
  }
}

function assertNoForbidden(field, v) {
  if (v.indexOf('\u0000') !== -1) throw new CanonicalError('nul_byte', field);
  if (v.indexOf('\r') !== -1) throw new CanonicalError('cr_character', field);
  for (const ch of v) {
    const cp = ch.codePointAt(0);
    if (cp >= 0xd800 && cp <= 0xdfff) throw new CanonicalError('lone_surrogate', field);
  }
}

function utf8Bytes(s) {
  // TextEncoder always emits well-formed UTF-8 for a well-formed JS string
  return new TextEncoder().encode(s);
}

function assertNfc(field, v) {
  // JS has no built-in NFC check that matches Python's unicodedata exactly for
  // every code point, so we implement the check the same way Python does:
  // compare against the NFC form produced by String.prototype.normalize, which
  // is ICU-backed and conforms to UAX #15.
  if (v.normalize('NFC') !== v) throw new CanonicalError('not_nfc', field);
}

/**
 * Accept a uint256-ish value only as a decimal string or BigInt.
 * Returns the canonical decimal string.
 *
 * NOTE (real finding, see FINDINGS.md): a negative literal must be rejected as
 * `negative`, not as `not_canonical_decimal`. The first version applied the
 * canonical-decimal regex to the *raw* string, so `-1` failed the regex before
 * the sign was ever examined. We now check the sign first so both languages
 * agree.
 */
function intToDecimal(value, field) {
  let s;
  if (typeof value === 'bigint') {
    if (value < 0n) throw new CanonicalError('negative', field);
    s = value.toString(10);
  } else if (typeof value === 'string') {
    if (value.startsWith('-')) throw new CanonicalError('negative', field);
    s = value;
  } else {
    throw new CanonicalError(
      'type',
      `${field} must be a decimal string or BigInt (JS number cannot represent uint256 safely)`
    );
  }
  if (!/^(0|[1-9][0-9]*)$/.test(s)) {
    // deliberately strict: no sign, no leading zeros, no exponent, no fraction
    throw new CanonicalError('not_canonical_decimal', `${field}=${s}`);
  }
  if (BigInt(s) > (2n ** 256n - 1n)) throw new CanonicalError('overflow_uint256', field);
  return s;
}

function checkToken(token) {
  assertString('token', token);
  if (!token.startsWith('0x') || token.length !== 42) {
    throw new CanonicalError('bad_token', token);
  }
  const body = token.slice(2);
  if (!/^[0-9a-fA-F]{40}$/.test(body)) throw new CanonicalError('bad_token', token);
  if (body !== body.toLowerCase()) throw new CanonicalError('non_canonical_hex', token);
  return token;
}

function lenPrefixed(field, text) {
  const body = utf8Bytes(text);
  const name = utf8Bytes(field);
  const len = utf8Bytes(String(body.length));
  const out = new Uint8Array(name.length + 1 + len.length + 1 + body.length);
  let o = 0;
  out.set(name, o); o += name.length;
  out[o++] = 0x20; // ' '
  out.set(len, o); o += len.length;
  out[o++] = 0x3a; // ':'
  out.set(body, o);
  return out;
}

function concat(parts, sep) {
  const total = parts.reduce((n, p) => n + p.length, 0) + Math.max(0, parts.length - 1) * sep.length;
  const out = new Uint8Array(total);
  let o = 0;
  parts.forEach((p, i) => {
    if (i > 0) { out.set(sep, o); o += sep.length; }
    out.set(p, o); o += p.length;
  });
  return out;
}

function canonicalBytes(brief) {
  if (!isPlainObject(brief)) throw new CanonicalError('type', 'brief must be an object');

  const keys = Object.keys(brief);
  const missing = ALLOWED_KEYS.filter((k) => !keys.includes(k));
  const extra = keys.filter((k) => !ALLOWED_KEYS.includes(k));
  if (missing.length) throw new CanonicalError('missing_field', missing.join(','));
  if (extra.length) throw new CanonicalError('unknown_field', extra.sort().join(','));

  const parts = [];

  for (const field of ['title', 'description', 'criteria']) {
    const v = brief[field];
    assertString(field, v);
    if (typeof v === 'string') {
      assertNoForbidden(field, v);
      assertNfc(field, v);
    }
    if (v === '') throw new CanonicalError('empty_field', field);
    parts.push(lenPrefixed(field, v));
  }

  parts.push(lenPrefixed('reward', intToDecimal(brief.reward, 'reward')));
  parts.push(lenPrefixed('token', checkToken(brief.token)));
  parts.push(lenPrefixed('deadline', intToDecimal(brief.deadline, 'deadline')));

  const joined = concat(parts, utf8Bytes('\n'));
  return concat([utf8Bytes(PREFIX), joined], new Uint8Array(0));
}

function keccak256(bytes) {
  return '0x' + _keccak(bytes);
}

function briefHash(brief) {
  return keccak256(canonicalBytes(brief));
}

function tryBriefHash(brief) {
  try {
    return { hash: briefHash(brief), kind: '' };
  } catch (e) {
    if (e instanceof CanonicalError) return { hash: '', kind: e.kind };
    throw e;
  }
}

module.exports = {
  CanonicalError,
  canonicalBytes,
  briefHash,
  tryBriefHash,
  keccak256,
  ALLOWED_KEYS,
  PREFIX,
};
