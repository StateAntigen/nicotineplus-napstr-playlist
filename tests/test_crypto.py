# SPDX-FileCopyrightText: 2026 StateAntigen
# SPDX-License-Identifier: 0BSD
"""BIP-340 and bech32 tests for :mod:`napstr_crypto`.

Runnable with any Python 3.8+ interpreter, independent of Nicotine+:

    python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "plugin", "napstr_playlist"))

import napstr_crypto as crypto  # noqa: E402  pylint: disable=wrong-import-position

# Official BIP-340 test vectors (signing vectors 0-2)
BIP340_VECTORS = [
    {
        "secret": "0000000000000000000000000000000000000000000000000000000000000003",
        "public": "F9308A019258C31049344F85F89D5229B531C845836F99B08601F113BCE036F9",
        "aux": "0000000000000000000000000000000000000000000000000000000000000000",
        "message": "0000000000000000000000000000000000000000000000000000000000000000",
        "signature": (
            "E907831F80848D1069A5371B402410364BDF1C5F8307B0084C55F1CE2DCA8215"
            "25F66A4A85EA8B71E482A74F382D2CE5EBEEE8FDB2172F477DF4900D310536C0"
        )
    },
    {
        "secret": "B7E151628AED2A6ABF7158809CF4F3C762E7160F38B4DA56A784D9045190CFEF",
        "public": "DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
        "aux": "0000000000000000000000000000000000000000000000000000000000000001",
        "message": "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
        "signature": (
            "6896BD60EEAE296DB48A229FF71DFE071BDE413E6D43F917DC8DCF8C78DE3341"
            "8906D11AC976ABCCB20B091292BFF4EA897EFCB639EA871CFA95F6DE339E4B0A"
        )
    },
    {
        "secret": "C90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B14E5C9",
        "public": "DD308AFEC5777E13121FA72B9CC1B7CC0139715309B086C960E18FD969774EB8",
        "aux": "C87AA53824B4D7AE2EB035A2B5BBBCCC080E76CDC6D1692C4B0B62D798E6D906",
        "message": "7E2D58D8B3BCDF1ABADEC7829054F90DDA9805AAB56C77333024B9D0A508B75C",
        "signature": (
            "5831AAEED7B44BB74E5EAB94BA9D4294C49BCF2A60728D8B4C200F50DD313C1B"
            "AB745879A5AD954A72C45A91C3A51D3C7ADEA98D82F8481E0E1E03674A6F3FB7"
        )
    }
]


def _hex(value):
    return bytes.fromhex(value)


class SchnorrTest(unittest.TestCase):

    def test_public_key_derivation(self):

        for vector in BIP340_VECTORS:
            with self.subTest(secret=vector["secret"]):
                self.assertEqual(
                    crypto.get_public_key(_hex(vector["secret"])).hex().upper(),
                    vector["public"]
                )

    def test_sign_matches_vectors(self):

        for vector in BIP340_VECTORS:
            with self.subTest(secret=vector["secret"]):
                signature = crypto.schnorr_sign(
                    _hex(vector["message"]), _hex(vector["secret"]), _hex(vector["aux"]))

                self.assertEqual(signature.hex().upper(), vector["signature"])

    def test_verify_accepts_valid_signatures(self):

        for vector in BIP340_VECTORS:
            with self.subTest(secret=vector["secret"]):
                self.assertTrue(crypto.schnorr_verify(
                    _hex(vector["message"]), _hex(vector["public"]), _hex(vector["signature"])))

    def test_verify_rejects_tampered_message(self):

        vector = BIP340_VECTORS[1]
        message = bytearray(_hex(vector["message"]))
        message[0] ^= 0x01

        self.assertFalse(crypto.schnorr_verify(
            bytes(message), _hex(vector["public"]), _hex(vector["signature"])))

    def test_verify_rejects_tampered_signature(self):

        vector = BIP340_VECTORS[1]

        for index in (0, 31, 32, 63):
            signature = bytearray(_hex(vector["signature"]))
            signature[index] ^= 0x01

            with self.subTest(index=index):
                self.assertFalse(crypto.schnorr_verify(
                    _hex(vector["message"]), _hex(vector["public"]), bytes(signature)))

    def test_verify_rejects_wrong_public_key(self):

        vector = BIP340_VECTORS[1]

        self.assertFalse(crypto.schnorr_verify(
            _hex(vector["message"]), _hex(BIP340_VECTORS[2]["public"]), _hex(vector["signature"])))

    def test_sign_is_deterministic_with_aux(self):

        secret = _hex(BIP340_VECTORS[0]["secret"])
        message = b"\x11" * 32
        aux = b"\x22" * 32

        first = crypto.schnorr_sign(message, secret, aux)
        second = crypto.schnorr_sign(message, secret, aux)

        self.assertEqual(first, second)

    def test_sign_rejects_bad_key_and_message(self):

        with self.assertRaises(crypto.CryptoError):
            crypto.schnorr_sign(b"\x00" * 32, b"\x00" * 32)

        with self.assertRaises(crypto.CryptoError):
            crypto.schnorr_sign(b"\x00" * 31, b"\x01" * 32)


class Bech32Test(unittest.TestCase):

    def test_round_trip_npub_and_nsec(self):

        secret = _hex(BIP340_VECTORS[0]["secret"])
        public = crypto.get_public_key(secret)

        nsec = crypto.encode_nsec(secret)
        npub = crypto.encode_npub(public)

        self.assertTrue(nsec.startswith("nsec1"))
        self.assertTrue(npub.startswith("npub1"))
        self.assertEqual(crypto.decode_bech32_key(nsec), secret)
        self.assertEqual(crypto.decode_bech32_key(npub), public)

    def test_normalize_secret_key_accepts_hex_and_nsec(self):

        secret = _hex(BIP340_VECTORS[1]["secret"])

        self.assertEqual(crypto.normalize_secret_key(BIP340_VECTORS[1]["secret"]), secret)
        self.assertEqual(crypto.normalize_secret_key(crypto.encode_nsec(secret)), secret)
        self.assertEqual(crypto.normalize_secret_key(f"  {BIP340_VECTORS[1]['secret']}  ".strip()), secret)
        self.assertIsNone(crypto.normalize_secret_key(""))
        self.assertIsNone(crypto.normalize_secret_key("nsec1notvalid"))
        self.assertIsNone(crypto.normalize_secret_key("abcd"))

    def test_decode_rejects_corrupted_checksum(self):

        secret = _hex(BIP340_VECTORS[0]["secret"])
        nsec = crypto.encode_nsec(secret)
        corrupted = nsec[:-1] + ("q" if nsec[-1] != "q" else "p")

        self.assertIsNone(crypto.bech32_decode(corrupted)[0])
        self.assertIsNone(crypto.decode_bech32_key(corrupted))

    def test_decode_rejects_mixed_case(self):

        secret = _hex(BIP340_VECTORS[0]["secret"])
        nsec = crypto.encode_nsec(secret)

        self.assertIsNone(crypto.bech32_decode(nsec.upper()[:6] + nsec[6:].lower())[0])


if __name__ == "__main__":
    unittest.main()
