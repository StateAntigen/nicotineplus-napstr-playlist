# SPDX-FileCopyrightText: 2026 StateAntigen
# SPDX-License-Identifier: 0BSD
"""Nostr event construction for the NAPSTR playlist kind.

Implements the rules from ``NIP-NAPSTR-PLAYLIST.md`` (kind ``30425``):
playlist identity is a canonical lowercase UUID, members are SHA-256 file IDs
carried both as ordered ``x`` tags and in the JSON content, and the content is
bounded to 128 KiB with at most 500 members.

Only the standard library is used so this module stays unit testable outside
of Nicotine+.
"""

import hashlib
import json
import re
import time
import unicodedata
import uuid

import napstr_crypto as crypto

__all__ = [
    "ALT_TEXT",
    "CLIENT_NAME",
    "KIND_PLAYLIST",
    "MARKER_TAG",
    "MAX_CONTENT_BYTES",
    "MAX_MEMBERS",
    "MAX_TAGS",
    "MAX_TAG_LENGTH",
    "PROTOCOL_VERSION",
    "build_playlist_event",
    "build_withdrawal_event",
    "compute_event_id",
    "is_withdrawal",
    "new_playlist_id",
    "sanitize_tags",
    "sanitize_text",
    "serialize_for_id",
    "sign_event",
    "validate_playlist_event",
]

PROTOCOL_VERSION = "napstr/1"
KIND_PLAYLIST = 30425
MARKER_TAG = "napstr-playlist"
ALT_TEXT = "Napstr public playlist"
CLIENT_NAME = "napstr-playlist (Nicotine+)"

MAX_CONTENT_BYTES = 128 * 1024
MAX_MEMBERS = 500
MAX_TITLE_LENGTH = 256
MAX_TAG_LENGTH = 32
MAX_TAGS = 12

# Unsafe control characters plus bidirectional formatting overrides, as
# forbidden by the catalogue metadata rules.
_UNSAFE_CHARACTERS = re.compile(
    "[\u0000-\u0008\u000a-\u001f\u007f-\u009f\u061c\u200e\u200f"
    "\u202a-\u202e\u2066-\u2069]"
)
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class EventError(ValueError):
    """Raised when an event cannot be built from the supplied values."""


def sanitize_text(value, max_length=MAX_TITLE_LENGTH):
    """Strip unsafe characters and bound the length of display metadata."""

    if value is None:
        return ""

    text = _UNSAFE_CHARACTERS.sub("", str(value))
    text = unicodedata.normalize("NFC", text).strip()

    return text[:max_length]


def new_playlist_id():
    """Return a fresh canonical lowercase UUID (version 4)."""

    return str(uuid.uuid4())


def is_canonical_uuid(value):
    return bool(value) and bool(_UUID_RE.match(value))


def is_file_id(value):
    return bool(value) and bool(_SHA256_RE.match(value))


def sanitize_tags(words):
    """Apply the catalogue tag rules to an author supplied word list.

    A word that cannot be carried is dropped and an over-long list is cut
    short, rather than rejecting the playlist.
    """

    if not words:
        return []

    if isinstance(words, str):
        words = words.split(",")

    result = []
    seen = set()

    for word in words:
        candidate = sanitize_text(word, MAX_TAG_LENGTH).strip().lower()
        candidate = " ".join(candidate.split())

        if not candidate or candidate in seen:
            continue

        seen.add(candidate)
        result.append(candidate)

        if len(result) >= MAX_TAGS:
            break

    return result


def build_playlist_event(
        playlist_id, title, tracks, tags=None, artist=None, mbid=None, image=None,
        client=CLIENT_NAME, created_at=None):
    """Build an unsigned kind 30425 event.

    ``tracks`` is an ordered sequence of mappings with ``file_id`` and the
    optional display hints ``title``, ``artist`` and ``album``.
    """

    if not is_canonical_uuid(playlist_id):
        raise EventError(f"playlist id must be a canonical lowercase UUID, got {playlist_id!r}")

    clean_title = sanitize_text(title, MAX_TITLE_LENGTH)

    if not clean_title:
        raise EventError("playlist title must be present and non-empty")

    if not tracks:
        raise EventError("a playlist needs at least one member")

    if len(tracks) > MAX_MEMBERS:
        raise EventError(f"a playlist carries at most {MAX_MEMBERS} members, got {len(tracks)}")

    content_tracks = []
    seen_file_ids = set()
    x_tags = []

    for index, track in enumerate(tracks):
        file_id = str(track.get("file_id", "")).lower()

        if not is_file_id(file_id):
            raise EventError(f"member {index + 1} is not a valid lowercase SHA-256 file ID: {file_id!r}")

        if file_id in seen_file_ids:
            raise EventError(f"member {file_id} is listed more than once")

        seen_file_ids.add(file_id)

        member = {
            "position": index + 1,
            "fileId": file_id
        }

        # Hints are display metadata only; they never affect membership.
        for key, source in (("title", "title"), ("artist", "artist"), ("album", "album")):
            hint = sanitize_text(track.get(source), MAX_TITLE_LENGTH)

            if hint:
                member[key] = hint

        content_tracks.append(member)
        x_tags.append(["x", file_id])

    author_tags = sanitize_tags(tags)

    content = {
        "protocol": PROTOCOL_VERSION,
        "playlistId": playlist_id,
        "title": clean_title
    }

    if author_tags:
        content["tags"] = author_tags

    clean_artist = sanitize_text(artist, MAX_TITLE_LENGTH)

    if clean_artist:
        content["artist"] = clean_artist

    if mbid:
        mbid = str(mbid).lower()

        if is_canonical_uuid(mbid):
            content["mbid"] = mbid

    if image:
        image = str(image).lower()

        # An https:// value is not a file ID, and must never be fetched.
        if is_file_id(image):
            content["image"] = image

    content["tracks"] = content_tracks

    event_tags = [
        ["d", playlist_id],
        ["t", MARKER_TAG],
        ["title", clean_title],
        ["alt", ALT_TEXT]
    ]

    clean_client = sanitize_text(client, MAX_TAG_LENGTH)

    if clean_client:
        event_tags.append(["client", clean_client])

    event_tags.extend(x_tags)
    event_tags.extend(["t", word] for word in author_tags)

    event = {
        "kind": KIND_PLAYLIST,
        "tags": event_tags,
        "content": json.dumps(content, separators=(",", ":"), ensure_ascii=False),
        "created_at": int(created_at if created_at is not None else time.time())
    }

    size = len(event["content"].encode("utf-8"))

    if size > MAX_CONTENT_BYTES:
        raise EventError(
            f"playlist content is {size} bytes, above the {MAX_CONTENT_BYTES} byte budget")

    return event


def build_withdrawal_event(playlist_id, created_at=None):
    """Build the withdrawal body that retracts a playlist coordinate."""

    if not is_canonical_uuid(playlist_id):
        raise EventError("playlist id must be a canonical lowercase UUID")

    return {
        "kind": KIND_PLAYLIST,
        "tags": [
            ["d", playlist_id],
            ["t", MARKER_TAG]
        ],
        "content": json.dumps(
            {"protocol": PROTOCOL_VERSION, "deleted": True}, separators=(",", ":")),
        "created_at": int(created_at if created_at is not None else time.time())
    }


def is_withdrawal(event):
    try:
        content = json.loads(event.get("content") or "{}")

    except (TypeError, ValueError):
        return False

    return content.get("deleted") is True


def serialize_for_id(event):
    """Return the canonical JSON an event ID commits to (NIP-01)."""

    payload = [
        0,
        event.get("pubkey", ""),
        int(event.get("created_at", 0)),
        int(event.get("kind", 0)),
        event.get("tags", []),
        event.get("content", "")
    ]

    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def compute_event_id(event):
    return hashlib.sha256(serialize_for_id(event)).hexdigest()


def sign_event(event, secret_key):
    """Add ``pubkey``, ``id`` and ``sig`` to an event and return the new event."""

    public_key = crypto.get_public_key(secret_key)
    event = dict(event)
    event["pubkey"] = public_key.hex()
    event.pop("id", None)
    event.pop("sig", None)

    event_id = compute_event_id(event)
    signature = crypto.schnorr_sign(bytes.fromhex(event_id), secret_key)

    event["id"] = event_id
    event["sig"] = signature.hex()

    return event


def signature_problems(event):
    """NIP-01 checks, for events that carry a pubkey, an id and a signature."""

    problems = []

    if not all(key in event for key in ("pubkey", "id", "sig")):
        return problems

    unsigned = {key: value for key, value in event.items() if key not in ("id", "sig")}

    if compute_event_id(unsigned) != event.get("id"):
        problems.append("event id does not match the serialized event")

    try:
        valid_signature = crypto.schnorr_verify(
            bytes.fromhex(event["id"]),
            bytes.fromhex(event["pubkey"]),
            bytes.fromhex(event["sig"]))

    except (TypeError, ValueError):
        valid_signature = False

    if not valid_signature:
        problems.append("event signature is invalid")

    return problems


def _with_signature_checks(event, problems):
    return problems + signature_problems(event)


def validate_playlist_event(event):
    """Return a list of validation problems, empty when the event is valid."""

    problems = []

    if not isinstance(event, dict):
        return ["event is not an object"]

    if event.get("kind") != KIND_PLAYLIST:
        problems.append(f"kind must be {KIND_PLAYLIST}")

    tags = event.get("tags")

    if not isinstance(tags, list):
        return _with_signature_checks(event, problems + ["tags must be a list"])

    def tag_values(name):
        return [tag[1] for tag in tags if isinstance(tag, list) and len(tag) == 2 and tag[0] == name]

    coordinate = tag_values("d")

    if len(coordinate) != 1 or not is_canonical_uuid(coordinate[0]):
        problems.append("a single canonical lowercase UUID 'd' tag is required")

    if MARKER_TAG not in tag_values("t"):
        problems.append(f"the literal '{MARKER_TAG}' marker tag is required")

    content_text = event.get("content")

    if not isinstance(content_text, str):
        return _with_signature_checks(event, problems + ["content must be a string"])

    if len(content_text.encode("utf-8")) > MAX_CONTENT_BYTES:
        problems.append(f"content exceeds {MAX_CONTENT_BYTES} bytes")

    try:
        content = json.loads(content_text)

    except ValueError:
        return _with_signature_checks(event, problems + ["content is not valid JSON"])

    if not isinstance(content, dict):
        return _with_signature_checks(event, problems + ["content is not a JSON object"])

    if content.get("deleted") is True:
        # A withdrawal body describes no playlist: the 'd' tag is the identity,
        # and there is deliberately no title and no member list.
        if "playlistId" in content:
            problems.append("a withdrawal body must not carry content.playlistId")

        return _with_signature_checks(event, problems)

    titles = tag_values("title")

    if len(titles) != 1 or not sanitize_text(titles[0]):
        problems.append("a non-empty 'title' tag is required")

    if tag_values("alt") != [ALT_TEXT]:
        problems.append(f"'alt' must be the literal {ALT_TEXT!r}")

    if content.get("protocol") != PROTOCOL_VERSION:
        problems.append(f"content.protocol must be {PROTOCOL_VERSION!r}")

    if coordinate and content.get("playlistId") != coordinate[0]:
        problems.append("content.playlistId must equal the 'd' tag value")

    if titles and content.get("title") != titles[0]:
        problems.append("content.title must equal the 'title' tag value")

    if content.get("tags") is not None and not isinstance(content["tags"], list):
        problems.append("content.tags must be a list when present")

    tracks = content.get("tracks")

    if not isinstance(tracks, list) or not tracks:
        return _with_signature_checks(event, problems + ["content.tracks must be a non-empty list"])

    if len(tracks) > MAX_MEMBERS:
        problems.append(f"content carries more than {MAX_MEMBERS} members")

    positions = []
    file_ids = []

    for index, track in enumerate(tracks):
        if not isinstance(track, dict):
            problems.append(f"member {index + 1} is not an object")
            continue

        positions.append(track.get("position"))
        file_ids.append(track.get("fileId"))

        if not is_file_id(track.get("fileId", "")):
            problems.append(f"member {index + 1} has an invalid file ID")

        if not isinstance(track.get("position"), int):
            problems.append(f"member {index + 1} has a non-integer position")

        for key in ("title", "artist", "album"):
            if key in track and not isinstance(track[key], str):
                problems.append(f"member {index + 1} hint '{key}' is not a string")

    if positions != list(range(1, len(tracks) + 1)):
        problems.append("member positions must be contiguous and start at 1")

    if len(set(file_ids)) != len(file_ids):
        problems.append("file IDs must be unique within the playlist")

    x_tags = tag_values("x")

    if len(x_tags) != len(file_ids):
        problems.append("there must be exactly one 'x' tag per member")

    elif set(x_tags) != set(file_ids):
        problems.append("'x' tags must carry the same file IDs as content.tracks")

    image = content.get("image")

    if image is not None and not is_file_id(image):
        problems.append("content.image must be a lowercase SHA-256 file ID when present")

    mbid = content.get("mbid")

    if mbid is not None and not is_canonical_uuid(mbid):
        problems.append("content.mbid must be a canonical lowercase UUID when present")

    for tag in tags:
        if isinstance(tag, list) and tag and tag[0] == "x" and tag[1] not in file_ids:
            problems.append("an 'x' tag does not correspond to a member")

    return _with_signature_checks(event, problems)
