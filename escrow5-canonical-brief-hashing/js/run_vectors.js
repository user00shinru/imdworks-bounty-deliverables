#!/usr/bin/env node
/**
 * Vector runner for the JavaScript implementation.
 *
 *   node run_vectors.js <vectors.json>      -> JSON {results, failures}
 *   node run_vectors.js --bytes             -> reads {vectors:[{id,input}]} on
 *                                              stdin, returns canonical bytes hex
 *
 * Kept as a thin adapter so the Python runner can drive both languages from a
 * single command.
 */
'use strict';

const fs = require('fs');
const path = require('path');

// js-sha3 is vendored so the repo is self-contained (no npm install needed)
const VENDOR = path.join(__dirname, 'vendor', 'js-sha3.js');
const { keccak256: keccakImpl } = require(fs.existsSync(VENDOR) ? VENDOR : 'js-sha3');

const { CanonicalError, canonicalBytes, tryBriefHash } = require('./brief_canon');

function hex(bytes) {
  return Buffer.from(bytes).toString('hex');
}

/**
 * Inverse of the generator's `enc`: rebuild exact JS types from the tagged
 * transport form.
 *   {"$int":"1000"}  -> 1000n   (BigInt, never a lossy number)
 *   {"$float":1.5}   -> 1.5     (number — used by the 'reward is a float' cases)
 *
 * NOTE (real finding, see FINDINGS.md): a naive `out[k] = v[k]` rebuild DROPS a
 * key literally named `__proto__` — `out['__proto__'] = …` hits the setter on
 * Object.prototype instead of creating an own property. That silently turns a
 * hostile `__proto__` field into a prototype mutation rather than an
 * `unknown_field` rejection. We therefore build a null-prototype object and
 * define keys explicitly, so `__proto__` stays an ordinary own key.
 */
function dec(v) {
  if (Array.isArray(v)) return v.map(dec);
  if (v !== null && typeof v === 'object') {
    const keys = Object.keys(v);
    if (keys.length === 1 && keys[0] === '$int') return BigInt(v.$int);
    if (keys.length === 1 && keys[0] === '$float') return v.$float;
    const out = Object.create(null);
    for (const k of keys) {
      Object.defineProperty(out, k, {
        value: dec(v[k]), enumerable: true, writable: true, configurable: true,
      });
    }
    return out;
  }
  return v;
}

function countResult() {
  try {
    return { hash: '0x' + keccakImpl(canonicalBytes(countResult.brief)), kind: '' };
  } catch (e) {
    return { hash: '', kind: e.kind || 'internal' };
  }
}

function handleVectors(file) {
  const doc = JSON.parse(fs.readFileSync(file, 'utf8'));
  const results = [];
  const failures = [];
  for (const v of doc.vectors) {
    const { hash, kind } = tryBriefHash(dec(v.input));
    const got = hash || kind;
    const ok = got === v.expect;
    results.push({ id: v.id, got, ok });
    if (!ok) failures.push({ id: v.id, note: v.note, expect: v.expect, got });
  }
  process.stdout.write(JSON.stringify({ results, failures }));
}

function handleBytes() {
  let raw = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', (c) => { raw += c; });
  process.stdin.on('end', () => {
    const payload = JSON.parse(raw);
    const results = [];
    for (const v of payload.vectors) {
      results.push({ id: v.id, bytes: hex(canonicalBytes(dec(v.input))) });
    }
    process.stdout.write(JSON.stringify({ results }));
  });
}

function main() {
  const arg = process.argv[2];
  if (arg === '--bytes') return handleBytes();
  if (!arg) {
    process.stderr.write('usage: run_vectors.js <vectors.json> | --bytes\n');
    process.exit(2);
  }
  handleVectors(arg);
}

main();
