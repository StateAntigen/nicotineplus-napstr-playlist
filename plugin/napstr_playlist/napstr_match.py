# SPDX-FileCopyrightText: 2026 StateAntigen
# SPDX-License-Identifier: 0BSD
"""Scoring of Soulseek search results (and local files) against playlist entries.

The goal is to decide, per entry, whether a candidate is *the* track: a
confidence score in ``0.0 .. 1.0`` plus a human readable explanation. The
plugin downloads automatically above a configurable threshold and asks the
user otherwise.

Only the standard library is used, so this module is unit testable outside of
Nicotine+.
"""

import os
import re
import unicodedata

__all__ = [
    "DEFAULT_WEIGHTS",
    "ScoringOptions",
    "audio_extension",
    "candidate_attributes",
    "compare_candidate",
    "describe_candidate",
    "find_local_matches",
    "find_ranked_candidates",
    "megabytes_to_bytes",
    "normalize_text",
    "score_candidate",
]

DEFAULT_WEIGHTS = {
    "artist": 0.40,
    "title": 0.45,
    "album": 0.05,
    "duration": 0.10
}

# Words that mark a different recording of the same song. When one of these
# appears in a candidate but not in the playlist entry, the candidate is
# penalised instead of being accepted as a match.
PENALTY_WORDS = (
    "live", "remix", "rmx", "bootleg", "cover", "karaoke", "instrumental",
    "acoustic", "demo", "rehearsal", "concert", "festival", "edit", "version",
    "remaster", "remastered", "re-recorded", "rerecorded", "sped up", "slowed",
    "nightcore", "8d", "bass boosted", "mashup", "medley", "interview",
    "mono", "stereo", "reprise", "outro", "intro", "reverb", "loop"
)

AUDIO_EXTENSIONS = (
    "flac", "mp3", "ogg", "oga", "opus", "m4a", "mp4", "aac", "wav", "wma",
    "aiff", "aif", "ape", "wv", "alac", "m4b", "mpc", "dsf", "dff", "tak", "tta"
)

LOSSLESS_EXTENSIONS = ("flac", "wav", "aiff", "aif", "ape", "wv", "alac", "dsf", "dff", "tta")

# Extension whose bitrate can be inferred from size and duration well enough to
# spot a file that cannot possibly be the desired track.
INFERRABLE_EXTENSIONS = ("mp3", "ogg", "oga", "opus", "m4a", "aac", "wma")

# Plausible implied bitrates for an inferrable file. A three minute song is
# 1.4 MB at 64 kbps and 35 MB at 1411 kbps, so anything outside this range is
# not a recording of that length - it is the wrong file or a stub.
MIN_PLAUSIBLE_KBPS = 32
MAX_PLAUSIBLE_KBPS = 1411

_BRACKET_CONTENT = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")
_MULTISPACE = re.compile(r"\s+")


class ScoringOptions:
    """Tunables for :func:`score_candidate`, mirroring the plugin settings.

    The filters here are hard: a candidate they reject scores ``0.0`` and is
    never downloaded, whatever its artist or title says.
    """

    __slots__ = (
        "weights", "allowed_extensions", "excluded_extensions", "preferred_extension",
        "min_bitrate", "max_bitrate", "min_size_bytes", "max_size_bytes",
        "duration_tolerance_seconds", "duration_penalty", "penalty_weight", "max_penalty",
        "required_title_ratio", "required_artist_ratio"
    )

    def __init__(
            self, weights=None, allowed_extensions=None, excluded_extensions=None,
            preferred_extension="any", min_bitrate=0, max_bitrate=0,
            min_size_bytes=0, max_size_bytes=0, duration_tolerance_seconds=10,
            duration_penalty=0.15, penalty_weight=0.15, max_penalty=0.45,
            required_title_ratio=0.6, required_artist_ratio=0.0):

        self.weights = dict(DEFAULT_WEIGHTS)

        if weights:
            self.weights.update(weights)

        self.allowed_extensions = tuple(allowed_extensions or AUDIO_EXTENSIONS)
        self.excluded_extensions = tuple(
            normalise_extension(extension) for extension in (excluded_extensions or ()))
        self.preferred_extension = normalise_extension(preferred_extension) or "any"
        self.min_bitrate = int(min_bitrate or 0)
        self.max_bitrate = int(max_bitrate or 0)
        self.min_size_bytes = int(min_size_bytes or 0)
        self.max_size_bytes = int(max_size_bytes or 0)
        self.duration_tolerance_seconds = max(int(duration_tolerance_seconds or 0), 0)
        self.duration_penalty = float(duration_penalty)
        self.penalty_weight = float(penalty_weight)
        self.max_penalty = float(max_penalty)
        self.required_title_ratio = float(required_title_ratio)
        self.required_artist_ratio = float(required_artist_ratio)

    @property
    def excluded_size_range(self):
        """True when a size limit is configured at all."""

        return bool(self.min_size_bytes or self.max_size_bytes)


def normalise_extension(value):
    """Lowercase an extension and drop a leading dot, if any."""

    return str(value or "").strip().lower().lstrip(".")


def megabytes_to_bytes(value):
    """Convert a megabyte setting to bytes; 0 (or rubbish) means no limit."""

    try:
        megabytes = float(value or 0)

    except (TypeError, ValueError):
        return 0

    return int(megabytes * 1024 * 1024) if megabytes > 0 else 0


def normalize_text(value):
    """Lowercase, de-accent and collapse a string for comparison."""

    if not value:
        return ""

    text = str(value)

    try:
        text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")

    except (UnicodeError, LookupError):
        pass

    text = text.lower()
    text = text.replace("&", " and ")

    return " ".join(_NON_ALPHANUMERIC.sub(" ", text).split())


def _without_brackets(value):
    """Drop parenthesised segments such as ``(feat. X)`` or ``[Remastered]``."""

    return _MULTISPACE.sub(" ", _BRACKET_CONTENT.sub(" ", value or "")).strip()


def _tokens(value):
    return [token for token in normalize_text(value).split() if token]


def _path_to_text(path):
    """Turn a Soulseek virtual path into comparable text."""

    text = (path or "").replace("\\", " ").replace("/", " ")

    return normalize_text(text)


def audio_extension(path):
    _base, separator, extension = (path or "").rpartition(".")

    if not separator:
        return ""

    return extension.lower()


def _ratio(found, expected):
    """Fraction of expected tokens present in ``found`` (token set)."""

    if not expected:
        return 0.0

    return len(set(expected) & set(found)) / float(len(set(expected)))


def _duration_score(entry_seconds, candidate_seconds, tolerance):
    """Return ``(value, reason, is_severe)`` for the duration component."""

    if not entry_seconds or candidate_seconds is None:
        # No usable data on either side: stay neutral instead of blocking
        return 0.5, "duration unknown", False

    difference = abs(float(entry_seconds) - float(candidate_seconds))
    tolerance = max(tolerance, 1)

    if difference <= tolerance:
        return 1.0, f"duration within {difference:.0f}s", False

    if difference >= tolerance * 4:
        # This far apart it is a different recording, not a different rip
        return 0.0, f"duration off by {difference:.0f}s", True

    score = 1.0 - ((difference - tolerance) / float(tolerance * 3))

    return max(score, 0.0), f"duration off by {difference:.0f}s", False


def candidate_attributes(candidate):
    """Return the audio attributes of a candidate, however it stores them.

    Playlist files keep candidates flat (``bitrate``, ``length``, ...) while
    hand written callers may pass an ``attributes`` mapping, so both shapes
    are accepted here and nowhere else.
    """

    attributes = {
        key: candidate[key] for key in ("bitrate", "length", "sample_rate", "bit_depth")
        if candidate.get(key) is not None
    }
    explicit = candidate.get("attributes")

    if isinstance(explicit, dict):
        attributes.update({key: value for key, value in explicit.items() if value is not None})

    return attributes


def _penalty_for(candidate_text, entry_text, options):

    penalty = 0.0
    matched_words = []

    for word in PENALTY_WORDS:
        if word in candidate_text and word not in entry_text:
            penalty += options.penalty_weight
            matched_words.append(word)

    return min(penalty, options.max_penalty), matched_words


def _artist_tokens(entry):
    artist = entry.get("artist") or entry.get("album_artist") or ""

    # "A feat. B" and "A & B" credit the first artist, which is what most
    # file names carry. ";" is Exportify's multi-artist separator.
    for separator in (";", " feat", " featuring", " ft.", " ft ", " with ", " x ", " & ", ","):

        if separator in artist.lower():
            index = artist.lower().index(separator)

            if index > 0:
                artist = artist[:index]

    return _tokens(artist)


def score_candidate(entry, path, size=None, attributes=None, options=None):
    """Score one candidate file against a playlist entry.

    Returns ``(score, reasons)`` where ``reasons`` is a list of short strings
    suitable for display in the UI or log.
    """

    options = options or ScoringOptions()
    reasons = []
    path = path or ""
    extension = audio_extension(path)

    if options.allowed_extensions and extension not in options.allowed_extensions:
        return 0.0, [f"unsupported format: {extension or 'unknown'}"]

    if options.excluded_extensions and extension in options.excluded_extensions:
        return 0.0, [f"excluded format: {extension}"]

    attributes = attributes or {}
    bitrate = attributes.get("bitrate")
    length = attributes.get("length")

    if options.min_bitrate and bitrate and int(bitrate) < options.min_bitrate:
        return 0.0, [f"bitrate {bitrate} kbps below the minimum of {options.min_bitrate}"]

    if options.max_bitrate and bitrate and int(bitrate) > options.max_bitrate:
        return 0.0, [f"bitrate {bitrate} kbps above the maximum of {options.max_bitrate}"]

    if size:
        size = int(size)

        if options.min_size_bytes and size < options.min_size_bytes:
            return 0.0, [
                f"{_human_size(size)} below the minimum of {_human_size(options.min_size_bytes)}"]

        if options.max_size_bytes and size > options.max_size_bytes:
            return 0.0, [
                f"{_human_size(size)} above the maximum of {_human_size(options.max_size_bytes)}"]

    candidate_text = _path_to_text(path)
    candidate_tokens = candidate_text.split()
    entry_title_text = normalize_text(entry.get("title"))
    entry_title_tokens = _tokens(_without_brackets(entry.get("title")) or entry.get("title"))
    artist_tokens = _artist_tokens(entry)
    album_tokens = _tokens(entry.get("album"))

    title_ratio = _ratio(candidate_tokens, entry_title_tokens)
    artist_ratio = _ratio(candidate_tokens, artist_tokens) if artist_tokens else 0.0
    album_ratio = _ratio(candidate_tokens, album_tokens) if album_tokens else 0.0

    if artist_tokens:
        if artist_ratio >= 1.0:
            reasons.append("artist matched")
        elif artist_ratio > 0:
            reasons.append(f"artist partially matched ({artist_ratio:.0%})")
        else:
            reasons.append("artist not found in filename")

    if title_ratio >= 1.0:
        reasons.append("title matched")

    elif title_ratio > 0:
        reasons.append(f"title partially matched ({title_ratio:.0%})")

    else:
        reasons.append("title not found in filename")

    if album_tokens and album_ratio:
        reasons.append(f"album matched ({album_ratio:.0%})")

    entry_seconds = None

    if entry.get("duration_ms"):
        entry_seconds = float(entry["duration_ms"]) / 1000.0

    duration_value, duration_reason, duration_severe = _duration_score(
        entry_seconds, length, options.duration_tolerance_seconds)
    reasons.append(duration_reason)

    weights = options.weights
    weight_total = sum(weights.values()) or 1.0

    score = (
        (weights["artist"] * artist_ratio)
        + (weights["title"] * title_ratio)
        + (weights["album"] * album_ratio)
        + (weights["duration"] * duration_value)
    ) / weight_total

    # A candidate that does not carry the title at all can never be right,
    # however well the duration lines up.
    if title_ratio < options.required_title_ratio:
        return max(score - 0.4, 0.0), reasons + ["title mismatch"]

    if artist_tokens and artist_ratio < options.required_artist_ratio:
        return max(score - 0.2, 0.0), reasons + ["artist mismatch"]

    penalty, penalty_words = _penalty_for(candidate_text, entry_title_text, options)

    if penalty_words:
        reasons.append("different recording: " + ", ".join(penalty_words))
        score -= penalty

    if duration_severe:
        reasons.append("duration mismatch, likely a different recording or the wrong file")
        score -= options.duration_penalty

    if options.preferred_extension != "any":
        if extension == options.preferred_extension:
            score += 0.05
            reasons.append(f"{extension} preferred")

        elif options.preferred_extension in LOSSLESS_EXTENSIONS and extension not in LOSSLESS_EXTENSIONS:
            score -= 0.05
            reasons.append(f"lossy {extension} while {options.preferred_extension} is preferred")

    if bitrate:
        reasons.append(f"{int(bitrate)} kbps")

    if extension:
        reasons.append(extension)

    return max(min(score, 1.0), 0.0), reasons


def describe_candidate(candidate):
    """One line summary of a candidate for display."""

    parts = [candidate.get("path", "")]

    details = []

    if candidate.get("username"):
        details.append(str(candidate["username"]))

    if candidate.get("bitrate"):
        details.append(f"{candidate['bitrate']} kbps")

    if candidate.get("length"):
        minutes, seconds = divmod(int(candidate["length"]), 60)
        details.append(f"{minutes}:{seconds:02d}")

    if candidate.get("size"):
        details.append(_human_size(candidate["size"]))

    if details:
        parts.append("(" + ", ".join(details) + ")")

    return " ".join(parts)


def _human_size(num_bytes):

    try:
        size = float(num_bytes)

    except (TypeError, ValueError):
        return ""

    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"

        size /= 1024

    return ""


def compare_candidate(candidate):
    """Sort key: best score first, then the higher bitrate, then the shorter path."""

    bitrate = candidate_attributes(candidate).get("bitrate") or 0

    try:
        bitrate = int(bitrate)

    except (TypeError, ValueError):
        bitrate = 0

    return (-float(candidate.get("score") or 0.0), -bitrate, len(candidate.get("path", "")))


def find_ranked_candidates(entry, candidates, options=None):
    """Return scored candidates sorted best first.

    Each candidate is a mapping with at least ``path``; ``score`` and
    ``reasons`` are filled in on the returned copies.
    """

    scored = []

    for candidate in candidates or []:
        candidate = dict(candidate)
        attributes = candidate_attributes(candidate)
        score, reasons = score_candidate(
            entry, candidate.get("path", ""), candidate.get("size"), attributes, options)
        candidate["score"] = round(score, 4)
        candidate["reasons"] = reasons

        if attributes:
            candidate["attributes"] = attributes

        scored.append(candidate)

    scored.sort(key=compare_candidate)

    return scored


def find_local_matches(entry, file_paths, options=None, threshold=0.8):
    """Return ``(path, score)`` pairs for local files that look like the entry."""

    options = options or ScoringOptions()
    entry_seconds = None

    if entry.get("duration_ms"):
        entry_seconds = float(entry["duration_ms"]) / 1000.0

    matches = []

    for file_path in file_paths:
        try:
            size = os.path.getsize(file_path)

        except OSError:
            size = None

        score, _reasons = score_candidate(
            entry, file_path, size=size, attributes=None, options=options)

        if entry_seconds and size:
            extension = audio_extension(file_path)

            if extension in INFERRABLE_EXTENSIONS:
                # A lossy file's size says what its bitrate must be for the
                # expected duration. Comparing that against a wide plausible
                # range catches stubs and wrong files without assuming a
                # particular quality (a 320 kbps album track is not a mismatch).
                implied_kbps = (size * 8) / entry_seconds / 1000.0

                if not MIN_PLAUSIBLE_KBPS <= implied_kbps <= MAX_PLAUSIBLE_KBPS:
                    continue

        if score >= threshold:
            matches.append((file_path, round(score, 4)))

    matches.sort(key=lambda item: -item[1])

    return matches
