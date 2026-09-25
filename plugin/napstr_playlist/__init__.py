# SPDX-FileCopyrightText: 2026 StateAntigen
# SPDX-License-Identifier: 0BSD
"""NAPSTR Playlist - import a Spotify playlist exported with Exportify,
match every entry against Soulseek, download it, hash it, and publish the
result as a NAPSTR playlist (Nostr kind 30425).

The workflow, driven by the ``/napstr`` command:

    /napstr load "C:\\Users\\me\\Downloads\\Rock.csv"
    /napstr search all        (or)  /napstr auto all
    /napstr list
    /napstr options 12        /napstr pick 12 2
    /napstr scan              (reuse files you already have)
    /napstr hash all
    /napstr publish

See README.md in the plugin folder for the full command reference.
"""

import json
import os
import threading
import time
import zipfile

from pynicotine.events import events
from pynicotine.pluginsystem import BasePlugin

import napstr_csv
import napstr_event
import napstr_match
import napstr_pace
import napstr_relay
import napstr_state

try:
    from pynicotine.slskmessages import FileAttribute

except ImportError:  # pragma: no cover - keeps static checks happy
    class FileAttribute:
        BITRATE = 0
        DURATION = 1
        VBR = 2
        ENCODER = 3
        SAMPLE_RATE = 4
        BIT_DEPTH = 5


LIST_PAGE_SIZE = 25
MAX_SCAN_FILES = 20000
MAX_CANDIDATES_PER_ENTRY = 200

FORMAT_CHOICES = ("any", "flac", "mp3", "ogg", "opus", "m4a", "wav")

# Statuses that never need another search
INACTIVE_STATUSES = (
    napstr_state.STATUS_HASHED, napstr_state.STATUS_SKIPPED, napstr_state.STATUS_DOWNLOADED,
    napstr_state.STATUS_DOWNLOADING, napstr_state.STATUS_QUEUED
)


class Plugin(BasePlugin):
    """NAPSTR playlist importer, matcher, hasher and publisher."""

    def __init__(self, *args, **kwargs):

        super().__init__(*args, **kwargs)

        self.settings = {
            "title": "",
            "author_tags": "",
            "playlist_id": "",
            "client_tag": napstr_event.CLIENT_NAME,
            "query_template": "{artist} {title}",
            "auto_pick": True,
            "auto_pick_threshold": 0.8,
            "search_timeout": 25,
            "search_interval": napstr_pace.DEFAULT_SEARCH_INTERVAL,
            "respect_server_interval": True,
            "preferred_format": "any",
            "excluded_formats": ["flac"],
            "min_bitrate": 0,
            "max_bitrate": 0,
            "min_size_mb": 0,
            "max_size_mb": 0,
            "duration_tolerance": 10,
            "download_folder": "",
            "scan_folders": [],
            "require_full": True,
            "relays": list(napstr_relay.DEFAULT_RELAYS),
            "nostr_key": "",
            "relay_timeout": 15,
            "insecure_tls": False
        }

        self.metasettings = {
            "title": {
                "description": "Playlist title published in the 'title' tag",
                "group": "Playlist",
                "type": "string"
            },
            "author_tags": {
                "description": "Your own search words, comma separated (never invented by the plugin)",
                "group": "Playlist",
                "type": "string"
            },
            "playlist_id": {
                "description": "Playlist UUID (blank generates one; keep it to revise the same playlist)",
                "group": "Playlist",
                "type": "string"
            },
            "client_tag": {
                "description": "Value of the optional 'client' provenance tag",
                "group": "Playlist",
                "type": "string"
            },
            "query_template": {
                "description": "Soulseek query template ({artist}, {title}, {album}, {album_artist})",
                "group": "Matching",
                "type": "string"
            },
            "auto_pick": {
                "description": "Download the best match without asking when it is confident",
                "group": "Matching",
                "type": "bool"
            },
            "auto_pick_threshold": {
                "description": "Confidence needed for an automatic download",
                "group": "Matching",
                "type": "float", "minimum": 0.5, "maximum": 1.0, "stepsize": 0.05
            },
            "search_timeout": {
                "description": "Seconds to collect results before deciding",
                "group": "Matching",
                "type": "integer", "minimum": 5, "maximum": 300
            },
            "search_interval": {
                "description": ("Seconds between search requests. The server bans accounts for "
                                "flooding, so this never goes below 45 seconds; 60 s matches the "
                                "server's own automated-search pace"),
                "group": "Matching",
                "type": "integer", "minimum": napstr_pace.MINIMUM_SEARCH_INTERVAL, "maximum": 600
            },
            "respect_server_interval": {
                "description": ("Also wait out the server's own wishlist wait period (it can be "
                                "several minutes; /napstr status names the limit in force)"),
                "group": "Matching",
                "type": "bool"
            },
            "preferred_format": {
                "description": "Preferred audio format:",
                "group": "Matching",
                "type": "dropdown",
                "options": FORMAT_CHOICES
            },
            "excluded_formats": {
                "description": "Formats never to download (e.g. flac):",
                "group": "Matching",
                "type": "list string"
            },
            "min_bitrate": {
                "description": "Minimum bitrate in kbps (0 accepts anything)",
                "group": "Matching",
                "type": "integer", "minimum": 0, "maximum": 1411
            },
            "max_bitrate": {
                "description": "Maximum bitrate in kbps (0 accepts anything)",
                "group": "Matching",
                "type": "integer", "minimum": 0, "maximum": 10000
            },
            "min_size_mb": {
                "description": "Minimum file size in MB (0 accepts anything)",
                "group": "Matching",
                "type": "integer", "minimum": 0, "maximum": 10000
            },
            "max_size_mb": {
                "description": "Maximum file size in MB (0 accepts anything)",
                "group": "Matching",
                "type": "integer", "minimum": 0, "maximum": 10000
            },
            "duration_tolerance": {
                "description": "Accepted duration difference in seconds",
                "group": "Matching",
                "type": "integer", "minimum": 0, "maximum": 120
            },
            "download_folder": {
                "description": "Folder for playlist downloads (blank uses a per-playlist staging folder)",
                "group": "Files",
                "type": "string"
            },
            "scan_folders": {
                "description": "Folders scanned by /napstr scan for files you already have:",
                "group": "Files",
                "type": "list string"
            },
            "require_full": {
                "description": "Refuse to publish unless every entry has a file ID (turn off to publish partial playlists)",
                "group": "Publishing",
                "type": "bool"
            },
            "relays": {
                "description": "Nostr relays to publish to:",
                "group": "Nostr",
                "type": "list string"
            },
            "nostr_key": {
                "description": "Private key as nsec1... or 64 hex characters",
                "group": "Nostr",
                "type": "string"
            },
            "relay_timeout": {
                "description": "Relay timeout in seconds",
                "group": "Nostr",
                "type": "integer", "minimum": 5, "maximum": 120
            },
            "insecure_tls": {
                "description": "Skip TLS certificate verification (only if a relay fails to connect)",
                "group": "Nostr",
                "type": "bool"
            }
        }

        self.commands = {
            "napstr": {
                "aliases": ["nap"],
                "callback": self.napstr_command,
                "description": "NAPSTR playlists: import, match, hash and publish",
                "group": "NAPSTR Playlist",
                "parameters": ["[action]", "[arguments..]"]
            }
        }

        self.playlist = None                 # napstr_state.PlaylistState
        self._search_positions = {}          # search token -> entry position
        self._search_timers = {}             # search token -> scheduled event id
        self._search_token = None            # the single search currently in flight
        self._search_queue = []              # positions waiting to be searched
        self._search_queue_timer = None
        self._search_api_name = None         # which Nicotine+ search API was detected
        self._pacer = napstr_pace.SearchPacer()  # one search at a time, never in bursts
        self._configured_interval = None     # the setting the pacer was built from
        self._auto_positions = set()         # positions that should auto-pick
        self._hash_cancellations = {}        # position -> threading.Event
        self._publishing = False
        self._shutting_down = False

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def init(self):

        events.connect("file-search-response", self.on_file_search_response)
        events.connect("log-message", self.on_log_message)
        self._configure_pacer()
        self.log("Loaded. Type /napstr help for the command list.")

        playlist_id = str(self.settings.get("playlist_id") or "").strip()

        if playlist_id:
            state = napstr_state.load_playlist(self.data_folder_path, playlist_id)

            if state is not None:
                self.playlist = state

    def shutdown_notification(self):

        self._shutting_down = True

        for cancel_event in self._hash_cancellations.values():
            cancel_event.set()

        if self.playlist is not None:
            try:
                self.playlist.save()

            except OSError as error:
                self.log(f"Could not save the playlist: {error}")

    def disable(self):

        self._shutting_down = True

        for timer_id in self._search_timers.values():
            events.cancel_scheduled(timer_id)

        self._search_timers.clear()

        if self._search_queue_timer is not None:
            events.cancel_scheduled(self._search_queue_timer)
            self._search_queue_timer = None

        if self.playlist is not None:
            self.playlist.save()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @property
    def data_folder_path(self):

        data_folder_path = getattr(self.config, "data_folder_path", None)

        if not data_folder_path:
            data_folder_path = self.path

        return data_folder_path

    def scoring_options(self):

        return napstr_match.ScoringOptions(
            preferred_extension=self.settings.get("preferred_format", "any"),
            excluded_extensions=self._excluded_extensions(),
            min_bitrate=self.settings.get("min_bitrate", 0),
            max_bitrate=self.settings.get("max_bitrate", 0),
            min_size_bytes=napstr_match.megabytes_to_bytes(self.settings.get("min_size_mb", 0)),
            max_size_bytes=napstr_match.megabytes_to_bytes(self.settings.get("max_size_mb", 0)),
            duration_tolerance_seconds=self.settings.get("duration_tolerance", 10)
        )

    def _excluded_extensions(self):

        excluded = []

        for value in self.settings.get("excluded_formats") or []:
            extension = napstr_match.normalise_extension(value)

            if extension and extension not in excluded:
                excluded.append(extension)

        return excluded

    def _filter_summary(self):
        """One line describing the active hard filters, for /napstr status."""

        parts = []

        if self._excluded_extensions():
            parts.append("excluding " + ", ".join(self._excluded_extensions()))

        if self.settings.get("min_bitrate"):
            parts.append(f"min {self.settings['min_bitrate']} kbps")

        if self.settings.get("max_bitrate"):
            parts.append(f"max {self.settings['max_bitrate']} kbps")

        if self.settings.get("min_size_mb"):
            parts.append(f"min {self.settings['min_size_mb']} MB")

        if self.settings.get("max_size_mb"):
            parts.append(f"max {self.settings['max_size_mb']} MB")

        if self.settings.get("preferred_format", "any") != "any":
            parts.append(f"preferring {self.settings['preferred_format']}")

        return ", ".join(parts) or "no format, bitrate or size filters"

    def _save_playlist(self):
        """Write the playlist document to disk, ignoring I/O failures."""

        if self.playlist is None:
            return None

        try:
            return self.playlist.save()

        except OSError as error:
            self.log(f"Could not save the playlist: {error}")
            return None

    def _report(self, message, notify=False):
        """Log a message, optionally raising a desktop notification.

        Thread safe: Nicotine+ marshals scheduled and thread callbacks on to
        the main thread, and ``log.add`` may be called from any thread.
        """

        self.log(message)

        if notify:
            try:
                self.core.notifications.show_notification(message, title="NAPSTR Playlist")

            except Exception:  # pylint: disable=broad-except
                pass

    def _require_playlist(self):

        if self.playlist is None:
            self.output("No playlist loaded. Use /napstr load <exportify.csv> or /napstr open <id>.")
            return False

        return True

    def _staging_folder(self):

        folder = str(self.settings.get("download_folder") or "").strip()

        if folder:
            return os.path.expandvars(os.path.expanduser(folder))

        return self.playlist.paths_for_download()

    def _credentials(self):
        """Return ``(secret_key_bytes, error_message)``."""

        raw_value = str(self.settings.get("nostr_key") or "").strip()

        if not raw_value:
            return None, "No Nostr private key set. Add your nsec1... key in the plugin settings."

        try:
            import napstr_crypto  # pylint: disable=import-outside-toplevel

            return napstr_crypto.decode_secret_key(raw_value), None

        except Exception as error:  # pylint: disable=broad-except
            return None, f"Invalid Nostr private key: {error}"

    def _relay_urls(self):

        relays = []

        for relay in self.settings.get("relays") or []:
            normalized = napstr_relay.normalize_relay_url(relay)

            if normalized and normalized not in relays:
                relays.append(normalized)

        return relays

    def _make_pool(self, relays):

        return napstr_relay.RelayPool(
            relays,
            timeout=int(self.settings.get("relay_timeout", 15)),
            insecure=bool(self.settings.get("insecure_tls", False))
        )

    @staticmethod
    def _status_label(entry):

        if entry.get("file_id"):
            return f"hashed:{entry['file_id'][:10]}"

        return str(entry.get("status", napstr_state.STATUS_NEW))

    @staticmethod
    def _entry_label(entry):

        artist = entry.get("artist") or entry.get("album_artist") or "?"
        title = entry.get("title") or "?"

        return f"{artist} - {title}"

    # ------------------------------------------------------------------
    # command entry point
    # ------------------------------------------------------------------

    def napstr_command(self, args, user=None, room=None, **_unused):

        parts = (args or "").strip().split(maxsplit=1)
        action = parts[0].lower() if parts else "help"
        rest = parts[1] if len(parts) > 1 else ""

        handlers = {
            "help": self._action_help,
            "load": self._action_load,
            "open": self._action_open,
            "playlists": self._action_playlists,
            "list": self._action_list,
            "search": self._action_search,
            "options": self._action_options,
            "pick": self._action_pick,
            "auto": self._action_auto,
            "skip": self._action_skip,
            "unskip": self._action_unskip,
            "setpath": self._action_setpath,
            "scan": self._action_scan,
            "hash": self._action_hash,
            "status": self._action_status,
            "title": self._action_title,
            "tags": self._action_tags,
            "review": self._action_review,
            "publish": self._action_publish,
            "unpublish": self._action_unpublish,
            "relays": self._action_relays,
            "resume": self._action_resume,
            "rate": self._action_rate,
            "reset": self._action_reset,
            "forget": self._action_forget,
            "export": self._action_export
        }

        handler = handlers.get(action)

        if handler is None:
            self.output(f"Unknown action '{action}'.")
            self._action_help("")
            return False

        handler(rest)

        return True

    def _action_help(self, _rest):

        self.output(
            "NAPSTR playlist commands:\n"
            "\t/napstr load <exportify.csv|zip> [| title]   import a playlist\n"
            "\t/napstr open <playlist id>                   load a stored playlist\n"
            "\t/napstr playlists                           list stored playlists\n"
            "\t/napstr list [page|status]                  show entries\n"
            "\t/napstr search <n|all|missing>              search Soulseek per entry\n"
            "\t/napstr options <n>                         ranked candidates for entry n\n"
            "\t/napstr pick <n> <rank>                     download that candidate\n"
            "\t/napstr auto <n|all>                        search and auto-pick\n"
            "\t/napstr skip <n> | unskip <n>               mark an entry\n"
            "\t/napstr setpath <n> <file>                  set the local file by hand\n"
            "\t/napstr scan                                find files you already have\n"
            "\t/napstr hash <n|all>                        SHA-256 the local files\n"
            "\t/napstr reset <n|all|missing>               clear decisions, keep file IDs\n"
            "\t/napstr forget <n|all|missing>              clear decisions and file IDs\n"
            "\t/napstr title <title> | tags <a,b,c>        playlist metadata\n"
            "\t/napstr review                              open the GUI review window\n"
            "\t/napstr publish | unpublish                 publish / retract the event\n"
            "\t/napstr relays                              relay connectivity check\n"
            "\t/napstr resume | rate <seconds>             resume a paused queue, change the pace\n"
            "\t/napstr status | export <file.json>         state and export"
        )

    # ------------------------------------------------------------------
    # importing playlists
    # ------------------------------------------------------------------

    @staticmethod
    def _split_path_and_title(rest):

        rest = (rest or "").strip()

        if rest.startswith('"'):
            end = rest.find('"', 1)

            if end > 0:
                return rest[1:end], rest[end + 1:].strip()

        if " | " in rest:
            path, title = rest.split(" | ", 1)

            return path.strip(), title.strip()

        return rest, ""

    def _action_load(self, rest):

        path, title = self._split_path_and_title(rest)

        if not path:
            self.output("Usage: /napstr load <exportify.csv|zip> [| title]")
            return False

        path = os.path.expandvars(os.path.expanduser(path))

        if not os.path.exists(path):
            self.output(f"File not found: {path}")
            return False

        if path.lower().endswith(".zip"):
            created = self._load_zip(path)

            if not created:
                return False

            self.output(f"Imported {len(created)} playlist(s): " + ", ".join(created))
            return True

        try:
            entries = napstr_csv.read_exportify_file(path)

        except napstr_csv.ExportifyError as error:
            self.output(f"Could not read the CSV: {error}")
            return False

        if not title:
            title = str(self.settings.get("title") or "").strip()

        if not title:
            title = os.path.splitext(os.path.basename(path))[0]

        playlist_id = str(self.settings.get("playlist_id") or "").strip()
        existing = napstr_state.load_playlist(self.data_folder_path, playlist_id) if playlist_id else None

        if existing is not None:
            self.playlist = existing

            if title:
                self.playlist.title = title

            self.playlist.dirty = True
            self._save_playlist()
            self._report(
                f"Revising playlist {self.playlist.id} ('{self.playlist.title}') with its "
                f"{len(self.playlist.entries)} stored entries. Load a different playlist by "
                "clearing the 'playlist_id' setting first.", notify=True)
            return True

        self.playlist = napstr_state.PlaylistState(
            self.data_folder_path,
            playlist_id=playlist_id or None,
            title=title,
            entries=entries,
            tags=napstr_event.sanitize_tags(self.settings.get("author_tags", "")),
            source=path
        )

        if not playlist_id:
            self.settings["playlist_id"] = self.playlist.id

        saved_path = self.playlist.save(force=True)

        self._report(
            f"Loaded '{self.playlist.title}' with {len(self.playlist.entries)} entries "
            f"(id {self.playlist.id}).\nSaved to {saved_path}\n"
            "Next: /napstr auto all, or /napstr search all to review matches by hand.", notify=True)

        return True

    def _load_zip(self, path):

        created = []

        try:
            with zipfile.ZipFile(path) as archive:
                csv_names = sorted(
                    name for name in archive.namelist()
                    if name.lower().endswith(".csv") and not name.startswith("__MACOSX"))

                if not csv_names:
                    self.output("The zip file contains no CSV files.")
                    return created

                for name in csv_names:
                    try:
                        text = archive.read(name).decode("utf-8-sig", "replace")
                        entries = napstr_csv.parse_exportify_csv(text, source=f"{path}:{name}")

                    except (napstr_csv.ExportifyError, KeyError, OSError) as error:
                        self.output(f"Skipped {name}: {error}")
                        continue

                    title = os.path.splitext(os.path.basename(name))[0]
                    state = napstr_state.PlaylistState(
                        self.data_folder_path,
                        title=title,
                        entries=entries,
                        tags=napstr_event.sanitize_tags(self.settings.get("author_tags", "")),
                        source=path
                    )
                    state.save(force=True)

                    if self.playlist is None:
                        self.playlist = state

                    created.append(f"{title} ({state.id})")

        except (OSError, zipfile.BadZipFile) as error:
            self.output(f"Could not read the zip file: {error}")

        return created

    def _action_open(self, rest):

        playlist_id = rest.strip()

        if not playlist_id:
            self._action_playlists("")
            return False

        state = napstr_state.load_playlist(self.data_folder_path, playlist_id)

        if state is None:
            self.output(f"No stored playlist with id {playlist_id}.")
            return False

        self.playlist = state
        self.settings["playlist_id"] = state.id
        self.output(
            f"Opened '{state.title}' ({len(state.entries)} entries, "
            f"{len(state.resolved_entries())} with a file ID). Publishing will revise "
            "this coordinate.")

        return True

    def _action_playlists(self, _rest):

        playlists = napstr_state.list_playlists(self.data_folder_path)

        if not playlists:
            self.output("No stored playlists yet.")
            return True

        self.output("Stored playlists:")

        for playlist_id, data in playlists.items():
            published = "published" if data["published"] else "not published"
            self.output(
                f"\t{playlist_id}  {data['title']}  "
                f"({data['with_file_id']}/{data['total']} hashed, {published})")

        return True

    # ------------------------------------------------------------------
    # listing
    # ------------------------------------------------------------------

    def _action_list(self, rest):

        if not self._require_playlist():
            return False

        argument = rest.strip().lower()
        entries = self.playlist.entries

        if argument and argument != "all":
            try:
                page = max(int(argument), 1)

            except ValueError:
                status = argument
                entries = [entry for entry in entries if entry.get("status") == status]

                if not entries:
                    self.output(f"No entries with status '{status}'.")
                    return True

                page = 1

        else:
            page = 1

        start = (page - 1) * LIST_PAGE_SIZE
        chunk = entries[start:start + LIST_PAGE_SIZE]

        if not chunk:
            self.output(f"No entries on page {page}.")
            return True

        self.output(
            f"{self.playlist.title} ({self.playlist.id}) - page {page} of "
            f"{max((len(entries) + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE, 1)}")

        for entry in chunk:
            self.output(
                f"\t{entry['position']:>3}. [{self._status_label(entry):<18}] {self._entry_label(entry)}")

        return True

    def _action_status(self, _rest):

        if not self._require_playlist():
            return False

        summary = self.playlist.summary()
        counts = ", ".join(
            f"{status}: {count}" for status, count in summary["counts"].items() if count)

        self.output(
            f"Playlist: {self.playlist.title}\n"
            f"\tCoordinate: {self.playlist.id}\n"
            f"\tSource: {self.playlist.source or '(unknown)'}\n"
            f"\tEntries: {summary['total']}\n"
            f"\tWith file ID: {summary['with_file_id']}\n"
            f"\tStatuses: {counts or 'none'}\n"
            f"\tAuthor tags: {', '.join(self.playlist.tags) or '(none - no word beyond the marker)'}\n"
            f"\tSearch pace: {self._pacer.describe_detail()}\n"
            f"\tFilters: {self._filter_summary()}\n"
            f"\tRelays: {len(self._relay_urls())}\n"
            f"\tPublished revisions: {len(self.playlist.published)}")

        if self.playlist.published:
            latest = self.playlist.published[-1]
            self.output(f"\tLatest event: {latest.get('event_id', '?')}")

        return True

    def _action_title(self, rest):

        if not self._require_playlist():
            return False

        title = napstr_event.sanitize_text(rest)

        if not title:
            self.output(f"Current title: {self.playlist.title}")
            return True

        self.playlist.title = title
        self.playlist.dirty = True
        self.playlist.save()
        self.output(f"Title set to '{title}'.")

        return True

    def _action_tags(self, rest):

        if not self._require_playlist():
            return False

        if not rest.strip():
            self.output(f"Current author tags: {', '.join(self.playlist.tags) or '(none)'}")
            return True

        self.playlist.tags = napstr_event.sanitize_tags(rest)
        self.playlist.dirty = True
        self.playlist.save()
        self.output(f"Author tags set to: {', '.join(self.playlist.tags) or '(none)'}")

        return True

    def _action_export(self, rest):

        if not self._require_playlist():
            return False

        path = rest.strip()

        if not path:
            path = os.path.join(
                napstr_state.state_folder_path(self.data_folder_path),
                f"{self.playlist.id}-export.json")

        path = os.path.expandvars(os.path.expanduser(path))

        try:
            self.playlist.save(force=True)

            with open(path, "w", encoding="utf-8") as file_handle:
                json.dump(self.playlist.to_dict(), file_handle, indent=2, ensure_ascii=False)

        except OSError as error:
            self.output(f"Could not export: {error}")
            return False

        self.output(f"Exported to {path}")

        return True

    # ------------------------------------------------------------------
    # searching
    # ------------------------------------------------------------------

    def _resolve_positions(self, rest, default=""):
        """Turn a command argument into a list of entry positions.

        There is deliberately no default target: an action without an
        argument must never fan out over the whole playlist by accident.
        """

        argument = (rest or "").strip().lower() or (default or "")

        if not argument:
            return []

        if argument in {"all", "*"}:
            return [entry["position"] for entry in self.playlist.entries]

        if argument == "missing":
            return [
                entry["position"] for entry in self.playlist.entries
                if not entry.get("file_id") and entry.get("status") not in INACTIVE_STATUSES
            ]

        try:
            position = int(argument)

        except ValueError:
            return []

        if self.playlist.entry(position) is None:
            return []

        return [position]

    def _action_search(self, rest):

        if not self._require_playlist():
            return False

        positions = self._resolve_positions(rest)

        if not positions:
            self.output("Usage: /napstr search <entry number|all|missing>")
            return False

        return self._queue_searches(positions)

    def _action_auto(self, rest):

        if not self._require_playlist():
            return False

        positions = self._resolve_positions(rest)

        if not positions:
            self.output("Usage: /napstr auto <entry number|all|missing>")
            return False

        already = [position for position in positions
                   if self._has_a_usable_file(self.playlist.entry(position) or {})]

        if already:
            self.output(
                f"{len(already)} of those {len(positions)} entries already have a hashed file; "
                "they will be left alone. Use /napstr forget <entry> before auto to fetch a "
                "different file on purpose, or /napstr auto missing to search only what is "
                "missing.")

        # Entries that already collected candidates can be decided right away
        pending = []

        for position in positions:
            entry = self.playlist.entry(position)

            if entry is None:
                continue

            if entry.get("candidates"):
                self._auto_positions.add(position)
                self._finish_search_for_position(position)
                continue

            pending.append(position)

        if pending:
            return self._queue_searches(pending, automatic=True)

        return True

    def _queue_searches(self, positions, automatic=False):

        if self._pacer.paused:
            self.output(
                f"Searching is paused: {self._pacer.pause_reason}. "
                "Run /napstr resume when you are ready to continue.")
            return False

        queued = 0

        for position in positions:
            entry = self.playlist.entry(position)

            if entry is None or entry.get("status") == napstr_state.STATUS_SKIPPED:
                continue

            if automatic:
                self._auto_positions.add(position)

            self._search_queue.append(position)
            queued += 1

        if not queued:
            self.output("Nothing to search for.")
            return False

        estimate = self._pacer.estimate_duration(len(self._search_queue))
        pace = (
            f" At {self._pacer.describe()} that takes about "
            f"{napstr_pace.human_duration(estimate)}." if estimate else "")

        self._report(
            f"Queued {queued} search(es) for '{self.playlist.title}'.{pace}", notify=True)
        self._pump_search_queue()

        return True

    def _pump_search_queue(self):
        """Start the next queued search if the pacer allows it.

        One search at a time, and never sooner than the pacer's interval. The
        server bans accounts that search in bursts, and a 100 track playlist
        sent at two second intervals is exactly such a burst: that is how the
        first version of this plugin earned a 30 minute ban.
        """

        if self._shutting_down:
            return

        if self._search_queue_timer is not None:
            events.cancel_scheduled(self._search_queue_timer)
            self._search_queue_timer = None

        if not self._search_queue:
            return

        if self._search_token is not None or self._pacer.paused:
            # A search is in flight, or the queue is stopped (see on_log_message)
            return

        self._refresh_pace()
        wait = self._pacer.seconds_until_allowed()

        if wait > 0:
            # Re-check periodically instead of sleeping for the whole gap, so a
            # resume or a settings change takes effect promptly.
            self._search_queue_timer = events.schedule(
                delay=min(wait, 30), callback=self._pump_later)
            return

        position = self._search_queue.pop(0)
        entry = self.playlist.entry(position)

        if entry is None:
            self._pump_search_queue()
            return

        token = self._start_search(entry)

        if token is None:
            self._search_queue.clear()
            self._pause_searches("no usable search API was found in this Nicotine+ version")
            self._report(
                "Stopped the batch: no Soulseek search request could be created. The public "
                "search API is deliberately not used as a fallback, because it opens a search "
                "tab per track and rewrites the config file every time.", notify=True)
            return

        self._pacer.note_search()
        self._search_token = token
        self._search_positions[token] = position
        entry["search_token"] = token
        entry["candidates"] = []
        self.playlist.set_status(entry, napstr_state.STATUS_SEARCHING)

        timeout = max(int(self.settings.get("search_timeout", 25)), 5)
        self._search_timers[token] = events.schedule(
            delay=timeout, callback=self._finish_search, callback_args=(token,))

    def _pump_later(self):

        self._search_queue_timer = None
        self._pump_search_queue()

    def _configure_pacer(self):

        interval = self.settings.get("search_interval", napstr_pace.DEFAULT_SEARCH_INTERVAL)

        self._configured_interval = interval
        self._pacer.set_interval(interval)
        self._adopt_server_pace()

    def _refresh_pace(self):
        """Pick up a pace change from the settings pane while a batch runs.

        The pump only calls :meth:`_adopt_server_pace` on its own, so editing
        ``search_interval`` in the plugin settings used to have no effect until
        the plugin was reloaded - a setting that quietly does nothing. A change
        in the settings wins because it is newer information; a back-off after a
        server warning did *not* change the setting, so it survives.
        """

        wanted = self.settings.get("search_interval", napstr_pace.DEFAULT_SEARCH_INTERVAL)

        if wanted != self._configured_interval:
            self._configure_pacer()
            return

        self._adopt_server_pace()

    def _adopt_server_pace(self):
        """Adopt the server's own automated-search interval as an extra floor.

        Server code 104 ("wishlist wait period") is how long the server wants
        between automated searches, and it is not always a minute: it can be
        several. It is treated as a floor by default, but a floor the user can
        decline, because a legitimate value nobody will wait out is worse than
        the honest slower pace they choose instead.
        """

        if not self.settings.get("respect_server_interval", True):
            # Still recorded, just not enforced: /napstr status has to be able to
            # show what the server asked for, or slow searching cannot be
            # explained.
            self._pacer.set_server_interval(
                getattr(self.core.search, "wishlist_interval", 0), enforce=False)
            return

        server_interval = getattr(self.core.search, "wishlist_interval", 0)

        if not server_interval or server_interval == self._pacer.server_interval:
            return

        self._pacer.set_server_interval(server_interval)
        self._report(
            "Soulseek's wishlist wait period is "
            f"{napstr_pace.human_duration(server_interval)}, so searches stay at least that "
            "far apart. /napstr status names the limit in force; turn off 'Respect the "
            "server interval' in the plugin settings to use your own pace instead.")

    def _pause_searches(self, reason):
        """Latched stop: only /napstr resume clears it."""

        self._pacer.pause(reason)
        self._search_queue.clear()

        if self._search_queue_timer is not None:
            events.cancel_scheduled(self._search_queue_timer)
            self._search_queue_timer = None

    def _release_search_token(self, token):
        """Free the search request and stop accepting results for it."""

        try:
            self.core.search.remove_search(token)

        except Exception as error:  # pylint: disable=broad-except
            self.log(f"Could not release search token {token}: {error}")

    def _start_search(self, entry):
        """Send one global search and return its token, or ``None``.

        The search is created directly rather than through ``Search.do_search``:
        that public call opens a search page in the UI (one per track) and calls
        ``config.write_configuration()`` for every search, which costs nothing on
        a single manual search and is unusable for a playlist.

        Nicotine+ 3.3.x exposes ``token``/``add_search``/``do_global_search``;
        later versions made those private. Whichever fits is used, and the result
        is logged once so a future mismatch is visible instead of silent.
        """

        term = napstr_csv.build_search_query(entry, self.settings.get("query_template"))

        if not term:
            return None

        entry["query"] = term
        search_module = self.core.search
        token = None
        error = "no usable search API"

        if hasattr(search_module, "add_search") and hasattr(search_module, "do_global_search"):
            token, error = self._create_search_public(search_module, term)
            api_name = "3.3.x"

        elif hasattr(search_module, "_add_search"):
            token, error = self._create_search_private(search_module, term)
            api_name = "private"

        else:
            api_name = "unknown"

        if token is None:
            self.log(f"Search API '{api_name}' failed for '{term}': {error}")
            return None

        if api_name != self._search_api_name:
            self._search_api_name = api_name
            self.log(f"Using the {api_name} Nicotine+ search API.")

        return token

    @staticmethod
    def _create_search_public(search_module, term):
        """Nicotine+ 3.3.x: public Search attributes (``token``, ``add_search``).

        Returns ``(token, error)`` so the caller can report the reason.
        """

        search = None

        try:
            # pylint: disable=import-outside-toplevel
            from pynicotine.slskmessages import increment_token

            search_module.token = increment_token(search_module.token)
            search = search_module.add_search(term, "global")
            search_module.do_global_search(search.term_transmitted)

            return search.token, None

        except Exception as error:  # pylint: disable=broad-except
            if search is not None:
                # Never leave a registered search behind that was never sent
                try:
                    search_module.remove_search(search.token)

                except Exception:  # pylint: disable=broad-except
                    pass

            return None, f"{type(error).__name__}: {error}"

    @staticmethod
    def _create_search_private(search_module, term):
        """Later Nicotine+: private search plumbing. Returns ``(token, error)``."""

        try:
            # pylint: disable=import-outside-toplevel,protected-access
            from pynicotine.slskmessages import increment_token

            search_module._token = token = increment_token(search_module._token)
            search = search_module._add_search(token, term, "global")

            if search is None:
                raise RuntimeError("the search request could not be created")

            search_module.add_allowed_token(token)
            search_module._send_global_search_request(search)

            return token, None

        except Exception as error:  # pylint: disable=broad-except
            return None, f"{type(error).__name__}: {error}"

    def on_log_message(self, _timestamp_format, message, _title=None, _level=None):
        """Stop a running batch when the server complains (main thread).

        The Soulseek server announces a flood ban as a chat message: "You have
        been banned for 30 minutes [...] Do not quickly repeat a search". Nothing
        here can undo a ban, but a batch must not keep searching through one, or
        the retries look like flooding all over again.
        """

        if self._shutting_down or self._pacer.paused or not message:
            return

        if self._search_token is None and not self._search_queue:
            return

        if napstr_pace.looks_like_ban(message):
            self._pause_searches("the server banned this session for flooding")
            self._report(
                "The Soulseek server reported a ban, most likely for searching too quickly. "
                "Searching is stopped and will not restart on its own: stay connected, wait out "
                "the ban, then run /napstr resume.", notify=True)
            return

        if napstr_pace.looks_like_rate_warning(message):
            before = self._pacer.requested_interval
            after = self._pacer.slow_down()

            if after <= before:
                self._report(
                    "The server warned about search volume; the pace is already as slow as "
                    f"this plugin goes ({napstr_pace.human_duration(after)} between searches).",
                    notify=True)
            else:
                self._report(
                    "The server warned about search volume; slowing down to "
                    f"{napstr_pace.human_duration(after)} between searches.",
                    notify=True)

    def server_disconnect_notification(self, userchoice):
        """A disconnect during a batch is how a ban starts, so stand down."""

        if self._shutting_down or self._pacer.paused:
            return

        if self._search_token is None and not self._search_queue:
            return

        last_search = self._pacer.last_search_at

        if last_search is None or (time.monotonic() - last_search) > 180:
            return

        self._pause_searches("the server disconnected during a search batch")
        self._report(
            "Searches paused: the server disconnected while a batch was running, which is how a "
            "flood ban starts. Reconnect, then run /napstr resume to continue.", notify=True)

    def on_file_search_response(self, msg):
        """Collect candidates from a search response (main thread)."""

        if self.playlist is None:
            return

        position = self._search_positions.get(getattr(msg, "token", None))

        if position is None:
            return

        entry = self.playlist.entry(position)

        if entry is None:
            return

        username = getattr(msg, "username", "") or getattr(msg, "search_username", "") or ""
        candidates = entry.setdefault("candidates", [])
        known = {(candidate.get("username"), candidate.get("path")) for candidate in candidates}
        added = 0

        for file_info in getattr(msg, "list", None) or []:
            try:
                _code, path, size, _extension, attributes = file_info

            except (TypeError, ValueError):
                continue

            key = (username, path)

            if key in known:
                continue

            known.add(key)

            attributes = attributes or {}
            candidates.append({
                "username": username,
                "path": path,
                "size": size or 0,
                "bitrate": attributes.get(FileAttribute.BITRATE),
                "length": attributes.get(FileAttribute.DURATION),
                "sample_rate": attributes.get(FileAttribute.SAMPLE_RATE),
                "bit_depth": attributes.get(FileAttribute.BIT_DEPTH)
            })
            added += 1

        if added and len(candidates) > MAX_CANDIDATES_PER_ENTRY:
            del candidates[MAX_CANDIDATES_PER_ENTRY:]

        if added:
            self.playlist.dirty = True

    def _finish_search(self, token):

        position = self._search_positions.pop(token, None)
        self._search_timers.pop(token, None)
        self._release_search_token(token)

        if self._search_token == token:
            self._search_token = None

        if position is not None and not self._shutting_down:
            entry = self.playlist.entry(position)

            if entry is not None:
                entry["search_token"] = None

            self._finish_search_for_position(position)

        # Keep the queue moving, at the pace the pacer allows
        self._pump_search_queue()

    def _finish_search_for_position(self, position):

        entry = self.playlist.entry(position)

        if entry is None:
            return

        ranked = napstr_match.find_ranked_candidates(
            entry, entry.get("candidates") or [], self.scoring_options())
        usable = [candidate for candidate in ranked if candidate["score"] > 0]
        rejected = len(ranked) - len(usable)

        if not usable:
            if ranked:
                # Something was found but the filters ruled all of it out; say
                # so, or an over-tight filter looks like an empty network.
                reasons = ranked[0].get("reasons") or []
                example = reasons[0] if reasons else "no reason recorded"

                self.playlist.set_status(
                    entry, napstr_state.STATUS_UNAVAILABLE,
                    f"{rejected} candidate(s) rejected by the filters")
                self._report(
                    f"Entry {position}: all {rejected} candidate(s) for '{entry.get('query')}' were "
                    f"rejected by the filters ({example}).", notify=True)

            else:
                self.playlist.set_status(entry, napstr_state.STATUS_UNAVAILABLE, "no results")
                self._report(f"Entry {position}: no results for '{entry.get('query')}'.", notify=True)

            self._save_playlist()
            return

        # Rejected candidates are kept so /napstr options can explain them
        entry["candidates"] = ranked
        self.playlist.dirty = True

        best = usable[0]
        threshold = float(self.settings.get("auto_pick_threshold", 0.8))
        automatic = bool(self.settings.get("auto_pick", True)) or position in self._auto_positions
        self._auto_positions.discard(position)

        if automatic and best["score"] >= threshold:

            if self._has_a_usable_file(entry):
                # Downloading a second copy of a track that is already hashed
                # wastes bandwidth and leaves the entry pointing at the original
                # regardless: the new file lands beside it as "Name (1).mp3" and
                # nothing ever links to it. /napstr forget is how to ask for a
                # different file on purpose.
                self.playlist.set_status(
                    entry, napstr_state.STATUS_HASHED,
                    "already have this file; skipped a duplicate download")
                self._report(
                    f"Entry {position} already has a hashed file; no second copy queued.")
                self._save_playlist()
                return

            self._download_candidate(entry, best, automatic=True)
            self._save_playlist()
            return

        self.playlist.set_status(
            entry, napstr_state.STATUS_REVIEW,
            f"best score {best['score']:.2f} below the automatic threshold"
            + (f"; {rejected} candidate(s) rejected by the filters" if rejected else ""))

        self._report(
            f"Entry {position} needs a choice: best is {best['score']:.2f} "
            f"({napstr_match.describe_candidate(best)}). Use /napstr options {position}.", notify=True)

        self._save_playlist()

    # ------------------------------------------------------------------
    # choosing and downloading
    # ------------------------------------------------------------------

    def _action_options(self, rest):

        if not self._require_playlist():
            return False

        try:
            position = int(rest.strip())

        except ValueError:
            self.output("Usage: /napstr options <entry number>")
            return False

        entry = self.playlist.entry(position)

        if entry is None:
            self.output(f"No entry {position}.")
            return False

        candidates = entry.get("candidates") or []

        if not candidates:
            self.output(
                f"Entry {position}: {self._entry_label(entry)} - no candidates yet. "
                f"Run /napstr search {position}.")
            return True

        self.output(f"Entry {position}: {self._entry_label(entry)} ({entry.get('status')})")
        chosen = entry.get("chosen") or {}

        for rank, candidate in enumerate(candidates[:10], start=1):
            marker = "*" if (chosen.get("username") == candidate.get("username")
                             and chosen.get("path") == candidate.get("path")) else " "

            self.output(f"\t{marker}{rank}. {float(candidate.get('score') or 0.0):.2f}  "
                        f"{napstr_match.describe_candidate(candidate)}")
            self.output(f"\t     {'; '.join(candidate.get('reasons') or [])}")

        self.output(f"\tDownload with /napstr pick {position} <rank>.")

        return True

    def _action_pick(self, rest):

        if not self._require_playlist():
            return False

        parts = rest.split()

        if len(parts) != 2:
            self.output("Usage: /napstr pick <entry number> <rank>")
            return False

        try:
            position = int(parts[0])
            rank = int(parts[1])

        except ValueError:
            self.output("Usage: /napstr pick <entry number> <rank>")
            return False

        entry = self.playlist.entry(position)
        candidates = (entry or {}).get("candidates") or []

        if not candidates or not 1 <= rank <= len(candidates):
            self.output(f"No candidate {rank} for entry {position}.")
            return False

        self._download_candidate(entry, candidates[rank - 1])
        self.playlist.save()

        return True

    def _has_a_usable_file(self, entry):
        """True when the entry already has a hashed file that is still on disk.

        /napstr reset keeps file IDs, so an entry can be searched again while it
        already holds a good file. /napstr auto all then used to fetch a second
        copy of every one of them.
        """

        if not entry.get("file_id"):
            return False

        local_path = str(entry.get("local_path") or "")

        return bool(local_path) and os.path.isfile(local_path)

    def _download_candidate(self, entry, candidate, automatic=False):

        folder_path = self._staging_folder()

        try:
            os.makedirs(folder_path, exist_ok=True)

        except OSError as error:
            self._report(f"Could not create {folder_path}: {error}", notify=True)
            return False

        attributes = {
            FileAttribute.BITRATE: candidate.get("bitrate"),
            FileAttribute.DURATION: candidate.get("length")
        }
        attributes = {key: value for key, value in attributes.items() if value}

        try:
            self.core.downloads.enqueue_download(
                candidate.get("username", ""), candidate.get("path", ""),
                folder_path=folder_path, size=int(candidate.get("size") or 0),
                file_attributes=attributes or None)

        except Exception as error:  # pylint: disable=broad-except
            self._report(f"Could not queue the download: {error}", notify=True)
            return False

        entry["chosen"] = {
            "username": candidate.get("username", ""),
            "path": candidate.get("path", ""),
            "size": int(candidate.get("size") or 0),
            "score": candidate.get("score"),
            # Recorded so the decision can be re-checked later. Without them a
            # later audit scores the same pair without its duration and reports
            # a good pick as a bad one (0.75 instead of 0.80 on real data).
            "bitrate": candidate.get("bitrate"),
            "length": candidate.get("length")
        }
        entry["score"] = candidate.get("score")
        self.playlist.set_status(entry, napstr_state.STATUS_QUEUED)

        prefix = "auto-picked" if automatic else "queued"
        self._report(
            f"Entry {entry['position']} {prefix} (score {candidate.get('score'):.2f}): "
            f"{napstr_match.describe_candidate(candidate)}", notify=automatic)

        return True

    def download_started_notification(self, user, virtual_path, real_path):

        entry = self._entry_for_transfer(user, virtual_path)

        if entry is not None:
            self.playlist.set_status(entry, napstr_state.STATUS_DOWNLOADING)
            self.playlist.save()

    def download_finished_notification(self, user, virtual_path, real_path):

        entry = self._entry_for_transfer(user, virtual_path)

        if entry is None:
            return

        if not real_path or not os.path.isfile(real_path):
            self.playlist.set_status(entry, napstr_state.STATUS_FAILED, "download did not land on disk")
            self.playlist.save()
            return

        entry["local_path"] = real_path
        self.playlist.set_status(entry, napstr_state.STATUS_DOWNLOADED)
        self._report(f"Entry {entry['position']} downloaded: {real_path}")
        self.playlist.save()
        self._queue_hash(entry)

    def _entry_for_transfer(self, user, virtual_path):

        if self.playlist is None:
            return None

        for entry in self.playlist.entries:
            chosen = entry.get("chosen") or {}

            if chosen.get("username") == user and chosen.get("path") == virtual_path:
                return entry

        return None

    # ------------------------------------------------------------------
    # skipping and manual paths
    # ------------------------------------------------------------------

    def _action_skip(self, rest):
        return self._set_status_for_positions(rest, napstr_state.STATUS_SKIPPED, "skipped", "skip")

    def _action_unskip(self, rest):
        return self._set_status_for_positions(rest, napstr_state.STATUS_NEW, "", "unskip")

    def _set_status_for_positions(self, rest, status, note, verb):

        if not self._require_playlist():
            return False

        positions = self._resolve_positions(rest)

        if not positions:
            self.output(f"Usage: /napstr {verb} <entry number>")
            return False

        for position in positions:
            entry = self.playlist.entry(position)

            if entry is not None:
                self.playlist.set_status(entry, status, note)
                self.output(f"Entry {position}: {note or status}.")

        self.playlist.save()

        return True

    def _action_reset(self, rest):
        return self._clear_entry_state(rest, drop_files=False, verb="reset")

    def _action_forget(self, rest):
        return self._clear_entry_state(rest, drop_files=True, verb="forget")

    def _clear_entry_state(self, rest, drop_files, verb):
        """Throw away search results, and optionally the hashed file ID too."""

        if not self._require_playlist():
            return False

        positions = self._resolve_positions(rest)

        if not positions:
            self.output(f"Usage: /napstr {verb} <entry number|all|missing>")
            return False

        changed = napstr_state.reset_entries(self.playlist, positions, drop_files=drop_files)

        if drop_files:
            kept = " including their file IDs and local paths"

        else:
            kept = " (file IDs kept, downloaded files untouched)"

        self._save_playlist()
        self.output(f"Cleared the search state of {changed} entr(y/ies){kept}.")

        if not drop_files:
            # A reset keeps the file, so /napstr auto will not fetch a replacement
            # for those entries. Saying so here is the difference between "it
            # ignored my reset" and knowing that forget is the command you want.
            held = [entry["position"] for entry in self.playlist.entries
                    if entry.get("file_id") and entry.get("local_path")]

            if held:
                self.output(
                    f"{len(held)} entr(y/ies) still hold a file, so /napstr auto will leave them "
                    "alone: " + ", ".join(str(position) for position in held[:10])
                    + ("..." if len(held) > 10 else "")
                    + " Use /napstr forget <entry> to fetch a different file for one of them.")

        return True

    def _action_setpath(self, rest):

        if not self._require_playlist():
            return False

        parts = rest.split(maxsplit=1)

        if len(parts) != 2:
            self.output("Usage: /napstr setpath <entry number> <file path>")
            return False

        try:
            position = int(parts[0])

        except ValueError:
            self.output("Usage: /napstr setpath <entry number> <file path>")
            return False

        entry = self.playlist.entry(position)
        path = os.path.expandvars(os.path.expanduser(parts[1].strip().strip('"')))

        if entry is None:
            self.output(f"No entry {position}.")
            return False

        if not os.path.isfile(path):
            self.output(f"Not a file: {path}")
            return False

        entry["local_path"] = path
        self.playlist.set_status(entry, napstr_state.STATUS_DOWNLOADED, "path set by hand")
        self.playlist.save()
        self._queue_hash(entry)

        return True

    # ------------------------------------------------------------------
    # scanning folders for files that are already there
    # ------------------------------------------------------------------

    def _action_resume(self, _rest):

        was_paused = self._pacer.paused

        self._pacer.resume()
        self._search_queue.clear()

        self.output(f"Searching resumed: {self._pacer.describe()}.")

        if was_paused:
            self.output(
                "A flood ban lasts about 30 minutes. Start with /napstr auto missing so the "
                "entries that already have candidates are not searched again.")

        return True

    def _action_rate(self, rest):

        value = rest.strip()

        if not value:
            self.output(f"Current pace: {self._pacer.describe_detail()}.")
            return True

        try:
            interval = float(value)

        except ValueError:
            self.output("Usage: /napstr rate <seconds between searches>")
            return False

        if interval < napstr_pace.MINIMUM_SEARCH_INTERVAL:
            self.output(
                f"Refusing {interval:.0f} s per search: the server bans accounts that search "
                f"faster than about {napstr_pace.MINIMUM_SEARCH_INTERVAL} s apart, and this "
                "plugin will not risk your account to go quicker.")
            return False

        self.settings["search_interval"] = interval
        self._configure_pacer()
        self.output(f"Pace set to {self._pacer.describe()}.")

        if self._pacer.limiting_reason() == "server_wait_period":
            self.output(
                "Note: Soulseek's wishlist wait period ("
                f"{napstr_pace.human_duration(self._pacer.server_interval)}) is slower than that, "
                "so searches stay further apart than you asked. Turn off 'Respect the server "
                "interval' in the plugin settings to pace at your own setting.")

        return True

    def _action_scan(self, _rest):

        if not self._require_playlist():
            return False

        folders = [str(folder) for folder in (self.settings.get("scan_folders") or []) if str(folder).strip()]

        if not folders:
            self.output("No folders configured. Add some under Files in the plugin settings.")
            return False

        unresolved = [
            entry["position"] for entry in self.playlist.entries
            if not entry.get("file_id") and entry.get("status") != napstr_state.STATUS_SKIPPED
        ]

        if not unresolved:
            self.output("Every entry already has a file ID.")
            return True

        self.output(f"Scanning {len(folders)} folder(s) for {len(unresolved)} entries...")

        thread = threading.Thread(
            target=self._scan_worker, args=(folders, unresolved),
            name="napstr-scan", daemon=True)
        thread.start()

        return True

    def _scan_worker(self, folders, positions):

        paths = []
        truncated = False

        for folder in folders:
            expanded = os.path.expandvars(os.path.expanduser(folder))

            if not os.path.isdir(expanded):
                continue

            for root, _dirs, names in os.walk(expanded):
                for name in names:
                    if napstr_match.audio_extension(name) not in napstr_match.AUDIO_EXTENSIONS:
                        continue

                    paths.append(os.path.join(root, name))

                    if len(paths) >= MAX_SCAN_FILES:
                        truncated = True
                        break

                if truncated:
                    break

            if truncated:
                break

        options = self.scoring_options()
        matches = []

        for position in positions:
            entry = self.playlist.entry(position)

            if entry is None:
                continue

            found = napstr_match.find_local_matches(entry, paths, options=options, threshold=0.85)

            if found:
                matches.append((position, found[0][0], found[0][1]))

        events.invoke_main_thread(self._on_scan_finished, matches, len(paths), truncated)

    def _on_scan_finished(self, matches, num_scanned, truncated):

        if self._shutting_down or self.playlist is None:
            return

        note = " (stopped early, folder is very large)" if truncated else ""

        for position, path, score in matches:
            entry = self.playlist.entry(position)

            if entry is None:
                continue

            entry["local_path"] = path
            entry["score"] = score
            self.playlist.set_status(
                entry, napstr_state.STATUS_DOWNLOADED, f"found locally (score {score:.2f})")
            self._report(f"Entry {position} found locally: {os.path.basename(path)} (score {score:.2f})")
            self._queue_hash(entry)

        self.playlist.save()
        self._report(
            f"Scan finished: {num_scanned} file(s) considered, "
            f"{len(matches)} entr(y/ies) matched{note}.")

    # ------------------------------------------------------------------
    # hashing
    # ------------------------------------------------------------------

    def _action_hash(self, rest):

        if not self._require_playlist():
            return False

        argument = rest.strip().lower()
        positions = self._resolve_positions(rest)

        # 'all' re-hashes everything, 'missing' and a single entry only fill in
        # entries that have no file ID yet.
        force = argument in {"all", "*"}

        if not positions:
            self.output("Usage: /napstr hash <entry number|all|missing>")
            return False

        queued = 0
        missing_path = []

        for position in positions:
            entry = self.playlist.entry(position)

            if entry is None:
                continue

            if entry.get("file_id") and not force:
                continue

            path = entry.get("local_path")

            if not path or not os.path.isfile(path):
                missing_path.append(position)
                continue

            self._queue_hash(entry)
            queued += 1

        if queued:
            self.output(f"Hashing {queued} file(s) in the background.")

        if missing_path:
            self.output(
                "No local file yet for: " + ", ".join(str(position) for position in missing_path[:20])
                + " (use /napstr pick, /napstr scan or /napstr setpath).")

        return True

    def _queue_hash(self, entry):

        if self._shutting_down:
            return

        position = entry["position"]
        path = entry.get("local_path")

        if not path:
            return

        cancel_event = threading.Event()
        self._hash_cancellations[position] = cancel_event

        thread = threading.Thread(
            target=self._hash_worker, args=(position, path, cancel_event),
            name=f"napstr-hash-{position}", daemon=True)
        thread.start()

    def _hash_worker(self, position, path, cancel_event):

        try:
            digest = napstr_state.sha256_file(path, cancel_event=cancel_event)
            error = None

        except (OSError, InterruptedError) as exception:
            digest = None
            error = str(exception)

        events.invoke_main_thread(self._on_hashed, position, digest, error)

    def _on_hashed(self, position, digest, error):

        if self._shutting_down or self.playlist is None:
            return

        self._hash_cancellations.pop(position, None)
        entry = self.playlist.entry(position)

        if entry is None:
            return

        if error:
            self.playlist.set_status(entry, napstr_state.STATUS_FAILED, f"hashing failed: {error}")
            self._report(f"Entry {position}: hashing failed: {error}", notify=True)

        else:
            entry["file_id"] = digest
            entry["hashed_at"] = int(time.time())
            self.playlist.set_status(entry, napstr_state.STATUS_HASHED)
            self._report(f"Entry {position} hashed: {digest}")

        self.playlist.save()

    # ------------------------------------------------------------------
    # review dialog
    # ------------------------------------------------------------------

    def _action_review(self, _rest):

        if not self._require_playlist():
            return False

        try:
            import napstr_dialog  # pylint: disable=import-outside-toplevel

        except ImportError as error:
            self.output(f"The GUI is not available here ({error}). Use the commands instead.")
            return False

        try:
            napstr_dialog.open_review_window(self)

        except Exception as error:  # pylint: disable=broad-except
            self.output(f"Could not open the review window: {error}")
            return False

        return True

    # ------------------------------------------------------------------
    # publishing
    # ------------------------------------------------------------------

    def _action_relays(self, _rest):

        relays = self._relay_urls()

        if not relays:
            self.output("No relays configured.")
            return False

        self.output(f"Checking {len(relays)} relay(s)...")

        thread = threading.Thread(target=self._relay_check_worker, args=(relays,),
                                  name="napstr-relays", daemon=True)
        thread.start()

        return True

    def _relay_check_worker(self, relays):

        try:
            num_events, results = self._make_pool(relays).query([{
                "kinds": [napstr_event.KIND_PLAYLIST],
                "#t": [napstr_event.MARKER_TAG],
                "limit": 5
            }])

        except napstr_relay.RelayError as error:
            events.invoke_main_thread(self._relay_check_finished, [], str(error))
            return

        events.invoke_main_thread(self._relay_check_finished, results, None, num_events)

    def _relay_check_finished(self, results, error, num_events=0):

        if self._shutting_down:
            return

        if error:
            self._report(f"Relay check failed: {error}", notify=True)
            return

        for result in results:
            self._report(f"Relay {result.relay_url}: {result.describe()}")

        reachable = sum(1 for result in results if result.ok)
        self._report(
            f"{reachable}/{len(results)} relay(s) reachable; "
            f"{num_events} existing napstr playlist event(s) seen in this sample.")

    def _action_publish(self, rest):

        if not self._require_playlist():
            return False

        if self._publishing:
            self.output("A publish is already in progress.")
            return False

        title = napstr_event.sanitize_text(rest) or self.playlist.title

        if not title:
            self.output("The playlist needs a title. Use /napstr title <title>.")
            return False

        secret_key, error = self._credentials()

        if secret_key is None:
            self.output(error)
            return False

        relays = self._relay_urls()

        if not relays:
            self.output("No relays configured.")
            return False

        tracks, skipped = self.playlist.publishable_tracks()

        if not tracks:
            self.output("No entry has a file ID yet. Run /napstr hash all first.")
            return False

        if skipped and self.settings.get("require_full"):
            details = "; ".join(
                f"{position}: {reason}" for position, reason in skipped[:10])
            remainder = f" (and {len(skipped) - 10} more)" if len(skipped) > 10 else ""

            self.output(
                f"Refusing to publish: 'require_full' is enabled and {len(skipped)} member "
                f"slot(s) cannot be included - {details}{remainder}.")
            self.output(
                "Resolve them (/napstr auto missing, /napstr scan, /napstr setpath), or turn "
                "'require_full' off to publish only the resolved members.")
            return False

        try:
            event = napstr_event.build_playlist_event(
                self.playlist.id, title, tracks,
                tags=self.playlist.tags,
                artist=self.playlist.artist,
                mbid=self.playlist.mbid,
                image=self.playlist.image,
                client=self.settings.get("client_tag") or napstr_event.CLIENT_NAME)
            event = napstr_event.sign_event(event, secret_key)

        except (napstr_event.EventError, ValueError) as build_error:
            self.output(f"Could not build the event: {build_error}")
            return False

        problems = napstr_event.validate_playlist_event(event)

        if problems:
            self.output("Refusing to publish, the event is invalid:")
            for problem in problems:
                self.output(f"\t- {problem}")
            return False

        # The playlist ID must survive a crash, or the coordinate is orphaned
        # on the relays with no way to revise or retract it.
        self.playlist.title = title
        self.playlist.dirty = True
        self.playlist.save(force=True)

        self._publishing = True

        if skipped:
            self.output(
                f"Note: {len(skipped)} member slot(s) are omitted because they have no file ID "
                "or repeat one; the event only carries the remaining members.")

        self.output(
            f"Publishing '{title}' with {len(tracks)} member(s) to {len(relays)} relay(s)...")

        thread = threading.Thread(target=self._publish_worker, args=(event, relays),
                                  name="napstr-publish", daemon=True)
        thread.start()

        return True

    def _publish_worker(self, event, relays):

        try:
            results = self._make_pool(relays).publish(event)

        except napstr_relay.RelayError as error:
            events.invoke_main_thread(self._publish_finished, event, None, str(error))
            return

        events.invoke_main_thread(self._publish_finished, event, results, None)

    def _publish_finished(self, event, results, error):

        self._publishing = False

        if self._shutting_down or self.playlist is None:
            return

        if error:
            self._report(f"Publishing failed: {error}", notify=True)
            return

        accepted = [result for result in results if result.ok]

        for result in results:
            self._report(f"Relay {result.relay_url}: {result.describe()}")

        if accepted:
            self.playlist.published.append({
                "event_id": event["id"],
                "created_at": event["created_at"],
                "relays": [result.relay_url for result in accepted],
                "title": self.playlist.title,
                "members": sum(1 for tag in event["tags"] if tag[0] == "x")
            })
            self.playlist.save(force=True)

            self._report(
                f"Published to {len(accepted)}/{len(results)} relay(s). "
                f"Event {event['id']}", notify=True)

        else:
            self._report(
                "No relay accepted the event. Check /napstr relays and the relay timeout.", notify=True)

    def _action_unpublish(self, _rest):

        if not self._require_playlist():
            return False

        if self._publishing:
            self.output("A publish is already in progress.")
            return False

        secret_key, error = self._credentials()

        if secret_key is None:
            self.output(error)
            return False

        relays = self._relay_urls()

        if not relays:
            self.output("No relays configured.")
            return False

        try:
            event = napstr_event.sign_event(
                napstr_event.build_withdrawal_event(self.playlist.id), secret_key)

        except (napstr_event.EventError, ValueError) as build_error:
            self.output(f"Could not build the withdrawal event: {build_error}")
            return False

        self._publishing = True
        self.output(f"Retracting playlist {self.playlist.id}...")

        thread = threading.Thread(target=self._unpublish_worker, args=(event, relays),
                                  name="napstr-unpublish", daemon=True)
        thread.start()

        return True

    def _unpublish_worker(self, event, relays):

        try:
            results = self._make_pool(relays).publish(event)

        except napstr_relay.RelayError as error:
            events.invoke_main_thread(self._unpublish_finished, None, str(error))
            return

        events.invoke_main_thread(self._unpublish_finished, results, None)

    def _unpublish_finished(self, results, error):

        self._publishing = False

        if self._shutting_down or self.playlist is None:
            return

        if error:
            self._report(f"Could not retract the playlist: {error}", notify=True)
            return

        for result in results:
            self._report(f"Relay {result.relay_url}: {result.describe()}")

        accepted = [result for result in results if result.ok]

        self.playlist.published.append({
            "event_id": None,
            "withdrawal": True,
            "created_at": int(time.time()),
            "relays": [result.relay_url for result in accepted]
        })
        self.playlist.dirty = True
        self.playlist.save(force=True)

        if accepted:
            self._report(f"Withdrawal published to {len(accepted)}/{len(results)} relay(s).", notify=True)

        else:
            self._report("No relay accepted the withdrawal.", notify=True)
