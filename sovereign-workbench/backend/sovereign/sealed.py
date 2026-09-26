"""Encryption to a device key, for exported instrument slices (spec §14).

A slice is encrypted with a random data key; the data key is wrapped to the
device: X25519 agreement between an ephemeral key and the device's Ed25519 key
(converted to its Montgomery form, RFC 7748 §4.1 / the standard birational map),
HKDF-SHA256 to derive the wrapping key, ChaCha20-Poly1305 (RFC 8439) for both
layers. Revoking the lease makes the client wipe the wrapped key; without it the
slice is ciphertext.

Standard library only, like `signing.py`, because the client vendors it. Pure
Python: tens of MB/s would need C; a slice of a few documents' text is well
under that. If the `cryptography` package is present it is used for the
symmetric layer. The tests check both against the RFCs' own vectors.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import struct

_P = 2 ** 255 - 19
_A24 = 121665


# ------------------------------------------------------------------ X25519

def _clamp(k: bytes) -> int:
    b = bytearray(k)
    b[0] &= 248
    b[31] &= 127
    b[31] |= 64
    return int.from_bytes(b, "little")


def x25519(k: bytes, u: bytes) -> bytes:
    """RFC 7748 scalar multiplication (Montgomery ladder, constant structure)."""
    scalar = _clamp(k)
    x1 = int.from_bytes(u, "little") & ((1 << 255) - 1)
    x2, z2, x3, z3, swap = 1, 0, x1, 1, 0
    for t in reversed(range(255)):
        bit = (scalar >> t) & 1
        swap ^= bit
        if swap:
            x2, x3, z2, z3 = x3, x2, z3, z2
        swap = bit
        a, b = (x2 + z2) % _P, (x2 - z2) % _P
        aa, bb = a * a % _P, b * b % _P
        e = (aa - bb) % _P
        c, d = (x3 + z3) % _P, (x3 - z3) % _P
        da, cb = d * a % _P, c * b % _P
        x3 = (da + cb) ** 2 % _P
        z3 = x1 * (da - cb) ** 2 % _P
        x2 = aa * bb % _P
        z2 = e * (aa + _A24 * e) % _P
    if swap:
        x2, z2 = x3, z3
    return (x2 * pow(z2, _P - 2, _P) % _P).to_bytes(32, "little")


def x25519_base(k: bytes) -> bytes:
    return x25519(k, (9).to_bytes(32, "little"))


def ed25519_pub_to_x25519(pub: bytes) -> bytes:
    """Edwards y -> Montgomery u = (1 + y) / (1 - y)."""
    y = int.from_bytes(pub, "little") & ((1 << 255) - 1)
    u = (1 + y) * pow((1 - y) % _P, _P - 2, _P) % _P
    return u.to_bytes(32, "little")


def ed25519_seed_to_x25519(seed: bytes) -> bytes:
    """The X25519 private scalar of an Ed25519 key: SHA-512(seed)[:32]."""
    return hashlib.sha512(seed).digest()[:32]


# ----------------------------------------------------------- ChaCha20-Poly1305

def _rotl(v: int, c: int) -> int:
    return ((v << c) & 0xFFFFFFFF) | (v >> (32 - c))


def _block(key: bytes, counter: int, nonce: bytes) -> bytes:
    s = [0x61707865, 0x3320646E, 0x79622D32, 0x6B206574,
         *struct.unpack("<8I", key), counter, *struct.unpack("<3I", nonce)]
    w = list(s)

    def qr(a: int, b: int, c: int, d: int) -> None:
        w[a] = (w[a] + w[b]) & 0xFFFFFFFF; w[d] = _rotl(w[d] ^ w[a], 16)
        w[c] = (w[c] + w[d]) & 0xFFFFFFFF; w[b] = _rotl(w[b] ^ w[c], 12)
        w[a] = (w[a] + w[b]) & 0xFFFFFFFF; w[d] = _rotl(w[d] ^ w[a], 8)
        w[c] = (w[c] + w[d]) & 0xFFFFFFFF; w[b] = _rotl(w[b] ^ w[c], 7)
    for _ in range(10):
        qr(0, 4, 8, 12); qr(1, 5, 9, 13); qr(2, 6, 10, 14); qr(3, 7, 11, 15)
        qr(0, 5, 10, 15); qr(1, 6, 11, 12); qr(2, 7, 8, 13); qr(3, 4, 9, 14)
    return struct.pack("<16I", *((w[i] + s[i]) & 0xFFFFFFFF for i in range(16)))


def chacha20(key: bytes, counter: int, nonce: bytes, data: bytes) -> bytes:
    out = bytearray(len(data))
    for i in range(0, len(data), 64):
        ks = _block(key, counter + i // 64, nonce)
        chunk = data[i:i + 64]
        out[i:i + len(chunk)] = bytes(a ^ b for a, b in zip(chunk, ks))
    return bytes(out)


def poly1305(key: bytes, msg: bytes) -> bytes:
    r = int.from_bytes(key[:16], "little") & 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF
    s = int.from_bytes(key[16:], "little")
    p, acc = (1 << 130) - 5, 0
    for i in range(0, len(msg), 16):
        n = int.from_bytes(msg[i:i + 16] + b"\x01", "little")
        acc = (acc + n) * r % p
    return ((acc + s) & ((1 << 128) - 1)).to_bytes(16, "little")


def _pad16(b: bytes) -> bytes:
    return b"\0" * (-len(b) % 16)


def _aead(key: bytes, nonce: bytes, data: bytes, aad: bytes, encrypt: bool) -> bytes:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
        c = ChaCha20Poly1305(key)
        return c.encrypt(nonce, data, aad) if encrypt else c.decrypt(nonce, data, aad)
    except ImportError:
        pass
    otk = _block(key, 0, nonce)[:32]
    if encrypt:
        ct = chacha20(key, 1, nonce, data)
    else:
        ct, tag = data[:-16], data[-16:]
    mac = poly1305(otk, aad + _pad16(aad) + ct + _pad16(ct)
                   + struct.pack("<QQ", len(aad), len(ct)))
    if encrypt:
        return ct + mac
    if not hmac.compare_digest(mac, tag):
        raise ValueError("authentication failed: the ciphertext or its key is wrong")
    return chacha20(key, 1, nonce, ct)


def seal(key: bytes, data: bytes, aad: bytes = b"") -> bytes:
    nonce = os.urandom(12)
    return nonce + _aead(key, nonce, data, aad, True)


def open_(key: bytes, blob: bytes, aad: bytes = b"") -> bytes:
    return _aead(key, blob[:12], blob[12:], aad, False)


def hkdf(ikm: bytes, info: bytes, length: int = 32, salt: bytes = b"") -> bytes:
    prk = hmac.new(salt or b"\0" * 32, ikm, hashlib.sha256).digest()
    out, t, i = b"", b"", 1
    while len(out) < length:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        out += t
        i += 1
    return out[:length]


# --------------------------------------------------------------- wrapping

def wrap_for_device(data_key: bytes, device_ed25519_pub: bytes,
                    context: bytes) -> dict[str, str]:
    """Wrap a data key so only the holder of the device's Ed25519 seed opens it."""
    eph = os.urandom(32)
    eph_pub = x25519_base(eph)
    shared = x25519(eph, ed25519_pub_to_x25519(device_ed25519_pub))
    kek = hkdf(shared, b"clawcal slice key wrap v1|" + context, salt=eph_pub)
    return {"ephemeral": eph_pub.hex(), "wrapped": seal(kek, data_key, context).hex()}


def unwrap_on_device(wrapped: dict[str, str], device_seed: bytes,
                     context: bytes) -> bytes:
    eph_pub = bytes.fromhex(wrapped["ephemeral"])
    shared = x25519(ed25519_seed_to_x25519(device_seed), eph_pub)
    kek = hkdf(shared, b"clawcal slice key wrap v1|" + context, salt=eph_pub)
    return open_(kek, bytes.fromhex(wrapped["wrapped"]), context)
