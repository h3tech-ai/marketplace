#!/usr/bin/env python3
# Copyright 2024-2026 H3Tech Inc. All rights reserved. PROPRIETARY.
"""Pure-Python Ed25519 sign/verify (RFC 8032).

synaptory runs on every user's machine with no pip install. The `cryptography`
library is preferred when present (faster C-accelerated verify), but this
module guarantees Ed25519 works with a stdlib-only fallback so users never hit
a missing-dependency wall.

The implementation below is the RFC 8032 reference, trimmed to what synaptory
uses (sign + verify; no batch, no prehash). It's slow (~50ms per verify) but
only runs once per session — imperceptible.

Public API (matches the shape we use from `cryptography`):
    load_pem_public_key(pem_bytes) -> PublicKey
    load_pem_private_key(pem_bytes) -> PrivateKey
    generate_private_key() -> PrivateKey
    PublicKey.verify(sig: bytes, data: bytes) -> None    # raises on fail
    PrivateKey.sign(data: bytes) -> bytes
    PrivateKey.public_key() -> PublicKey
    PrivateKey.private_bytes_raw() -> bytes (32 bytes)
    PublicKey.public_bytes_raw() -> bytes (32 bytes)
    PrivateKey.private_pem() -> bytes (PKCS8)
    PublicKey.public_pem() -> bytes (SubjectPublicKeyInfo)
"""

from __future__ import annotations

import base64
import hashlib
import os

# --- Curve parameters (Ed25519) ---
#
# Affine-coordinate (x, y) RFC 8032 reference implementation. Slower than
# extended coordinates but simpler, with known-correct sign/verify roundtrip
# across all Python 3.x versions. Runs once per session; speed is irrelevant.
_b = 256
_q = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493

_d = -121665 * pow(121666, _q - 2, _q) % _q
_I = pow(2, (_q - 1) // 4, _q)


def _H(m: bytes) -> bytes:
    return hashlib.sha512(m).digest()


def _inv(x: int) -> int:
    return pow(x, _q - 2, _q)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * _inv(_d * y * y + 1)
    x = pow(xx, (_q + 3) // 8, _q)
    if (x * x - xx) % _q != 0:
        x = (x * _I) % _q
    if x % 2 != 0:
        x = _q - x
    return x


_By = 4 * _inv(5)
_Bx = _xrecover(_By)
_B = [_Bx % _q, _By % _q]


def _edwards(P, Q):
    x1, y1 = P
    x2, y2 = Q
    x3 = (x1 * y2 + x2 * y1) * _inv(1 + _d * x1 * x2 * y1 * y2)
    y3 = (y1 * y2 + x1 * x2) * _inv(1 - _d * x1 * x2 * y1 * y2)
    return [x3 % _q, y3 % _q]


def _scalarmult(P, e: int):
    if e == 0:
        return [0, 1]
    Q = _scalarmult(P, e // 2)
    Q = _edwards(Q, Q)
    if e & 1:
        Q = _edwards(Q, P)
    return Q


def _encodeint(y: int) -> bytes:
    bits = [(y >> i) & 1 for i in range(_b)]
    return bytes(
        sum(bits[i * 8 + j] << j for j in range(8)) for i in range(_b // 8)
    )


def _encodepoint(P) -> bytes:
    x, y = P
    bits = [(y >> i) & 1 for i in range(_b - 1)] + [x & 1]
    return bytes(
        sum(bits[i * 8 + j] << j for j in range(8)) for i in range(_b // 8)
    )


def _bit(h: bytes, i: int) -> int:
    return (h[i // 8] >> (i % 8)) & 1


def _publickey(sk: bytes) -> bytes:
    h = _H(sk)
    a = 2**(_b - 2) + sum(2**i * _bit(h, i) for i in range(3, _b - 2))
    A = _scalarmult(_B, a)
    return _encodepoint(A)


def _Hint(m: bytes) -> int:
    h = _H(m)
    return sum(2**i * _bit(h, i) for i in range(2 * _b))


def _sign(sk_seed: bytes, pk: bytes, m: bytes) -> bytes:
    h = _H(sk_seed)
    a = 2**(_b - 2) + sum(2**i * _bit(h, i) for i in range(3, _b - 2))
    r = _Hint(h[_b // 8 : _b // 4] + m)
    R = _scalarmult(_B, r)
    S = (r + _Hint(_encodepoint(R) + pk + m) * a) % _L
    return _encodepoint(R) + _encodeint(S)


def _isoncurve(P) -> bool:
    x, y = P
    return (-x * x + y * y - 1 - _d * x * x * y * y) % _q == 0


def _decodeint(s: bytes) -> int:
    return sum(2**i * _bit(s, i) for i in range(0, _b))


def _decodepoint(s: bytes):
    y = sum(2**i * _bit(s, i) for i in range(0, _b - 1))
    x = _xrecover(y)
    if x & 1 != _bit(s, _b - 1):
        x = _q - x
    P = [x, y]
    if not _isoncurve(P):
        raise ValueError("decoding point that is not on curve")
    return P


def _verify(pk: bytes, m: bytes, s: bytes) -> None:
    if len(s) != _b // 4:
        raise ValueError("signature length is wrong")
    if len(pk) != _b // 8:
        raise ValueError("public-key length is wrong")
    R = _decodepoint(s[: _b // 8])
    A = _decodepoint(pk)
    S = _decodeint(s[_b // 8 : _b // 4])
    h = _Hint(_encodepoint(R) + pk + m)
    if _scalarmult(_B, S) != _edwards(R, _scalarmult(A, h)):
        raise ValueError("Ed25519 signature verification failed")


# --- PEM encoding/decoding ---
#
# Ed25519 PEMs are tiny and structured. Rather than pull in pyasn1, we handle
# the two specific formats `cryptography` emits (PKCS8 private, SPKI public)
# with hand-rolled DER parsing. Layout is stable per RFC 8410.

_PKCS8_PREFIX = bytes.fromhex("302e020100300506032b657004220420")  # 16 bytes
_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")  # 12 bytes


def _strip_pem(pem: bytes, label: str) -> bytes:
    text = pem.decode("ascii", errors="replace").strip()
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("-----")]
    return base64.b64decode("".join(lines))


class PublicKey:
    __slots__ = ("_raw",)

    def __init__(self, raw: bytes):
        if len(raw) != 32:
            raise ValueError("Ed25519 public key must be 32 bytes")
        self._raw = raw

    def verify(self, signature: bytes, data: bytes) -> None:
        _verify(self._raw, data, signature)

    def public_bytes_raw(self) -> bytes:
        return self._raw

    def public_pem(self) -> bytes:
        der = _SPKI_PREFIX + self._raw
        b64 = base64.encodebytes(der).decode("ascii").replace("\n", "")
        lines = [b64[i : i + 64] for i in range(0, len(b64), 64)]
        return (
            b"-----BEGIN PUBLIC KEY-----\n"
            + "\n".join(lines).encode("ascii")
            + b"\n-----END PUBLIC KEY-----\n"
        )


class PrivateKey:
    __slots__ = ("_seed", "_public")

    def __init__(self, seed: bytes):
        if len(seed) != 32:
            raise ValueError("Ed25519 private seed must be 32 bytes")
        self._seed = seed
        self._public = PublicKey(_publickey(seed))

    def sign(self, data: bytes) -> bytes:
        return _sign(self._seed, self._public._raw, data)

    def public_key(self) -> PublicKey:
        return self._public

    def private_bytes_raw(self) -> bytes:
        return self._seed

    def private_pem(self) -> bytes:
        der = _PKCS8_PREFIX + self._seed
        b64 = base64.encodebytes(der).decode("ascii").replace("\n", "")
        lines = [b64[i : i + 64] for i in range(0, len(b64), 64)]
        return (
            b"-----BEGIN PRIVATE KEY-----\n"
            + "\n".join(lines).encode("ascii")
            + b"\n-----END PRIVATE KEY-----\n"
        )


def load_pem_public_key(pem: bytes) -> PublicKey:
    der = _strip_pem(pem, "PUBLIC KEY")
    if not der.startswith(_SPKI_PREFIX) or len(der) != len(_SPKI_PREFIX) + 32:
        raise ValueError("not an Ed25519 SubjectPublicKeyInfo")
    return PublicKey(der[len(_SPKI_PREFIX) :])


def load_pem_private_key(pem: bytes) -> PrivateKey:
    der = _strip_pem(pem, "PRIVATE KEY")
    if not der.startswith(_PKCS8_PREFIX) or len(der) != len(_PKCS8_PREFIX) + 32:
        raise ValueError("not an Ed25519 PKCS8 private key")
    return PrivateKey(der[len(_PKCS8_PREFIX) :])


def generate_private_key() -> PrivateKey:
    return PrivateKey(os.urandom(32))


# --- Preferred-backend resolver ---
#
# Use `cryptography` when available (faster C verify), fall back to pure-Python.
# Both expose the same small surface we need: .verify(sig, data) / .sign(data).

def _try_cryptography_public(pem: bytes):
    try:
        from cryptography.hazmat.primitives import serialization
        return serialization.load_pem_public_key(pem)
    except ImportError:
        return None


def _try_cryptography_private(pem: bytes):
    try:
        from cryptography.hazmat.primitives import serialization
        return serialization.load_pem_private_key(pem, password=None)
    except ImportError:
        return None


def load_public_key_preferred(pem: bytes):
    """Return a verify-capable public key, preferring `cryptography` if installed."""
    k = _try_cryptography_public(pem)
    return k if k is not None else load_pem_public_key(pem)


def load_private_key_preferred(pem: bytes):
    """Return a sign-capable private key, preferring `cryptography` if installed."""
    k = _try_cryptography_private(pem)
    return k if k is not None else load_pem_private_key(pem)
