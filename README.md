# NAPSTR Playlist for Nicotine+

A Nicotine+ plugin that takes a Spotify playlist exported with
[Exportify](https://exportify.net/), finds each track on Soulseek, hashes the
files it gets, and publishes the result as a NAPSTR playlist event
(Nostr kind `30425`) as specified in
[`NIP-NAPSTR-PLAYLIST.md`](https://raw.githubusercontent.com/StateAntigen/napstr/refs/heads/feat/napstr-playlist-codec-30425/NIP-NAPSTR-PLAYLIST.md).

```
Exportify CSV  ->  per-entry Soulseek search  ->  scored candidates
               ->  download / reuse your own files  ->  SHA-256 file IDs
               ->  signed kind 30425 playlist event  ->  published to relays
```

## Contents

| Path | What it is |
| --- | --- |
| `plugin/napstr_playlist/` | The plugin itself (this folder is what gets installed) |
| `plugin/napstr_playlist/__init__.py` | Commands, state machine, search/download/hash/publish orchestration |
| `plugin/napstr_playlist/napstr_csv.py` | Exportify CSV parsing and Soulseek query building |
| `plugin/napstr_playlist/napstr_match.py` | Candidate scoring (artist/title/album/duration, penalties, format) |
| `plugin/napstr_playlist/napstr_pace.py` | Search pacing and flood-ban detection |
| `plugin/napstr_playlist/napstr_state.py` | Playlist JSON documents, staging folders, SHA-256 hashing |
| `plugin/napstr_playlist/napstr_crypto.py` | secp256k1 Schnorr (BIP-340) and bech32, no dependencies |
| `plugin/napstr_playlist/napstr_relay.py` | RFC 6455 WebSocket client and Nostr relay pool |
| `plugin/napstr_playlist/napstr_event.py` | Kind 30425 event build, sign and validate |
| `plugin/napstr_playlist/napstr_dialog.py` | Optional GTK4 review window |
| `tests/` | 274 unit tests, runnable with any Python 3.8+ interpreter |
| `tools/selfcheck.py` | Static checks (compile, undefined attributes, callback ownership) |
| `tools/reset_playlist.py` | Clears a playlist's decisions, re-queues files that no longer pass the filters |
| `tools/deploy.ps1` | Installs/uninstalls the plugin into `%APPDATA%\nicotine\plugins` |

## Install

```powershell
.\tools\deploy.ps1
```

Then, in Nicotine+: **Preferences → Plugins → enable “NAPSTR Playlist”**
(close and reopen the Preferences dialog first if it was open; no restart
needed). Open the plugin's settings pane and set at least:

* **Private key as nsec1... or 64 hex characters** — your Nostr identity.
* **Nostr relays to publish to** — defaults to `relay.damus.io`, `nos.lol`,
  `relay.nostr.com`, `relay.primal.net`, `relay.snort.social`, `nostr.mom`
  and `relay.nostr.band`.

Everything else has a sane default. The plugin listens on the `chatroom`,
`private_chat` and `cli` command interfaces.

## Usage

In the log pane (CLI tab), a chat room, or a private chat:

```
/napstr load "C:\Users\you\Downloads\Rock.csv"
```

The CSV's file name becomes the playlist title (`/napstr title <title>` to
change it, or `load <file.csv> | My title`). `load` always starts a **new**
playlist with a fresh UUID; use `/napstr open <id>` to come back to one later.
Point the `playlist_id` setting at an existing ID if you want `load` to revise
that playlist instead.

Then let it find the tracks:

```
/napstr auto all          # search every entry; download the best match when confident
/napstr list              # progress, page by page
/napstr options 12        # ranked candidates for entry 12
/napstr pick 12 2         # download candidate 2 by hand
```

**Searching is deliberately slow.** One search goes out at a time, 60 seconds
apart by default, so a 100 track playlist takes about 1 h 40 min to work
through. That is not a bug, and it is not tunable downwards past 45 seconds:
see [Search pace and server bans](#search-pace-and-server-bans). Start a batch
and walk away; `/napstr status` shows the pace and how much is left to search.

Anything the auto-picker was not confident about is marked `review` and
reported in the log (and as a desktop notification). Once files are on disk:

```
/napstr scan              # match entries against folders you already have
/napstr hash all          # SHA-256 the local files -> NAPSTR file IDs
/napstr status            # summary: entries, hashed count, published revisions
/napstr orphans           # list files nothing points at any more
/napstr publish           # sign and publish the kind 30425 event
```

`/napstr review` opens a GTK window listing every entry with its status, score
and candidate picker, if you prefer clicking to typing. `/napstr unpublish`
publishes the withdrawal body that retracts the coordinate.

If anything stops a batch, `/napstr resume` clears it once you have dealt with
the cause, and `/napstr rate <seconds>` changes the pace.

Changed your filters and want another go at the entries you are unhappy with?

```
/napstr reset 12          # forget the search results for entry 12
/napstr reset missing     # ... for every entry without a file ID yet
/napstr reset all         # ... for every entry
/napstr forget 22         # same, but also drop the file ID and local path
/napstr exclude 40        # drop entry 40 from the playlist for good
/napstr include 40        # ... and put it back
```

`reset` keeps what is *known* (a file ID, the local path) and clears what was
*decided* (candidates, the pick, scores, notes), so a re-run searches the entry
again and skips the hash step if you keep the file. `forget` drops the file
too, so the entry is searched, downloaded and hashed afresh.

An entry that already has a hashed file is left alone by `/napstr auto` rather
than fetched a second time: the second copy lands beside the first as
`Name (1).mp3` and nothing ever links to it. To ask for a different file on
purpose, `/napstr forget <entry>` first.

Note that `/napstr reset missing` skips entries that are queued or downloading,
so after a stalled batch use `/napstr reset all` (which keeps file IDs) followed
by `/napstr auto missing`.

## Tracks that are not on Soulseek at all

Some entries cannot be had from other people - a pressing nobody shares, a
bootleg, a DJ edit. Those searches come back empty every time, and repeating
them spends the one resource this plugin spends carefully. There are two honest
ways out, and both are things you do on purpose:

**Use the copy you already have.** Point the entry at your own file and it is
hashed, published and treated like any other member:

```
/napstr setpath 40 "D:\Vinyl Rips\A2 - That Track.flac"
```

`/napstr scan` does the same in bulk: it matches your `scan_folders` against the
entries that are still unresolved (by artist and title, then confirmed by
content), so a whole playlist of things you own can be filled in one pass.

**Drop it from the playlist.** `/napstr exclude` leaves the entry in the JSON,
with your reason, but takes it out of the workflow:

```
/napstr exclude 40 only ever pressed on acetate
/napstr exclude missing unavailable
/napstr include 40                # bring it back
```

An excluded entry is not searched again (not by `missing`, not by `all`), is not
counted as a gap by `require_full` - so one unobtainable track no longer blocks
the whole publish - and is not in the published event. `/napstr publish` says
which entries it left out, and `/napstr status` counts them.

Exclusions are sticky on purpose. `/napstr reset all` - the routine fix after a
stalled batch - leaves them alone and says so, `skip`/`unskip` do not resurrect
them, and `tools/reset_playlist.py` will not touch their files even with
`--delete-unlinked`, because the file may be your own copy. Only `/napstr
include` (or the tool's `--include-excluded`) puts one back. That is deliberate:
dropping a track is a decision, and no blanket command should quietly undo it.

`skip` is the softer sibling: it pauses an entry without closing the door. A
skipped entry is not searched either, but it still counts as missing for
`require_full`, so it is the one to use when you mean "not right now".

Full command list: `/napstr help`.

## When a source turns out to be dead

Peers refuse downloads, and the two refusals people see most often mean
different things:

* **"Overwhelmed with requests" / "too many files"** is a busy peer, not a dead
  one. Nicotine+ answers it by putting the download back in the queue, so it
  waits its turn and eventually starts. Nothing is retried, because there is
  nothing to retry - the source is fine, it is just busy.
* **"File not shared from the uploader"** (and banned, logged off, connection
  lost, disallowed extension) means that source is never going to serve this
  file. Nicotine+ aborts the transfer, which the plugin listens for.

When a source dies, the entry is not left sitting at `queued` forever. The
failed source is recorded, the next best candidate above `auto_pick_threshold`
is queued in its place, and this repeats up to three sources per entry. After
that the entry is marked `failed` with the peer's own wording in its notes and
the log tells you to use `/napstr options <n>`. Only candidates you would have
auto-picked are used: if the runner-up is a 0.42 wrong-mix, the plugin stops and
asks rather than downloading it.

A **cancel is your decision**, so it is never retried behind your back - the
entry is marked `failed` and points at `/napstr options <n>`. A failed source is
not offered again for that entry (it is kept in the entry's `tried` list, which
`/napstr reset` clears), and the periodic sweep catches the rare failure that
arrives without a transfer status at all.

Stray files left behind by all this are listed by `/napstr orphans`: files in
the playlist's own staging folder that no entry points at any more, including
duplicates such as `Name (1).mp3` and Nicotine+'s `.~`/`.part` leftovers.

```
/napstr orphans           # list them; nothing is deleted
/napstr orphans delete    # remove them
```

Files written in the last two minutes are always kept, so a download that has
just finished is never deleted before it is linked. The command refuses to run
at all if you set your own `download_folder`, because the files there are yours
to manage - the per-playlist staging folder is the only place this is
unambiguous.

## Search pace and server bans

The Soulseek server bans accounts for searching too quickly. Its own words:

> You have been banned for 30 minutes. This is usually the result of doing too
> many operations at once. [...] Do not repeat the same text several times in a
> row. Do not quickly repeat a search.

An early version of this plugin earned exactly that ban: it sent one search
every two seconds, so a 100 track playlist fired roughly 26 searches a minute
and the server cut the session off after about a minute and a half. Three things
came out of it, all of them in the code now:

* **One search at a time, with a floor.** `napstr_pace.SearchPacer` allows a
  single search in flight, enforces a minimum gap of 45 seconds, and adopts the
  server's own automated-search interval as a further floor when it publishes
  one. `/napstr rate` refuses to go below the floor rather than quietly
  accepting a number it will not honour.
* **A pace that says where it came from.** Three numbers decide the gap: your
  `search_interval` setting, the server's wishlist wait period (server code 104,
  which is not always a minute - it can be several), and the 45 second floor.
  `/napstr status` prints all three (`one search every 12 min (your setting 45 s,
  server wait period 12 min, hard floor 45 s)`), and the log names the server's
  period when it is first adopted. `respect_server_interval` turns that one
  floor off if you would rather pace at your own setting; Nicotine+ itself does
  not apply the wishlist period to searches you start by hand.
* **Backing off is capped.** A server warning about search volume doubles the
  pace *you* asked for - never the effective interval, or twelve minutes would
  become twenty-four - and stops at
  `napstr_pace.MAX_SEARCH_INTERVAL` (10 minutes). A pace nobody will sit through
  gets switched off, which protects the account less than a slow one.
* **A latched stop.** The server's ban message is watched for in the log, as is
  a disconnect during a batch. Either one pauses all searching and clears the
  queue. Nothing restarts it automatically - in particular not a reconnect,
  because reconnecting quickly during a ban is another pattern the server
  punishes. `/napstr resume` is the only way back, and it tells you how long the
  ban runs.
* **Focused queries.** Exportify joins multiple artists with `;`, and Soulseek
  requires *every* transmitted word to match, so `Above & Beyond;Malou Letting
  Go` returns nothing at all. Queries now use the first credited artist only and
  drop feature credits such as `(feat. Luke Steele)`. `&` and `+` are left alone,
  because they belong to single artist names.

One more thing the ban exposed: the plugin used to fall back to the public
`Search.do_search` whenever the internal search call did not fit. That call
opens a GUI search tab per track and rewrites the config file every time, and
the fallback hid both behind one log line. It is gone: if the search API does
not match, the batch stops and says so. Failing loudly beats flooding politely.

If you do get banned: leave Nicotine+ connected, wait out the ban, then run
`/napstr resume` and continue with `/napstr auto missing`, which only searches
entries that still have no decision.

### Per-entry options

Each entry is scored on artist (40%), title (45%), album (5%) and duration
(10%), with:

* a hard reject for the wrong title, unsupported formats, an **excluded
  format**, a bitrate outside your **minimum and maximum**, or a file size
  outside your **minimum and maximum**;
* penalties for a different recording (`live`, `remix`, `cover`, `karaoke`,
  `instrumental`, `sped up`, ...) unless the playlist entry names one;
* a penalty when the duration is more than four tolerances away, so a wrong
  edit does not get auto-downloaded;
* a penalty when the file name credits an artist the entry does not, which is
  what catches a file whose title "matched" only because the title is the last
  word of another artist's name: an entry `Focus (feat. CLOVES)` by John Summit
  once scored 0.98 against
  `Go Back Feat. Julia Church - Sub Focus, Julia Church, John Summit (Orig Mix) 136.mp3`,
  a different song, because "focus" came from `Sub Focus` and "john summit" was
  a feature credit on somebody else's track;
* a bonus for your preferred format.

The album column is ignored when it just repeats the title, which Exportify does
(87 of 100 entries in a real playlist). Counting it again scores the same weak
evidence twice, and it is the weak evidence that lets a wrong file through.

Anything scoring at or above `auto_pick_threshold` (default `0.8`) is
downloaded automatically; everything else waits for `/napstr pick`. `/napstr
options <n>` shows the score breakdown per candidate.

The filters are hard: a rejected candidate is never downloaded, and when every
candidate for an entry is rejected the entry is marked `unavailable` with the
reason in its notes instead of quietly picking the least bad one. `/napstr
status` shows the active filters, and `/napstr list` reports how many candidates
were dropped by them.

### Filters

| Filter | Setting | Notes |
| --- | --- | --- |
| Format | `excluded_formats` | `flac` is excluded by default; the word `lossless` covers `flac`, `aif`, `aiff`, `wav`, `ape`, `wv`, `alac`, `dsf`, `dff` and `tta` in one entry |
| Format | `preferred_format` | Soft preference (a score bonus), not a filter |
| Bitrate | `min_bitrate`, `max_bitrate` | kbps; `0` means "no limit". A reported bitrate is trusted, a local file's size is only checked when the format lets it be inferred |
| Size | `min_size_mb`, `max_size_mb` | MB; `0` means "no limit" |

A local file whose size implies an impossible bitrate (a 60 MB `.mp3` for a
three minute track, or a 20 kB stub) is skipped by `/napstr scan` rather than
matched, so neither junk files nor truncated downloads become playlist members.

Once you tighten a filter, the files you already downloaded under the old rules
are still on disk and still linked. `tools/reset_playlist.py` re-checks every
linked file against the filters and re-queues only the ones that no longer
qualify:

```powershell
python tools/reset_playlist.py --dry-run --bitrate-report
python tools/reset_playlist.py --exclude flac,aif --min-bitrate 192
```

It backs up the playlist JSON first, never deletes files unless you pass
`--delete-unlinked --yes`, and `--keep-files` / `--drop-files` override the
re-check entirely. Entries you excluded are left completely alone (state and
files) unless you add `--include-excluded`. Run it with Nicotine+ closed, or
`/napstr load` the playlist again afterwards so the running plugin sees the new
state.

Once a file has been unlinked, nothing in the playlist points at it any more, so
it is reported separately:

```powershell
python tools/reset_playlist.py --orphans          # list what nothing points at
python tools/reset_playlist.py --orphans --yes    # delete it
```

The same check is available inside Nicotine+ as `/napstr orphans`, in front of
the playlist the plugin has loaded (and `/napstr orphans delete` removes them).
Both use one implementation, so they can never disagree about what is stray.
Both also leave files written in the last couple of minutes alone: a download
that just finished may not be linked to its entry yet.

Files in the staging folder are also matched by content, not by name, so a peer
that shares a file with an odd name (leading `.~`, for example - that is the
uploader's own naming, not a half-finished download, which Nicotine+ keeps in
its own `incomplete` folder) is still recognised as a member.

## What gets published

The event follows the NIP exactly:

* `d` — the playlist's UUID, stable across revisions (persisted in the
  playlist JSON, and never recomputed from content);
* `t` — the literal `napstr-playlist` marker, followed only by *your* search
  words (`/napstr tags rock,80s`) — the plugin never invents words;
* `title`, `alt`, and an optional `client` tag;
* one `x` tag per member, in member order, holding the **SHA-256 of the file**;
* JSON content with `protocol`, `playlistId`, `title`, `tracks[]`
  (`position`, `fileId`, plus `title`/`artist`/`album` hints).

Validation follows the NIP's rules before anything leaves the machine:
canonical lowercase UUID, non-empty bounded title, 1–500 members with
contiguous positions, unique valid lowercase SHA-256 file IDs, exactly one `x`
tag per member, `alt` exactly `Napstr public playlist`, and a 128 KiB content
budget. The signed event is checked with `napstr_event.validate_playlist_event`
— including the NIP-01 event ID and signature — before it is sent anywhere.

Two deliberate behaviours worth knowing:

* **Publishing refuses incomplete playlists by default.** `require_full` is on,
  so `/napstr publish` stops and lists the entries that have no file ID yet
  instead of silently publishing a shorter playlist. Turn the setting off to
  publish only the resolved members — the NIP requires contiguous positions
  and unique members, so unresolved or repeated entries are dropped either way.
* **Publishing is public.** A playlist reveals its title and membership, so the
  NIP has no private variant. There is also no seeding requirement: a playlist
  is curation, not a claim that you hold the files.

The plugin publishes only kind `30425`. It does **not** publish the kind `30421`
catalogue entries (or kind `30422` heartbeats) that make the files themselves
discoverable to other NAPSTR clients — those are a separate feature.

Note that one `napstr-playlist` event kind is heavily co-occupied on relays, so
discovery always includes the `#t` marker filter.

## Settings reference

| Setting | Default | Meaning |
| --- | --- | --- |
| `title` | *(from the CSV name)* | Title tag value |
| `author_tags` | *(none)* | Your own search words, comma separated |
| `playlist_id` | *(blank)* | Set it to revise an existing playlist on `load` |
| `client_tag` | `napstr-playlist (Nicotine+)` | Optional provenance tag |
| `query_template` | `{artist} {title}` | Soulseek query, supports `{album}`, `{album_artist}`, `{isrc}` |
| `auto_pick` | on | Decide automatically when confident |
| `auto_pick_threshold` | `0.8` | Confidence required to auto-download |
| `search_timeout` | 25 s | How long to collect results per entry |
| `search_interval` | 60 s | Gap between searches (floor 45 s; the server bans floods) |
| `respect_server_interval` | on | Also wait out the server's wishlist wait period |
| `preferred_format` | `any` | `flac`, `mp3`, `ogg`, `opus`, `m4a`, `wav` |
| `excluded_formats` | `flac` | Formats never downloaded; the word `lossless` covers them all |
| `min_bitrate` | 0 | Reject candidates and files below this bitrate (kbps) |
| `max_bitrate` | 0 | Reject candidates and files above this bitrate (kbps) |
| `min_size_mb` | 0 | Reject files smaller than this (MB) |
| `max_size_mb` | 0 | Reject files larger than this (MB) |
| `duration_tolerance` | 10 s | Accepted duration difference |
| `download_folder` | *(blank)* | Blank uses `<data folder>\napstr\files\<playlist id>` |
| `scan_folders` | *(empty)* | Folders searched by `/napstr scan` |
| `require_full` | on | Refuse to publish unless every entry has a file ID; excluded entries do not count |
| `relays` | 7 public relays | Where events are published |
| `nostr_key` | *(blank)* | `nsec1...` or 64 hex characters |
| `relay_timeout` | 15 s | Per-relay timeout |
| `insecure_tls` | off | Skip TLS verification if a relay fails to handshake |

State lives in `%APPDATA%\nicotine\napstr\`: one JSON document per playlist
under `playlists\`, an `index.json`, and downloaded files under
`files\<playlist id>\`. The playlist JSON is the source of truth and is safe to
edit or back up.

Settings are stored per plugin, and a value already saved in your Nicotine+
config always wins over a new default — so if you had the plugin enabled before
a defaults change (for example the relay list), edit that setting in the plugin
settings pane to pick up the new list.

## Development

```powershell
python tests\test_crypto.py     # BIP-340 official vectors + bech32
python tests\test_event.py      # kind 30425 building, signing, validation
python tests\test_csv.py        # Exportify parsing, query building
python tests\test_match.py      # scoring and ranking
python tests\test_state.py      # playlist documents, hashing
python tests\test_relay.py      # WebSocket framing, relay pool (no network)
python tests\test_pace.py       # search pacing and ban detection
python tests\test_plugin.py     # the plugin itself, against a fake Nicotine+
python tests\test_tool.py       # tools/reset_playlist.py, run as its own process

python tools\selfcheck.py       # compile + undefined attributes + callback ownership
```

`tests/fake_nicotine.py` stands in for the host application (an event
scheduler, a `Search` that mimics Nicotine+ 3.3.10, downloads, notifications), so
`test_plugin.py` runs the real plugin code end to end: loading a CSV, pacing a
batch, surviving a ban message, picking a candidate, hashing a file and signing
an event. It is the layer that catches the mistakes a static check cannot.

Two rules came out of the incident above and are worth keeping:

* any code path that could send a search must be paced by `napstr_pace`, and
  `test_plugin.py` asserts that a 102 entry batch leaves exactly one search in
  flight and that the public `Search.do_search` is never called;
* after a batch of edits, verify *each* intended change landed. Two of them
  silently did not during this work - a missing `log-message` subscription and a
  missing `_pacer` - and neither would have been noticed without the harness.

The tests import the helper modules directly and need no Nicotine+ and no
third-party packages, so they run under any Python 3.8+ interpreter. The plugin
itself runs inside Nicotine+'s frozen Python 3.12, which is why every dependency
(Schnorr signatures, bech32, WebSockets) is implemented in the standard library.

### Implementation notes

Two things in the plugin touch Nicotine+ internals rather than the documented
plugin API, both verified against Nicotine+ 3.3.10 source and both with a clear
failure mode:

* searches are created directly (`Search.token`, `add_search`, `do_global_search`)
  so that a 200-track playlist does not open 200 search tabs or rewrite the
  config file 200 times. Nicotine+ 3.3.x keeps those names public; later versions
  made them private, and both spellings are supported. If neither is present the
  batch stops rather than falling back to `Search.do_search`;
* `disable`/`init` bookkeeping relies on plugin callbacks being **bound methods
  of the `Plugin` class**, because that is how Nicotine+ identifies callbacks to
  remove when a plugin is disabled. `tools/selfcheck.py` enforces this.

## License

0BSD — see `LICENSE`. Do whatever you like with it; no attribution required.

Note that the plugin runs inside Nicotine+, which is GPL-3.0-or-later, and it
imports `pynicotine` modules at runtime. That dependency is on *their* code, not
this one; this repository is offered under 0BSD so nothing here restricts what
you do with it.
