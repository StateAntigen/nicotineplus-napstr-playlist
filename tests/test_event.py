# SPDX-FileCopyrightText: 2026 StateAntigen
# SPDX-License-Identifier: 0BSD
"""Tests for the NAPSTR kind 30425 event builder and validator."""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "plugin", "napstr_playlist"))

import napstr_crypto  # noqa: E402  pylint: disable=wrong-import-position
import napstr_event  # noqa: E402  pylint: disable=wrong-import-position

SECRET_KEY = bytes.fromhex(
    "B7E151628AED2A6ABF7158809CF4F3C762E7160F38B4DA56A784D9045190CFEF")

PLAYLIST_ID = "77abf082-7075-4d36-afe2-e9710ac6b33c"

FILE_ID_1 = "48e5979efa6a56dc3cab293b954ae84e36f464b936bd8c534838189abcc93c68"
FILE_ID_2 = "cdf1741591bf1e580b1e7a2712ce781ef7ade8b12ebf309731d264498accf5ce"


def make_tracks():
    return [
        {"file_id": FILE_ID_1, "title": "Enter Sandman", "artist": "Metallica", "album": "Metallica"},
        {"file_id": FILE_ID_2, "title": "Rooster", "artist": "Alice In Chains"}
    ]


class BuildEventTest(unittest.TestCase):

    def test_build_and_validate_signed_event(self):

        event = napstr_event.build_playlist_event(
            PLAYLIST_ID, "rock", make_tracks(), tags=["rock", "metallica"])
        signed = napstr_event.sign_event(event, SECRET_KEY)

        self.assertEqual(napstr_event.validate_playlist_event(signed), [])
        self.assertEqual(signed["kind"], 30425)
        self.assertEqual(signed["pubkey"], napstr_crypto.get_public_key(SECRET_KEY).hex())

        # Required tags, in the documented order
        self.assertEqual(signed["tags"][0], ["d", PLAYLIST_ID])
        self.assertEqual(signed["tags"][1], ["t", "napstr-playlist"])
        self.assertEqual(signed["tags"][2], ["title", "rock"])
        self.assertEqual(signed["tags"][3], ["alt", "Napstr public playlist"])
        self.assertEqual(signed["tags"][4][0], "client")

        # One 'x' tag per member, in member order, then the author's words
        x_tags = [tag[1] for tag in signed["tags"] if tag[0] == "x"]
        self.assertEqual(x_tags, [FILE_ID_1, FILE_ID_2])
        self.assertEqual([tag[1] for tag in signed["tags"] if tag[0] == "t"],
                         ["napstr-playlist", "rock", "metallica"])

    def test_content_shape(self):

        event = napstr_event.build_playlist_event(PLAYLIST_ID, "rock", make_tracks())
        content = json.loads(event["content"])

        self.assertEqual(content["protocol"], "napstr/1")
        self.assertEqual(content["playlistId"], PLAYLIST_ID)
        self.assertEqual(content["title"], "rock")
        self.assertEqual(
            content["tracks"],
            [
                {"position": 1, "fileId": FILE_ID_1, "title": "Enter Sandman",
                 "artist": "Metallica", "album": "Metallica"},
                {"position": 2, "fileId": FILE_ID_2, "title": "Rooster",
                 "artist": "Alice In Chains"}
            ])

        # A playlist carries no size, format or filename claims
        for key in ("totalSize", "filename", "format", "mime", "size"):
            self.assertNotIn(key, content)

    def test_empty_hints_are_omitted(self):

        event = napstr_event.build_playlist_event(
            PLAYLIST_ID, "silence", [{"file_id": FILE_ID_1, "title": "", "artist": ""}])
        track = json.loads(event["content"])["tracks"][0]

        self.assertEqual(track, {"position": 1, "fileId": FILE_ID_1})

    def test_playlist_needs_a_title(self):

        with self.assertRaises(napstr_event.EventError):
            napstr_event.build_playlist_event(PLAYLIST_ID, "   ", make_tracks())

    def test_playlist_id_must_be_a_canonical_uuid(self):

        for bad_id in ("", "not-a-uuid", PLAYLIST_ID.upper(), "77abf0827075-4d36-afe2-e9710ac6b33c"):
            with self.subTest(playlist_id=bad_id):
                with self.assertRaises(napstr_event.EventError):
                    napstr_event.build_playlist_event(bad_id, "rock", make_tracks())

    def test_duplicate_file_ids_are_rejected(self):

        tracks = make_tracks() + [{"file_id": FILE_ID_1, "title": "Again"}]

        with self.assertRaises(napstr_event.EventError):
            napstr_event.build_playlist_event(PLAYLIST_ID, "rock", tracks)

    def test_invalid_file_id_is_rejected(self):

        with self.assertRaises(napstr_event.EventError):
            napstr_event.build_playlist_event(
                PLAYLIST_ID, "rock", [{"file_id": "abc", "title": "Short"}])

    def test_member_limit(self):

        tracks = [
            {"file_id": f"{index:064x}"[-64:]} for index in range(1, 502)
        ]

        with self.assertRaises(napstr_event.EventError):
            napstr_event.build_playlist_event(PLAYLIST_ID, "huge", tracks)

    def test_content_budget(self):

        long_title = "T" * 256
        tracks = [
            {"file_id": f"{index:064x}", "title": long_title, "artist": long_title, "album": long_title}
            for index in range(500)
        ]

        with self.assertRaises(napstr_event.EventError):
            napstr_event.build_playlist_event(PLAYLIST_ID, "budget", tracks)

    def test_unsafe_characters_are_stripped(self):

        event = napstr_event.build_playlist_event(
            PLAYLIST_ID, "rock\u202e evil\u0007", make_tracks())
        titles = [tag[1] for tag in event["tags"] if tag[0] == "title"]

        self.assertEqual(titles, ["rock evil"])

    def test_image_must_be_a_file_id(self):

        event = napstr_event.build_playlist_event(
            PLAYLIST_ID, "rock", make_tracks(), image="https://example.com/cover.jpg")
        self.assertNotIn("image", json.loads(event["content"]))

        event = napstr_event.build_playlist_event(
            PLAYLIST_ID, "rock", make_tracks(), image=FILE_ID_2)
        self.assertEqual(json.loads(event["content"])["image"], FILE_ID_2)

    def test_mbid_must_be_a_uuid(self):

        event = napstr_event.build_playlist_event(
            PLAYLIST_ID, "rock", make_tracks(), mbid="not-a-uuid")
        self.assertNotIn("mbid", json.loads(event["content"]))

        event = napstr_event.build_playlist_event(
            PLAYLIST_ID, "rock", make_tracks(), mbid=PLAYLIST_ID)
        self.assertEqual(json.loads(event["content"])["mbid"], PLAYLIST_ID)


class TagsTest(unittest.TestCase):

    def test_tag_rules(self):

        self.assertEqual(napstr_event.sanitize_tags("rock, METALLICA , rock"), ["rock", "metallica"])

        many = [f"word{index}" for index in range(20)]
        self.assertEqual(len(napstr_event.sanitize_tags(many)), napstr_event.MAX_TAGS)
        self.assertEqual(napstr_event.sanitize_tags(many), many[:napstr_event.MAX_TAGS])

        long_word = napstr_event.sanitize_tags(["x" * 80])
        self.assertEqual(len(long_word[0]), napstr_event.MAX_TAG_LENGTH)
        self.assertEqual(napstr_event.sanitize_tags(""), [])
        self.assertEqual(napstr_event.sanitize_tags(None), [])

    def test_no_words_is_valid(self):

        event = napstr_event.build_playlist_event(PLAYLIST_ID, "quiet", make_tracks(), tags=[])
        signed = napstr_event.sign_event(event, SECRET_KEY)

        self.assertEqual([tag[1] for tag in signed["tags"] if tag[0] == "t"],
                         [napstr_event.MARKER_TAG])
        self.assertEqual(napstr_event.validate_playlist_event(signed), [])


class ValidateEventTest(unittest.TestCase):

    def setUp(self):
        self.event = napstr_event.sign_event(
            napstr_event.build_playlist_event(PLAYLIST_ID, "rock", make_tracks()), SECRET_KEY)

    def test_detects_changed_content(self):

        tampered = dict(self.event)
        tampered["content"] = json.dumps(
            {"protocol": "napstr/1", "playlistId": PLAYLIST_ID, "title": "rock",
             "tracks": [{"position": 1, "fileId": FILE_ID_1}]})

        # Removing a member changes the bytes the id commits to, even though
        # the signature itself is still the author's signature over the old id.
        self.assertIn("event id does not match the serialized event",
                      napstr_event.validate_playlist_event(tampered))

    def test_detects_a_tampered_signature(self):

        tampered = dict(self.event)
        signature = bytearray(bytes.fromhex(self.event["sig"]))
        signature[0] ^= 0x01
        tampered["sig"] = bytes(signature).hex()

        self.assertIn("event signature is invalid",
                      napstr_event.validate_playlist_event(tampered))

    def test_detects_a_missing_marker(self):

        tampered = dict(self.event)
        tampered["tags"] = [tag for tag in self.event["tags"] if tag[0] != "t"]

        self.assertIn("the literal 'napstr-playlist' marker tag is required",
                      napstr_event.validate_playlist_event(tampered))

    def test_detects_a_mismatched_x_tag(self):

        tampered = dict(self.event)
        tampered["tags"] = [tag if tag[0] != "x" else ["x", FILE_ID_2] for tag in self.event["tags"]]
        problems = napstr_event.validate_playlist_event(tampered)

        self.assertTrue(any("same file IDs" in problem for problem in problems), problems)

    def test_detects_wrong_alt_text(self):

        tampered = dict(self.event)
        tampered["tags"] = [["alt", "Napstr playlist"] if tag[0] == "alt" else tag
                            for tag in self.event["tags"]]

        self.assertIn("'alt' must be the literal 'Napstr public playlist'",
                      napstr_event.validate_playlist_event(tampered))

    def test_detects_non_contiguous_positions(self):

        tampered = dict(self.event)
        content = json.loads(self.event["content"])
        content["tracks"][1]["position"] = 5
        tampered["content"] = json.dumps(content)

        self.assertIn("member positions must be contiguous and start at 1",
                      napstr_event.validate_playlist_event(tampered))


class WithdrawalTest(unittest.TestCase):

    def test_withdrawal_event_validates(self):

        event = napstr_event.build_withdrawal_event(PLAYLIST_ID)
        signed = napstr_event.sign_event(event, SECRET_KEY)

        self.assertTrue(napstr_event.is_withdrawal(signed))
        self.assertEqual(napstr_event.validate_playlist_event(signed), [])
        self.assertEqual(
            json.loads(signed["content"]), {"protocol": "napstr/1", "deleted": True})
        self.assertEqual([tag[1] for tag in signed["tags"] if tag[0] == "d"], [PLAYLIST_ID])

    def test_a_playlist_is_not_a_withdrawal(self):

        event = napstr_event.build_playlist_event(PLAYLIST_ID, "rock", make_tracks())
        self.assertFalse(napstr_event.is_withdrawal(event))


class EventIdTest(unittest.TestCase):

    def test_event_id_matches_nip01_serialization(self):

        event = napstr_event.build_playlist_event(PLAYLIST_ID, "rock", make_tracks(),
                                                  created_at=1700000000)
        signed = napstr_event.sign_event(event, SECRET_KEY)

        serialized = json.dumps(
            [0, signed["pubkey"], 1700000000, 30425, signed["tags"], signed["content"]],
            separators=(",", ":"), ensure_ascii=False).encode()

        import hashlib  # pylint: disable=import-outside-toplevel

        self.assertEqual(signed["id"], hashlib.sha256(serialized).hexdigest())
        self.assertEqual(len(signed["sig"]), 128)

    def test_signing_gives_a_stable_id_and_valid_signatures(self):

        event = napstr_event.build_playlist_event(PLAYLIST_ID, "rock", make_tracks(),
                                                  created_at=1700000000)
        first = napstr_event.sign_event(event, SECRET_KEY)
        second = napstr_event.sign_event(event, SECRET_KEY)

        # The id commits to the event, not to the signature, so it is stable;
        # the signatures differ only because BIP-340 uses fresh auxiliary
        # randomness, and both must verify.
        self.assertEqual(first["id"], second["id"])

        for signed in (first, second):
            self.assertTrue(napstr_crypto.schnorr_verify(
                bytes.fromhex(signed["id"]),
                bytes.fromhex(signed["pubkey"]),
                bytes.fromhex(signed["sig"])))

        self.assertEqual(napstr_event.validate_playlist_event(first), [])
        self.assertEqual(napstr_event.validate_playlist_event(second), [])


if __name__ == "__main__":
    unittest.main()
