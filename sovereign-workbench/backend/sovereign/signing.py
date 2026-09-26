"""Ed25519 signatures for artefacts the appliance vouches for.

The sovereignty report and the audit export are filed by a security officer and
verified later, offline, by someone who does not trust the appliance. That
needs an asymmetric signature: the public key is printed in the report, the
private key never leaves `SOVEREIGN_DATA_DIR/keys`.

The implementation is the RFC 8032 reference algorithm in pure Python, so the
appliance and an offline verifier need no third-party package. It is slow
(tens of milliseconds per operation) and signs a few documents a day. If the
`cryptography` package is installed it is used instead, and the tests check
both paths against the RFC's own test vectors.

Not constant-time. The key is used only to sign documents the appliance itself
produced, on the appliance; there is no remote party timing it.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

_p = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_d = -121665 * pow(121666, _p - 2, _p) % _p
_I = pow(2, (_p - 1) // 4, _p)


def _sha512(b: bytes) -> bytes:
    return hashlib.sha512(b).digest()


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(_d * y * y + 1, _p - 2, _p)
    x = pow(xx, (_p + 3) // 8, _p)
    if (x * x - xx) % _p != 0:
        x = (x * _I) % _p
    if x % 2 != 0:
        x = _p - x
    return x


_By = 4 * pow(5, _p - 2, _p) % _p
_Bx = _xrecover(_By)
_B = (_Bx, _By, 1, _Bx * _By % _p)          # extended coordinates


def _add(P, Q):
    x1, y1, z1, t1 = P
    x2, y2, z2, t2 = Q
    a = (y1 - x1) * (y2 - x2) % _p
    b = (y1 + x1) * (y2 + x2) % _p
    c = t1 * 2 * _d * t2 % _p
    dd = z1 * 2 * z2 % _p
    e, f, g, h = b - a, dd - c, dd + c, b + a
    return (e * f % _p, g * h % _p, f * g % _p, e * h % _p)


def _mul(s: int, P):
    Q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _encode(P) -> bytes:
    x, y, z, _ = P
    zi = pow(z, _p - 2, _p)
    x, y = x * zi % _p, y * zi % _p
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decode(s: bytes):
    if len(s) != 32:
        raise ValueError("bad point length")
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= _p:
        raise ValueError("point not on curve")
    x = _xrecover(y)
    if (x & 1) != sign:
        x = _p - x
    P = (x, y, 1, x * y % _p)
    # on-curve check: -x^2 + y^2 = 1 + d x^2 y^2
    if (-x * x + y * y - 1 - _d * x * x * y * y) % _p != 0:
        raise ValueError("point not on curve")
    return P


def _eq(P, Q) -> bool:
    x1, y1, z1, _ = P
    x2, y2, z2, _ = Q
    return (x1 * z2 - x2 * z1) % _p == 0 and (y1 * z2 - y2 * z1) % _p == 0


def _secret_expand(seed: bytes) -> tuple[int, bytes]:
    h = _sha512(seed)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(seed: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        k = Ed25519PrivateKey.from_private_bytes(seed)
        return k.public_key().public_bytes(serialization.Encoding.Raw,
                                           serialization.PublicFormat.Raw)
    except ImportError:
        pass
    a, _ = _secret_expand(seed)
    return _encode(_mul(a, _B))


def sign(seed: bytes, msg: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        return Ed25519PrivateKey.from_private_bytes(seed).sign(msg)
    except ImportError:
        pass
    a, prefix = _secret_expand(seed)
    A = _encode(_mul(a, _B))
    r = int.from_bytes(_sha512(prefix + msg), "little") % _L
    R = _encode(_mul(r, _B))
    h = int.from_bytes(_sha512(R + A + msg), "little") % _L
    s = (r + h * a) % _L
    return R + s.to_bytes(32, "little")


def verify(pub: bytes, msg: bytes, sig: bytes) -> bool:
    if len(pub) != 32 or len(sig) != 64:
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature
        try:
            Ed25519PublicKey.from_public_bytes(pub).verify(sig, msg)
            return True
        except InvalidSignature:
            return False
    except ImportError:
        pass
    try:
        A = _decode(pub)
        R = _decode(sig[:32])
    except ValueError:
        return False
    s = int.from_bytes(sig[32:], "little")
    if s >= _L:
        return False
    h = int.from_bytes(_sha512(sig[:32] + pub + msg), "little") % _L
    return _eq(_mul(s, _B), _add(R, _mul(h, A)))


def canonical(obj: Any) -> bytes:
    """The one byte encoding everything signed in the trust domain uses.

    The node and every client sign and verify the same bytes, so this function
    is shared rather than re-implemented: a client whose JSON differed by one
    space would see every node signature fail.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str).encode("utf-8")


def key_id(pub: bytes) -> str:
    """Stable identifier for a public key: hex SHA-256 of its raw bytes."""
    return hashlib.sha256(pub).hexdigest()


def fingerprint(pub: bytes) -> str:
    """Short, human-comparable identity of a public key."""
    d = hashlib.sha256(pub).hexdigest()[:24]
    return ":".join(d[i:i + 4] for i in range(0, 24, 4))


# ------------------------------------------------------------ appliance key

def appliance_key(keys_dir: Path) -> tuple[bytes, bytes]:
    """(seed, public key) for this appliance, created on first use, mode 0600."""
    keys_dir.mkdir(parents=True, exist_ok=True)
    path = keys_dir / "appliance.ed25519"
    if not path.exists():
        seed = os.urandom(32)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(seed)
        (keys_dir / "appliance.ed25519.pub").write_text(public_key(seed).hex() + "\n")
    seed = path.read_bytes()
    if len(seed) != 32:
        raise ValueError(f"{path} is not a 32-byte Ed25519 seed")
    return seed, public_key(seed)
