# SPDX-FileCopyrightText: 2026 StateAntigen
# SPDX-License-Identifier: 0BSD
"""Tests for search pacing and flood-ban detection.

These guard the one behaviour in the plugin that can get an account banned.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "plugin", "napstr_playlist"))

import napstr_pace  # noqa: E402  pylint: disable=wrong-import-position

# The message the Soulseek server sent after a hundred searches in 215 seconds
BAN_MESSAGE = (
    "You have been banned for 30 minutes. This is usually the result of doing too many "
    "operations at once. Do not flood chat rooms or private chat with many messages. "
    "Do not repeat the same text several times in a row. Do not quickly repeat a search."
)


class BanDetectionTest(unittest.TestCase):

    def test_detects_the_real_ban_message(self):
        self.assertTrue(napstr_pace.looks_like_ban(BAN_MESSAGE))

    def test_detects_short_forms(self):
        for text in ("You have been banned for 30 minutes.",
                     "System Message: You are banned",
                     "too many operations at once",
                     "Do not quickly repeat a search"):
            with self.subTest(text=text):
                self.assertTrue(napstr_pace.looks_like_ban(text))

    def test_ignores_ordinary_traffic(self):
        for text in ("",
                     None,
                     "Banned From Heaven - Children of Bodom.flac",
                     "Searching for wishlist item \"banned\"",
                     "Download finished: user x, file y",
                     "Entry 4 downloaded"):
            with self.subTest(text=text):
                self.assertFalse(napstr_pace.looks_like_ban(text))

    def test_detects_soft_warnings(self):
        self.assertTrue(napstr_pace.looks_like_rate_warning("Too many searches, slow down"))
        self.assertFalse(napstr_pace.looks_like_rate_warning("Download finished"))


class IntervalTest(unittest.TestCase):

    def test_default_matches_the_server_pace(self):

        pacer = napstr_pace.SearchPacer()

        self.assertEqual(pacer.interval, napstr_pace.DEFAULT_SEARCH_INTERVAL)
        self.assertGreaterEqual(napstr_pace.DEFAULT_SEARCH_INTERVAL, napstr_pace.MINIMUM_SEARCH_INTERVAL)

    def test_floor_cannot_be_undercut(self):

        # The settings ask for two seconds; the pacer refuses to go that fast
        pacer = napstr_pace.SearchPacer(interval=2)

        self.assertEqual(pacer.interval, napstr_pace.MINIMUM_SEARCH_INTERVAL)

    def test_server_interval_wins_when_larger(self):

        pacer = napstr_pace.SearchPacer(interval=60)
        pacer.set_server_interval(120)

        self.assertEqual(pacer.interval, 120)

    def test_a_larger_setting_is_honoured(self):

        pacer = napstr_pace.SearchPacer(interval=300)

        self.assertEqual(pacer.interval, 300)

    def test_a_burst_is_impossible(self):
        """The regression that matters: 100 searches must not take 215 seconds."""

        pacer = napstr_pace.SearchPacer()
        now = 1000.0

        num_searches = 100
        elapsed = 0.0

        for _ in range(num_searches):
            while not pacer.can_search(now):
                now += 1.0
                elapsed += 1.0

            pacer.note_search(now)
            now += 25.0  # the collection window
            elapsed += 25.0

        self.assertGreaterEqual(elapsed, 100 * 45.0)
        self.assertGreater(elapsed, 215.0 * 10)


class BackOffTest(unittest.TestCase):
    """Slowing down after a warning must be visible and must not run away."""

    def test_doubles_what_the_user_asked_for(self):

        pacer = napstr_pace.SearchPacer(interval=60)

        self.assertEqual(pacer.slow_down(), 120.0)
        self.assertEqual(pacer.slow_down(), 240.0)

    def test_doubles_the_requested_pace_not_the_effective_one(self):
        """A server wait period of ten minutes must not become twenty."""

        pacer = napstr_pace.SearchPacer(interval=60)
        pacer.set_server_interval(600)
        pacer.slow_down()

        self.assertEqual(pacer.requested_interval, 120.0)
        self.assertEqual(pacer.interval, 600.0)

    def test_growth_is_capped(self):

        pacer = napstr_pace.SearchPacer(interval=60)

        for _ in range(20):
            pacer.slow_down()

        self.assertEqual(pacer.requested_interval, napstr_pace.MAX_SEARCH_INTERVAL)
        self.assertLess(pacer.interval, 3600.0)

    def test_a_slower_setting_is_never_lowered(self):

        pacer = napstr_pace.SearchPacer(interval=1800)
        pacer.slow_down()

        self.assertEqual(pacer.requested_interval, 1800.0)

    def test_the_cap_is_above_the_default_and_the_floor(self):

        self.assertGreater(napstr_pace.MAX_SEARCH_INTERVAL, napstr_pace.DEFAULT_SEARCH_INTERVAL)
        self.assertGreater(napstr_pace.MAX_SEARCH_INTERVAL, napstr_pace.MINIMUM_SEARCH_INTERVAL)


class DescriptionTest(unittest.TestCase):
    """A 12 minute pace must explain itself in the log."""

    def test_detail_names_every_input(self):

        pacer = napstr_pace.SearchPacer(interval=45)
        pacer.set_server_interval(720)

        detail = pacer.describe_detail()

        self.assertIn("one search every 12 min", detail)
        self.assertIn("your setting 45 s", detail)
        self.assertIn("server wait period 12 min", detail)
        self.assertIn("hard floor 45 s", detail)

    def test_detail_omits_the_server_part_when_there_is_none(self):

        pacer = napstr_pace.SearchPacer(interval=60)

        detail = pacer.describe_detail()

        self.assertIn("one search every 1 min", detail)
        self.assertNotIn("server wait period", detail)

    def test_the_limiting_floor_is_named(self):

        server_bound = napstr_pace.SearchPacer(interval=60)
        server_bound.set_server_interval(720)

        self.assertEqual(server_bound.limiting_reason(), "server_wait_period")

        floor_bound = napstr_pace.SearchPacer(interval=10)

        self.assertEqual(floor_bound.limiting_reason(), "hard_floor")

        own_pace = napstr_pace.SearchPacer(interval=120)

        self.assertEqual(own_pace.limiting_reason(), "")


class PacingTest(unittest.TestCase):

    def test_first_search_is_allowed_immediately(self):

        pacer = napstr_pace.SearchPacer()

        self.assertTrue(pacer.can_search(now=0.0))
        self.assertEqual(pacer.seconds_until_allowed(now=0.0), 0.0)

    def test_second_search_waits(self):

        pacer = napstr_pace.SearchPacer(interval=60)
        pacer.note_search(now=100.0)

        self.assertFalse(pacer.can_search(now=100.0))
        self.assertFalse(pacer.can_search(now=159.0))
        self.assertEqual(pacer.seconds_until_allowed(now=130.0), 30.0)
        self.assertTrue(pacer.can_search(now=160.0))

    def test_estimate(self):

        pacer = napstr_pace.SearchPacer(interval=60)

        self.assertEqual(pacer.estimate_duration(0), 0)
        self.assertEqual(pacer.estimate_duration(1), 0)
        self.assertEqual(pacer.estimate_duration(101), 6000)


class PauseTest(unittest.TestCase):

    def test_pause_is_latched(self):

        pacer = napstr_pace.SearchPacer(interval=60)

        pacer.pause("banned")

        self.assertTrue(pacer.paused)
        self.assertFalse(pacer.can_search(now=10_000.0))
        self.assertEqual(pacer.seconds_until_allowed(now=10_000.0), float("inf"))
        self.assertIn("banned", pacer.describe())

    def test_resume_clears_the_wait(self):

        pacer = napstr_pace.SearchPacer(interval=60)
        pacer.note_search(now=0.0)
        pacer.pause("server disconnected")

        self.assertFalse(pacer.can_search(now=1000.0))

        pacer.resume()

        self.assertFalse(pacer.paused)
        self.assertTrue(pacer.can_search(now=1000.0))
        self.assertIn("one search every", pacer.describe())

    def test_interval_can_be_raised_while_paused(self):

        pacer = napstr_pace.SearchPacer()
        pacer.set_interval(pacer.interval * 2)

        self.assertEqual(pacer.interval, napstr_pace.DEFAULT_SEARCH_INTERVAL * 2)


class HumanDurationTest(unittest.TestCase):

    def test_formats(self):

        self.assertEqual(napstr_pace.human_duration(0), "0 s")
        self.assertEqual(napstr_pace.human_duration(45), "45 s")
        self.assertEqual(napstr_pace.human_duration(60), "1 min")
        self.assertEqual(napstr_pace.human_duration(150), "2 min 30 s")
        self.assertEqual(napstr_pace.human_duration(3600), "1 h")
        self.assertEqual(napstr_pace.human_duration(7200), "2 h")
        self.assertEqual(napstr_pace.human_duration(-5), "0 s")


if __name__ == "__main__":
    unittest.main()
