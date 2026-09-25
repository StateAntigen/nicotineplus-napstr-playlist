# SPDX-FileCopyrightText: 2026 StateAntigen
# SPDX-License-Identifier: 0BSD
"""Tests for the Exportify CSV parser."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "plugin", "napstr_playlist"))

import napstr_csv  # noqa: E402  pylint: disable=wrong-import-position

HEADER = ("Track URI,Track Name,Artist URI(s),Artist Name(s),Album URI,Album Name,"
          "Album Artist URI(s),Album Artist Name(s),Album Release Date,"
          "Disc Number,Track Number,Track Duration (ms),ISRC,Added By,Added At")

ROWS = [
    "spotify:track:1,Enter Sandman,spotify:artist:1,Metallica,spotify:album:1,Metallica,"
    "spotify:artist:1,Metallica,1991-08-12,1,1,331000,USAM19100001,someone,2024-01-02T03:04:05Z",

    "spotify:track:2,Rooster,spotify:artist:2,Alice In Chains,spotify:album:2,Dirt,"
    "spotify:artist:2,Alice In Chains,1992-09-29,1,6,250000,USSM19200002,someone,2024-01-02T03:04:05Z"
]


class ParseCsvTest(unittest.TestCase):

    def test_parses_a_typical_export(self):

        text = "\r\n".join([HEADER] + ROWS) + "\r\n"
        entries = napstr_csv.parse_exportify_csv(text)

        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0], {
            "uri": "spotify:track:1",
            "title": "Enter Sandman",
            "artist": "Metallica",
            "album": "Metallica",
            "album_artist": "Metallica",
            "duration_ms": 331000,
            "isrc": "USAM19100001",
            "disc_number": "1",
            "track_number": "1",
            "added_at": "2024-01-02T03:04:05Z",
            "release_date": "1991-08-12",
            "source": ""
        })
        self.assertEqual(entries[1]["artist"], "Alice In Chains")

    def test_tolerates_a_utf8_bom(self):

        text = "\ufeff" + "\r\n".join([HEADER] + ROWS[:1])
        entries = napstr_csv.parse_exportify_csv(text)

        self.assertEqual(entries[0]["title"], "Enter Sandman")

    def test_tolerates_reordered_and_extra_columns(self):

        header = "Track Name,Artist Name(s),Album Name,Popularity,Danceability"
        text = f"{header}\nRooster,Alice In Chains,Dirt,80,0.4\n"
        entries = napstr_csv.parse_exportify_csv(text)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["title"], "Rooster")
        self.assertEqual(entries[0]["artist"], "Alice In Chains")
        self.assertEqual(entries[0]["album"], "Dirt")
        self.assertIsNone(entries[0]["duration_ms"])

    def test_accepts_a_bare_track_name_column(self):

        entries = napstr_csv.parse_exportify_csv("Track Name,Song Length\nSong,3:45\n")

        self.assertEqual(entries[0]["title"], "Song")
        self.assertEqual(entries[0]["duration_ms"], 225000)

    def test_rejects_files_without_a_track_column(self):

        with self.assertRaises(napstr_csv.ExportifyError):
            napstr_csv.parse_exportify_csv("Foo,Bar\n1,2\n")

    def test_rejects_empty_input(self):

        for text in ("", "   ", "\n"):
            with self.subTest(text=text):
                with self.assertRaises(napstr_csv.ExportifyError):
                    napstr_csv.parse_exportify_csv(text)

    def test_keeps_repeated_tracks(self):

        row = ROWS[0]
        entries = napstr_csv.parse_exportify_csv(f"{HEADER}\n{row}\n{row}\n")

        self.assertEqual(len(entries), 2)

    def test_skips_blank_rows(self):

        entries = napstr_csv.parse_exportify_csv(f"{HEADER}\n{ROWS[0]}\n,\n")

        self.assertEqual(len(entries), 1)


class ReadFileTest(unittest.TestCase):

    def test_reads_a_utf8_file(self):

        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "Rock.csv")

            with open(path, "w", encoding="utf-8-sig", newline="") as file_handle:
                file_handle.write("\r\n".join([HEADER] + ROWS))

            entries = napstr_csv.read_exportify_file(path)

            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0]["source"], path)

    def test_missing_file_is_reported(self):

        with self.assertRaises(napstr_csv.ExportifyError):
            napstr_csv.read_exportify_file(os.path.join(tempfile.gettempdir(), "does-not-exist.csv"))

    def test_no_path_is_reported(self):

        with self.assertRaises(napstr_csv.ExportifyError):
            napstr_csv.read_exportify_file("")


class QueryTest(unittest.TestCase):

    def test_default_template(self):

        entry = {"artist": "Metallica", "title": "Enter Sandman", "album": "Metallica",
                 "album_artist": "Metallica"}

        self.assertEqual(napstr_csv.build_search_query(entry), "Metallica Enter Sandman")

    def test_custom_template(self):

        entry = {"artist": "Alice In Chains", "title": "Rooster", "album": "Dirt",
                 "album_artist": "Alice In Chains"}

        self.assertEqual(
            napstr_csv.build_search_query(entry, "{album_artist} - {title}"),
            "Alice In Chains Rooster")

    def test_strips_soulseek_control_characters(self):

        entry = {"artist": "AC/DC", "title": "It's a Long Way", "album": "High Voltage",
                 "album_artist": "AC/DC"}

        query = napstr_csv.build_search_query(entry)

        self.assertNotIn("'", query)
        self.assertNotIn('"', query)
        self.assertEqual(query, "AC/DC It s a Long Way")

    def test_broken_template_falls_back(self):

        entry = {"artist": "A", "title": "B", "album": "", "album_artist": "A"}

        self.assertEqual(napstr_csv.build_search_query(entry, "{unknown}"), "A B")

    def test_album_artist_fallback(self):

        entry = {"artist": "", "title": "Song", "album": "", "album_artist": "Various Artists"}

        self.assertEqual(napstr_csv.build_search_query(entry), "Various Artists Song")

    def test_multi_artist_credit_uses_the_first_artist_only(self):

        # Exportify joins credits with ';'. Every word is a required term on
        # Soulseek, so the full credit list matches nothing (this is what made
        # half of a real playlist come back with "no results").
        entry = {"artist": "Above & Beyond;Malou", "title": "Letting Go",
                 "album": "", "album_artist": ""}

        self.assertEqual(napstr_csv.primary_artist(entry), "Above & Beyond")
        self.assertEqual(napstr_csv.build_search_query(entry), "Above & Beyond Letting Go")

    def test_single_artist_names_keep_ampersands_and_plus(self):

        for artist in ("Above & Beyond", "Sultan + Shepard", "Eli & Fur", "KREAM"):
            with self.subTest(artist=artist):
                entry = {"artist": artist, "title": "Song", "album": "", "album_artist": ""}

                self.assertEqual(napstr_csv.primary_artist(entry), artist)

    def test_feature_credits_are_dropped_from_the_title(self):

        self.assertEqual(
            napstr_csv.build_search_query(
                {"artist": "Anyma", "title": "Human Now (feat. Luke Steele)",
                 "album": "", "album_artist": ""}),
            "Anyma Human Now")

        self.assertEqual(napstr_csv.strip_feature_credits("Song [feat. X]"), "Song")
        self.assertEqual(napstr_csv.strip_feature_credits("Song (ft. X)"), "Song")
        self.assertEqual(napstr_csv.strip_feature_credits("Song (with X)"), "Song")

    def test_meaningful_brackets_are_kept(self):

        # 'Extended Mix' and 'Remix' change which recording is wanted, so they
        # stay in the search term.
        self.assertEqual(
            napstr_csv.strip_feature_credits("Deep Dive (Extended Mix)"),
            "Deep Dive (Extended Mix)")
        self.assertEqual(
            napstr_csv.strip_feature_credits("Dakota (YOTTO Remix)"),
            "Dakota (YOTTO Remix)")


if __name__ == "__main__":
    unittest.main()
