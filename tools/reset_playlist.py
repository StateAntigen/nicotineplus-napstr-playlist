#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 StateAntigen
# SPDX-License-Identifier: 0BSD
"""Reset a NAPSTR playlist document for a fresh search run.

Clears what was *decided* about every entry (search results, the chosen file,
scores, notes) so the next ``/napstr auto missing`` searches again. Entries
whose downloaded file is gone, or no longer passes the given format, bitrate
and size limits, also lose their file ID and local path so they are searched,
downloaded and hashed afresh.

Files on disk are never touched: the JSON is backed up first, and the linked
files of unlinked entries are only deleted when ``--delete-unlinked --yes`` is
given explicitly.

Examples::

    python tools/reset_playlist.py --dry-run
    python tools/reset_playlist.py --exclude flac,aif --min-bitrate 192
    python tools/reset_playlist.py --drop-files --dry-run
"""

import argparse
import datetime
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "plugin", "napstr_playlist"))

# Track titles and file names are full of non-cp1252 characters ("Halo" with a
# macron, curly quotes); a cp1252 Windows console would otherwise raise while
# printing them. Keep the console's own encoding and substitute instead.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(errors="replace")

    except (AttributeError, ValueError):
        pass

import napstr_match  # noqa: E402  pylint: disable=wrong-import-position
import napstr_state  # noqa: E402  pylint: disable=wrong-import-position

# Mirrors the plugin defaults, so a plain run means "what the plugin would do".
DEFAULT_EXCLUDED = "flac"


def default_data_folder():

    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")

    return os.path.join(appdata, "nicotine")


def playlists_folder(data_folder_path):

    return os.path.join(napstr_state.state_folder_path(data_folder_path), "playlists")


def playlist_file_path(data_folder_path, playlist_id):

    return os.path.join(playlists_folder(data_folder_path), f"{playlist_id}.json")


def resolve_playlist(data_folder_path, wanted):
    """Return ``(playlist_id, path, error)`` for the requested playlist."""

    playlists = napstr_state.list_playlists(data_folder_path)

    if not playlists:
        return None, None, f"No playlists found in {playlists_folder(data_folder_path)}"

    if not wanted:
        if len(playlists) > 1:
            names = ", ".join(f"{item['id']} ({item.get('title') or 'untitled'})"
                              for item in playlists.values())
            return None, None, f"Several playlists exist; pick one with --playlist: {names}"

        playlist_id = next(iter(playlists))

        return playlist_id, playlist_file_path(data_folder_path, playlist_id), None

    needle = wanted.strip().lower()
    partial = None

    for playlist_id, item in playlists.items():
        title = str(item.get("title") or "").lower()
        lowered = playlist_id.lower()

        if needle in (lowered, title, f"{lowered}.json"):
            return playlist_id, playlist_file_path(data_folder_path, playlist_id), None

        if partial is None and (lowered.startswith(needle) or needle in title):
            partial = playlist_id

    if partial:
        return partial, playlist_file_path(data_folder_path, partial), None

    return None, None, f"No playlist matches {wanted!r}"


def parse_extensions(value):
    """Split a comma separated format list, expanding the word "lossless"."""

    return napstr_match.expand_extensions(str(value or "").replace(";", ",").split(","))


def megabytes_to_bytes(value):

    return napstr_match.megabytes_to_bytes(value)


def backup_file(path, suffix=".bak"):

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = f"{path}{suffix}-{stamp}"

    shutil.copy2(path, target)

    return target


def handle_orphans(args, playlist, playlist_id):
    """Report, and optionally delete, the files nothing points at any more."""

    folder = napstr_state.staging_folder_path(args.data_folder, playlist_id)

    # The same grace period the plugin command uses: a download that has just
    # finished may not be linked to its entry yet, and deleting that would
    # destroy a file the playlist is about to claim.
    orphans, partials, skipped = napstr_state.find_orphans(
        playlist, folder, minimum_age_seconds=napstr_state.ORPHAN_MINIMUM_AGE_SECONDS)

    print(f"Staging  : {folder}")
    print()
    print(f"{len(orphans)} unreferenced file(s) and {len(partials)} incomplete download(s).")

    for path in orphans:
        try:
            size = napstr_match.human_size(os.path.getsize(path))

        except OSError:
            size = "?"

        print(f"  {size:>10}  {os.path.basename(path)}")

    for path in partials:
        print(f"  {'partial':>10}  {os.path.basename(path)}  (incomplete download)")

    for path in skipped:
        print(f"  {'recent':>10}  {os.path.basename(path)}  (written moments ago, kept)")

    if not orphans and not partials:
        return 0

    print()

    if args.dry_run:
        print("Dry run: nothing deleted.")
        return 0

    if not args.yes:
        print("Add --yes to delete them.")
        return 0

    deleted = 0

    for path in orphans + partials:
        try:
            os.remove(path)

        except OSError as error:
            print(f"Could not delete {path}: {error}")

        else:
            deleted += 1

    print(f"Deleted  : {deleted} file(s)")

    return 0


def implied_bitrate(local_path, entry, size):
    """Bitrate a file must have to be that big, or ``None`` when unknowable."""

    if not entry.get("duration_ms"):
        return None

    if napstr_match.audio_extension(local_path) not in napstr_match.INFERRABLE_EXTENSIONS:
        return None

    seconds = float(entry["duration_ms"]) / 1000.0

    if seconds <= 0:
        return None

    return (size * 8) / seconds / 1000.0


def evaluate_entry(entry, options):
    """Decide whether the file already linked to an entry is still wanted.

    Returns ``(keep, detail)``. A missing or unwanted file means the entry is
    searched again, rather than silently keeping a file the filters now reject.
    """

    local_path = str(entry.get("local_path") or "")

    if not local_path:
        return False, "nothing downloaded yet"

    if not os.path.isfile(local_path):
        return False, "downloaded file is gone"

    try:
        size = os.path.getsize(local_path)

    except OSError as error:
        return False, f"cannot read the file ({error})"

    if options.excluded_extensions:
        extension = napstr_match.audio_extension(local_path)

        if extension in options.excluded_extensions:
            return False, f"excluded format: {extension}"

    if options.allowed_extensions:
        extension = napstr_match.audio_extension(local_path)

        if extension not in options.allowed_extensions:
            return False, f"unsupported format: {extension or 'unknown'}"

    if options.min_size_bytes and size < options.min_size_bytes:
        return False, (f"{napstr_match.human_size(size)} below the minimum of "
                       f"{napstr_match.human_size(options.min_size_bytes)}")

    if options.max_size_bytes and size > options.max_size_bytes:
        return False, (f"{napstr_match.human_size(size)} above the maximum of "
                       f"{napstr_match.human_size(options.max_size_bytes)}")

    kbps = implied_bitrate(local_path, entry, size)

    if kbps is not None:
        if options.min_bitrate and kbps < options.min_bitrate:
            return False, f"about {kbps:.0f} kbps, below the minimum of {options.min_bitrate}"

        if options.max_bitrate and kbps > options.max_bitrate:
            return False, f"about {kbps:.0f} kbps, above the maximum of {options.max_bitrate}"

        if not napstr_match.MIN_PLAUSIBLE_KBPS <= kbps <= napstr_match.MAX_PLAUSIBLE_KBPS:
            return False, f"size implies an impossible {kbps:.0f} kbps"

        detail = f"kept, about {kbps:.0f} kbps"

    else:
        detail = "kept"

    return True, f"{detail} ({napstr_match.human_size(size)})"


def main():

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-folder", default=default_data_folder(),
                        help="Nicotine+ data folder (default: %APPDATA%%\\nicotine)")
    parser.add_argument("--playlist", default="",
                        help="Playlist id, title or file name (default: the only playlist)")
    parser.add_argument("--exclude", default=DEFAULT_EXCLUDED,
                        help=("Formats to treat as unwanted; 'lossless' covers them all "
                              f"(default: {DEFAULT_EXCLUDED})"))
    parser.add_argument("--min-bitrate", type=int, default=0)
    parser.add_argument("--max-bitrate", type=int, default=0)
    parser.add_argument("--min-size-mb", type=int, default=0)
    parser.add_argument("--max-size-mb", type=int, default=0)
    parser.add_argument("--keep-files", action="store_true",
                        help="Only clear decisions; keep every file ID and local path")
    parser.add_argument("--include-excluded", action="store_true",
                        help="Also reset entries you excluded, which are otherwise left alone")
    parser.add_argument("--drop-files", action="store_true",
                        help="Drop every file ID and local path, whatever is on disk")
    parser.add_argument("--delete-unlinked", action="store_true",
                        help="Delete the files of unlinked entries (needs --yes)")
    parser.add_argument("--orphans", action="store_true",
                        help="Report (and with --yes delete) staging files no entry points at")
    parser.add_argument("--bitrate-report", action="store_true",
                        help="List the implied bitrate of every downloaded file, lowest first")
    parser.add_argument("--yes", action="store_true",
                        help="Confirm the deletions requested by --delete-unlinked")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would change without writing anything")

    args = parser.parse_args()

    if args.keep_files and args.drop_files:
        parser.error("--keep-files and --drop-files are mutually exclusive")

    playlist_id, path, error = resolve_playlist(args.data_folder, args.playlist)

    if error:
        print(error)
        return 1

    playlist = napstr_state.load_playlist(args.data_folder, playlist_id)

    if playlist is None:
        print(f"Could not read {path}")
        return 1

    options = napstr_match.ScoringOptions(
        excluded_extensions=parse_extensions(args.exclude),
        min_bitrate=args.min_bitrate,
        max_bitrate=args.max_bitrate,
        min_size_bytes=megabytes_to_bytes(args.min_size_mb),
        max_size_bytes=megabytes_to_bytes(args.max_size_mb)
    )

    print(f"Playlist : {playlist.title} ({playlist.id})")
    print(f"File     : {path}")
    print(f"Entries  : {len(playlist.entries)}")

    if args.keep_files:
        print("Filters  : ignored, every file ID is kept")

    else:
        limits = []

        if options.excluded_extensions:
            limits.append("excluding " + ", ".join(options.excluded_extensions))

        if options.min_bitrate:
            limits.append(f"min {options.min_bitrate} kbps")

        if options.max_bitrate:
            limits.append(f"max {options.max_bitrate} kbps")

        if options.min_size_bytes:
            limits.append(f"min {args.min_size_mb} MB")

        if options.max_size_bytes:
            limits.append(f"max {args.max_size_mb} MB")

        print("Filters  : " + (", ".join(limits) or "none, only missing files are re-queued"))

    if args.orphans:
        print()

        return handle_orphans(args, playlist, playlist_id)

    print()

    unlinked = []
    cleared = 0
    bitrates = []
    excluded = []

    for entry in playlist.entries:

        # An excluded entry is out of the playlist on purpose. Its file may be
        # the user's own copy, so nothing here touches it either - not the
        # state, and not the file.
        if (entry.get("status") == napstr_state.STATUS_EXCLUDED
                and not args.include_excluded):
            excluded.append(entry.get("position"))
            continue

        if args.drop_files:
            keep, detail = False, "dropped by --drop-files"

        elif args.keep_files:
            keep, detail = True, "kept by --keep-files"

        else:
            keep, detail = evaluate_entry(entry, options)

        if args.bitrate_report:
            local_path = str(entry.get("local_path") or "")

            if local_path and os.path.isfile(local_path):
                try:
                    kbps = implied_bitrate(local_path, entry, os.path.getsize(local_path))

                except OSError:
                    kbps = None

                bitrates.append((kbps, entry.get("position"), entry.get("title", ""),
                                 napstr_match.audio_extension(local_path)))

        had_file = bool(entry.get("file_id") or entry.get("local_path"))

        if had_file and not keep:
            unlinked.append((entry.get("position"), entry.get("title", ""),
                             str(entry.get("local_path") or ""), detail))

        if entry.get("candidates") or entry.get("chosen") or entry.get("status") != napstr_state.STATUS_NEW:
            cleared += 1

        # The plugin's own reset, so a field it clears (the dead-source list is
        # newer than this tool) cannot be left behind by a copy kept in here.
        # Exclusions are guarded by reset_entries itself, which is why the flag
        # has to be passed down: without it --include-excluded silently did
        # nothing at all.
        napstr_state.reset_entries(
            playlist, positions=[entry["position"]], drop_files=not keep,
            protect_excluded=not args.include_excluded)
    print(f"Decisions cleared for {cleared} of {len(playlist.entries)} entries.")
    print(f"File IDs dropped for {len(unlinked)} entries.")

    if excluded:
        print(f"Left alone: {len(excluded)} deliberately excluded entr(y/ies) "
              "(--include-excluded resets them too).")

    if unlinked:
        print()
        print("Re-queued because their downloaded file no longer qualifies:")

        for position, title, local_path, detail in unlinked:
            print(f"  {position:>4}  {title[:44]:<44}  {detail}")
            print(f"        {local_path}")

    if args.bitrate_report and bitrates:
        print()
        print("Implied bitrate of the downloaded files, lowest first:")

        for kbps, position, title, extension in sorted(
                bitrates, key=lambda item: (item[0] is None, item[0] or 0)):
            shown = f"{kbps:>5.0f} kbps" if kbps else "  ?    "
            print(f"  {shown}  {extension or '?':<5} {position:>4}  {title[:46]}")

    print()

    if args.dry_run:
        print("Dry run: nothing written.")

        if unlinked:
            print(f"{len(unlinked)} files would be unlinked, none deleted.")

        return 0

    backup = backup_file(path)

    print(f"Backup   : {backup}")

    playlist.dirty = True
    playlist.save()

    print(f"Written  : {path}")

    if not unlinked:
        return 0

    if not args.delete_unlinked:
        print()
        print("The files above are untouched. Delete them yourself, or re-run with")
        print("--delete-unlinked --yes to remove exactly those files.")

        return 0

    if not args.yes:
        print()
        print("--delete-unlinked also needs --yes. Nothing was deleted.")

        return 0

    deleted = 0

    for _position, _title, local_path, _detail in unlinked:
        if not local_path or not os.path.isfile(local_path):
            continue

        try:
            os.remove(local_path)

        except OSError as error:
            print(f"Could not delete {local_path}: {error}")

        else:
            deleted += 1

    print(f"Deleted  : {deleted} files")

    return 0


if __name__ == "__main__":
    sys.exit(main())
