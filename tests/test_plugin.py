# SPDX-FileCopyrightText: 2026 StateAntigen
# SPDX-License-Identifier: 0BSD
"""Behavioural tests for the plugin, run against a fake Nicotine+ host.

The plugin is instantiated for real and driven through ``/napstr`` commands,
which is what catches the mistakes a static check cannot: wrong attribute names
in the host API, missing initialisers, and - the reason this file exists -
anything that would send searches too fast.

The single most important assertion here is that a batch of searches is paced
and never overlaps: an earlier version sent 100 searches in 215 seconds and the
account was banned for 30 minutes.
"""

import hashlib
import os
import sys
import tempfile
import time
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
PLUGIN_DIR = os.path.join(REPO_ROOT, "plugin", "napstr_playlist")

# The fake host must be importable before the plugin module is
sys.path.insert(0, TESTS_DIR)
sys.path.insert(0, os.path.join(REPO_ROOT, "plugin"))
sys.path.insert(0, PLUGIN_DIR)

import fake_nicotine  # noqa: E402  pylint: disable=wrong-import-position

DATA_FOLDER = tempfile.mkdtemp(prefix="napstr-test-")

SCHEDULER, CORE, CONFIG, _BASE = fake_nicotine.install(DATA_FOLDER)

import napstr_playlist  # noqa: E402  pylint: disable=wrong-import-position
import napstr_event  # noqa: E402  pylint: disable=wrong-import-position
import napstr_pace  # noqa: E402  pylint: disable=wrong-import-position
import napstr_relay  # noqa: E402  pylint: disable=wrong-import-position

BAN_MESSAGE = (
    "You have been banned for 30 minutes. This is usually the result of doing too many "
    "operations at once. Do not quickly repeat a search."
)

SECRET_KEY = "B7E151628AED2A6ABF7158809CF4F3C762E7160F38B4DA56A784D9045190CFEF"

CSV_HEADER = "Track URI,Track Name,Artist Name(s),Album Name,Track Duration (ms)"
CSV_ROWS = [
    "spotify:track:1,Enter Sandman,Metallica,Metallica,331000",
    "spotify:track:2,Rooster,Alice In Chains;Layne Staley,Dirt,250000",
    "spotify:track:3,Human Now (feat. Luke Steele),Anyma;Luke Steele,Genesys,200000"
]


def write_csv(name="Rock.csv"):

    path = os.path.join(DATA_FOLDER, name)

    with open(path, "w", encoding="utf-8", newline="") as file_handle:
        file_handle.write("\r\n".join([CSV_HEADER] + CSV_ROWS) + "\r\n")

    return path


def write_audio_file(name="have.mp3", content=b"x" * 4096):
    """A stand-in for a downloaded file, for entries that already hold one."""

    folder = tempfile.mkdtemp(prefix="napstr-have-")
    path = os.path.join(folder, name)

    with open(path, "wb") as file_handle:
        file_handle.write(content)

    return path


class PluginTestCase(unittest.TestCase):
    """A fresh plugin instance against a fresh fake host, per test."""

    def setUp(self):

        SCHEDULER.callbacks.clear()
        SCHEDULER.scheduled.clear()
        SCHEDULER.cancelled.clear()

        self.core = CORE
        self.core.search = fake_nicotine.FakeSearch()
        self.core.downloads = fake_nicotine.FakeDownloads()
        self.core.notifications = fake_nicotine.FakeNotifications()

        self.plugin = napstr_playlist.Plugin()
        self.plugin.core = self.core
        self.plugin.config = CONFIG
        self.plugin.human_name = "NAPSTR Playlist"
        self.plugin.settings["nostr_key"] = SECRET_KEY
        self.plugin.init()

        self.addCleanup(self._stop_plugin)

    def _stop_plugin(self):
        self.plugin._shutting_down = True

    # -- helpers -----------------------------------------------------------

    def run_command(self, argument):
        self.plugin.napstr_command(argument)
        return "\n".join(self.plugin.output_lines)

    def load_playlist(self):
        self.run_command(f'load "{write_csv()}"')
        return self.plugin.playlist

    def advance_pacer(self):
        """Pretend the last search was long ago, so pacing lets the next one go."""

        self.plugin._pacer.note_search(now=time.monotonic() - 100_000)

    def wait_for(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            if predicate():
                return True

            time.sleep(0.02)

        return False


class LoadingTest(PluginTestCase):

    def test_load_creates_a_playlist(self):

        playlist = self.load_playlist()

        self.assertIsNotNone(playlist)
        self.assertEqual(len(playlist.entries), 3)
        self.assertEqual(playlist.title, "Rock")
        self.assertTrue(os.path.isfile(playlist.file_path()))
        self.assertEqual(playlist.entries[0]["title"], "Enter Sandman")

    def test_load_without_playlist_reports_a_usable_error(self):

        message = self.run_command("status")

        self.assertIn("No playlist loaded", message)


class SearchPacingTest(PluginTestCase):
    """The regression that matters: never burst, never overlap."""

    def test_only_one_search_is_sent_at_a_time(self):

        self.load_playlist()
        self.run_command("search all")

        self.assertEqual(len(self.core.search.sent), 1)
        self.assertEqual(len(self.plugin._search_queue), 2)

    def test_queries_are_focused(self):

        self.load_playlist()
        self.run_command("search all")

        _token, term = self.core.search.sent[0]

        self.assertEqual(term, "Metallica Enter Sandman")

    def test_the_next_search_waits_for_the_interval(self):

        self.load_playlist()
        self.run_command("search all")

        # Close the collection window: the second search must still wait
        SCHEDULER.run_due()

        self.assertEqual(len(self.core.search.sent), 1)
        self.assertIsNotNone(self.plugin._search_queue_timer)

        self.advance_pacer()
        SCHEDULER.run_due()

        self.assertEqual(len(self.core.search.sent), 2)

    def test_multi_artist_entries_use_the_primary_artist(self):

        playlist = self.load_playlist()

        self.assertEqual(
            napstr_playlist.napstr_csv.build_search_query(playlist.entries[1]),
            "Alice In Chains Rooster")

    def test_a_hundred_entries_cannot_take_two_seconds_each(self):

        self.load_playlist()
        self.plugin.playlist.entries = self.plugin.playlist.entries * 34  # 102 entries

        for index, entry in enumerate(self.plugin.playlist.entries, start=1):
            entry["position"] = index

        self.run_command("search all")

        # The batch is queued, but at most one search has left the machine
        self.assertEqual(len(self.core.search.sent), 1)
        self.assertEqual(len(self.plugin._search_queue), 101)
        self.assertGreaterEqual(self.plugin._pacer.interval, 45)

        estimate = self.plugin._pacer.estimate_duration(102)

        self.assertGreater(estimate, 100 * 45)

    def test_searching_is_refused_while_paused(self):

        self.load_playlist()
        self.plugin._pause_searches("test pause")

        message = self.run_command("search all")

        self.assertIn("paused", message)
        self.assertEqual(self.core.search.sent, [])


class BanHandlingTest(PluginTestCase):

    def test_the_ban_message_stops_the_batch(self):

        self.load_playlist()
        self.run_command("search all")
        self.assertEqual(len(self.core.search.sent), 1)

        SCHEDULER.emit("log-message", "%x %X", BAN_MESSAGE, None, "default")

        self.assertTrue(self.plugin._pacer.paused)
        self.assertEqual(self.plugin._search_queue, [])

        # Even when the interval has passed, nothing restarts on its own
        self.advance_pacer()
        self.plugin._pump_search_queue()

        self.assertEqual(len(self.core.search.sent), 1)

        # A notification is raised, so the user is not left guessing
        titles = [title for title, _message in self.core.notifications.shown]

        self.assertIn("NAPSTR Playlist", titles)

    def test_resume_restarts_the_queue(self):

        self.load_playlist()
        self.run_command("search all")
        SCHEDULER.emit("log-message", "%x %X", BAN_MESSAGE, None, "default")

        self.run_command("resume")

        self.assertFalse(self.plugin._pacer.paused)

        self.plugin._queue_searches([2])

        # The search that was already in flight still owns the slot, and a
        # search cannot be unsent; no new one goes out until its window closes.
        self.assertEqual(len(self.core.search.sent), 1)

        SCHEDULER.run_due()

        # After an explicit resume the wait has been served, so the queue picks
        # up immediately instead of idling for another interval.
        self.assertEqual(len(self.core.search.sent), 2)

    def test_ordinary_log_lines_do_not_pause(self):

        self.load_playlist()
        self.run_command("search all")

        for message in ("Entry 4 downloaded", "Banned From Heaven.flac", "Rescan complete"):
            SCHEDULER.emit("log-message", "%x %X", message, None, "default")

        self.assertFalse(self.plugin._pacer.paused)

    def test_a_disconnect_during_a_batch_pauses(self):

        self.load_playlist()
        self.run_command("search all")

        self.plugin.server_disconnect_notification("")

        self.assertTrue(self.plugin._pacer.paused)
        self.assertIn("disconnected", self.plugin._pacer.pause_reason)

    def test_a_disconnect_with_nothing_running_is_ignored(self):

        self.load_playlist()

        self.plugin.server_disconnect_notification("")

        self.assertFalse(self.plugin._pacer.paused)

    def test_rate_command_enforces_the_floor(self):

        message = self.run_command("rate 2")

        self.assertIn("Refusing", message)
        self.assertEqual(self.plugin._pacer.interval, 60)
        self.run_command("rate 120")

        self.assertEqual(self.plugin._pacer.interval, 120)

    def test_server_supplied_interval_is_adopted(self):

        self.core.search.wishlist_interval = 90
        self.load_playlist()
        self.run_command("search all")

        self.assertEqual(self.plugin._pacer.interval, 90)

    def test_a_long_server_interval_is_explained_in_the_log(self):
        """The 12 minute case: adopted, but never silently."""

        self.core.search.wishlist_interval = 720
        self.load_playlist()
        self.run_command("search all")

        self.assertEqual(self.plugin._pacer.interval, 720)

        log = "\n".join(self.plugin.log_lines)

        self.assertIn("wishlist wait period is 12 min", log)
        self.assertIn("Respect the server interval", log)

    def test_the_server_interval_can_be_declined(self):

        self.core.search.wishlist_interval = 720
        self.plugin.settings["respect_server_interval"] = False
        self.plugin.settings["search_interval"] = 60
        self.plugin._configure_pacer()

        self.load_playlist()
        self.run_command("search all")

        self.assertEqual(self.plugin._pacer.interval, 60)

    def test_status_names_the_limit_in_force(self):

        self.core.search.wishlist_interval = 720
        self.plugin.settings["search_interval"] = 45
        self.plugin._configure_pacer()
        self.load_playlist()

        status = self.run_command("status")

        self.assertIn("server wait period 12 min", status)
        self.assertIn("your setting 45 s", status)
        self.assertIn("hard floor 45 s", status)

    def test_rate_says_so_when_the_server_is_slower(self):
        """Asking for 45 s and silently getting 12 min is the bug being fixed."""

        self.core.search.wishlist_interval = 720
        self.plugin._configure_pacer()

        message = self.run_command("rate 45")

        self.assertIn("wishlist wait period (12 min) is slower", message)
        self.assertEqual(self.plugin._pacer.interval, 720)

    def test_a_rate_warning_backs_off_without_running_away(self):

        self.load_playlist()
        self.run_command("search all")

        for _ in range(10):
            self.plugin.on_log_message(None, "Too many searches, slow down", None, None)

        self.assertEqual(self.plugin._pacer.requested_interval,
                         napstr_pace.MAX_SEARCH_INTERVAL)

        self.assertIn("slowing down to 2 min", "\n".join(self.plugin.log_lines))

    def test_a_rate_warning_does_not_add_to_the_server_wait_period(self):

        self.core.search.wishlist_interval = 720
        self.plugin.settings["search_interval"] = 60
        self.plugin._configure_pacer()
        self.load_playlist()
        self.run_command("search all")

        self.plugin.on_log_message(None, "Too many searches, slow down", None, None)

        # The requested pace doubles; the twelve minutes that the server asked
        # for stays twelve minutes rather than becoming twenty-four
        self.assertEqual(self.plugin._pacer.requested_interval, 120)
        self.assertEqual(self.plugin._pacer.interval, 720)

    def test_a_setting_change_is_picked_up_mid_batch(self):
        """Editing search_interval in the pane used to need a plugin reload."""

        self.load_playlist()
        self.run_command("search all")

        self.plugin.settings["search_interval"] = 120
        self.plugin._refresh_pace()

        self.assertEqual(self.plugin._pacer.requested_interval, 120)

    def test_picking_up_a_setting_does_not_undo_a_back_off(self):

        self.load_playlist()
        self.run_command("search all")

        self.plugin.on_log_message(None, "Too many searches, slow down", None, None)
        backed_off = self.plugin._pacer.requested_interval

        self.plugin._refresh_pace()

        self.assertEqual(self.plugin._pacer.requested_interval, backed_off)
        self.assertGreater(backed_off, self.plugin.settings["search_interval"])

    def test_a_declined_server_interval_is_still_reported(self):
        """Hiding the number when it is not enforced is how "why so slow" dies."""

        self.core.search.wishlist_interval = 720
        self.plugin.settings["respect_server_interval"] = False
        self.plugin.settings["search_interval"] = 60
        self.plugin._configure_pacer()

        self.assertEqual(self.plugin._pacer.interval, 60)
        self.assertIn("server wait period 12 min, not enforced",
                      self.plugin._pacer.describe_detail())

        self.load_playlist()
        self.assertIn("server wait period 12 min, not enforced", self.run_command("status"))

    def test_auto_does_not_fetch_a_second_copy_of_a_track_we_have(self):
        """The duplicate downloads: an entry with a hash was searched again."""

        playlist = self.load_playlist()
        entry = playlist.entries[0]
        path = write_audio_file()

        entry["file_id"] = "ab" * 32
        entry["local_path"] = path
        entry["candidates"] = [{
            "username": "user1", "path": "Music\\Metallica\\Enter Sandman.mp3",
            "size": 8 * 1024 * 1024, "bitrate": 320, "length": 331
        }]

        self.run_command("auto all")

        self.assertEqual(self.core.downloads.enqueued, [])
        self.assertEqual(entry["status"], "hashed")
        self.assertIn("skipped a duplicate download", entry["notes"] or "")

    def test_auto_all_says_how_many_entries_already_have_files(self):

        playlist = self.load_playlist()
        entry = playlist.entries[0]
        entry["file_id"] = "cd" * 32
        entry["local_path"] = write_audio_file()

        message = self.run_command("auto all")

        self.assertIn("already have a hashed file", message)
        self.assertIn("/napstr forget", message)

    def test_forget_then_auto_does_fetch_a_replacement(self):
        """The escape hatch: asking for a different file is explicit."""

        playlist = self.load_playlist()
        entry = playlist.entries[0]
        entry["file_id"] = "ef" * 32
        entry["local_path"] = write_audio_file()

        self.run_command("forget 1")

        self.assertEqual(entry["file_id"], "")

        entry["candidates"] = [{
            "username": "user2", "path": "Music\\Metallica\\Enter Sandman (2).mp3",
            "size": 8 * 1024 * 1024, "bitrate": 320, "length": 331
        }]

        self.run_command("auto 1")

        self.assertEqual(len(self.core.downloads.enqueued), 1)

    def test_a_missing_file_does_not_block_a_fresh_download(self):
        """A hash whose file was deleted must not stop the entry being filled."""

        playlist = self.load_playlist()
        entry = playlist.entries[0]
        entry["file_id"] = "12" * 32
        entry["local_path"] = os.path.join(tempfile.mkdtemp(), "gone.mp3")
        entry["candidates"] = [{
            "username": "user1", "path": "Music\\Metallica\\Enter Sandman.mp3",
            "size": 8 * 1024 * 1024, "bitrate": 320, "length": 331
        }]

        self.run_command("auto all")

        self.assertEqual(len(self.core.downloads.enqueued), 1)

    def test_reset_says_when_auto_will_skip_the_entries_it_kept(self):
        """A reset keeps the file, so auto will not fetch a replacement.

        Without this the reset looks ignored: the user resets an entry they think
        is wrong, auto skips it, and nothing explains why.
        """

        playlist = self.load_playlist()
        entry = playlist.entries[0]
        entry["file_id"] = "34" * 32
        entry["local_path"] = write_audio_file()
        entry["candidates"] = [{"username": "u", "path": "p"}]

        message = self.run_command("reset all")

        self.assertIn("still hold a file", message)
        self.assertIn("/napstr forget", message)
        self.assertEqual(entry["file_id"], "34" * 32)

    def test_forget_does_not_print_the_reset_hint(self):

        playlist = self.load_playlist()
        playlist.entries[0]["file_id"] = "56" * 32
        playlist.entries[0]["local_path"] = write_audio_file()

        message = self.run_command("forget all")

        self.assertNotIn("still hold a file", message)

    def test_the_choice_records_what_it_was_chosen_on(self):
        """An audit later must be able to re-score the pair it is looking at."""

        playlist = self.load_playlist()
        entry = playlist.entries[0]
        entry["candidates"] = [{
            "username": "user1", "path": "Music\\Metallica\\Enter Sandman.mp3",
            "size": 8 * 1024 * 1024, "bitrate": 320, "length": 331
        }]

        self.run_command("auto 1")

        self.assertEqual(entry["chosen"]["bitrate"], 320)
        self.assertEqual(entry["chosen"]["length"], 331)
        self.assertEqual(entry["chosen"]["username"], "user1")

    def test_bans_and_disconnects_are_not_rate_warnings(self):

        self.load_playlist()
        self.run_command("search all")

        before = self.plugin._pacer.requested_interval
        self.plugin.on_log_message(None, BAN_MESSAGE, None, None)

        self.assertTrue(self.plugin._pacer.paused)
        self.assertEqual(self.plugin._pacer.requested_interval, before)


class SearchApiTest(PluginTestCase):

    def test_the_public_search_api_is_never_used(self):

        # FakeSearch.do_search raises if it is reached
        self.load_playlist()

        self.run_command("search all")

        self.assertEqual(self.core.search.do_search_calls, 0)

    def test_searches_are_released_afterwards(self):

        self.load_playlist()
        self.run_command("search all")

        token, _term = self.core.search.sent[0]
        SCHEDULER.run_due()

        self.assertIn(token, self.core.search.removed)

    def test_a_future_private_api_is_supported(self):

        class PrivateSearch:
            """The naming scheme used after 3.3.x."""

            def __init__(self):
                self._token = 500
                self.searches = {}
                self.sent = []
                self.allowed = []

            def _add_search(self, token, term, mode, room=None, users=None):
                import types  # pylint: disable=import-outside-toplevel

                search = types.SimpleNamespace(token=token, term=term, term_transmitted=term)
                self.searches[token] = search

                return search

            def _send_global_search_request(self, search):
                self.sent.append((search.token, search.term))

            def add_allowed_token(self, token):
                self.allowed.append(token)

            def remove_search(self, token):
                self.searches.pop(token, None)

        private = PrivateSearch()
        self.core.search = private

        self.load_playlist()
        self.run_command("search all")

        self.assertEqual(len(private.sent), 1)
        self.assertEqual(private.sent[0][0], 501)

    def test_an_unknown_api_stops_instead_of_flooding(self):

        class UnknownSearch:
            pass

        self.core.search = UnknownSearch()

        self.load_playlist()
        self.run_command("search all")

        self.assertTrue(self.plugin._pacer.paused)
        self.assertEqual(self.plugin._search_queue, [])
        self.assertIn("no usable search API", self.plugin._pacer.pause_reason)


class DownloadTest(PluginTestCase):

    def test_pick_enqueues_the_chosen_candidate(self):

        playlist = self.load_playlist()
        entry = playlist.entries[0]
        entry["candidates"] = [{
            "username": "user1", "path": "Music\\Metallica\\Enter Sandman.flac",
            "size": 1000, "bitrate": 1005, "length": 331, "score": 0.95
        }]

        self.run_command("pick 1 1")

        self.assertEqual(len(self.core.downloads.enqueued), 1)
        queued = self.core.downloads.enqueued[0]

        self.assertEqual(queued["username"], "user1")
        self.assertEqual(queued["path"], "Music\\Metallica\\Enter Sandman.flac")
        self.assertIn(playlist.id, queued["folder_path"])
        self.assertEqual(entry["status"], "queued")

    def test_download_finished_triggers_hashing(self):

        playlist = self.load_playlist()
        entry = playlist.entries[0]
        entry["chosen"] = {"username": "user1", "path": "Music\\Song.flac"}

        content = b"pretend audio"
        real_path = os.path.join(DATA_FOLDER, "download.flac")

        with open(real_path, "wb") as file_handle:
            file_handle.write(content)

        self.plugin.download_finished_notification("user1", "Music\\Song.flac", real_path)

        self.assertTrue(self.wait_for(lambda: entry["file_id"]))
        self.assertEqual(entry["file_id"], hashlib.sha256(content).hexdigest())
        self.assertEqual(entry["status"], "hashed")

    def test_setpath_hashes_the_file(self):

        playlist = self.load_playlist()
        content = b"another file"
        path = os.path.join(DATA_FOLDER, "manual.mp3")

        with open(path, "wb") as file_handle:
            file_handle.write(content)

        self.run_command(f'setpath 1 "{path}"')

        self.assertTrue(self.wait_for(lambda: playlist.entries[0]["file_id"]))
        self.assertEqual(playlist.entries[0]["file_id"], hashlib.sha256(content).hexdigest())


class DownloadFailureTest(PluginTestCase):
    """A refused or dead download moves on to the next source by itself.

    A real session lost good files to peers that answered "File not shared" and
    then sat at "queued" forever, so the two things tested here are that a dead
    source is replaced and that a live one is never touched.
    """

    def setUp(self):

        super().setUp()

        self.playlist = self.load_playlist()
        self.entry = self.playlist.entries[0]
        self.entry["candidates"] = [
            {"username": "dead1", "path": "Music\\Metallica\\Enter Sandman.mp3",
             "size": 1000, "bitrate": 320, "length": 331, "score": 0.95},
            {"username": "dead2", "path": "Music\\Metallica\\Enter Sandman.mp3",
             "size": 1000, "bitrate": 320, "length": 331, "score": 0.93},
            {"username": "alive", "path": "Music\\Metallica\\Enter Sandman.mp3",
             "size": 1000, "bitrate": 320, "length": 331, "score": 0.91}
        ]

    def fail_current(self, status="File not shared."):
        """Abort the currently chosen transfer the way Nicotine+ would."""

        chosen = self.entry["chosen"]
        transfer = self.core.downloads.fail(chosen["username"], chosen["path"], status)
        SCHEDULER.emit("abort-download", transfer, status, True)

        return transfer

    def test_abort_download_is_subscribed(self):

        # events.connect raises ValueError for a name Nicotine+ does not have,
        # so this also proves the event really exists in 3.3.10.
        self.assertTrue(SCHEDULER.callbacks.get("abort-download"))

    def test_a_dead_source_is_replaced_automatically(self):

        self.run_command("pick 1 1")
        self.assertEqual(self.core.downloads.enqueued[0]["username"], "dead1")

        self.fail_current()

        self.assertEqual(len(self.core.downloads.enqueued), 2)
        self.assertEqual(self.core.downloads.enqueued[1]["username"], "dead2")
        self.assertEqual(self.entry["chosen"]["username"], "dead2")
        self.assertEqual(self.entry["status"], "queued")
        self.assertEqual(self.entry["tried"], [{
            "username": "dead1", "path": "Music\\Metallica\\Enter Sandman.mp3",
            "reason": "File not shared."
        }])

    def test_a_dead_source_is_never_chosen_twice(self):

        self.run_command("pick 1 1")
        self.fail_current()
        self.fail_current()
        self.fail_current()

        usernames = [item["username"] for item in self.core.downloads.enqueued]
        self.assertEqual(usernames, ["dead1", "dead2", "alive"])

    def test_the_hunt_stops_after_three_sources(self):

        self.entry["candidates"].append(
            {"username": "also-alive", "path": "Music\\Metallica\\Enter Sandman.mp3",
             "size": 1000, "bitrate": 320, "length": 331, "score": 0.90})

        self.run_command("pick 1 1")

        for _attempt in range(3):
            self.fail_current()

        # Three failures is the cap: the fourth candidate is left for the user
        self.assertEqual(len(self.core.downloads.enqueued), 3)
        self.assertEqual(self.entry["status"], "failed")
        self.assertIn("/napstr options 1", self.entry["notes"])
        self.assertIn("/napstr options 1", "\n".join(self.plugin.log_lines))

    def test_a_cancelled_download_is_not_retried(self):

        self.run_command("pick 1 1")
        self.fail_current("Cancelled")

        self.assertEqual(len(self.core.downloads.enqueued), 1)
        self.assertEqual(self.entry["status"], "failed")
        self.assertIn("cancelled", self.entry["notes"])

    def test_a_candidate_below_the_threshold_is_left_for_the_user(self):

        self.entry["candidates"] = [
            {"username": "dead1", "path": "Music\\Metallica\\Enter Sandman.mp3",
             "size": 1000, "bitrate": 320, "length": 331, "score": 0.95},
            {"username": "wrong-mix", "path": "Music\\Metallica\\Enter Sandman (live).mp3",
             "size": 1000, "bitrate": 320, "length": 331, "score": 0.42}
        ]

        self.run_command("pick 1 1")
        self.fail_current()

        self.assertEqual(len(self.core.downloads.enqueued), 1)
        self.assertEqual(self.entry["status"], "failed")

    def test_an_entry_that_already_has_a_file_ignores_a_late_abort(self):

        self.run_command("pick 1 1")
        self.entry["file_id"] = "a" * 64
        self.fail_current()

        self.assertEqual(len(self.core.downloads.enqueued), 1)
        self.assertEqual(self.entry["chosen"]["username"], "dead1")

    def test_a_transfer_that_is_not_ours_is_ignored(self):

        self.run_command("pick 1 1")
        SCHEDULER.emit("abort-download", fake_nicotine.FakeTransfer("stranger", "Other.mp3"),
                       "File not shared.", True)

        self.assertEqual(len(self.core.downloads.enqueued), 1)
        self.assertEqual(self.entry["status"], "queued")

    def test_an_abort_for_a_transfer_we_do_not_own_is_ignored(self):

        self.run_command("pick 1 1")

        # Same user and path as our queued download, but a different object -
        # an upload aborting, say. Nicotine+ emits this event for uploads too.
        impostor = fake_nicotine.FakeTransfer("dead1", "Music\\Metallica\\Enter Sandman.mp3")
        SCHEDULER.emit("abort-download", impostor, "File not shared.", True)

        self.assertEqual(len(self.core.downloads.enqueued), 1)
        self.assertEqual(self.entry["status"], "queued")

    def test_a_vanished_transfer_is_caught_by_the_sweep(self):

        self.run_command("pick 1 1")
        self.core.downloads.forget("dead1", "Music\\Metallica\\Enter Sandman.mp3")

        self.plugin._sweep_queued_downloads()

        self.assertEqual(len(self.core.downloads.enqueued), 2)
        self.assertEqual(self.entry["chosen"]["username"], "dead2")

    def test_the_sweep_leaves_a_live_transfer_alone(self):

        self.run_command("pick 1 1")

        self.plugin._sweep_queued_downloads()

        self.assertEqual(len(self.core.downloads.enqueued), 1)
        self.assertEqual(self.entry["status"], "queued")

    def test_disable_cancels_the_sweep(self):

        timer_id = self.plugin._download_sweep_timer

        self.assertIsNotNone(timer_id)

        self.plugin.disable()

        self.assertIn(timer_id, SCHEDULER.cancelled)
        self.assertIsNone(self.plugin._download_sweep_timer)


class OrphansTest(PluginTestCase):
    """Stray files in a playlist's own staging folder, and nothing else."""

    def setUp(self):

        super().setUp()

        self.playlist = self.load_playlist()
        self.folder = self.playlist.paths_for_download()
        os.makedirs(self.folder, exist_ok=True)

        self.kept = self.write_staged("Enter Sandman.mp3", age=600)
        self.orphan = self.write_staged("Rooster (1).mp3", age=600)
        self.partial = self.write_staged(".~Rooster.mp3", age=600)
        self.fresh = self.write_staged("Would.mp3", age=0)

        # The kept file is the one an entry points at
        self.playlist.entries[0]["local_path"] = self.kept

    def write_staged(self, name, age):
        """A file in the staging folder, written ``age`` seconds ago."""

        path = os.path.join(self.folder, name)

        with open(path, "wb") as file_handle:
            file_handle.write(b"x" * 2048)

        when = time.time() - age
        os.utime(path, (when, when))

        return path

    def test_lists_stray_files_and_deletes_nothing(self):

        output = self.run_command("orphans")

        self.assertIn("Rooster (1).mp3", output)
        self.assertIn(".~Rooster.mp3", output)
        self.assertNotIn("Enter Sandman.mp3", output)
        self.assertIn("Nothing was deleted", output)

        self.assertTrue(os.path.isfile(self.orphan))
        self.assertTrue(os.path.isfile(self.partial))

    def test_delete_removes_stray_files_and_keeps_the_rest(self):

        output = self.run_command("orphans delete")

        self.assertIn("Deleted 2 file(s)", output)
        self.assertFalse(os.path.exists(self.orphan))
        self.assertFalse(os.path.exists(self.partial))
        self.assertTrue(os.path.isfile(self.kept))

    def test_a_file_written_a_moment_ago_is_never_deleted(self):

        output = self.run_command("orphans delete")

        self.assertIn("Would.mp3  skipped, written less than a minute ago", output)
        self.assertIn("Deleted 2 file(s)", output)
        self.assertTrue(os.path.isfile(self.fresh))

    def test_a_lone_fresh_file_is_reported_but_kept(self):

        os.remove(self.orphan)
        os.remove(self.partial)

        output = self.run_command("orphans delete")

        self.assertIn("No stray files", output)
        self.assertIn("left alone for now", output)
        self.assertTrue(os.path.isfile(self.fresh))

    def test_a_chosen_download_folder_is_refused(self):

        self.plugin.settings["download_folder"] = self.folder

        output = self.run_command("orphans delete")

        self.assertIn("Refusing", output)
        self.assertTrue(os.path.isfile(self.orphan))

    def test_a_clean_folder_says_so(self):

        self.run_command("orphans delete")
        os.remove(self.fresh)

        self.assertIn("No stray files", self.run_command("orphans"))

    def test_orphans_needs_a_playlist(self):

        self.plugin.playlist = None

        self.assertIn("No playlist loaded", self.run_command("orphans"))


class PublishTest(PluginTestCase):

    def setUp(self):

        super().setUp()

        self.published = []

        def fake_publish(_pool, event, timeout=None):
            self.published.append(event)

            return [napstr_relay.RelayResult("wss://relay.test", accepted=True, message="ok")]

        # No network in tests: the pool is stubbed at the class level
        self._real_publish = napstr_relay.RelayPool.publish
        napstr_relay.RelayPool.publish = fake_publish
        self.addCleanup(self._restore_publish)

    def _restore_publish(self):
        napstr_relay.RelayPool.publish = self._real_publish

    def test_publish_refuses_an_incomplete_playlist(self):

        playlist = self.load_playlist()
        entry = playlist.entries[0]
        entry["file_id"] = "48e5979efa6a56dc3cab293b954ae84e36f464b936bd8c534838189abcc93c68"

        message = self.run_command("publish")

        self.assertIn("Refusing to publish", message)
        self.assertIn("member slot", message)
        self.assertEqual(self.published, [])

    def test_publish_signs_and_sends_a_valid_event(self):

        playlist = self.load_playlist()

        for index, entry in enumerate(playlist.entries):
            entry["file_id"] = f"{index + 1:064x}"

        self.run_command("publish")

        self.assertTrue(self.wait_for(lambda: self.published))

        event = self.published[0]

        self.assertEqual(event["kind"], 30425)
        self.assertEqual(napstr_event.validate_playlist_event(event), [])
        self.assertEqual(
            [tag[1] for tag in event["tags"] if tag[0] == "x"],
            [entry["file_id"] for entry in playlist.entries])

        self.assertTrue(self.wait_for(lambda: playlist.published))
        self.assertEqual(playlist.published[0]["event_id"], event["id"])

    def test_publish_needs_a_key(self):

        self.plugin.settings["nostr_key"] = ""
        self.load_playlist()

        message = self.run_command("publish")

        self.assertIn("No Nostr private key", message)
        self.assertEqual(self.published, [])

    def test_publish_refuses_a_partial_playlist_when_required(self):

        playlist = self.load_playlist()

        for entry in playlist.entries:
            entry["file_id"] = "11" * 32

        message = self.run_command("publish")

        # Three entries, one file ID: duplicate members are dropped by the NIP
        self.assertIn("Refusing to publish", message)
        self.assertEqual(self.published, [])


class FilterTest(PluginTestCase):
    """Excluded formats and the min/max limits, end to end."""

    def test_flac_is_excluded_by_default(self):

        self.assertEqual(self.plugin._excluded_extensions(), ["flac"])
        self.assertIn("excluding flac", self.plugin._filter_summary())

        options = self.plugin.scoring_options()
        score, reasons = napstr_playlist.napstr_match.score_candidate(
            {"title": "Enter Sandman", "artist": "Metallica"},
            "Music\\Metallica\\Enter Sandman.flac", options=options)

        self.assertEqual(score, 0.0)
        self.assertEqual(reasons, ["excluded format: flac"])

    def test_filters_reach_the_summary(self):

        self.plugin.settings["min_bitrate"] = 192
        self.plugin.settings["max_bitrate"] = 320
        self.plugin.settings["min_size_mb"] = 2
        self.plugin.settings["max_size_mb"] = 25
        self.plugin.settings["excluded_formats"] = ["flac", ".wav"]

        summary = self.plugin._filter_summary()

        for expected in ("excluding flac, wav", "min 192 kbps", "max 320 kbps",
                         "min 2 MB", "max 25 MB"):
            self.assertIn(expected, summary)

        options = self.plugin.scoring_options()

        self.assertEqual(options.excluded_extensions, ("flac", "wav"))
        self.assertEqual(options.max_bitrate, 320)
        self.assertEqual(options.min_size_bytes, 2 * 1024 * 1024)

    def test_the_lossless_word_in_the_setting_excludes_everything_lossless(self):
        """Setting "lossless" must reject the formats "flac" alone missed."""

        self.plugin.settings["excluded_formats"] = ["flac", "lossless"]

        excluded = self.plugin._excluded_extensions()

        for extension in ("flac", "aif", "aiff", "wav", "ape", "alac", "dsf"):
            self.assertIn(extension, excluded)

        self.assertIn("excluding", self.plugin._filter_summary())
        self.assertIn("aiff", self.plugin._filter_summary())

    def test_a_flac_pick_is_never_downloaded(self):

        playlist = self.load_playlist()
        entry = playlist.entries[0]
        entry["candidates"] = [{
            "username": "user1", "path": "Music\\Metallica\\Enter Sandman.flac",
            "size": 30 * 1024 * 1024, "bitrate": 1005, "length": 331, "score": 0.99
        }]
        entry["query"] = "Metallica Enter Sandman"

        self.plugin._finish_search_for_position(1)

        self.assertEqual(self.core.downloads.enqueued, [])
        self.assertEqual(entry["status"], "unavailable")
        self.assertIn("rejected by the filters", entry["notes"])
        self.assertIn("excluded format: flac", "\n".join(self.plugin.log_lines))

    def test_a_filtered_candidate_leaves_the_good_one_usable(self):

        playlist = self.load_playlist()
        entry = playlist.entries[0]
        entry["candidates"] = [
            {"username": "lossless", "path": "Music\\Metallica\\Enter Sandman.flac",
             "size": 30 * 1024 * 1024, "bitrate": 1005, "length": 331},
            {"username": "lossy", "path": "Music\\Metallica\\Enter Sandman.mp3",
             "size": 8 * 1024 * 1024, "bitrate": 320, "length": 331}
        ]

        self.plugin._finish_search_for_position(1)

        self.assertEqual(len(self.core.downloads.enqueued), 1)
        self.assertEqual(self.core.downloads.enqueued[0]["username"], "lossy")
        self.assertEqual(entry["status"], "queued")

        # The rejected one is still visible in /napstr options, with its reason
        self.assertEqual(len(entry["candidates"]), 2)
        self.assertEqual(entry["candidates"][1]["score"], 0.0)


class ResetTest(PluginTestCase):

    def test_reset_clears_decisions_and_keeps_file_ids(self):

        playlist = self.load_playlist()
        entry = playlist.entries[0]
        entry["file_id"] = "ab" * 32
        entry["local_path"] = "C:\\Music\\song.mp3"
        entry["candidates"] = [{"username": "u", "path": "p", "score": 0.9}]
        entry["chosen"] = {"username": "u", "path": "p"}

        self.run_command("reset all")

        self.assertTrue(entry["file_id"])
        self.assertEqual(entry["candidates"], [])
        self.assertIsNone(entry["chosen"])
        self.assertEqual(entry["status"], "new")

    def test_forget_also_drops_file_ids(self):

        playlist = self.load_playlist()
        entry = playlist.entries[0]
        entry["file_id"] = "ab" * 32
        entry["local_path"] = "C:\\Music\\song.mp3"

        self.run_command("forget all")

        self.assertEqual(entry["file_id"], "")
        self.assertEqual(entry["local_path"], "")
        self.assertEqual(entry["status"], "new")

    def test_reset_needs_a_target(self):

        self.load_playlist()

        self.assertIn("Usage: /napstr reset", self.run_command("reset"))

    def test_the_reset_survives_a_reload(self):

        playlist = self.load_playlist()
        playlist.entries[0]["file_id"] = "cd" * 32
        playlist.entries[0]["candidates"] = [{"username": "u", "path": "p"}]

        self.run_command("reset all")

        import napstr_state  # pylint: disable=import-outside-toplevel

        reloaded = napstr_state.load_playlist(CONFIG.data_folder_path, playlist.id)

        self.assertEqual(reloaded.entries[0]["file_id"], "cd" * 32)
        self.assertEqual(reloaded.entries[0]["candidates"], [])


class ExclusionTest(PluginTestCase):
    """Tracks that are not on Soulseek at all, and copies you supply yourself.

    Some entries cannot be had from other people - a pressing nobody shares, a
    bootleg. Searching for them again on every batch spends the one resource
    this plugin spends carefully, and they stall a require_full publish, so the
    user can drop them deliberately or point the entry at their own file.
    """

    def setUp(self):

        super().setUp()

        self.playlist = self.load_playlist()
        self.entry = self.playlist.entries[0]

        self.published = []

        def fake_publish(_pool, event, timeout=None):
            self.published.append(event)

            return [napstr_relay.RelayResult("wss://relay.test", accepted=True, message="ok")]

        self._real_publish = napstr_relay.RelayPool.publish
        napstr_relay.RelayPool.publish = fake_publish
        self.addCleanup(self._restore_publish)

    def _restore_publish(self):
        napstr_relay.RelayPool.publish = self._real_publish

    def test_excluding_records_the_reason(self):

        output = self.run_command("exclude 1 not shared by anyone")

        self.assertEqual(self.entry["status"], "excluded")
        self.assertIn("not shared by anyone", self.entry["notes"])
        self.assertIn("Excluded 1", output)

    def test_exclude_needs_a_target(self):

        self.assertIn("Usage: /napstr exclude", self.run_command("exclude"))

    def test_an_excluded_entry_is_not_a_gap_for_require_full(self):

        for index, entry in enumerate(self.playlist.entries):
            entry["file_id"] = f"{index + 1:064x}"

        self.run_command("exclude 2")
        self.run_command("publish")

        self.assertTrue(self.wait_for(lambda: self.published))

        event = self.published[0]

        self.assertEqual(len([tag for tag in event["tags"] if tag[0] == "x"]), 2)
        self.assertEqual(napstr_event.validate_playlist_event(event), [])

    def test_publish_reports_what_it_was_asked_to_leave_out(self):

        for index, entry in enumerate(self.playlist.entries):
            entry["file_id"] = f"{index + 1:064x}"

        self.run_command("exclude 2")
        self.run_command("publish")

        self.assertIn("excluded on purpose", "\n".join(self.plugin.output_lines))

    def test_an_excluded_entry_is_not_searched_again(self):

        self.run_command("exclude 2")
        self.run_command("search all")

        # One search is in flight and the rest wait; entry 2 is in neither
        sent_terms = [term for _token, term in self.core.search.sent]

        self.assertEqual(sent_terms, ["Metallica Enter Sandman"])
        self.assertEqual(self.plugin._search_queue, [3])

    def test_a_file_you_supply_yourself_fills_an_impossible_entry(self):

        self.playlist.set_status(self.entry, "unavailable", "no results")

        path = write_audio_file()
        self.run_command(f'setpath 1 "{path}"')

        self.assertTrue(self.wait_for(lambda: self.entry["file_id"]))

        tracks, skipped = self.playlist.publishable_tracks()

        self.assertEqual([track["position"] for track in tracks], [1])
        self.assertEqual([position for position, _reason in skipped], [2, 3])

    def test_a_reset_leaves_an_excluded_entry_alone(self):

        self.entry["candidates"] = [{"username": "u", "path": "p", "score": 0.9}]
        self.run_command("exclude 1")

        output = self.run_command("reset all")

        self.assertEqual(self.entry["status"], "excluded")
        self.assertIn("Left excluded", output)
        self.assertIn("/napstr include", output)

    def test_forget_also_leaves_an_excluded_entry_alone(self):

        self.entry["file_id"] = "cd" * 32
        self.run_command("exclude 1")
        self.run_command("forget all")

        self.assertEqual(self.entry["status"], "excluded")
        self.assertEqual(self.entry["file_id"], "cd" * 32)

    def test_unskip_all_does_not_resurrect_an_excluded_entry(self):

        self.run_command("exclude 2")
        self.run_command("unskip all")

        self.assertEqual(self.playlist.entries[1]["status"], "excluded")
        self.assertEqual(self.playlist.entries[0]["status"], "new")

    def test_a_scan_does_not_look_for_an_excluded_entry(self):

        self.run_command("exclude 2")
        self.run_command("exclude 3")
        self.plugin.settings["scan_folders"] = [DATA_FOLDER]

        # Only entry 1 is still wanted, so it is the only one scanned for
        message = self.run_command("scan")

        self.assertIn("Scanning 1 folder(s) for 1 entries", message)

    def test_include_puts_an_entry_back(self):

        self.run_command("exclude 2")

        message = self.run_command("include 2")

        self.assertEqual(self.playlist.entries[1]["status"], "new")
        self.assertIn("Put 1", message)

        # And it is searched again
        self.run_command("search all")

        self.assertEqual(self.plugin._search_queue, [2, 3])

    def test_including_an_entry_that_still_has_its_file_says_hashed(self):

        path = write_audio_file()
        self.run_command(f'setpath 1 "{path}"')

        self.assertTrue(self.wait_for(lambda: self.entry["file_id"]))

        self.run_command("exclude 1")
        self.run_command("include 1")

        self.assertEqual(self.entry["status"], "hashed")

    def test_including_something_that_was_never_excluded_says_so(self):

        message = self.run_command("include 2")

        self.assertIn("Not excluded", message)
        self.assertEqual(self.playlist.entries[1]["status"], "new")

    def test_an_excluded_entry_with_a_file_is_shown_as_excluded(self):

        path = write_audio_file()
        self.run_command(f'setpath 1 "{path}"')

        self.assertTrue(self.wait_for(lambda: self.entry["file_id"]))

        self.run_command("exclude 1")

        listing = self.run_command("list excluded")

        self.assertIn("[excluded", listing)
        self.assertNotIn("hashed:", listing)

    def test_an_exclusion_survives_a_reload(self):

        self.run_command("exclude 1 bob has the only copy")

        import napstr_state  # pylint: disable=import-outside-toplevel

        reloaded = napstr_state.load_playlist(CONFIG.data_folder_path, self.playlist.id)

        self.assertEqual(reloaded.entries[0]["status"], "excluded")
        self.assertIn("bob has the only copy", reloaded.entries[0]["notes"])


class SkipAndStatusTest(PluginTestCase):

    def test_skip_and_unskip(self):

        playlist = self.load_playlist()

        self.run_command("skip 2")

        self.assertEqual(playlist.entries[1]["status"], "skipped")

        self.run_command("unskip 2")

        self.assertEqual(playlist.entries[1]["status"], "new")

    def test_status_reports_the_pace(self):

        self.load_playlist()
        self.run_command("status")

        text = "\n".join(self.plugin.output_lines)

        self.assertIn("Search pace", text)
        self.assertIn("one search every", text)
        self.assertIn("Filters", text)
        self.assertIn("excluding flac", text)

    def test_help_lists_the_new_commands(self):

        self.run_command("help")

        text = "\n".join(self.plugin.output_lines)

        self.assertIn("/napstr resume | rate <seconds>", text)


if __name__ == "__main__":
    unittest.main()
