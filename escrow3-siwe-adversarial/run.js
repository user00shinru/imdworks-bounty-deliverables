#!/usr/bin/env node
/**
 * escrow 3 — adversarial wallet sign-in regression suite.
 *
 * Black-box: every case goes through the public `challenge` -> `verify` surface of the
 * service only. Wallets are generated in-process; nothing touches a network, a real
 * key, or production infrastructure.
 *
 * Failure conditions apply to the render/log, never to the suite's behaviour.
 */
import { ethers } from './src/vendor.js';
import { SignInService, AuthError } from './src/service.js';
import { ReplayableAuth, RaceableAuth } from './src/vulnerable.js';
import { parseSiwe, buildSiwe, iso } from './src/siwe.js';

const ORIGIN = 'https://dapp.local';
const CHAIN = 4663;

const results = [];
let caseId = 0;

function record(name, ok, detail = '') {
  caseId += 1;
  results.push({ id: caseId, name, ok: !!ok, detail });
}

function wallet(seed) {
  return new ethers.Wallet('0x' + String(seed).padStart(64, '0'));
}

/** Fresh service + wallet + issued challenge. */
function fresh(opts = {}) {
  const w = wallet(0x11);
  const svc = new SignInService({ chainId: CHAIN, ...opts });
  const ch = svc.challenge(w.address);
  return { w, svc, ch };
}

/**
 * Assert `fn` rejects with an AuthError carrying exactly `code`.
 *
 * Returns the error on success. THROWS on any mismatch so a wrong rejection code
 * fails the case loudly instead of being silently accepted — a helper that returns
 * a truthy "wrong" object is how a suite ends up green while testing nothing.
 */
async function expectReject(fn, code) {
  let caught;
  try {
    await fn();
  } catch (e) {
    caught = e;
  }
  if (!caught) throw new Error(`expected ${code || 'a rejection'} but the call succeeded`);
  if (!(caught instanceof AuthError)) {
    throw new Error(`expected AuthError ${code}, got ${caught.constructor.name}: ${caught.message}`);
  }
  if (code && caught.code !== code) throw new Error(`expected ${code}, got ${caught.code}`);
  return caught;
}

/** Sign a message with a wallet. */
const sign = (w, msg) => w.signMessage(msg);

// ============================================================ 1. happy path

async function case_happy() {
  const { w, svc, ch } = fresh();
  const sig = await sign(w, ch.message);
  const out = await svc.verify({ message: ch.message, signature: sig });
  const sess = svc.session(out.token);
  record('valid sign-in succeeds and yields a working session',
    out.address.toLowerCase() === w.address.toLowerCase()
    && sess.address.toLowerCase() === w.address.toLowerCase(),
    `session for ${sess.address}`);
}

// ============================================================ 2. nonce replay

async function case_replay_sequential() {
  const { w, svc, ch } = fresh();
  const sig = await sign(w, ch.message);
  await svc.verify({ message: ch.message, signature: sig });
  const e = await expectReject(
    () => svc.verify({ message: ch.message, signature: sig }), 'NONCE_REPLAY');
  record('replaying a completed verification is rejected', !!e, e ? e.code : 'SECOND VERIFY SUCCEEDED');
}

async function case_replay_after_failed_signature() {
  // The nonce must burn even when the signature is wrong, so an attacker cannot
  // retry guesses against a still-valid nonce.
  const { w, svc, ch } = fresh();
  const bad = await sign(wallet(0x99), ch.message);
  const e1 = await expectReject(
    () => svc.verify({ message: ch.message, signature: bad }), 'WRONG_SIGNER');
  const good = await sign(w, ch.message);
  const e2 = await expectReject(
    () => svc.verify({ message: ch.message, signature: good }), 'NONCE_REPLAY');
  record('a failed signature burns the nonce (no retry window)', !!e1 && !!e2,
    e1 && e2 ? `${e1.code} then ${e2.code}` : 'nonce reusable after failure');
}

// ============================================================ 3. concurrency

async function case_concurrent_one_wins() {
  const { w, svc, ch } = fresh();
  const sig = await sign(w, ch.message);
  const settled = await Promise.allSettled([
    svc.verify({ message: ch.message, signature: sig }),
    svc.verify({ message: ch.message, signature: sig }),
  ]);
  const ok = settled.filter((s) => s.status === 'fulfilled').length;
  const win = settled.find((s) => s.status === 'fulfilled');
  const lose = settled.find((s) => s.status === 'rejected');
  const token = win?.value?.token;
  record('two concurrent verifications of one nonce: exactly one succeeds',
    ok === 1,
    `fulfilled=${ok}, rejected=${2 - ok}, loser=${lose?.reason?.code ?? 'n/a'}`);
  // The losing call must not have minted a session, and the winner's token must resolve.
  const winnerSession = token ? svc.session(token) : null;
  record('winner holds the only session for that nonce',
    !!winnerSession && svc.sessions.size === 1,
    `sessions=${svc.sessions.size}, winner=${winnerSession?.address ?? 'none'}`);
  return settled;
}

async function case_concurrent_many() {
  const { w, svc, ch } = fresh();
  const sig = await sign(w, ch.message);
  const n = 8;
  const settled = await Promise.allSettled(
    Array.from({ length: n }, () => svc.verify({ message: ch.message, signature: sig })));
  const ok = settled.filter((s) => s.status === 'fulfilled').length;
  record(`N-parallel (${n}) same-nonce: exactly one succeeds`, ok === 1, `fulfilled=${ok}`);
}

// ============================================================ 4. wrong signer

async function case_wrong_signer() {
  const { w, svc, ch } = fresh();
  const other = wallet(0x99);
  const sig = await sign(other, ch.message);   // signed by a different key
  const e = await expectReject(
    () => svc.verify({ message: ch.message, signature: sig }), 'WRONG_SIGNER');
  record('signature from another wallet is rejected', !!e, e ? e.code : 'ACCEPTED');
}

async function case_address_swapped_in_message() {
  // The message is re-built with a different address but the signature is over the
  // ORIGINAL message, so recovery cannot match the (rewritten) address.
  const { w, svc, ch } = fresh();
  const sig = await sign(w, ch.message);
  const p = parseSiwe(ch.message);
  const forged = ch.message.replace(p.address, wallet(0x99).address);
  const e = await expectReject(
    () => svc.verify({ message: forged, signature: sig }), 'ADDRESS_MISMATCH');
  record('address swapped in the message body is rejected', !!e, e ? e.code : 'ACCEPTED');
}

// ============================================================ 5. origin / URI

async function case_uri_substituted() {
  const { w, svc, ch } = fresh();
  const sig = await sign(w, ch.message);
  const forged = ch.message.replace(`URI: ${ORIGIN}`, 'URI: https://evil.example');
  const e = await expectReject(
    () => svc.verify({ message: forged, signature: sig }), 'URI_MISMATCH');
  record('substituted URI is rejected', !!e, e ? e.code : 'ACCEPTED');
}

async function case_origin_lookalike() {
  const { w, svc, ch } = fresh();
  const sig = await sign(w, ch.message);
  const forged = ch.message.replace(`URI: ${ORIGIN}`, 'URI: https://dapp.local.evil.example');
  const e = await expectReject(
    () => svc.verify({ message: forged, signature: sig }), 'URI_MISMATCH');
  record('lookalike origin (superset domain) is rejected', !!e, e ? e.code : 'ACCEPTED');
}

async function case_origin_header_mismatch() {
  const { w, svc, ch } = fresh();
  const sig = await sign(w, ch.message);
  const e = await expectReject(
    () => svc.verify({ message: ch.message, signature: sig }, { origin: 'https://evil.example' }),
    'BAD_ORIGIN');
  record('request Origin header mismatch is rejected', !!e, e ? e.code : 'ACCEPTED');
}

async function case_challenge_wrong_origin() {
  const svc = new SignInService({ chainId: CHAIN });
  let caught = null;
  try { svc.challenge(wallet(0x11).address, { origin: 'https://evil.example' }); }
  catch (e) { caught = e; }
  record('challenge from a foreign origin is refused', caught?.code === 'BAD_ORIGIN', caught?.code ?? 'ISSUED');
}

// ============================================================ 6. chain id

async function case_chain_mismatch() {
  const { w, svc, ch } = fresh();
  const sig = await sign(w, ch.message);
  const forged = ch.message.replace(`Chain ID: ${CHAIN}`, 'Chain ID: 1');
  const e = await expectReject(
    () => svc.verify({ message: forged, signature: sig }), 'CHAIN_MISMATCH');
  record('chain-id substitution is rejected', !!e, e ? e.code : 'ACCEPTED');
}

async function case_challenge_wrong_chain() {
  const svc = new SignInService({ chainId: CHAIN });
  let caught = null;
  try { svc.challenge(wallet(0x11).address, { chainId: 1 }); }
  catch (e) { caught = e; }
  record('challenge for another chain is refused', caught?.code === 'BAD_CHAIN', caught?.code ?? 'ISSUED');
}

// ============================================================ 7. expiry boundaries

async function case_expiry_exact_boundary() {
  // right on the deadline: NOT expired (the check is `t > expiresAt`)
  const svc = new SignInService({ chainId: CHAIN });
  let clock = 1_700_000_000_000;
  svc.now = () => clock;
  const w = wallet(0x11);
  const ch = svc.challenge(w.address);
  const sig = await sign(w, ch.message);
  clock = ch.expiresAt;                       // exactly at the deadline
  const out = await svc.verify({ message: ch.message, signature: sig });
  record('nonce exactly at its deadline is still valid', !!out.token, `token=${!!out.token}`);
}

async function case_expiry_one_ms_over() {
  const svc = new SignInService({ chainId: CHAIN });
  let clock = 1_700_000_000_000;
  svc.now = () => clock;
  const w = wallet(0x11);
  const ch = svc.challenge(w.address);
  const sig = await sign(w, ch.message);
  clock = ch.expiresAt + 1;                   // one millisecond past
  const e = await expectReject(
    () => svc.verify({ message: ch.message, signature: sig }), 'NONCE_EXPIRED');
  record('nonce one millisecond past its deadline is rejected', !!e, e ? e.code : 'ACCEPTED');
}

async function case_message_expiry_past() {
  const svc = new SignInService({ chainId: CHAIN });
  let clock = 1_700_000_000_000;
  svc.now = () => clock;
  const w = wallet(0x11);
  const msg = buildSiwe({
    domain: 'dapp.local', address: w.address, uri: ORIGIN, chainId: CHAIN,
    nonce: 'deadbeefdeadbeef', issuedAt: iso(clock - 3_600_000),
    expirationTime: iso(clock - 120_000),
  });
  const sig = await sign(w, msg);
  // register the nonce by hand so the ONLY failing condition is the expired window
  svc.nonces.set('deadbeefdeadbeef', { address: w.address.toLowerCase(), issuedAt: clock, expiresAt: clock + 60_000, used: false });
  const e = await expectReject(
    () => svc.verify({ message: msg, signature: sig }), 'MESSAGE_EXPIRED');
  record('message whose Expiration Time has passed is rejected', !!e, e ? e.code : 'ACCEPTED');
}

async function case_issued_in_future() {
  const svc = new SignInService({ chainId: CHAIN });
  const clock = 1_700_000_000_000;
  svc.now = () => clock;
  const w = wallet(0x11);
  const msg = buildSiwe({
    domain: 'dapp.local', address: w.address, uri: ORIGIN, chainId: CHAIN,
    nonce: 'fut0fut0fut0fut0', issuedAt: iso(clock + 3_600_000),
  });
  const sig = await sign(w, msg);
  svc.nonces.set('fut0fut0fut0fut0', { address: w.address.toLowerCase(), issuedAt: clock, expiresAt: clock + 60_000, used: false });
  const e = await expectReject(
    () => svc.verify({ message: msg, signature: sig }), 'ISSUED_IN_FUTURE');
  record('Issued At in the future is rejected', !!e, e ? e.code : 'ACCEPTED');
}

// ============================================================ 8. malformed input

const MALFORMED = [
  ['empty message', ''],
  ['missing header', '0x0000000000000000000000000000000000000000\n\nURI: https://dapp.local'],
  ['bad header text', 'Hello there\n0x0000000000000000000000000000000000000000'],
  ['address not 20 bytes', 'https://dapp.local wants you to sign in with your Ethereum account:\n0x1234\n\nURI: https://dapp.local'],
  ['missing nonce', null],   // built below
  ['nonce replaced with empty', null],
  ['duplicate Nonce field', null],
  ['nonce with disallowed chars', null],
  ['URI missing', null],
  ['statement without blank separator', null],
];

async function case_malformed_matrix() {
  const w = wallet(0x11);
  const base = buildSiwe({
    domain: 'dapp.local', address: w.address, uri: ORIGIN, chainId: CHAIN,
    nonce: 'a1b2c3d4e5f6a7b8', issuedAt: iso(Date.now()), expirationTime: iso(Date.now() + 300_000),
  });

  // A message WITH a statement, so the statement-separator rule is exercisable.
  const withStmt = buildSiwe({
    domain: 'dapp.local', address: w.address, statement: 'Sign in to the test service.',
    uri: ORIGIN, chainId: CHAIN, nonce: 'a1b2c3d4e5f6a7b8', issuedAt: iso(Date.now()),
  });

  const variants = {
    'empty message': '',
    'missing header': `0x0000000000000000000000000000000000000000\n\nURI: ${ORIGIN}`,
    'bad header text': `Hello there\n${w.address}`,
    'address not 20 bytes': `${ORIGIN} wants you to sign in with your Ethereum account:\n0x1234\n\nURI: ${ORIGIN}`,
    'missing nonce': base.replace(/^Nonce: .*\n/m, ''),
    'nonce replaced with empty': base.replace(/^Nonce: .*/m, 'Nonce: '),
    'duplicate Nonce field': base + '\nNonce: ffffffffffffffff',
    'nonce with disallowed chars': base.replace(/^Nonce: .*/m, 'Nonce: not a valid nonce!!'),
    'nonce too short': base.replace(/^Nonce: .*/m, 'Nonce: abc'),
    'URI missing': base.replace(/^URI: .*\n/m, ''),
    'statement without blank separator': withStmt.replace(
      'Sign in to the test service.\n\nURI:', 'Sign in to the test service.\nURI:'),
    'no blank line after address': withStmt.replace(`${w.address}\n\n`, `${w.address}\n`),
    'control character injected': base.replace(/^Nonce: (.*)$/m, 'Nonce: $1\u0000'),
    'NUL inside the URI': base.replace(`URI: ${ORIGIN}`, `URI: ${ORIGIN}\u0000evil`),
    'bad Issued At': base.replace(/^Issued At: .*/m, 'Issued At: yesterday'),
  };

  let passed = 0;
  const misses = [];
  for (const [label, msg] of Object.entries(variants)) {
    let rejected = false;
    try { parseSiwe(msg); } catch { rejected = true; }
    // Also confirm the service refuses it end-to-end.
    let svcRejected = false;
    try {
      const svc = new SignInService({ chainId: CHAIN });
      await svc.verify({ message: msg, signature: '0x' + '00'.repeat(65) });
    } catch { svcRejected = true; }
    if (rejected && svcRejected) passed++;
    else misses.push(`${label} (parser:${rejected} service:${svcRejected})`);
  }
  record(`malformed-message matrix: ${Object.keys(variants).length} variants all rejected`,
    misses.length === 0, misses.join('; ') || `all ${passed} refused`);
  void MALFORMED;
}

async function case_bad_signature_encoding() {
  // A 32-byte blob is a structurally valid r||s with implicit recovery, so it decodes
  // and recovers to some unrelated address -> WRONG_SIGNER, not BAD_SIGNATURE. Both are
  // correct outcomes; the point is that none of these are ever accepted.
  const bads = {
    'empty string': { sig: '', codes: ['BAD_SIGNATURE'] },
    'short r/s': { sig: '0x1234', codes: ['BAD_SIGNATURE'] },
    '32-byte r||s (implicit v)': { sig: '0x' + 'ab'.repeat(64), codes: ['BAD_SIGNATURE', 'WRONG_SIGNER'] },
    'non-hex': { sig: '0x' + 'zz'.repeat(65), codes: ['BAD_SIGNATURE'] },
    'v out of range': { sig: '0x' + '11'.repeat(64) + '99', codes: ['BAD_SIGNATURE', 'WRONG_SIGNER'] },
  };
  const missed = [];
  // Each encoding gets its OWN fresh nonce: a signature-level failure burns the nonce,
  // so reusing one challenge would make the second case report NONCE_REPLAY instead of
  // the encoding fault it is meant to test.
  for (const [label, { sig, codes }] of Object.entries(bads)) {
    const { svc, ch } = fresh();
    let got = null;
    try {
      await svc.verify({ message: ch.message, signature: sig });
    } catch (e) { got = e; }
    if (!got) missed.push(`${label}: ACCEPTED`);
    else if (!(got instanceof AuthError) || !codes.includes(got.code)) {
      missed.push(`${label}: got ${got?.code ?? got?.constructor?.name}`);
    }
  }
  record('malformed signature encodings are all rejected', missed.length === 0,
    missed.join('; ') || `all ${Object.keys(bads).length} refused`);
}

async function case_malformed_request_shape() {
  const { svc } = fresh();
  const bads = [
    ['null payload', null],
    ['string payload', 'hello'],
    ['missing signature', { message: 'x' }],
    ['missing message', { signature: '0x00' }],
    ['numeric signature', { message: 'x', signature: 12 }],
  ];
  const missed = [];
  for (const [label, payload] of bads) {
    let ok = false;
    try { await svc.verify(payload ?? {}, {}); } catch { ok = true; }
    if (!ok) missed.push(label);
  }
  record('malformed request shapes are rejected', missed.length === 0, missed.join('; ') || 'all refused');
}

// ============================================================ 9. nonce binding

async function case_nonce_bound_to_other_address() {
  const svc = new SignInService({ chainId: CHAIN });
  const a = wallet(0x11);
  const b = wallet(0x22);
  const chA = svc.challenge(a.address);
  const p = parseSiwe(chA.message);
  // B signs a message that carries A's nonce but B's address
  const msgB = buildSiwe({
    domain: 'dapp.local', address: b.address, uri: ORIGIN, chainId: CHAIN,
    nonce: p.nonce, issuedAt: p.issuedAt,
  });
  const sig = await sign(b, msgB);
  const e = await expectReject(
    () => svc.verify({ message: msgB, signature: sig }), 'ADDRESS_MISMATCH');
  record("a nonce issued to A cannot be used by B", !!e, e ? e.code : 'ACCEPTED');
}

async function case_unknown_nonce() {
  const svc = new SignInService({ chainId: CHAIN });
  const w = wallet(0x11);
  const msg = buildSiwe({
    domain: 'dapp.local', address: w.address, uri: ORIGIN, chainId: CHAIN,
    nonce: 'ffffffffffffffff', issuedAt: iso(Date.now()),
  });
  const sig = await sign(w, msg);
  const e = await expectReject(
    () => svc.verify({ message: msg, signature: sig }), 'UNKNOWN_NONCE');
  record('a nonce this service never issued is rejected', !!e, e ? e.code : 'ACCEPTED');
}

async function case_nonce_uniqueness() {
  const svc = new SignInService({ chainId: CHAIN });
  const w = wallet(0x11);
  const seen = new Set();
  for (let i = 0; i < 200; i++) seen.add(svc.challenge(w.address).nonce);
  record('200 issued nonces are all distinct', seen.size === 200, `${seen.size}/200 unique`);
}

// ============================================================ 10. session lifecycle

async function case_session_expiry() {
  const svc = new SignInService({ chainId: CHAIN });
  let clock = 1_700_000_000_000;
  svc.now = () => clock;
  const w = wallet(0x11);
  const ch = svc.challenge(w.address);
  const out = await svc.verify({ message: ch.message, signature: await sign(w, ch.message) });
  svc.session(out.token);                        // valid now
  clock = out.expiresAt + 1;
  let caught = null;
  try { svc.session(out.token); } catch (e) { caught = e; }
  record('session is rejected after its TTL', caught?.code === 'SESSION_EXPIRED', caught?.code ?? 'VALID');
}

async function case_unknown_session() {
  const svc = new SignInService({ chainId: CHAIN });
  let caught = null;
  try { svc.session('deadbeef'); } catch (e) { caught = e; }
  record('unknown session token is rejected', caught?.code === 'NO_SESSION', caught?.code ?? 'ACCEPTED');
}

async function case_session_isolation() {
  // Two wallets sign in; each token must resolve to its own address.
  const svc = new SignInService({ chainId: CHAIN });
  const a = wallet(0x11);
  const b = wallet(0x22);
  const ca = svc.challenge(a.address);
  const cb = svc.challenge(b.address);
  const oa = await svc.verify({ message: ca.message, signature: await sign(a, ca.message) });
  const ob = await svc.verify({ message: cb.message, signature: await sign(b, cb.message) });
  const sa = svc.session(oa.token).address;
  const sb = svc.session(ob.token).address;
  record('concurrent sessions stay bound to their own wallet',
    sa.toLowerCase() === a.address.toLowerCase()
    && sb.toLowerCase() === b.address.toLowerCase(), `${sa} / ${sb}`);
}

async function case_dead_nonce_after_signature_failure() {
  // A signature-level failure must burn the nonce so the same message cannot be
  // retried with a better signature. (A *shape*-level rejection — missing field,
  // non-string signature — is refused before the nonce is consumed, which is correct:
  // it never proved possession of the nonce.)
  const { w, svc, ch } = fresh();
  await expectReject(
    () => svc.verify({ message: ch.message, signature: '0x' + '00'.repeat(65) }), 'BAD_SIGNATURE');
  const sigNow = await sign(w, ch.message);
  const e = await expectReject(
    () => svc.verify({ message: ch.message, signature: sigNow }), 'NONCE_REPLAY');
  record('a bad-signature attempt burns the nonce (no retry window)', true, e.code);

  // And the converse, stated explicitly so the boundary is documented:
  const shape = await expectReject(
    () => svc.verify({ message: ch.message, signature: 12345 }), 'MALFORMED_REQUEST');
  record('a malformed request shape does NOT consume the nonce', true, shape.code);
}

// ============================================================ 11. vulnerable impls must FAIL

async function case_detect_vulnerable_replay() {
  const vuln = new ReplayableAuth({ chainId: CHAIN });
  const w = wallet(0x11);
  const ch = vuln.challenge(w.address);
  const sig = await sign(w, ch.message);
  await vuln.verify({ message: ch.message, signature: sig });
  let replayed = false;
  try { await vuln.verify({ message: ch.message, signature: sig }); replayed = true; } catch { /* fixed impl */ }
  record('VULN #1 ReplayableAuth: replay is DETECTED (implementation is exploitable)',
    replayed, replayed ? 'second verify succeeded — hole confirmed' : 'NOT exploitable — test is wrong');
}

async function case_detect_vulnerable_race() {
  const vuln = new RaceableAuth({ chainId: CHAIN });
  const w = wallet(0x11);
  const ch = vuln.challenge(w.address);
  const sig = await sign(w, ch.message);
  const settled = await Promise.allSettled([
    vuln.verify({ message: ch.message, signature: sig }),
    vuln.verify({ message: ch.message, signature: sig }),
  ]);
  const ok = settled.filter((s) => s.status === 'fulfilled').length;
  record('VULN #2 RaceableAuth: double-spend race is DETECTED (both verifies win)',
    ok === 2, `fulfilled=${ok}/2`);
}

// ============================================================ 12. suite self-consistency

async function case_parser_roundtrip() {
  const w = wallet(0x77);
  const msg = buildSiwe({
    domain: 'dapp.local', address: w.address, statement: 'Line one.\nLine two.',
    uri: ORIGIN, chainId: CHAIN, nonce: 'cafebabecafebabe',
    issuedAt: iso(Date.now()), expirationTime: iso(Date.now() + 600_000),
    resources: ['https://dapp.local/tos', 'https://dapp.local/privacy'],
  });
  const p = parseSiwe(msg);
  const ok = p.address.toLowerCase() === w.address.toLowerCase()
    && p.nonce === 'cafebabecafebabe' && p.chainId === CHAIN
    && p.statement === 'Line one.\nLine two.'
    && p.resources.length === 2;
  record('parser round-trips a message with statement + resources', ok,
    ok ? 'all fields match' : JSON.stringify(p));
}

// ============================================================ run

async function main() {
  const t0 = Date.now();
  console.log('='.repeat(74));
  console.log('escrow 3 — adversarial wallet sign-in regression suite');
  console.log('='.repeat(74));
  console.log('service origin:', ORIGIN, '| chain:', CHAIN, '| wallets: generated in-process\n');

  const cases = [
    case_happy,
    case_replay_sequential,
    case_replay_after_failed_signature,
    case_concurrent_one_wins,
    case_concurrent_many,
    case_wrong_signer,
    case_address_swapped_in_message,
    case_uri_substituted,
    case_origin_lookalike,
    case_origin_header_mismatch,
    case_challenge_wrong_origin,
    case_chain_mismatch,
    case_challenge_wrong_chain,
    case_expiry_exact_boundary,
    case_expiry_one_ms_over,
    case_message_expiry_past,
    case_issued_in_future,
    case_malformed_matrix,
    case_bad_signature_encoding,
    case_malformed_request_shape,
    case_nonce_bound_to_other_address,
    case_unknown_nonce,
    case_nonce_uniqueness,
    case_session_expiry,
    case_unknown_session,
    case_session_isolation,
    case_dead_nonce_after_signature_failure,
    case_detect_vulnerable_replay,
    case_detect_vulnerable_race,
    case_parser_roundtrip,
  ];

  for (const fn of cases) {
    try {
      await fn();
    } catch (e) {
      record(`${fn.name} (threw)`, false, e.message);
    }
  }

  let pass = 0;
  for (const r of results) {
    const flag = r.ok ? '✅' : '❌';
    console.log(`${flag} ${String(r.id).padStart(2)}. ${r.name}`);
    if (!r.ok || process.env.VERBOSE) console.log(`     ${r.detail}`);
    if (r.ok) pass++;
  }

  const dur = Date.now() - t0;
  const verdict = pass === results.length ? 'PASS' : 'FAIL';
  console.log('\n' + '='.repeat(74));
  console.log(`verdict: ${verdict}  (${pass}/${results.length} cases, ${dur} ms)`);
  console.log('='.repeat(74));

  const report = {
    suite: 'imdworks escrow 3 — adversarial wallet sign-in regression suite',
    origin: ORIGIN, chainId: CHAIN,
    total: results.length, passed: pass,
    durationMs: dur, verdict,
    cases: results,
  };
  const fs = await import('node:fs');
  fs.writeFileSync(new URL('./report.json', import.meta.url), JSON.stringify(report, null, 2));
  console.log('report: report.json');
  process.exit(verdict === 'PASS' ? 0 : 1);
}

main();
