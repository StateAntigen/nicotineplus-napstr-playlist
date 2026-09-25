# SPDX-FileCopyrightText: 2026 StateAntigen
# SPDX-License-Identifier: 0BSD
"""A fake Nicotine+ host, so the plugin can be exercised without Nicotine+.

The plugin runs inside a frozen interpreter, so the fastest way to catch a
runtime mistake (a wrong attribute name, a missing initialiser) is to run the
real plugin code against stand-ins for the few Nicotine+ objects it touches.

``FakeSearch`` deliberately mimics **Nicotine+ 3.3.10**, whose public attributes
are ``token``/``add_search``/``do_global_search``. The earlier versions of this
plugin were written against a different naming scheme and degraded silently;
:meth:`FakeSearch.do_search` exists purely to fail the test if the plugin ever
falls back to the tab-spamming public API again.
"""

import types


class Scheduler:
    """Stand-in for ``pynicotine.events.events`` schedule/cancel/emit."""

    def __init__(self):
        self.callbacks = {}
        self.scheduled = []
        self.cancelled = set()
        self._next_id = 0

    # -- events ------------------------------------------------------------

    def connect(self, event_name, function):
        self.callbacks.setdefault(event_name, []).append(function)

    def emit(self, event_name, *args, **kwargs):
        for function in list(self.callbacks.get(event_name, [])):
            function(*args, **kwargs)

    # -- scheduling --------------------------------------------------------

    def schedule(self, delay, callback, callback_args=None, repeat=False):
        self._next_id += 1
        self.scheduled.append({
            "id": self._next_id,
            "delay": delay,
            "callback": callback,
            "args": tuple(callback_args or ()),
            "repeat": repeat
        })

        return self._next_id

    def cancel_scheduled(self, event_id):
        self.cancelled.add(event_id)

    def invoke_main_thread(self, callback, *args, **kwargs):
        """Niladic marshalling is synchronous here, which keeps tests simple."""

        callback(*args, **kwargs)

    def pending(self):
        return [event for event in self.scheduled if event["id"] not in self.cancelled]

    def run_due(self):
        """Fire every pending scheduled callback once."""

        for event in self.pending():
            if not event["repeat"]:
                self.cancelled.add(event["id"])

            event["callback"](*event["args"])

        return len(self.pending())


class FakeSearch:
    """Nicotine+ 3.3.10's ``Search``, as far as this plugin touches it."""

    def __init__(self):
        self.token = 100
        self.searches = {}
        self.wishlist_interval = 0
        self.sent = []
        self.removed = []
        self.do_search_calls = 0

    def add_search(self, term, mode="global", room=None, users=None, is_ignored=False):
        search = types.SimpleNamespace(
            token=self.token, term=term, term_transmitted=term, mode=mode, is_ignored=is_ignored)
        self.searches[self.token] = search

        return search

    def do_global_search(self, text):
        self.sent.append((self.token, text))

    def remove_search(self, token):
        self.removed.append(token)
        self.searches.pop(token, None)

    def do_search(self, *_args, **_kwargs):
        """The public API: one GUI search tab and one config write per call."""

        self.do_search_calls += 1
        raise AssertionError(
            "the plugin fell back to Search.do_search, which spams the UI and the server")


class FakeTransfer:
    """One entry of ``core.downloads.transfers``, as far as the plugin reads it."""

    def __init__(self, username, virtual_path, status="Queued"):
        self.username = username
        self.virtual_path = virtual_path
        self.status = status
        self.filename = virtual_path.rsplit("\\", 1)[-1]


class FakeDownloads:
    """Nicotine+ 3.3.10's ``Downloads``.

    ``transfers`` is keyed by ``username + virtual_path`` and a second enqueue
    for the same pair is ignored, both of which are real 3.3.10 behaviour that
    the plugin's failure handling has to work with rather than around.
    """

    def __init__(self):
        self.enqueued = []
        self.transfers = {}

    def enqueue_download(self, username, virtual_path, folder_path=None, size=0,
                         file_attributes=None, bypass_all=False):
        self.enqueued.append({
            "username": username, "path": virtual_path, "folder_path": folder_path,
            "size": size, "attributes": file_attributes
        })

        key = username + virtual_path

        if key not in self.transfers:
            self.transfers[key] = FakeTransfer(username, virtual_path)

    def fail(self, username, virtual_path, status):
        """Mark a queued transfer dead, the way Nicotine+ does before aborting."""

        key = username + virtual_path
        self.transfers.setdefault(key, FakeTransfer(username, virtual_path)).status = status

        return self.transfers[key]

    def forget(self, username, virtual_path):
        """Drop a transfer entirely, the way a cleared entry disappears."""

        self.transfers.pop(username + virtual_path, None)


class FakeNotifications:

    def __init__(self):
        self.shown = []

    def show_notification(self, message, title=None):
        self.shown.append((title, message))


class FakeCore:

    def __init__(self):
        self.search = FakeSearch()
        self.downloads = FakeDownloads()
        self.notifications = FakeNotifications()
        self.chatrooms = types.SimpleNamespace(joined_rooms={}, echo_message=lambda *a, **k: None)
        self.privatechat = types.SimpleNamespace(show_user=lambda *a, **k: None)


class FakeConfig:

    def __init__(self, data_folder_path):
        self.data_folder_path = data_folder_path


def install(data_folder_path, search=None):
    """Install the fake ``pynicotine`` package and return the pieces.

    Returns ``(scheduler, core, config, base_plugin)``. Must be called before
    importing the plugin module.
    """

    scheduler = Scheduler()
    core = FakeCore()

    if search is not None:
        core.search = search

    config = FakeConfig(data_folder_path)

    pynicotine = types.ModuleType("pynicotine")
    events_module = types.ModuleType("pynicotine.events")
    pluginsystem = types.ModuleType("pynicotine.pluginsystem")
    slskmessages = types.ModuleType("pynicotine.slskmessages")

    events_module.events = scheduler

    class BasePlugin:
        """The parts of ``pynicotine.pluginsystem.BasePlugin`` this plugin uses."""

        parent = None
        config = None
        core = None
        human_name = "NAPSTR Playlist"

        def __init__(self):
            self.messages = []
            self.log_lines = []
            self.output_lines = []

        def log(self, msg, msg_args=None):
            self.log_lines.append(msg if msg_args is None else msg % msg_args)

        def output(self, text):
            self.output_lines.append(text)

        def echo_message(self, text, message_type="local"):
            self.output_lines.append(text)

    pluginsystem.BasePlugin = BasePlugin

    def increment_token(token):
        return token + 1

    def initial_token():
        return 100

    class FileAttribute:
        BITRATE = 0
        DURATION = 1
        VBR = 2
        ENCODER = 3
        SAMPLE_RATE = 4
        BIT_DEPTH = 5

    slskmessages.increment_token = increment_token
    slskmessages.initial_token = initial_token
    slskmessages.FileAttribute = FileAttribute

    pynicotine.events = events_module
    pynicotine.pluginsystem = pluginsystem
    pynicotine.slskmessages = slskmessages

    import sys  # pylint: disable=import-outside-toplevel

    sys.modules["pynicotine"] = pynicotine
    sys.modules["pynicotine.events"] = events_module
    sys.modules["pynicotine.pluginsystem"] = pluginsystem
    sys.modules["pynicotine.slskmessages"] = slskmessages

    return scheduler, core, config, BasePlugin
