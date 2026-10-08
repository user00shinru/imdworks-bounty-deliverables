/**
 * EIP-4361 (SIWE) message parsing + construction, strict enough for a regression suite.
 *
 * The parser is deliberately unforgiving: a sign-in service that accepts a malformed
 * message is one of the failure modes this suite is built to catch, so `parseSiwe`
 * rejects anything ambiguous instead of guessing.
 */

const ADDRESS_RE = /^0x[0-9a-fA-F]{40}$/;

export class SiweError extends Error {
  constructor(msg, field) {
    super(msg);
    this.name = 'SiweError';
    this.field = field;
  }
}

/** Split `key: value` lines, rejecting duplicates of a single-valued field. */
function parseKeyValues(lines, singleKeys) {
  const out = {};
  const seen = new Set();
  for (const line of lines) {
    const m = line.match(/^([A-Za-z ]+):\s?(.*)$/);
    if (!m) continue;
    const key = m[1].trim();
    if (!singleKeys.includes(key)) continue;
    if (seen.has(key)) throw new SiweError(`duplicate field: ${key}`, key);
    seen.add(key);
    out[key] = m[2];
  }
  return out;
}

const SINGLE = ['URI', 'Version', 'Chain ID', 'Nonce', 'Issued At', 'Expiration Time', 'Not Before', 'Request ID'];

/**
 * Parse an EIP-4361 message.
 * @returns {{scheme,domain,address,statement,uri,version,chainId,nonce,issuedAt,expirationTime,notBefore,requestId,resources}}
 * @throws {SiweError} on any structural problem
 */
export function parseSiwe(message) {
  if (typeof message !== 'string' || message.length === 0) {
    throw new SiweError('empty message', 'message');
  }
  // Reject control characters other than newline/tab early: a message that smuggles
  // CR/LF/NUL is either malformed or an injection attempt.
  if (/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(message)) {
    throw new SiweError('control character in message', 'message');
  }

  const lines = message.split('\n');
  const header = lines[0] || '';

  // `${scheme}://${domain} wants you to sign in with your Ethereum account:`
  const hm = header.match(/^(?:([A-Za-z][A-Za-z0-9+.-]*):\/\/)?([^\s]+) wants you to sign in with your Ethereum account:$/);
  if (!hm) throw new SiweError('bad header line', 'header');
  const scheme = hm[1] || null;
  const domain = hm[2];

  const address = (lines[1] || '').trim();
  if (!ADDRESS_RE.test(address)) throw new SiweError('invalid address line', 'address');

  // EIP-4361: line 2 must be blank, then an optional statement, then a blank line,
  // then the key/value block starting at `URI:`.
  if (lines[2] !== '') {
    throw new SiweError('missing blank line after address', 'statement');
  }
  let idx = 3;
  let statement = null;
  const uriIdx = lines.findIndex((l, i) => i >= 3 && /^URI:/.test(l));
  if (uriIdx === -1) throw new SiweError('missing URI field', 'uri');
  const stmtLines = lines.slice(3, uriIdx);
  if (stmtLines.length) {
    // The statement must end with a blank line before `URI:`.
    if (stmtLines[stmtLines.length - 1] !== '') {
      throw new SiweError('statement not separated by a blank line', 'statement');
    }
    const body = stmtLines.slice(0, -1).join('\n');
    statement = body.length ? body : null;
  }
  idx = uriIdx;

  // Resources section: everything after a lone `Resources:` line, as `- item`.
  let resources = [];
  const resIdx = lines.findIndex((l) => l === 'Resources:');
  let kvLines = lines.slice(idx);
  if (resIdx !== -1) {
    kvLines = lines.slice(idx, resIdx);
    resources = lines.slice(resIdx + 1)
      .filter((l) => l.startsWith('- '))
      .map((l) => l.slice(2));
    // Anything after Resources: that is neither `- ` nor empty is malformed.
    const stray = lines.slice(resIdx + 1).find((l) => l.length && !l.startsWith('- '));
    if (stray) throw new SiweError('malformed resource line', 'resources');
  }

  const kv = parseKeyValues(kvLines, SINGLE);
  for (const required of ['URI', 'Version', 'Chain ID', 'Nonce', 'Issued At']) {
    if (!kv[required]) throw new SiweError(`missing ${required}`, required);
  }
  const chainId = Number(kv['Chain ID']);
  if (!Number.isInteger(chainId)) throw new SiweError('non-integer Chain ID', 'Chain ID');
  if (kv['Version'] !== '1') throw new SiweError('unsupported Version', 'Version');
  if (!kv['Nonce']) throw new SiweError('empty Nonce', 'Nonce');
  if (!/^[A-Za-z0-9]{8,}$/.test(kv['Nonce'])) {
    throw new SiweError('Nonce must be at least 8 alphanumeric characters', 'Nonce');
  }
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$/.test(kv['Issued At'])) {
    throw new SiweError('bad Issued At', 'Issued At');
  }
  for (const f of ['Expiration Time', 'Not Before']) {
    if (kv[f] && !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$/.test(kv[f])) {
      throw new SiweError(`bad ${f}`, f);
    }
  }

  return {
    scheme,
    domain,
    address,
    statement,
    uri: kv['URI'],
    version: kv['Version'],
    chainId,
    nonce: kv['Nonce'],
    issuedAt: kv['Issued At'],
    expirationTime: kv['Expiration Time'] || null,
    notBefore: kv['Not Before'] || null,
    requestId: kv['Request ID'] || null,
    resources,
  };
}

/** Build an EIP-4361 message from fields. Used by the fixture generator. */
export function buildSiwe(f) {
  const {
    domain, address, statement = null,
    uri = `https://${domain}`,
    version = '1', chainId = 4663, nonce,
    issuedAt, expirationTime = null, notBefore = null, requestId = null, resources = [],
    scheme = 'https',
  } = f;
  const head = `${scheme}://${domain} wants you to sign in with your Ethereum account:`;
  const parts = [head, address];
  if (statement !== null) parts.push('', statement);
  parts.push('', `URI: ${uri}`, `Version: ${version}`, `Chain ID: ${chainId}`, `Nonce: ${nonce}`, `Issued At: ${issuedAt}`);
  if (expirationTime) parts.push(`Expiration Time: ${expirationTime}`);
  if (notBefore) parts.push(`Not Before: ${notBefore}`);
  if (requestId) parts.push(`Request ID: ${requestId}`);
  if (resources.length) parts.push('Resources:', ...resources.map((r) => `- ${r}`));
  return parts.join('\n');
}

/** Normalise an address for comparison; throws on anything that is not 20 hex bytes. */
export function normAddress(a) {
  if (typeof a !== 'string' || !ADDRESS_RE.test(a)) throw new SiweError('invalid address', 'address');
  return a.toLowerCase();
}

/** ISO-8601 UTC seconds, the only format EIP-4361 allows. */
export function iso(ms) {
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z');
}
