# SPDX-FileCopyrightText: 2026 StateAntigen
# SPDX-License-Identifier: 0BSD
"""Search pacing and flood-ban protection.

Automated searching is the one thing this plugin does that can get a Nicotine+
account banned. The Soulseek server says so itself::

    You have been banned for 30 minutes. This is usually the result of doing
    too many operations at once. [...] Do not repeat the same text several
    times in a row. Do not quickly repeat a search.

A hundred searches at two second intervals trips that, so every search this
plugin sends goes through :class:`SearchPacer`: one search at a time, a hard
minimum gap between them, an optional server-supplied floor, and a latched stop
that no queued work can restart on its own once a ban or a disconnect is seen.

Standard library only, so it is unit testable outside Nicotine+.
"""

import time

__all__ = [
    "BAN_PHRASES",
    "DEFAULT_SEARCH_INTERVAL",
    "MAX_SEARCH_INTERVAL",
    "MINIMUM_SEARCH_INTERVAL",
    "RATE_WARNING_PHRASES",
    "SearchPacer",
    "human_duration",
    "looks_like_ban",
    "looks_like_rate_warning",
]

# A gap this plugin will never go below, whatever the settings say. The ban
# was earned at roughly 26 searches per minute, so the floor is deliberately
# about twenty times slower than that.
MINIMUM_SEARCH_INTERVAL = 45

# Default gap. The server publishes a "wishlist wait period" for automated
# searches, which is normally 60 seconds, so this matches that pace.
DEFAULT_SEARCH_INTERVAL = 60

# Ceiling for backing off after a server warning about search volume. Growth is
# capped because a hundred track playlist already takes hours at ten minutes per
# search, and a pace nobody will sit through gets switched off - which is worse
# protection than a slow one. A pace the user asked for is never lowered.
MAX_SEARCH_INTERVAL = 600

# Phrases from the server that mean "stop now". Matched case-insensitively
# against every log line that passes through Nicotine+.
BAN_PHRASES = (
    "you have been banned",
    "banned for",
    "too many operations",
    "do not quickly repeat a search",
    "you are banned",
    "flooding"
)

# Softer warnings: worth slowing down for, but not worth stopping.
RATE_WARNING_PHRASES = (
    "too many searches",
    "searching too fast",
    "please slow down",
    "rate limit"
)


def _normalize(text):
    return " ".join(str(text or "").lower().split())


def looks_like_ban(text):
    """True when a log line looks like a service ban, not a song lyric."""

    normalized = _normalize(text)

    if not normalized:
        return False

    return any(phrase in normalized for phrase in BAN_PHRASES)


def looks_like_rate_warning(text):

    normalized = _normalize(text)

    return bool(normalized) and any(phrase in normalized for phrase in RATE_WARNING_PHRASES)


def human_duration(seconds):

    seconds = int(max(seconds, 0))

    if seconds < 60:
        return f"{seconds} s"

    minutes, remainder = divmod(seconds, 60)

    if minutes < 60:
        return f"{minutes} min" if not remainder else f"{minutes} min {remainder} s"

    hours, minutes = divmod(minutes, 60)

    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


class SearchPacer:
    """Decides when the next automated search may be sent."""

    def __init__(self, interval=DEFAULT_SEARCH_INTERVAL, server_interval=0, minimum=MINIMUM_SEARCH_INTERVAL):

        self.minimum = max(float(minimum), 1.0)
        self.requested_interval = float(interval or DEFAULT_SEARCH_INTERVAL)
        self.server_interval = float(server_interval or 0)
        self.last_search_at = None
        self.paused = False
        self.pause_reason = ""
        self.paused_at = None

    # -- configuration -----------------------------------------------------

    @property
    def interval(self):
        """The effective gap: the strictest of the three floors."""

        return max(self.minimum, self.requested_interval, self.server_interval)

    def set_interval(self, interval):

        self.requested_interval = float(interval or DEFAULT_SEARCH_INTERVAL)

    def set_server_interval(self, server_interval):
        """Adopt the server's own automated-search pace when it tells us one."""

        self.server_interval = max(float(server_interval or 0), 0)

    def slow_down(self, factor=2.0, ceiling=MAX_SEARCH_INTERVAL):
        """Back off after a server warning and return the new requested pace.

        Doubles what the *user* asked for rather than the effective interval: if
        the server's own wait period is what is slowing us down, doubling on top
        of it would add minutes for nothing. Never lowers the requested pace, and
        never grows past ``ceiling`` - see :data:`MAX_SEARCH_INTERVAL`.
        """

        target = self.requested_interval * float(factor)
        limit = max(float(ceiling), self.requested_interval)

        self.set_interval(min(target, limit))

        return self.requested_interval

    # -- pacing ------------------------------------------------------------

    def note_search(self, now=None):

        self.last_search_at = time.monotonic() if now is None else now

    def seconds_until_allowed(self, now=None):

        if self.paused:
            return float("inf")

        if self.last_search_at is None:
            return 0.0

        now = time.monotonic() if now is None else now
        elapsed = now - self.last_search_at

        return max(self.interval - elapsed, 0.0)

    def can_search(self, now=None):
        return self.seconds_until_allowed(now) <= 0

    def estimate_duration(self, num_searches):
        """Seconds a batch of ``num_searches`` would take at the current pace."""

        if num_searches <= 1:
            return 0.0

        return self.interval * (num_searches - 1)

    # -- stopping ----------------------------------------------------------

    def pause(self, reason, now=None):
        """Latched stop: only :meth:`resume` clears it."""

        self.paused = True
        self.pause_reason = reason
        self.paused_at = time.monotonic() if now is None else now

    def resume(self, now=None):

        self.paused = False
        self.pause_reason = ""
        self.paused_at = None
        self.last_search_at = None if now is None else now

    def describe(self):

        if self.paused:
            return f"paused ({self.pause_reason})"

        return f"one search every {human_duration(self.interval)}"

    def describe_detail(self):
        """Spell out which of the three floors produced the current pace.

        "One search every 12 min" is a surprising thing to read in a log, and
        without the inputs there is no way to tell whether a setting, the
        server's own wait period, or a back-off after a warning caused it.
        """

        parts = [f"your setting {human_duration(self.requested_interval)}"]

        if self.server_interval:
            parts.append(
                f"server wait period {human_duration(self.server_interval)}")

        parts.append(f"hard floor {human_duration(self.minimum)}")

        return f"{self.describe()} ({', '.join(parts)})"

    def limiting_reason(self):
        """Name the floor that is holding the pace up, or ``""`` when none is."""

        if self.paused:
            return self.pause_reason

        if self.server_interval >= max(self.requested_interval, self.minimum):
            return "server_wait_period"

        if self.minimum >= self.requested_interval:
            return "hard_floor"

        return ""
