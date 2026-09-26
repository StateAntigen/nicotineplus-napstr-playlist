# SPDX-FileCopyrightText: 2026 StateAntigen
# SPDX-License-Identifier: 0BSD
"""End-to-end tests for tools/reset_playlist.py, which edits real documents.

The tool is run as its own process, the way the README tells people to run it,
because the interesting failure here is a silent no-op: `--include-excluded`
was accepted and quietly ignored once, since the guard that protects excluded
entries lives in napstr_state.reset_entries rather than in the tool.
"""

import os
import subprocess
import sys
import tempfile
import time
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
PLUGIN_DIR = os.path.join(REPO_ROOT, "plugin", "napstr_playlist")
TOOL = os.path.join(REPO_ROOT, "tools", "reset_playlist.py")

sys.path.insert(0, PLUGIN_DIR)

import napstr_state  # noqa: E402  pylint: disable=wrong-import-position

ENTRIES = [
    {"uri": "spotify:track:1", "title": "Keep Me", "artist": "X", "album": "L",
     "album_artist": "X", "duration_ms": 100000},
    {"uri": "spotify:track:2", "title": "Only On Acetate", "artist": "X", "album": "L",
     "album_artist": "X", "duration_ms": 100000},
    {"uri": "spotify:track:3", "title": "Nobody Has It", "artist": "X", "album": "L",
     "album_artist": "X", "duration_ms": 100000}
]


class ResetToolTest(unittest.TestCase):

    def setUp(self):

        self.data = tempfile.mkdtemp(prefix="napstr-tool-")
        self.own_folder = tempfile.mkdtemp(prefix="napstr-tool-own-")
        self.own_file = self.write_file(os.path.join(self.own_folder, "own copy.mp3"))
        self.excluded_file = self.write_file(os.path.join(self.own_folder, "acetate rip.mp3"))

        self.playlist = napstr_state.PlaylistState(
            self.data, title="Tool playlist", entries=[dict(entry) for entry in ENTRIES])

        self.playlist.entries[0]["file_id"] = "ab" * 32
        self.playlist.entries[0]["local_path"] = self.own_file

        self.playlist.entries[1]["status"] = napstr_state.STATUS_EXCLUDED
        self.playlist.entries[1]["notes"] = "excluded by hand: only pressing is on acetate"
        self.playlist.entries[1]["file_id"] = "cd" * 32
        self.playlist.entries[1]["local_path"] = self.excluded_file

        self.playlist.entries[2]["status"] = napstr_state.STATUS_UNAVAILABLE
        self.playlist.entries[2]["notes"] = "no results"

        self.playlist.save(force=True)

    def write_file(self, path, age=600):
        """A file in the staging folder, written ``age`` seconds ago."""

        with open(path, "wb") as file_handle:
            file_handle.write(b"x" * 4096)

        when = time.time() - age
        os.utime(path, (when, when))

        return path

    def run_tool(self, *arguments):

        result = subprocess.run(
            [sys.executable, TOOL, "--data-folder", self.data,
             "--playlist", self.playlist.id, *arguments],
            capture_output=True, text=True, errors="replace", check=False)

        self.assertEqual(result.returncode, 0, result.stderr)

        return result.stdout

    def reload(self):
        return napstr_state.load_playlist(self.data, self.playlist.id)

    # -- exclusions --------------------------------------------------------

    def test_an_excluded_entry_and_its_file_are_left_alone(self):

        output = self.run_tool()

        self.assertIn("Left alone: 1 deliberately excluded", output)

        reloaded = self.reload()

        self.assertEqual(reloaded.entries[1]["status"], napstr_state.STATUS_EXCLUDED)
        self.assertEqual(reloaded.entries[1]["file_id"], "cd" * 32)
        self.assertIn("acetate", reloaded.entries[1]["notes"])
        self.assertTrue(os.path.isfile(self.excluded_file))

    def test_a_deletion_pass_does_not_touch_an_excluded_files(self):

        # Entry 1 fails the filters and its file is deleted, as asked; the
        # excluded entry's file is the user's own copy and must survive.
        self.run_tool("--delete-unlinked", "--yes")

        self.assertFalse(os.path.exists(self.own_file))
        self.assertTrue(os.path.isfile(self.excluded_file))
        self.assertEqual(self.reload().entries[1]["status"], napstr_state.STATUS_EXCLUDED)

    def test_include_excluded_is_what_resets_them(self):

        output = self.run_tool("--include-excluded", "--keep-files")

        self.assertNotIn("Left alone", output)
        self.assertEqual(self.reload().entries[1]["status"], napstr_state.STATUS_NEW)
        self.assertTrue(os.path.isfile(self.excluded_file))

    def test_a_dry_run_writes_nothing(self):

        path = os.path.join(self.data, "napstr", "playlists", f"{self.playlist.id}.json")

        with open(path, "r", encoding="utf-8") as file_handle:
            before = file_handle.read()

        output = self.run_tool("--dry-run")

        self.assertIn("Dry run", output)

        with open(path, "r", encoding="utf-8") as file_handle:
            self.assertEqual(file_handle.read(), before)

    # -- orphans -----------------------------------------------------------

    def test_orphans_lists_without_deleting(self):

        folder = napstr_state.staging_folder_path(self.data, self.playlist.id)
        os.makedirs(folder, exist_ok=True)

        stray = self.write_file(os.path.join(folder, "Duplicate (1).mp3"))
        kept = self.write_file(os.path.join(folder, "never linked.part"))

        output = self.run_tool("--orphans")

        self.assertIn("Duplicate (1).mp3", output)
        self.assertIn("incomplete download", output)
        self.assertIn("Add --yes to delete them", output)
        self.assertTrue(os.path.isfile(stray))
        self.assertTrue(os.path.isfile(kept))

        self.run_tool("--orphans", "--yes")

        self.assertFalse(os.path.exists(stray))
        self.assertFalse(os.path.exists(kept))

    def test_orphans_keeps_a_file_written_a_moment_ago(self):

        folder = napstr_state.staging_folder_path(self.data, self.playlist.id)
        os.makedirs(folder, exist_ok=True)

        fresh = self.write_file(os.path.join(folder, "just finished.mp3"), age=0)

        output = self.run_tool("--orphans", "--yes")

        self.assertIn("written moments ago, kept", output)
        self.assertTrue(os.path.isfile(fresh))


if __name__ == "__main__":
    unittest.main()
