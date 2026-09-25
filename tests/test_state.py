# SPDX-FileCopyrightText: 2026 StateAntigen
# SPDX-License-Identifier: 0BSD
"""Tests for playlist persistence, member derivation and file hashing."""

import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "plugin", "napstr_playlist"))

import napstr_state  # noqa: E402  pylint: disable=wrong-import-position

FILE_ID_1 = "48e5979efa6a56dc3cab293b954ae84e36f464b936bd8c534838189abcc93c68"
FILE_ID_2 = "cdf1741591bf1e580b1e7a2712ce781ef7ade8b12ebf309731d264498accf5ce"

ENTRIES = [
    {"uri": "spotify:track:1", "title": "Enter Sandman", "artist": "Metallica",
     "album": "Metallica", "album_artist": "Metallica", "duration_ms": 331000,
     "isrc": "USAM19100001"},
    {"uri": "spotify:track:2", "title": "Rooster", "artist": "Alice In Chains",
     "album": "Dirt", "album_artist": "Alice In Chains", "duration_ms": 250000,
     "isrc": "USSM19200002"},
    {"uri": "spotify:track:3", "title": "Would?", "artist": "Alice In Chains",
     "album": "Dirt", "album_artist": "Alice In Chains", "duration_ms": 200000,
     "isrc": "USSM19300003"}
]


class HashingTest(unittest.TestCase):

    def test_sha256_matches_hashlib(self):

        content = os.urandom(5000)

        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "song.flac")

            with open(path, "wb") as file_handle:
                file_handle.write(content)

            # A small chunk size exercises the streaming path
            self.assertEqual(
                napstr_state.sha256_file(path, chunk_size=512),
                hashlib.sha256(content).hexdigest())

    def test_reports_progress(self):

        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "song.mp3")

            with open(path, "wb") as file_handle:
                file_handle.write(b"x" * 3000)

            totals = []
            napstr_state.sha256_file(path, chunk_size=1000, progress_callback=totals.append)

            self.assertEqual(totals, [1000, 2000, 3000])

    def test_cancellation(self):

        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "song.mp3")

            with open(path, "wb") as file_handle:
                file_handle.write(b"x" * 3000)

            cancel_event = threading.Event()
            cancel_event.set()

            with self.assertRaises(InterruptedError):
                napstr_state.sha256_file(path, chunk_size=1000, cancel_event=cancel_event)


class PlaylistStateTest(unittest.TestCase):

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.playlist = napstr_state.PlaylistState(
            self.folder, title="Rock", entries=ENTRIES, tags=["rock"])

    def test_entries_get_sequential_positions(self):

        self.assertEqual([entry["position"] for entry in self.playlist.entries], [1, 2, 3])
        self.assertEqual(self.playlist.entry(2)["title"], "Rooster")
        self.assertIsNone(self.playlist.entry(0))
        self.assertIsNone(self.playlist.entry(99))
        self.assertIsNone(self.playlist.entry("nonsense"))

    def test_entry_by_uri(self):

        self.assertEqual(self.playlist.entry_by_uri("spotify:track:3")["title"], "Would?")
        self.assertIsNone(self.playlist.entry_by_uri("spotify:track:404"))

    def test_metadata_is_carried_from_the_csv(self):

        entry = self.playlist.entries[0]

        self.assertEqual(entry["duration_ms"], 331000)
        self.assertEqual(entry["isrc"], "USAM19100001")
        self.assertEqual(entry["status"], napstr_state.STATUS_NEW)
        self.assertEqual(entry["candidates"], [])
        self.assertEqual(entry["file_id"], "")

    def test_publishable_tracks_skips_unresolved_members(self):

        self.playlist.entries[1]["file_id"] = FILE_ID_1

        tracks, skipped = self.playlist.publishable_tracks()

        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["position"], 1)
        self.assertEqual(tracks[0]["file_id"], FILE_ID_1)
        self.assertEqual([position for position, _reason in skipped], [1, 3])

    def test_publishable_tracks_drops_duplicate_file_ids(self):

        for entry in self.playlist.entries:
            entry["file_id"] = FILE_ID_1

        tracks, skipped = self.playlist.publishable_tracks()

        # The first member keeps the file ID, the later repeats are dropped
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["position"], 1)
        self.assertEqual([position for position, _reason in skipped], [2, 3])
        self.assertTrue(all("duplicate" in reason for _position, reason in skipped), skipped)

    def test_publishable_tracks_keeps_hints(self):

        self.playlist.entries[0]["file_id"] = FILE_ID_2
        tracks, _skipped = self.playlist.publishable_tracks()

        self.assertEqual(tracks[0]["title"], "Enter Sandman")
        self.assertEqual(tracks[0]["artist"], "Metallica")
        self.assertEqual(tracks[0]["album"], "Metallica")

    def test_summary(self):

        self.playlist.entries[0]["file_id"] = FILE_ID_2
        self.playlist.set_status(self.playlist.entries[1], napstr_state.STATUS_SKIPPED)

        summary = self.playlist.summary()

        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["with_file_id"], 1)
        self.assertEqual(summary["counts"][napstr_state.STATUS_SKIPPED], 1)

    def test_save_and_load_round_trip(self):

        self.playlist.entries[0]["local_path"] = "C:\\Music\\Enter Sandman.flac"
        self.playlist.entries[0]["file_id"] = FILE_ID_1
        self.playlist.entries[0]["candidates"] = [{
            "username": "user1", "path": "Music\\Enter Sandman.flac", "size": 100,
            "score": 0.91, "reasons": ["artist matched"]
        }]
        self.playlist.set_status(self.playlist.entries[2], napstr_state.STATUS_UNAVAILABLE, "no results")

        path = self.playlist.save(force=True)

        self.assertTrue(os.path.isfile(path))

        loaded = napstr_state.load_playlist(self.folder, self.playlist.id)

        self.assertEqual(loaded.id, self.playlist.id)
        self.assertEqual(loaded.title, "Rock")
        self.assertEqual(loaded.tags, ["rock"])
        self.assertEqual(len(loaded.entries), 3)
        self.assertEqual(loaded.entries[0]["file_id"], FILE_ID_1)
        self.assertEqual(loaded.entries[0]["candidates"][0]["score"], 0.91)
        self.assertEqual(loaded.entries[2]["notes"], "no results")

    def test_save_is_skipped_when_not_dirty(self):

        self.playlist.save(force=True)
        self.assertFalse(self.playlist.dirty)

        # No write happens, so a hand edit of the file survives
        with open(self.playlist.file_path(), encoding="utf-8") as file_handle:
            data = json.load(file_handle)

        data["playlist"]["title"] = "Edited on disk"

        with open(self.playlist.file_path(), "w", encoding="utf-8") as file_handle:
            json.dump(data, file_handle)

        self.playlist.save()

        with open(self.playlist.file_path(), encoding="utf-8") as file_handle:
            self.assertEqual(json.load(file_handle)["playlist"]["title"], "Edited on disk")

    def test_index_lists_playlists(self):

        self.playlist.save(force=True)

        playlists = napstr_state.list_playlists(self.folder)

        self.assertIn(self.playlist.id, playlists)
        self.assertEqual(playlists[self.playlist.id]["title"], "Rock")
        self.assertEqual(playlists[self.playlist.id]["total"], 3)
        self.assertFalse(playlists[self.playlist.id]["published"])

    def test_delete_playlist(self):

        self.playlist.save(force=True)
        napstr_state.delete_playlist(self.folder, self.playlist.id)

        self.assertIsNone(napstr_state.load_playlist(self.folder, self.playlist.id))
        self.assertNotIn(self.playlist.id, napstr_state.list_playlists(self.folder))

    def test_load_missing_or_broken_playlist(self):

        self.assertIsNone(napstr_state.load_playlist(self.folder, "does-not-exist"))

        folder = napstr_state.state_folder_path(self.folder)
        os.makedirs(os.path.join(folder, "playlists"), exist_ok=True)
        broken = os.path.join(folder, "playlists", "broken.json")

        with open(broken, "w", encoding="utf-8") as file_handle:
            file_handle.write("{not json")

        self.assertIsNone(napstr_state.load_playlist(self.folder, "broken"))

    def test_paths(self):

        expected_root = os.path.join(self.folder, "napstr")

        self.assertEqual(napstr_state.state_folder_path(self.folder), expected_root)
        self.assertEqual(
            napstr_state.staging_folder_path(self.folder, "abc"),
            os.path.join(expected_root, "files", "abc"))
        self.assertTrue(self.playlist.paths_for_download().endswith(self.playlist.id))

    def test_new_playlist_id_is_a_canonical_uuid(self):

        playlist_id = napstr_state.new_playlist_id()

        self.assertEqual(playlist_id, playlist_id.lower())
        self.assertEqual(len(playlist_id), 36)


class WriteTest(unittest.TestCase):
    """Saves must survive the file locking that Windows does while scanning."""

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.playlist = napstr_state.PlaylistState(
            self.folder, title="Rock", entries=ENTRIES)

    def test_a_locked_index_does_not_lose_the_playlist(self):
        """index.json is a derived cache; a save must not fail over it."""

        real_replace = os.replace
        index_path = os.path.join(self.folder, "napstr", "index.json")

        def replace(source, destination):
            if str(destination) == index_path:
                raise PermissionError(32, "The process cannot access the file")

            return real_replace(source, destination)

        os.replace = replace
        self.addCleanup(setattr, os, "replace", real_replace)

        written = self.playlist.save()

        self.assertTrue(os.path.isfile(written))

        with open(written, encoding="utf-8") as file_handle:
            document = json.load(file_handle)

        self.assertEqual(document["playlist"]["title"], "Rock")
        self.assertEqual(len(document["entries"]), 3)

    def test_a_failed_write_leaves_no_debris_and_keeps_the_old_document(self):

        path = self.playlist.save()
        self.playlist.title = "Changed"
        self.playlist.dirty = True

        real_dump = json.dump

        def dump(data, handle, **kwargs):
            real_dump(data, handle, **kwargs)
            raise OSError("disk full")

        json.dump = dump
        self.addCleanup(setattr, json, "dump", real_dump)

        with self.assertRaises(OSError):
            self.playlist.save()

        with open(path, encoding="utf-8") as file_handle:
            self.assertEqual(json.load(file_handle)["playlist"]["title"], "Rock")

        leftovers = [name for name in os.listdir(os.path.dirname(path)) if name.endswith(".tmp")]

        self.assertEqual(leftovers, [])

    def test_two_saves_do_not_share_a_temporary_file(self):

        seen = []
        real_replace = os.replace

        def replace(source, destination):
            seen.append(os.path.basename(source))
            return real_replace(source, destination)

        os.replace = replace
        self.addCleanup(setattr, os, "replace", real_replace)

        self.playlist.save()
        self.playlist.dirty = True
        self.playlist.save()

        self.assertEqual(len(seen), 4)
        self.assertEqual(len(set(seen)), 4, seen)


class ResetEntriesTest(unittest.TestCase):
    """reset_entries clears decisions but keeps facts about files on disk."""

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.playlist = napstr_state.PlaylistState(
            self.folder, title="Rock", entries=ENTRIES, tags=["rock"])

        for index, entry in enumerate(self.playlist.entries, start=1):
            entry["file_id"] = f"{index:064x}"
            entry["local_path"] = f"C:\\Music\\song{index}.mp3"
            entry["hashed_at"] = 1234
            entry["candidates"] = [{"username": "u", "path": "p", "score": 0.9}]
            entry["chosen"] = {"username": "u", "path": "p"}
            entry["score"] = 0.9
            entry["query"] = "query"
            entry["notes"] = "note"
            entry["status"] = napstr_state.STATUS_HASHED

    def test_reset_keeps_file_ids_by_default(self):

        changed = napstr_state.reset_entries(self.playlist)

        self.assertEqual(changed, 3)
        self.assertTrue(self.playlist.dirty)

        for entry in self.playlist.entries:
            self.assertEqual(entry["status"], napstr_state.STATUS_NEW)
            self.assertEqual(entry["candidates"], [])
            self.assertIsNone(entry["chosen"])
            self.assertIsNone(entry["score"])
            self.assertEqual(entry["query"], "")
            self.assertEqual(entry["notes"], "")
            self.assertTrue(entry["file_id"])
            self.assertTrue(entry["local_path"])

    def test_forget_drops_file_ids(self):

        napstr_state.reset_entries(self.playlist, drop_files=True)

        for entry in self.playlist.entries:
            self.assertEqual(entry["file_id"], "")
            self.assertEqual(entry["local_path"], "")
            self.assertIsNone(entry["hashed_at"])
            self.assertEqual(entry["status"], napstr_state.STATUS_NEW)

    def test_only_the_named_entries_are_touched(self):

        napstr_state.reset_entries(self.playlist, positions=[2])

        self.assertEqual(self.playlist.entries[1]["status"], napstr_state.STATUS_NEW)
        self.assertEqual(self.playlist.entries[1]["candidates"], [])
        self.assertIsNone(self.playlist.entries[1]["chosen"])
        self.assertEqual(self.playlist.entries[1]["query"], "")

        # Positions 1 and 3 keep everything, including their resolved state
        for untouched in (self.playlist.entries[0], self.playlist.entries[2]):
            self.assertEqual(untouched["status"], napstr_state.STATUS_HASHED)
            self.assertEqual(len(untouched["candidates"]), 1)
            self.assertEqual(untouched["query"], "query")

    def test_an_empty_selection_changes_nothing(self):

        self.assertEqual(napstr_state.reset_entries(self.playlist, positions=[]), 0)

        for entry in self.playlist.entries:
            self.assertEqual(entry["status"], napstr_state.STATUS_HASHED)
            self.assertEqual(len(entry["candidates"]), 1)
            self.assertTrue(entry["file_id"])


if __name__ == "__main__":
    unittest.main()
