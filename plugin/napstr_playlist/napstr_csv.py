# SPDX-FileCopyrightText: 2026 StateAntigen
# SPDX-License-Identifier: 0BSD
"""Parser for Spotify playlist exports produced by https://exportify.net/.

Exportify writes UTF-8 CSV with a header row. A typical file starts with::

    Track URI,Track Name,Artist URI(s),Artist Name(s),Album URI,Album Name,...

Older and newer exports differ in which optional columns are present, so
columns are resolved by name and unknown columns are ignored.
"""

import csv
import io
import os
import re

__all__ = [
    "ExportifyError",
    "build_search_query",
    "parse_exportify_csv",
    "primary_artist",
    "read_exportify_file",
    "strip_feature_credits",
]

# Header aliases, lowercased and stripped of punctuation
_COLUMN_ALIASES = {
    "uri": ("track uri", "trackuri", "spotify uri", "url"),
    "title": ("track name", "trackname", "name", "title", "song"),
    "artist": ("artist name(s)", "artist names", "artist name", "artist", "artists"),
    "album": ("album name", "albumname", "album"),
    "album_artist": ("album artist name(s)", "album artist names", "album artist name", "album artist"),
    "duration_ms": (
        "track duration (ms)", "track duration", "duration (ms)", "duration ms",
        "song length", "track length", "length", "duration"
    ),
    "isrc": ("isrc",),
    "disc_number": ("disc number", "disc"),
    "track_number": ("track number", "track"),
    "added_at": ("added at",),
    "added_by": ("added by",),
    "release_date": ("album release date", "release date"),
    "popularity": ("popularity",),
    "explicit": ("explicit?", "explicit"),
}


class ExportifyError(ValueError):
    """Raised when a file does not look like an Exportify CSV export."""


# Exportify joins several artists with ";". Every word in a Soulseek search is
# a required term, so searching the whole credit list almost never matches
# anything: "Above & Beyond;Malou Letting Go" asks for five words at once.
_ARTIST_SEPARATORS = (";",)

# Bracketed feature credits are noise in a search term: "(feat. Luke Steele)"
# adds three required words that file names usually do not carry.
_FEATURE_MARKERS = (
    "feat", "ft.", "ft ", "ft.", "featuring", "with ", "w/", "prod.", "produced by",
    "starring", "vs.", "vs "
)

_BRACKETS = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_MULTISPACE = re.compile(r"\s+")


def _normalize_header(name):
    if name is None:
        return ""

    return " ".join(str(name).replace("\ufeff", "").strip().lower().split())


def _resolve_columns(fieldnames):

    resolved = {}
    normalized = {}

    for field in fieldnames or []:
        normalized.setdefault(_normalize_header(field), field)

    for key, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                resolved[key] = normalized[alias]
                break

    return resolved


def _clean(value):
    if value is None:
        return ""

    return " ".join(str(value).replace("\ufeff", "").split())


def _parse_duration(value):
    text = _clean(value)

    if not text:
        return None

    try:
        return int(float(text))

    except ValueError:
        # Some exports use "3:45"
        parts = text.split(":")

        try:
            seconds = 0

            for part in parts:
                seconds = seconds * 60 + int(float(part))

            return seconds * 1000

        except ValueError:
            return None


def parse_exportify_csv(text, source=""):
    """Parse CSV text into a list of entry dictionaries."""

    if not text or not text.strip():
        raise ExportifyError("the CSV file is empty")

    # Exportify writes plain UTF-8, but tolerate a BOM from Excel round trips
    handle = io.StringIO(text.lstrip("\ufeff"), newline="")
    reader = csv.DictReader(handle)

    if not reader.fieldnames:
        raise ExportifyError("the CSV file has no header row")

    columns = _resolve_columns(reader.fieldnames)

    if "title" not in columns:
        raise ExportifyError(
            "no track name column found - is this an Exportify export? "
            f"columns were: {', '.join(reader.fieldnames[:6])}")

    entries = []

    for row in reader:
        if not row:
            continue

        title = _clean(row.get(columns.get("title", ""), ""))

        if not title or title.lower() in {"track name", "name"}:
            # Blank row or a repeated header
            continue

        uri = _clean(row.get(columns.get("uri", ""), ""))
        duration_ms = _parse_duration(row.get(columns.get("duration_ms", ""), ""))

        entry = {
            "uri": uri,
            "title": title,
            "artist": _clean(row.get(columns.get("artist", ""), "")),
            "album": _clean(row.get(columns.get("album", ""), "")),
            "album_artist": _clean(row.get(columns.get("album_artist", ""), "")),
            "duration_ms": duration_ms,
            "isrc": _clean(row.get(columns.get("isrc", ""), "")),
            "disc_number": _clean(row.get(columns.get("disc_number", ""), "")),
            "track_number": _clean(row.get(columns.get("track_number", ""), "")),
            "added_at": _clean(row.get(columns.get("added_at", ""), "")),
            "release_date": _clean(row.get(columns.get("release_date", ""), "")),
            "source": source
        }

        entries.append(entry)

    if not entries:
        raise ExportifyError("no tracks found in the CSV file")

    return entries


def read_exportify_file(file_path):
    """Read and parse an Exportify CSV file from disk."""

    if not file_path:
        raise ExportifyError("no file path given")

    expanded = os.path.expandvars(os.path.expanduser(str(file_path).strip().strip('"')))

    if not os.path.isfile(expanded):
        raise ExportifyError(f"file not found: {expanded}")

    try:
        with open(expanded, encoding="utf-8-sig", newline="") as file_handle:
            text = file_handle.read()

    except OSError as error:
        raise ExportifyError(f"could not read {expanded}: {error}") from error

    except UnicodeDecodeError as error:
        raise ExportifyError(f"{expanded} is not UTF-8 text: {error}") from error

    return parse_exportify_csv(text, source=expanded)


def primary_artist(entry):
    """Return the first credited artist of an entry.

    "A;B;C" becomes "A". A single artist whose name contains "&" or "+" is
    left intact, because those characters belong to one name ("Above & Beyond",
    "Sultan + Shepard"), not to a credit list.
    """

    for key in ("artist", "album_artist"):
        value = str(entry.get(key) or "").strip()

        if not value:
            continue

        for separator in _ARTIST_SEPARATORS:
            value = value.split(separator)[0]

        value = value.strip()

        if value:
            return value

    return ""


def strip_feature_credits(title):
    """Remove bracketed feature credits such as ``(feat. X)`` from a title."""

    def replace(match):
        inner = match.group(0)[1:-1].strip().lower()

        if any(inner.startswith(marker) for marker in _FEATURE_MARKERS):
            return " "

        return match.group(0)

    return _MULTISPACE.sub(" ", _BRACKETS.sub(replace, str(title or ""))).strip()


def build_search_query(entry, template="{artist} {title}"):
    """Render a Soulseek search query for an entry.

    Deliberately terse: one artist and the bare title. Soulseek requires every
    transmitted word to appear in a result, so a longer query is a worse query.
    """

    artist = primary_artist(entry)
    values = {
        "artist": artist,
        "album_artist": artist,
        "title": strip_feature_credits(entry.get("title")),
        "album": entry.get("album") or "",
        "isrc": entry.get("isrc") or ""
    }

    try:
        query = template.format(**values)

    except (KeyError, IndexError, ValueError):
        query = f"{values['artist']} {values['title']}"

    # Soulseek treats these as control characters in search terms
    for character in "-\"'":
        query = query.replace(character, " ")

    return " ".join(query.split()).strip()
