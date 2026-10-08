#!/usr/bin/env python3
"""
Canonical brief hashing — Python reference implementation.

Deterministic serialization of a bounty brief:
    {title, description, criteria, reward, token, deadline}

Design goals
------------
* Byte-identical output across Python and JavaScript.
* Reject anything that is not unambiguously canonical, rather than silently
  normalising it (a hash over normalised input would not prove byte equality).
* Never mutate already-committed content: the brief text is taken exactly as
  stored; the only transformations are the ones this spec defines.

Canonical form
--------------
1. Every string field is UTF-8 encoded **as-is**. No Unicode normalisation,
   no whitespace stripping, no line-ending rewriting. NFC and NFD are
   *different* briefs and hash differently — this is deliberate, see SPEC.md.
2. `reward` MUST be a JSON integer (bigint). It is serialised in decimal with
   no sign, no leading zeros (except the single digit `0`), no exponent.
3. `deadline` MUST be. JSON integer, Unix seconds, no fraction.
4. Keys are emitted in a fixed order: title, description, criteria, reward,
   token, deadline. Key order in the input object is irrelevant; the output
   order is fixed.
5. The canonical byte stream is the *concatenation* of RFC-8259-style
   length-prefixed records (see `canonical_bytes`) rather than naive
   `json.dumps`, so that no serializer's escaping choices can leak in.

Only `keccak256(canonical_bytes(brief))` is the brief hash.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple
import re

from Crypto.Hash import keccak

# canonical unsigned decimal: no sign, no leading zeros, no exponent, no fraction
_DECIMAL_RE = re.compile(r"^(0|[1-9][0-9]*)$")

# ---------------------------------------------------------------------------
# error taxonomy
# ---------------------------------------------------------------------------

class CanonicalError(ValueError):
    """Raised when a brief cannot be canonicalised unambiguously."""

    def __init__(self, kind: str, detail: str = ""):
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind}: {detail}" if detail else kind)


ALLOWED_KEYS = ("title", "description", "criteria", "reward", "token", "deadline")

# characters that RFC-8259 forbids in raw strings; we reject rather than escape
_FORBIDDEN = {
    "\x00": "nul_byte",
    "\n": "raw_newline",       # must be present as the two-char sequence \n? no:
    "\r": "raw_carriage_return",
    "\t": "raw_tab",
}


def _check_scalar_string(field: str, value: Any) -> None:
    if not isinstance(value, str):
        raise CanonicalError("type", f"{field} must be a string, got {type(value).__name__}")


def _check_no_forbidden(field: str, value: str) -> None:
    """Control characters make byte equality ambiguous across editors."""
    if "\x00" in value:
        raise CanonicalError("nul_byte", field)
    if "\r" in value:
        raise CanonicalError("cr_character", field)
    # lone surrogates cannot be UTF-8 encoded
    for ch in value:
        if 0xD800 <= ord(ch) <= 0xDFFF:
            raise CanonicalError("lone_surrogate", field)


def _check_utf8(field: str, value: str) -> bytes:
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as e:
        raise CanonicalError("not_utf8", f"{field}: {e}") from e


def _check_nfc_requirement(field: str, value: str) -> None:
    """
    We do NOT normalise. We only *detect* and reject strings that are not
    already in NFC, because two visually identical briefs that hash
    differently are a support nightmare and the brief says 'explicit
    rejection rules' is a requirement.
    """
    import unicodedata

    if unicodedata.normalize("NFC", value) != value:
        raise CanonicalError("not_nfc", field)


# ---------------------------------------------------------------------------
# numeric handling
# ---------------------------------------------------------------------------

def _int_to_decimal(value: Any, field: str) -> str:
    """
    Accept an int, or a *canonical* decimal string (no sign, no leading zeros,
    no exponent). Mirrors the JS implementation exactly — a decimal string is
    permitted because uint256 does not fit a JS number.
    """
    if isinstance(value, bool):
        raise CanonicalError("type", f"{field} must be an integer")
    if isinstance(value, int):
        if value < 0:
            raise CanonicalError("negative", field)
        if value > 2**256 - 1:
            raise CanonicalError("overflow_uint256", field)
        return str(value)
    if isinstance(value, str):
        if not _DECIMAL_RE.match(value):
            raise CanonicalError("not_canonical_decimal", f"{field}={value}")
        if int(value) > 2**256 - 1:
            raise CanonicalError("overflow_uint256", field)
        return value
    raise CanonicalError("type", f"{field} must be an integer")


def _check_token(token: str) -> str:
    _check_scalar_string("token", token)
    if not token.startswith("0x") or len(token) != 42:
        raise CanonicalError("bad_token", token)
    body = token[2:]
    if any(c not in "0123456789abcdefABCDEF" for c in body):
        raise CanonicalError("bad_token", token)
    # canonical spelling: lower-case hex
    if body != body.lower():
        raise CanonicalError("non_canonical_hex", token)
    return token


def _check_deadline(deadline: Any) -> int:
    d = _int_to_decimal(deadline, "deadline")
    return int(d)


# ---------------------------------------------------------------------------
# canonical serialisation
# ---------------------------------------------------------------------------

def _field_bytes(field: str, text: str) -> bytes:
    """One record: <field-name> = <byte-length>:<utf8 bytes>"""
    name = field.encode("ascii")
    body = text.encode("utf-8")
    return name + b" " + str(len(body)).encode("ascii") + b":" + body


def canonical_bytes(brief: Dict[str, Any]) -> bytes:
    """
    Produce the canonical byte stream for `brief`.

    Raises CanonicalError on any input that is not unambiguously canonical.
    """
    if not isinstance(brief, dict):
        raise CanonicalError("type", "brief must be an object")

    # 1. exact key set — missing AND extra fields are both rejected
    keys = set(brief.keys())
    missing = [k for k in ALLOWED_KEYS if k not in keys]
    extra = [k for k in keys if k not in ALLOWED_KEYS]
    if missing:
        raise CanonicalError("missing_field", ",".join(missing))
    if extra:
        raise CanonicalError("unknown_field", ",".join(sorted(extra)))

    out: List[bytes] = []

    for field in ("title", "description", "criteria"):
        v = brief[field]
        _check_scalar_string(field, v)
        _check_no_forbidden(field, v)
        _check_nfc_requirement(field, v)
        if v == "":
            raise CanonicalError("empty_field", field)
        out.append(_field_bytes(field, v))

    reward = _int_to_decimal(brief["reward"], "reward")
    out.append(_field_bytes("reward", reward))

    token = _check_token(brief["token"])
    out.append(_field_bytes("token", token))

    deadline = _int_to_decimal(brief["deadline"], "deadline")
    out.append(_field_bytes("deadline", deadline))

    joined = b"\n".join(out)
    return b"imdworks-brief-v1\n" + joined


def keccak256(data: bytes) -> bytes:
    k = keccak.new(digest_bits=256)
    k.update(data)
    return k.digest()


def brief_hash(brief: Dict[str, Any]) -> str:
    """Return 0x-prefixed lowercase keccak256 of the canonical bytes."""
    return "0x" + keccak256(canonical_bytes(brief)).hex()


def try_brief_hash(brief: Dict[str, Any]) -> Tuple[str, str]:
    """
    Returns (hash_or_empty, rejection_kind_or_empty).
    Exactly one of the two is non-empty.
    """
    try:
        return brief_hash(brief), ""
    except CanonicalError as e:
        return "", e.kind
