# SPDX-FileCopyrightText: 2026 StateAntigen
# SPDX-License-Identifier: 0BSD
"""Tests for candidate scoring, the part that decides what gets downloaded."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "plugin", "napstr_playlist"))

import napstr_match  # noqa: E402  pylint: disable=wrong-import-position

ENTRY = {
    "title": "Enter Sandman",
    "artist": "Metallica",
    "album": "Metallica",
    "duration_ms": 331000
}

GOOD_PATH = "Music\\Metallica\\Metallica\\01 - Enter Sandman.flac"
GOOD_ATTRIBUTES = {"bitrate": 1005, "length": 331}


class NormalizeTest(unittest.TestCase):

    def test_normalizes_punctuation_accents_and_case(self):

        self.assertEqual(napstr_match.normalize_text("Björk - Jóga (Live)"), "bjork joga live")
        self.assertEqual(napstr_match.normalize_text("AC/DC"), "ac dc")
        self.assertEqual(napstr_match.normalize_text("Simon & Garfunkel"), "simon and garfunkel")
        self.assertEqual(napstr_match.normalize_text(""), "")
        self.assertEqual(napstr_match.normalize_text(None), "")

    def test_audio_extension(self):

        self.assertEqual(napstr_match.audio_extension("a\\b\\Song.FLAC"), "flac")
        self.assertEqual(napstr_match.audio_extension("no-extension"), "")


class FilterTest(unittest.TestCase):
    """Hard filters: excluded formats and bitrate/size limits."""

    def test_excluded_format_is_rejected(self):

        options = napstr_match.ScoringOptions(excluded_extensions=["flac"])
        score, reasons = napstr_match.score_candidate(ENTRY, GOOD_PATH, options=options)

        self.assertEqual(score, 0.0)
        self.assertEqual(reasons, ["excluded format: flac"])

    def test_excluded_format_list_is_normalised(self):

        options = napstr_match.ScoringOptions(excluded_extensions=["FLAC", ".Flac", " wav "])

        self.assertEqual(options.excluded_extensions, ("flac", "flac", "wav"))
        self.assertEqual(
            napstr_match.score_candidate(ENTRY, "a\\b\\song.flac", options=options)[0], 0.0)
        self.assertEqual(
            napstr_match.score_candidate(ENTRY, "a\\b\\song.wav", options=options)[0], 0.0)

    def test_other_formats_are_unaffected(self):

        options = napstr_match.ScoringOptions(excluded_extensions=["flac"])
        score, _reasons = napstr_match.score_candidate(
            ENTRY, "Music\\Metallica\\Enter Sandman.mp3",
            attributes={"bitrate": 320, "length": 331}, options=options)

        self.assertGreater(score, 0.9)

    def test_maximum_bitrate(self):

        options = napstr_match.ScoringOptions(max_bitrate=320)
        score, reasons = napstr_match.score_candidate(
            ENTRY, GOOD_PATH, attributes={"bitrate": 1005}, options=options)

        self.assertEqual(score, 0.0)
        self.assertIn("above the maximum", reasons[0])

        # At the limit is fine, and an unknown bitrate is not rejected
        self.assertGreater(napstr_match.score_candidate(
            ENTRY, "Music\\Metallica\\Enter Sandman.mp3",
            attributes={"bitrate": 320}, options=options)[0], 0.0)
        self.assertGreater(napstr_match.score_candidate(
            ENTRY, GOOD_PATH, attributes={}, options=options)[0], 0.0)

    def test_minimum_and_maximum_size(self):

        options = napstr_match.ScoringOptions(
            min_size_bytes=napstr_match.megabytes_to_bytes(2),
            max_size_bytes=napstr_match.megabytes_to_bytes(20))

        rejected_small = napstr_match.score_candidate(
            ENTRY, GOOD_PATH, size=1024, options=options)
        rejected_big = napstr_match.score_candidate(
            ENTRY, GOOD_PATH, size=napstr_match.megabytes_to_bytes(50), options=options)
        accepted = napstr_match.score_candidate(
            ENTRY, GOOD_PATH, size=napstr_match.megabytes_to_bytes(10), options=options)

        self.assertEqual(rejected_small[0], 0.0)
        self.assertIn("below the minimum", rejected_small[1][0])
        self.assertEqual(rejected_big[0], 0.0)
        self.assertIn("above the maximum", rejected_big[1][0])
        self.assertGreater(accepted[0], 0.9)

    def test_size_filters_ignore_unknown_sizes(self):

        options = napstr_match.ScoringOptions(max_size_bytes=napstr_match.megabytes_to_bytes(1))

        self.assertGreater(napstr_match.score_candidate(ENTRY, GOOD_PATH, size=0, options=options)[0], 0.9)
        self.assertGreater(napstr_match.score_candidate(ENTRY, GOOD_PATH, options=options)[0], 0.9)

    def test_the_default_plugin_filters_would_have_caught_the_junk(self):
        """Regression for the real playlist: a 128 kbps pick and a 58 MB .aif."""

        options = napstr_match.ScoringOptions(
            excluded_extensions=["flac"], min_bitrate=192,
            max_size_bytes=napstr_match.megabytes_to_bytes(25))

        low_bitrate = napstr_match.score_candidate(
            ENTRY, "x\\Odd Mob feat. Lizzy Land - Never Alone.mp3",
            attributes={"bitrate": 128, "length": 204}, options=options)
        oversized = napstr_match.score_candidate(
            ENTRY, GOOD_PATH, size=napstr_match.megabytes_to_bytes(57.9), options=options)
        lossless = napstr_match.score_candidate(ENTRY, GOOD_PATH, options=options)

        self.assertEqual(low_bitrate[0], 0.0)
        self.assertEqual(oversized[0], 0.0)
        self.assertEqual(lossless[0], 0.0)


class MegabyteTest(unittest.TestCase):

    def test_conversion(self):

        self.assertEqual(napstr_match.megabytes_to_bytes(0), 0)
        self.assertEqual(napstr_match.megabytes_to_bytes(None), 0)
        self.assertEqual(napstr_match.megabytes_to_bytes(""), 0)
        self.assertEqual(napstr_match.megabytes_to_bytes("nonsense"), 0)
        self.assertEqual(napstr_match.megabytes_to_bytes(-5), 0)
        self.assertEqual(napstr_match.megabytes_to_bytes(1), 1048576)
        self.assertEqual(napstr_match.megabytes_to_bytes(2.5), 2621440)

    def test_local_matches_respect_the_filters(self):

        import tempfile  # pylint: disable=import-outside-toplevel

        with tempfile.TemporaryDirectory() as folder:
            flac_path = os.path.join(folder, "Metallica - Enter Sandman.flac")
            mp3_path = os.path.join(folder, "Metallica - Enter Sandman.mp3")

            for path in (flac_path, mp3_path):
                with open(path, "wb") as file_handle:
                    file_handle.write(b"x" * 4096)

            # No duration on the entry, so only the format filter is in play
            entry = dict(ENTRY, duration_ms=None)
            options = napstr_match.ScoringOptions(excluded_extensions=["flac"])
            matches = napstr_match.find_local_matches(
                entry, [flac_path, mp3_path], options=options, threshold=0.5)

            self.assertEqual([path for path, _score in matches], [mp3_path])

    def test_implausible_sizes_are_skipped_when_scanning(self):
        """A 320 kbps file must not be rejected just for being large."""

        import tempfile  # pylint: disable=import-outside-toplevel

        entry = dict(ENTRY, duration_ms=331000)  # 5:31

        def write(folder, name, num_bytes):
            path = os.path.join(folder, name)

            with open(path, "wb") as file_handle:
                file_handle.write(b"x" * num_bytes)

            return path

        with tempfile.TemporaryDirectory() as folder:
            # 331 s at 320 kbps is about 13 MB
            plausible_320 = write(folder, "Metallica - Enter Sandman (320).mp3", 13 * 1024 * 1024)
            plausible_128 = write(folder, "Metallica - Enter Sandman (128).mp3", 5 * 1024 * 1024)
            stub = write(folder, "Metallica - Enter Sandman (stub).mp3", 2048)

            matches = napstr_match.find_local_matches(
                entry, [plausible_320, plausible_128, stub], threshold=0.5)
            found = {path for path, _score in matches}

            self.assertIn(plausible_320, found)
            self.assertIn(plausible_128, found)
            self.assertNotIn(stub, found)


class ScoreTest(unittest.TestCase):

    def test_a_confident_match_scores_high(self):

        score, reasons = napstr_match.score_candidate(ENTRY, GOOD_PATH, attributes=GOOD_ATTRIBUTES)

        self.assertGreaterEqual(score, 0.9)
        self.assertIn("artist matched", reasons)
        self.assertIn("title matched", reasons)

    def test_wrong_artist_is_penalised(self):

        score, reasons = napstr_match.score_candidate(
            ENTRY, "Music\\Somebody Else\\Enter Sandman.mp3", attributes=GOOD_ATTRIBUTES)

        self.assertLess(score, 0.6)
        self.assertIn("artist not found in filename", reasons)

    def test_wrong_title_is_rejected(self):

        score, reasons = napstr_match.score_candidate(
            ENTRY, "Music\\Metallica\\Nothing Else Matters.flac", attributes=GOOD_ATTRIBUTES)

        self.assertLess(score, 0.4)
        self.assertIn("title mismatch", reasons)

    def test_live_versions_are_penalised_but_still_scored(self):

        score, reasons = napstr_match.score_candidate(
            ENTRY, "Music\\Metallica\\Enter Sandman (Live at Wembley).mp3",
            attributes={"bitrate": 320, "length": 331})

        self.assertLess(score, 0.9)
        self.assertTrue(any("different recording" in reason for reason in reasons), reasons)

    def test_entry_that_names_a_live_version_is_not_penalised(self):

        entry = dict(ENTRY, title="Enter Sandman (Live)")
        score, _reasons = napstr_match.score_candidate(
            entry, "Music\\Metallica\\Enter Sandman (Live).mp3",
            attributes={"bitrate": 320, "length": 331})

        self.assertGreaterEqual(score, 0.9)

    def test_duration_mismatch_reduces_confidence(self):

        score, reasons = napstr_match.score_candidate(
            ENTRY, GOOD_PATH, attributes={"length": 431})

        self.assertLess(score, 0.8)
        self.assertTrue(any("duration off by" in reason for reason in reasons), reasons)

    def test_unknown_duration_stays_neutral(self):

        score, _reasons = napstr_match.score_candidate(ENTRY, GOOD_PATH, attributes={})

        self.assertGreaterEqual(score, 0.85)

    def test_minimum_bitrate_filters_candidates(self):

        options = napstr_match.ScoringOptions(min_bitrate=320)
        score, reasons = napstr_match.score_candidate(
            ENTRY, GOOD_PATH, attributes={"bitrate": 128, "length": 331}, options=options)

        self.assertEqual(score, 0.0)
        self.assertIn("below the minimum", reasons[0])

    def test_unsupported_extension_is_filtered(self):

        score, reasons = napstr_match.score_candidate(ENTRY, "Music\\Metallica\\Enter Sandman.txt")

        self.assertEqual(score, 0.0)
        self.assertIn("unsupported format", reasons[0])

    def test_preferred_format_breaks_a_tie(self):

        mp3 = napstr_match.score_candidate(
            ENTRY, "Music\\Metallica\\Enter Sandman.mp3", attributes={"bitrate": 320, "length": 331})[0]
        flac = napstr_match.score_candidate(
            ENTRY, "Music\\Metallica\\Enter Sandman.flac", attributes={"bitrate": 1005, "length": 331})[0]

        self.assertLessEqual(mp3, flac)

        flac_preference = napstr_match.ScoringOptions(preferred_extension="flac")
        lossy_score = napstr_match.score_candidate(
            ENTRY, "Music\\Metallica\\Enter Sandman.mp3",
            attributes={"bitrate": 320, "length": 331}, options=flac_preference)[0]

        self.assertLess(lossy_score, flac)

    def test_score_is_bounded(self):

        for path in ("", "\\", "a" * 300, "Music\\Metallica\\Enter Sandman.mp3"):
            with self.subTest(path=path):
                score, _reasons = napstr_match.score_candidate(ENTRY, path)
                self.assertGreaterEqual(score, 0.0)
                self.assertLessEqual(score, 1.0)


class RankedCandidatesTest(unittest.TestCase):

    def test_ranks_best_first_and_annotates(self):

        candidates = [
            {"username": "user1", "path": "Music\\Metallica\\Enter Sandman (Live).mp3",
             "attributes": {"bitrate": 128, "length": 400}},
            {"username": "user2", "path": GOOD_PATH, "attributes": GOOD_ATTRIBUTES},
            {"username": "user3", "path": "Music\\Metallica\\Enter Sandman.mp3",
             "attributes": {"bitrate": 320, "length": 331}}
        ]

        ranked = napstr_match.find_ranked_candidates(ENTRY, candidates)

        self.assertEqual(ranked[0]["username"], "user2")
        self.assertTrue(all("score" in candidate and "reasons" in candidate for candidate in ranked))
        self.assertEqual(
            [candidate["score"] for candidate in ranked],
            sorted((candidate["score"] for candidate in ranked), reverse=True))

    def test_empty_candidate_list(self):

        self.assertEqual(napstr_match.find_ranked_candidates(ENTRY, []), [])
        self.assertEqual(napstr_match.find_ranked_candidates(ENTRY, None), [])


class LocalMatchTest(unittest.TestCase):

    def test_finds_the_right_local_file(self):

        paths = [
            "C:\\Music\\Metallica\\Enter Sandman.flac",
            "C:\\Music\\Other\\Something Else.flac"
        ]

        matches = napstr_match.find_local_matches(ENTRY, paths)

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0][0], paths[0])

    def test_threshold_can_be_raised(self):

        self.assertEqual(
            napstr_match.find_local_matches(ENTRY, ["C:\\Music\\Metallica\\Enter Sandman.flac"],
                                            threshold=1.01),
            [])


class DescribeTest(unittest.TestCase):

    def test_describe_includes_useful_details(self):

        text = napstr_match.describe_candidate({
            "path": "Music\\Metallica\\Enter Sandman.flac", "username": "user1",
            "bitrate": 1005, "length": 331, "size": 41943040
        })

        self.assertIn("Enter Sandman.flac", text)
        self.assertIn("user1", text)
        self.assertIn("1005 kbps", text)
        self.assertIn("5:31", text)
        self.assertIn("40.0 MiB", text)

    def test_describe_survives_a_bare_candidate(self):

        self.assertEqual(napstr_match.describe_candidate({"path": "a.mp3"}).strip(), "a.mp3")


if __name__ == "__main__":
    unittest.main()
