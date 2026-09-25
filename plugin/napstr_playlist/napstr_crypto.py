# SPDX-FileCopyrightText: 2026 StateAntigen
# SPDX-License-Identifier: 0BSD
"""Dependency-free secp256k1 Schnorr (BIP-340) and bech32 helpers.

Nicotine+ ships a frozen Python runtime with no third-party packages, so the
Nostr signing primitives live here rather than in a library such as
``coincurve``/``pynostr``. Only the standard library is imported, which also
means this module can be unit tested outside of Nicotine+.

Everything here follows BIP-340 (x-only public keys, 64 byte Schnorr
signatures) and BIP-173 (bech32, used for ``npub``/``nsec``).
"""

import hashlib
import secrets

__all__ = [
    "bech32_decode",
    "bech32_encode",
    "decode_secret_key",
    "encode_npub",
    "encode_nsec",
    "get_public_key",
    "is_valid_public_key",
    "normalize_secret_key",
    "schnorr_sign",
    "schnorr_verify",
    "xor_bytes",
]

# secp256k1 domain parameters
P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
Gx = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
Gy = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8

_G = (Gx, Gy, 1)

# bech32 (BIP-173)
BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_CHARSET_REV = {char: index for index, char in enumerate(BECH32_CHARSET)}
_BECH32_CONST = 1
_BECH32M_CONST = 0x2BC830A3


class CryptoError(ValueError):
    """Raised when key material or signatures are malformed."""


# ---------------------------------------------------------------------------
# Elliptic curve arithmetic (Jacobian coordinates)
# ---------------------------------------------------------------------------

def _jacobian_double(point):
    x1, y1, z1 = point

    if not y1 or not z1:
        return 0, 1, 0

    a = (x1 * x1) % P
    b = (y1 * y1) % P
    c = (b * b) % P
    d = (2 * ((x1 + b) * (x1 + b) - a - c)) % P
    e = (3 * a) % P
    f = (e * e) % P
    x3 = (f - 2 * d) % P
    y3 = (e * (d - x3) - 8 * c) % P
    z3 = (2 * y1 * z1) % P

    return x3, y3, z3


def _jacobian_add(point1, point2):
    x1, y1, z1 = point1
    x2, y2, z2 = point2

    if not z1:
        return point2

    if not z2:
        return point1

    z1z1 = (z1 * z1) % P
    z2z2 = (z2 * z2) % P
    u1 = (x1 * z2z2) % P
    u2 = (x2 * z1z1) % P
    s1 = (y1 * z2 * z2z2) % P
    s2 = (y2 * z1 * z1z1) % P

    if u1 == u2:
        if s1 != s2:
            # P + (-P) = point at infinity
            return 0, 1, 0

        return _jacobian_double(point1)

    h = (u2 - u1) % P
    i = (2 * h) ** 2 % P
    j = (h * i) % P
    r = (2 * (s2 - s1)) % P
    v = (u1 * i) % P
    x3 = (r * r - j - 2 * v) % P
    y3 = (r * (v - x3) - 2 * s1 * j) % P
    z3 = (((z1 + z2) ** 2 - z1z1 - z2z2) * h) % P

    return x3, y3, z3


def _jacobian_multiply(scalar, point):
    result = (0, 1, 0)
    addend = point

    while scalar:
        if scalar & 1:
            result = _jacobian_add(result, addend)

        addend = _jacobian_double(addend)
        scalar >>= 1

    return result


def _jacobian_to_affine(point):
    x, y, z = point

    if not z:
        return None

    z_inverse = pow(z, P - 2, P)
    z_inverse_squared = (z_inverse * z_inverse) % P

    return (x * z_inverse_squared) % P, (y * z_inverse_squared * z_inverse) % P


def _lift_x(x):
    """Return the curve point with the given x coordinate and even y."""

    if x <= 0 or x >= P:
        raise CryptoError("x coordinate out of range")

    y_squared = (pow(x, 3, P) + 7) % P
    y = pow(y_squared, (P + 1) // 4, P)

    if (y * y) % P != y_squared:
        raise CryptoError("x coordinate is not on the curve")

    if y & 1:
        y = P - y

    return x, y


# ---------------------------------------------------------------------------
# BIP-340 Schnorr signatures
# ---------------------------------------------------------------------------

def _tagged_hash(tag, data):
    if isinstance(tag, str):
        tag = tag.encode("ascii")

    tag_hash = hashlib.sha256(tag).digest()

    return hashlib.sha256(tag_hash + tag_hash + data).digest()


def xor_bytes(left, right):
    return bytes(a ^ b for a, b in zip(left, right))


def get_public_key(secret_key):
    """Return the 32 byte x-only public key for a 32 byte secret key."""

    secret_int = int.from_bytes(secret_key, "big")

    if not 1 <= secret_int <= N - 1:
        raise CryptoError("secret key out of range")

    point = _jacobian_to_affine(_jacobian_multiply(secret_int, _G))

    if point is None:
        raise CryptoError("invalid secret key")

    return point[0].to_bytes(32, "big")


def is_valid_public_key(public_key):
    try:
        _lift_x(int.from_bytes(public_key, "big"))

    except (CryptoError, ValueError):
        return False

    return True


def schnorr_sign(message, secret_key, aux_rand=None):
    """Sign a 32 byte message hash, returning a 64 byte BIP-340 signature."""

    if len(message) != 32:
        raise CryptoError("message must be 32 bytes")

    if len(secret_key) != 32:
        raise CryptoError("secret key must be 32 bytes")

    secret_int = int.from_bytes(secret_key, "big")

    if not 1 <= secret_int <= N - 1:
        raise CryptoError("secret key out of range")

    public_point = _jacobian_to_affine(_jacobian_multiply(secret_int, _G))

    if public_point is None:
        raise CryptoError("invalid secret key")

    public_x, public_y = public_point

    # The nonce is derived from the secret key, so negate it when the public
    # key has an odd y coordinate.
    secret = secret_int if not public_y & 1 else N - secret_int

    if aux_rand is None:
        aux_rand = secrets.token_bytes(32)

    elif len(aux_rand) != 32:
        raise CryptoError("auxiliary randomness must be 32 bytes")

    masked = xor_bytes(secret.to_bytes(32, "big"), _tagged_hash(b"BIP0340/aux", aux_rand))
    nonce_hash = _tagged_hash(
        b"BIP0340/nonce", masked + public_x.to_bytes(32, "big") + message)
    nonce = int.from_bytes(nonce_hash, "big") % N

    if nonce == 0:
        raise CryptoError("nonce is zero, retry with different randomness")

    nonce_point = _jacobian_to_affine(_jacobian_multiply(nonce, _G))

    if nonce_point is None:
        raise CryptoError("invalid nonce")

    nonce_x, nonce_y = nonce_point

    if nonce_y & 1:
        nonce = N - nonce

    challenge = int.from_bytes(
        _tagged_hash(
            b"BIP0340/challenge",
            nonce_x.to_bytes(32, "big") + public_x.to_bytes(32, "big") + message),
        "big") % N

    signature = nonce_x.to_bytes(32, "big") + ((nonce + challenge * secret) % N).to_bytes(32, "big")

    if not schnorr_verify(message, public_x.to_bytes(32, "big"), signature):
        raise CryptoError("generated signature failed self verification")

    return signature


def schnorr_verify(message, public_key, signature):
    """Verify a BIP-340 signature. Returns True when the signature is valid."""

    if len(message) != 32 or len(public_key) != 32 or len(signature) != 64:
        return False

    try:
        public_point = _lift_x(int.from_bytes(public_key, "big"))

    except CryptoError:
        return False

    public_x, public_y = public_point
    nonce_x = int.from_bytes(signature[:32], "big")

    if nonce_x >= P:
        return False

    try:
        nonce_point = _lift_x(nonce_x)

    except CryptoError:
        return False

    challenge = int.from_bytes(
        _tagged_hash(b"BIP0340/challenge", signature[:32] + public_key + message), "big") % N
    nonce = int.from_bytes(signature[32:], "big")

    if nonce >= N:
        return False

    # R = s*G - e*P
    left = _jacobian_multiply(nonce, _G)
    right = _jacobian_multiply(N - challenge, (public_point[0], public_point[1], 1))
    candidate = _jacobian_to_affine(_jacobian_add(left, right))

    if candidate is None:
        return False

    candidate_x, candidate_y = candidate

    return candidate_x == nonce_x and not candidate_y & 1


# ---------------------------------------------------------------------------
# bech32 (BIP-173) - used for npub/nsec
# ---------------------------------------------------------------------------

def _bech32_polymod(values):
    generator = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    checksum = 1

    for value in values:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value

        for index in range(5):
            if (top >> index) & 1:
                checksum ^= generator[index]

    return checksum


def _bech32_hrp_expand(hrp):
    return [ord(char) >> 5 for char in hrp] + [0] + [ord(char) & 31 for char in hrp]


def _bech32_verify_checksum(hrp, data):
    check = _bech32_polymod(_bech32_hrp_expand(hrp) + data)

    if check == _BECH32_CONST:
        return "bech32"

    if check == _BECH32M_CONST:
        return "bech32m"

    return None


def bech32_decode(bech):
    """Decode a bech32/bech32m string into ``(hrp, data)`` or ``(None, None)``."""

    if not isinstance(bech, str):
        return None, None

    if any(ord(char) < 33 or ord(char) > 126 for char in bech):
        return None, None

    if bech.lower() != bech and bech.upper() != bech:
        return None, None

    bech = bech.lower()
    position = bech.rfind("1")

    if position < 1 or position + 7 > len(bech) or len(bech) > 90:
        return None, None

    if any(char not in _BECH32_CHARSET_REV for char in bech[position + 1:]):
        return None, None

    hrp = bech[:position]
    data = [_BECH32_CHARSET_REV[char] for char in bech[position + 1:]]

    if not _bech32_verify_checksum(hrp, data):
        return None, None

    return hrp, data[:-6]


def bech32_encode(hrp, data):
    combined = data + _bech32_create_checksum(hrp, data, _BECH32_CONST)
    return hrp + "1" + "".join(BECH32_CHARSET[value] for value in combined)


def _bech32_create_checksum(hrp, data, constant):
    values = _bech32_hrp_expand(hrp) + data
    polymod = _bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ constant
    return [(polymod >> (5 * (5 - index))) & 31 for index in range(6)]


def _convertbits(data, from_bits, to_bits, pad=True):
    accumulator = 0
    bits = 0
    result = []
    max_value = (1 << to_bits) - 1
    max_accumulator = (1 << (from_bits + to_bits - 1)) - 1

    for value in data:
        if value < 0 or value >> from_bits:
            return None

        accumulator = ((accumulator << from_bits) | value) & max_accumulator
        bits += from_bits

        while bits >= to_bits:
            bits -= to_bits
            result.append((accumulator >> bits) & max_value)

    if pad:
        if bits:
            result.append((accumulator << (to_bits - bits)) & max_value)

    elif bits >= from_bits or ((accumulator << (to_bits - bits)) & max_value):
        return None

    return result


def decode_bech32_key(value):
    """Decode an ``npub``/``nsec`` string into raw 32 bytes.

    Returns ``None`` when the value is not a well formed 32 byte bech32 key.
    """

    hrp, data = bech32_decode(value)

    if hrp is None or data is None:
        return None

    decoded = _convertbits(data, 5, 8, False)

    if decoded is None or len(decoded) != 32:
        return None

    return bytes(decoded)


def encode_bech32_key(hrp, key):
    data = _convertbits(list(key), 8, 5)

    if data is None:
        raise CryptoError("could not convert key to bech32")

    return bech32_encode(hrp, data)


def encode_npub(public_key):
    return encode_bech32_key("npub", public_key)


def encode_nsec(secret_key):
    return encode_bech32_key("nsec", secret_key)


def normalize_secret_key(value):
    """Accept a 64 character hex key or an ``nsec`` string.

    Returns 32 raw bytes, or ``None`` when the value cannot be interpreted.
    """

    if not value:
        return None

    value = value.strip()

    if value.lower().startswith("nsec1"):
        return decode_bech32_key(value.lower())

    try:
        raw = bytes.fromhex(value)

    except ValueError:
        return None

    if len(raw) != 32:
        return None

    return raw


def decode_secret_key(value):
    """Like :func:`normalize_secret_key` but raises on invalid input."""

    raw = normalize_secret_key(value)

    if raw is None:
        raise CryptoError("expected an nsec1... key or 64 hex characters")

    secret_int = int.from_bytes(raw, "big")

    if not 1 <= secret_int <= N - 1:
        raise CryptoError("secret key is out of range")

    return raw
