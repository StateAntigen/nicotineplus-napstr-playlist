# SPDX-FileCopyrightText: 2026 StateAntigen
# SPDX-License-Identifier: 0BSD
"""Playlist state persistence and file hashing.

A playlist under construction is a JSON document, one file per playlist,
stored next to Nicotine+ user data in ``<data folder>/napstr/playlists/``.
Publishing is a separate, explicit step: the document holds the playlist UUID
so that revisions replace the same NAPSTR coordinate, which is what the NIP
requires clients to persist.
"""

import hashlib
import json
import os
import tempfile
import time
import uuid

__all__ = [
    "HASH_CHUNK_SIZE",
    "PLUGIN_FOLDER_NAME",
    "STATUSES",
    "PlaylistState",
    "delete_playlist",
    "list_playlists",
    "load_playlist",
    "new_playlist_id",
    "reset_entries",
    "sha256_file",
    "state_folder_path",
    "staging_folder_path",
]

PLUGIN_FOLDER_NAME = "napstr"
HASH_CHUNK_SIZE = 1024 * 1024
SCHEMA_VERSION = 1

STATUS_NEW = "new"
STATUS_SEARCHING = "searching"
STATUS_REVIEW = "review"
STATUS_QUEUED = "queued"
STATUS_DOWNLOADING = "downloading"
STATUS_DOWNLOADED = "downloaded"
STATUS_HASHED = "hashed"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"
STATUS_UNAVAILABLE = "unavailable"

STATUSES = (
    STATUS_NEW, STATUS_SEARCHING, STATUS_REVIEW, STATUS_QUEUED, STATUS_DOWNLOADING,
    STATUS_DOWNLOADED, STATUS_HASHED, STATUS_SKIPPED, STATUS_FAILED, STATUS_UNAVAILABLE
)

# Statuses that need no further action
FINAL_STATUSES = (STATUS_HASHED, STATUS_SKIPPED)


def new_playlist_id():
    return str(uuid.uuid4())


def state_folder_path(data_folder_path):
    return os.path.join(data_folder_path, PLUGIN_FOLDER_NAME)


def staging_folder_path(data_folder_path, playlist_id):
    return os.path.join(state_folder_path(data_folder_path), "files", playlist_id)


def _playlists_folder_path(data_folder_path):
    return os.path.join(state_folder_path(data_folder_path), "playlists")


def _index_file_path(data_folder_path):
    return os.path.join(state_folder_path(data_folder_path), "index.json")


def sha256_file(file_path, chunk_size=HASH_CHUNK_SIZE, progress_callback=None, cancel_event=None):
    """Return the lowercase SHA-256 hex digest of a file.

    NAPSTR file IDs are the SHA-256 of the file's bytes, so this is the value
    that goes into the ``x`` tags and ``content.tracks[].fileId``.
    """

    digest = hashlib.sha256()
    total = 0

    with open(file_path, "rb") as file_handle:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("hashing cancelled")

            chunk = file_handle.read(chunk_size)

            if not chunk:
                break

            digest.update(chunk)
            total += len(chunk)

            if progress_callback is not None:
                progress_callback(total)

    return digest.hexdigest()


class PlaylistState:
    """In-memory playlist document with explicit save."""

    def __init__(self, data_folder_path, playlist_id=None, title="", entries=None,
                 tags=None, source="", artist=None, mbid=None, image=None):
        self.data_folder_path = data_folder_path
        self.id = playlist_id or new_playlist_id()
        self.title = title or "Untitled playlist"
        self.tags = list(tags or [])
        self.source = source or ""
        self.artist = artist
        self.mbid = mbid
        self.image = image
        self.created = int(time.time())
        self.published = []
        self.entries = []
        self.dirty = True

        for index, entry in enumerate(entries or [], start=1):
            self.add_entry(entry, position=index)

    # -- entries -----------------------------------------------------------

    def add_entry(self, entry, position=None):
        record = {
            "position": position if position is not None else len(self.entries) + 1,
            "uri": entry.get("uri", ""),
            "title": entry.get("title", ""),
            "artist": entry.get("artist", ""),
            "album": entry.get("album", ""),
            "album_artist": entry.get("album_artist", ""),
            "duration_ms": entry.get("duration_ms"),
            "isrc": entry.get("isrc", ""),
            "status": STATUS_NEW,
            "query": "",
            "search_token": None,
            "candidates": [],
            "score": None,
            "chosen": None,
            "local_path": "",
            "file_id": "",
            "hashed_at": None,
            "notes": ""
        }

        record["position"] = len(self.entries) + 1
        self.entries.append(record)
        self.dirty = True

        return record

    def entry(self, position):
        """Return the entry with the given 1-based position, or ``None``."""

        try:
            position = int(position)

        except (TypeError, ValueError):
            return None

        if 1 <= position <= len(self.entries):
            return self.entries[position - 1]

        return None

    def entry_by_uri(self, uri):
        for entry in self.entries:
            if uri and entry.get("uri") == uri:
                return entry

        return None

    def set_status(self, entry, status, note=None):
        entry["status"] = status

        if note is not None:
            entry["notes"] = note

        self.dirty = True

    def paths_for_download(self):
        """Folder that members downloaded for this playlist are stored in."""

        return staging_folder_path(self.data_folder_path, self.id)

    # -- derivation --------------------------------------------------------

    def resolved_entries(self):
        return [entry for entry in self.entries if entry.get("file_id")]

    def publishable_tracks(self):
        """Return ``(tracks, skipped)`` in member order, deduplicated by file ID.

        The NIP forbids repeating a file ID inside one playlist, and positions
        must be contiguous from 1, so duplicates are dropped and reported.
        """

        tracks = []
        skipped = []
        seen = set()

        for entry in self.entries:
            file_id = entry.get("file_id")

            if not file_id:
                skipped.append((entry["position"], "no file ID yet"))
                continue

            if file_id in seen:
                skipped.append((entry["position"], f"duplicate file ID of an earlier member ({file_id})"))
                continue

            seen.add(file_id)
            tracks.append({
                "position": len(tracks) + 1,
                "file_id": file_id,
                "title": entry.get("title", ""),
                "artist": entry.get("artist", ""),
                "album": entry.get("album", "")
            })

        return tracks, skipped

    def summary(self):

        counts = {status: 0 for status in STATUSES}

        for entry in self.entries:
            counts[entry.get("status", STATUS_NEW)] = counts.get(entry.get("status", STATUS_NEW), 0) + 1

        return {
            "total": len(self.entries),
            "with_file_id": len(self.resolved_entries()),
            "counts": counts
        }

    # -- persistence -------------------------------------------------------

    def to_dict(self):

        return {
            "schema": SCHEMA_VERSION,
            "playlist": {
                "id": self.id,
                "title": self.title,
                "tags": list(self.tags),
                "source": self.source,
                "artist": self.artist,
                "mbid": self.mbid,
                "image": self.image,
                "created": self.created,
                "published": list(self.published)
            },
            "entries": self.entries
        }

    @classmethod
    def from_dict(cls, data, data_folder_path):

        playlist = data.get("playlist") or {}
        state = cls(
            data_folder_path,
            playlist_id=playlist.get("id") or new_playlist_id(),
            title=playlist.get("title", ""),
            tags=playlist.get("tags") or [],
            source=playlist.get("source", ""),
            artist=playlist.get("artist"),
            mbid=playlist.get("mbid"),
            image=playlist.get("image")
        )
        state.created = playlist.get("created") or int(time.time())
        state.published = list(playlist.get("published") or [])
        state.entries = list(data.get("entries") or [])

        for index, entry in enumerate(state.entries, start=1):
            entry["position"] = index
            entry.setdefault("status", STATUS_NEW)
            entry.setdefault("candidates", [])
            entry.setdefault("local_path", "")
            entry.setdefault("file_id", "")
            entry.setdefault("notes", "")

        state.dirty = False

        return state

    def file_path(self):
        return os.path.join(_playlists_folder_path(self.data_folder_path), f"{self.id}.json")

    def save(self, force=False):
        """Write the document, then refresh the index. Returns the file path."""

        if not (self.dirty or force):
            return self.file_path()

        file_path = self.file_path()

        _write_json(file_path, self.to_dict())
        self.dirty = False
        self._write_index()

        return file_path

    def _write_index(self):
        """Refresh the convenience index; never let it break a save.

        The index is derived - :func:`list_playlists` rebuilds it by scanning
        the playlist documents - so a locked or unwritable index costs nothing
        but a stale summary, while failing the caller's save over it would lose
        real state. Windows holds files open for scanning often enough that this
        does happen.
        """

        index = load_index(self.data_folder_path)
        index[self.id] = {
            "title": self.title,
            "source": self.source,
            "updated": int(time.time()),
            "total": len(self.entries),
            "with_file_id": len(self.resolved_entries())
        }

        try:
            _write_json(_index_file_path(self.data_folder_path), index)

        except OSError:
            pass


def _write_json(file_path, data):
    """Write JSON through a uniquely named temporary file, then swap it in.

    The temporary name has to be unique. A fixed ``file.json.tmp`` lets two
    writers interleave into one file and swap in each other's half-written
    bytes, which is a corrupted playlist. The swap itself is atomic on both
    POSIX and Windows, so a reader sees either the old document or the new one.
    """

    folder = os.path.dirname(file_path)

    if folder:
        os.makedirs(folder, exist_ok=True)

    handle, temporary_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(file_path)}.", suffix=".tmp", dir=folder or None)

    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file_handle:
            json.dump(data, file_handle, indent=2, ensure_ascii=False)

        os.replace(temporary_path, file_path)

    except BaseException:
        try:
            os.remove(temporary_path)

        except OSError:
            pass

        raise


def reset_entries(playlist, positions=None, drop_files=False):
    """Clear what was *decided* about entries, keeping what is *known*.

    Search results and choices are throwaway; a file ID and local path are
    facts about bytes on disk, so they survive unless ``drop_files`` is set.
    Returns the number of entries changed.
    """

    wanted = None if positions is None else set(positions)
    changed = 0

    for entry in playlist.entries:
        if wanted is not None and entry.get("position") not in wanted:
            continue

        entry["candidates"] = []
        entry["chosen"] = None
        entry["score"] = None
        entry["search_token"] = None
        entry["query"] = ""
        entry["notes"] = ""

        if drop_files:
            entry["file_id"] = ""
            entry["local_path"] = ""
            entry["hashed_at"] = None

        entry["status"] = STATUS_NEW
        changed += 1

    if changed:
        playlist.dirty = True

    return changed


def load_index(data_folder_path):

    file_path = _index_file_path(data_folder_path)

    if not os.path.isfile(file_path):
        return {}

    try:
        with open(file_path, encoding="utf-8") as file_handle:
            return json.load(file_handle) or {}

    except (OSError, ValueError):
        return {}


def list_playlists(data_folder_path):
    """Return ``{playlist_id: summary}`` for every stored playlist."""

    folder = _playlists_folder_path(data_folder_path)
    playlists = {}

    if not os.path.isdir(folder):
        return playlists

    for name in sorted(os.listdir(folder)):
        if not name.endswith(".json"):
            continue

        playlist_id = name[:-len(".json")]
        state = load_playlist(data_folder_path, playlist_id)

        if state is None:
            continue

        summary = state.summary()
        playlists[playlist_id] = {
            "id": playlist_id,
            "title": state.title,
            "source": state.source,
            "total": summary["total"],
            "with_file_id": summary["with_file_id"],
            "published": bool(state.published),
            "updated": os.path.getmtime(os.path.join(folder, name))
        }

    return playlists


def load_playlist(data_folder_path, playlist_id):

    if not playlist_id:
        return None

    file_path = os.path.join(_playlists_folder_path(data_folder_path), f"{playlist_id}.json")

    if not os.path.isfile(file_path):
        return None

    try:
        with open(file_path, encoding="utf-8") as file_handle:
            data = json.load(file_handle)

    except (OSError, ValueError):
        return None

    if not isinstance(data, dict):
        return None

    return PlaylistState.from_dict(data, data_folder_path)


def delete_playlist(data_folder_path, playlist_id):

    file_path = os.path.join(_playlists_folder_path(data_folder_path), f"{playlist_id}.json")

    if os.path.isfile(file_path):
        os.remove(file_path)

    index = load_index(data_folder_path)

    if playlist_id in index:
        del index[playlist_id]

        index_path = _index_file_path(data_folder_path)
        temporary_path = f"{index_path}.tmp"

        with open(temporary_path, "w", encoding="utf-8") as file_handle:
            json.dump(index, file_handle, indent=2, ensure_ascii=False)

        os.replace(temporary_path, index_path)
