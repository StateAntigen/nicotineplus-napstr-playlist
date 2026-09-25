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
/napstr hash all        # SHA-256 -> NAPSTR file IDs
/napstr reset 12        # forget the search results for entry 12
/napstr publish         # sign and publish the event
```

`/napstr help` lists every command, `/napstr review` opens a GTK window, and
the plugin settings hold your Nostr key, relays and matching preferences.

**Downloads are filtered.** A candidate is rejected outright when its format is
excluded (`flac` by default), its bitrate falls outside your minimum and maximum,
or its size falls outside yours, and an entry whose every candidate is rejected
is marked `unavailable` instead of getting the least bad one. `/napstr reset
<entry|missing|all>` re-queues entries that you want searched again.

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
