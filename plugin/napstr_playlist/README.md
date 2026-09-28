# NAPSTR Playlist

Import a Spotify playlist exported with [Exportify](https://exportify.net/),
match every entry on Soulseek, SHA-256 the files you keep, and publish the
result as a NAPSTR playlist event (Nostr kind `30425`).

```
/napstr load "C:\Users\you\Downloads\Rock.csv"
/napstr auto all        # search + auto-pick confident matches
/napstr list            # progress
/napstr options 12      # candidates for one entry
/napstr pick 12 2       # download a specific candidate
/napstr scan            # match files you already have
/napstr setpath 12 "D:\rips\track.flac"   # use your own copy for one entry
/napstr hash all        # SHA-256 -> NAPSTR file IDs
/napstr reset 12        # forget the search results for entry 12
/napstr exclude 40      # drop entry 40 from the playlist for good
/napstr orphans         # list stray files (add 'delete' to remove them)
/napstr publish         # sign and publish the event
```

`/napstr help` lists every command, `/napstr review` opens a GTK window, and
the plugin settings hold your Nostr key, relays and matching preferences.

**Downloads are filtered.** A candidate is rejected outright when its format is
excluded (`lossless` and `m4a` by default), its bitrate falls outside your minimum and maximum,
or its size falls outside yours, and an entry whose every candidate is rejected
is marked `unavailable` instead of getting the least bad one. A file name that
credits a different artist than the entry also loses points, which is what stops
a file whose title only "matched" inside another artist's name from winning.
`/napstr reset <entry|missing|all>` clears the search results but keeps the file
ID and the local file, so it re-searches without re-hashing. To ask for a
different file on purpose, `/napstr forget <entry>` instead: it drops the file
ID too, which is also what lets `/napstr auto` fetch a replacement (an entry
that already has a hashed file is left alone, so that a track is never
downloaded twice). Note that `reset missing` skips queued and downloading
entries, so after a stalled batch use `reset all` followed by `auto missing`.

**Tracks that are not on Soulseek are handled on purpose.** An entry whose
searches keep coming back empty can be filled from your own files with
`/napstr setpath <entry> <file>` (or `/napstr scan` in bulk), or dropped with
`/napstr exclude <entry> [reason]`: an excluded entry is not searched again, is
not counted as a gap by `require_full`, and is not published - `/napstr publish`
says which entries it left out. Exclusions are sticky: `/napstr reset all`, the
routine fix after a stalled batch, leaves them alone rather than quietly undoing
them, and `/napstr include <entry>` is the only thing that puts one back.
`skip`/`unskip` is the softer pair - a paused entry that still counts as
missing.

**A playlist is owned by (author, id).** `/napstr status` and `/napstr publish`
name the npub the plugin publishes as, because a key that is not the one the
rest of your setup uses is otherwise invisible: a Napstr app treats only
playlists authored by its own identity as its own, even when it can read them.
If you want the app to own what this plugin publishes, use one key for both -
and `/napstr unpublish` **before** you change it, because a withdrawal is signed
with the key that published the event.

**A dead source is replaced automatically.** When a peer refuses a download
(`File not shared.`, banned, logged off, connection lost) the plugin records
that source as tried and queues the next best candidate above your
`auto_pick_threshold`, up to three sources per entry, then marks the entry
`failed` with the reason and points at `/napstr options <n>`. A cancel is yours
to make, so it is never retried. A peer that is merely busy answers "too many
files" - Nicotine+ queues that as normal and the download simply waits, so
nothing is retried for it. Files nothing points at any more (an unlinked entry,
a duplicate `Name (1).mp3`, an interrupted download) are listed by `/napstr
orphans`, which only ever looks in the playlist's own staging folder and only
deletes with the literal word `delete`.

**Searching is paced on purpose.** One search is in flight at a time, 60 seconds
apart by default and never faster than 45 seconds, because the Soulseek server
bans accounts that search in bursts. The server's own wishlist wait period is
adopted as an extra floor when it publishes one, and `/napstr status` prints
which of the three limits is in force - a twelve minute gap looks like a bug
until you can see that the server asked for it. A batch that gets interrupted
(the server says so, or it disconnects mid-batch) stops and waits for `/napstr
resume`. `/napstr rate <seconds>` changes the pace; `/napstr status` shows it.

Modules:

| File | Purpose |
| --- | --- |
| `__init__.py` | `Plugin(BasePlugin)`: commands and the whole workflow |
| `napstr_csv.py` | Exportify CSV parsing, Soulseek query building |
| `napstr_match.py` | Candidate scoring and ranking |
| `napstr_pace.py` | Search pacing and flood-ban detection (one search at a time, 45 s floor) |
| `napstr_state.py` | Playlist JSON documents, staging folders, SHA-256 |
| `napstr_crypto.py` | secp256k1 Schnorr (BIP-340) + bech32, standard library only |
| `napstr_relay.py` | RFC 6455 WebSocket client and Nostr relay pool |
| `napstr_event.py` | Kind 30425 event build, sign, validate |
| `napstr_dialog.py` | Optional GTK4 review window (imported lazily) |

State is stored outside this folder, in `<Nicotine+ data folder>/napstr/`, so
reinstalling or updating the plugin never touches your playlists.

Licensed under 0BSD; see the `LICENSE` file in the repository root.
