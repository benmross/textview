# TextView

TextView is a private, installable viewer for all of the owner's available
messages—not an Android-only archive. It unifies SMS, MMS, RCS, and iMessage
history in one timeline backed by `messages.db`. It provides:

- fast conversation browsing with automatic history pagination;
- grouped full-text search with total occurrence counts, the latest matching
  message per conversation, and previous/next navigation through every match;
- dedicated searching inside an individual conversation;
- SMS, MMS, RCS and **iMessage** presentation with group conversations;
- global and per-conversation attachment galleries filterable by images,
  videos, audio, files, or all types;
- a fullscreen attachment carousel with swipe, arrow-key, and on-screen
  navigation plus an exact "Show in conversation" jump;
- reactions rendered from the canonical `messages.db.reaction_events` table,
  covering Android fallback formats and native iMessage reactions;
- native iMessage replies, available edit histories, app-message cards,
  message-effect labels, location notices, and group-change announcements;
- standalone, positioned, and stickerback iMessage stickers, including
  animated HEIC-sequence stickers in Firefox;
- vCard names and locally cached profile photos;
- responsive iPhone and desktop layouts, dark mode, and an offline app shell.

iMessage bubbles render blue; SMS, MMS and RCS render green. Apple sticker,
edit, effect, and app-balloon semantics are normalized during import rather
than decoded during web requests.

The complete normalized message database remains in `messages.db`.
`textview-index.db` is a small,
disposable performance index containing conversation, attachment, contact, and
search metadata; it can always be rebuilt.

For future work, start with [ARCHITECTURE.md](ARCHITECTURE.md) for data flow and
performance invariants, [OPERATIONS.md](OPERATIONS.md) for maintenance and
recovery, [SCHEMA.md](SCHEMA.md) for the database contract, and
[IMPORT_REPORT.md](IMPORT_REPORT.md) for source coverage and known gaps.

## Run locally

Requirements are Python 3 and Node 22.5 or newer. There are no npm
dependencies.

```bash
npm run index
npm start
```

The server listens only on `127.0.0.1:8787` by default. Override this with
`TEXTVIEW_HOST` or `TEXTVIEW_PORT`.

## Sources and rebuilding

The canonical source archive and clean rebuild use five sources: three SMS
Backup & Restore XML exports, the Mac mini's `chat.db`, and one
BlueBubbles/OpenBubbles export. See `IMPORT_REPORT.md` for coverage and known
gaps.

A full rebuild is one command:

```bash
./tools/rebuild_full.sh          # builds messages-rebuilt.db, does not swap
```

It runs the stages in the required order and leaves the live database untouched.
Use the checked publication procedure in `OPERATIONS.md` to deploy the result.

### Where are the original sources?

**Not for the app to work.** `messages.db` is self-contained: attachment bytes
are decoded into `media`, every original source record is preserved in
`message_sources`, and each source's SHA-256 is recorded in `backups`.

The originals needed for a clean rebuild are under the
application-neutral archive at `/mnt/Backups/message-sources`. The retained XML
paths are:

- `/mnt/Backups/message-sources/android/sms-backup-restore/sms-20250212171642.xml`
- `/mnt/Backups/message-sources/android/sms-backup-restore/sms-20250619153114.xml`
- `/mnt/Backups/message-sources/android/sms-backup-restore/sms-20260610030051.xml`

The archive also contains the current Mac checkpoint, its attachment trees, the
BlueBubbles container, supplemental attachment stores, exact
schema documentation, a machine-readable inventory, and a SHA-256 manifest.
It is deliberately documented as reusable source data rather than as a
TextView-owned format. **Back up `messages.db` itself too**: it preserves the
normalized history and import decisions, while the source archive preserves
the independently reusable originals.

## iMessage sync

New iMessages arrive automatically. A systemd user timer pulls a checkpointed
copy of the Mac mini's `chat.db` over key-based SSH every hour, imports
idempotently, mirrors new attachments, archives the checkpoint and assets, and
rebuilds the indexes when source, message, part, media, or sticker-event counts
change. Image thumbnails, video
posters, HEIC/AVIF previews, browser-compatible audio, and transparent static
or animated sticker WebPs are prebuilt during that import before the refreshed
app index is published. A derivative failure prevents publication. The app
index is published atomically; the server switches both SQLite views together
between requests.

```bash
systemctl --user link "$PWD/deploy/textview-imessage-sync.service"
systemctl --user link "$PWD/deploy/textview-imessage-sync.timer"
systemctl --user daemon-reload
systemctl --user enable --now textview-imessage-sync.timer
journalctl --user -u textview-imessage-sync.service -f
```

Authentication uses `~/.ssh/textview_mac`; no password is stored in any script.
Run `tools/sync_imessage.sh` directly to sync on demand.

Archive maintenance is independently callable:

```bash
python3 tools/source_archive.py sync
python3 tools/source_archive.py verify
python3 tools/source_archive.py verify --deep
```

## Tools

| Tool | Purpose |
|---|---|
| `import_archive.py` | Import SMS Backup & Restore XML into the unified database |
| `merge_cross_backup_duplicates.py` | Merge exact MMS/RCS cross-backup twins |
| `normalize_protocol.py` | Decode `proto:` metadata, build reaction events |
| `annotate_media.py` | Detect attachment container types from the bytes |
| `import_imessage.py` | Import/repair macOS `chat.db` (text plus Apple semantic records) |
| `import_bluebubbles.py` | Import a BlueBubbles/OpenBubbles export container |
| `attachment_resolution.py` | Resolve missing-path attachments without basename guessing |
| `backfill_attachments.py` | Fill missing attachment bytes from other stores |
| `rebuild_search.py` | Rebuild the FTS5 index after an incremental import |
| `sync_imessage.sh` | Hourly Mac → archive sync (systemd) |
| `rebuild_full.sh` | End-to-end rebuild in the correct order |
| `build_app_index.py` | Build the disposable `textview-index.db` |
| `build_thumbnails.py` | Pre-generate browser media derivatives |
| `generate_sticker.py` | Convert one static/animated sticker to transparent WebP |
| `generate_audio.py` | Convert one archived audio attachment to Opus/WebM |
| `finalize_archive.py` | Deep database, provenance, JSON, and attachment-hash validation |
| `audit_attachments.py` | Check stored media signatures and decodable containers |

## Validate the database

Run the deterministic consistency report after a rebuild, migration, or repair:

```bash
python3 tools/finalize_archive.py --deep
python3 tools/audit_attachments.py
```

The first command requires every source count, foreign key, materialized
provenance count, JSON value, attachment length/hash, FTS row, and required
index to agree. It atomically replaces `validation.json` and exits nonzero on
any invariant violation. The second refreshes `attachment-audit.json` and
distinguishes malformed stored bytes from an inaccurate source MIME label.

## Update contacts

Replace `contacts.vcf`, then run:

```bash
npm run index
```

This refreshes names and downloads each remote vCard photo once into
`data/contact-photos`. Normal app usage never requests a Google-hosted image.

Prebuild every missing thumbnail, image preview, and audio compatibility copy with:

```bash
systemctl --user start textview-thumbnails.service
journalctl --user -u textview-thumbnails.service -f
```

## Run continuously

This server has user lingering enabled, so the supplied user service starts at
boot and does not require an interactive login:

```bash
systemctl --user link "$PWD/deploy/textview-user.service"
systemctl --user daemon-reload
systemctl --user enable --now textview-user.service
systemctl --user link "$PWD/deploy/textview-health.service"
systemctl --user link "$PWD/deploy/textview-health.timer"
systemctl --user enable --now textview-health.timer
```

View logs with:

```bash
journalctl --user -u textview-user.service -f
```

The health timer checks localhost once per minute and restarts only TextView if
the process stops responding. The service also has bounded Node heap and
systemd memory limits to prevent a pathological search from wedging the host.

`deploy/textview.service` is also available if a system-wide service is ever
preferred.

## Tailscale HTTPS and iPhone installation

Keep TextView bound to localhost and publish it only inside the tailnet:

```bash
tailscale serve --bg --https=8444 http://127.0.0.1:8787
```

This server uses port 8444 because the default Tailscale HTTPS endpoint already
belongs to another app. Open
`https://<host>.<tailnet>.ts.net:8444/` in Safari on the iPhone. Choose
Share, **Add to Home Screen**, then **Add**. Tailscale supplies the trusted
HTTPS origin required by service workers and installed PWAs.

To inspect or remove the proxy:

```bash
tailscale serve status
sudo tailscale serve reset
```

## Data and privacy

- The app server is read-only against both SQLite files.
- Original attachments are streamed from `messages.db`; only explicitly
  prebuilt browser derivatives are stored beside the archive.
- Large attachments use SQLite incremental BLOB reads with bounded memory and
  HTTP byte-range support for seeking in audio and video.
- Message threads and galleries use locally cached 480 px WebP image
  thumbnails and video posters; original attachment bytes load only in the
  fullscreen viewer. The HTTP server never generates derivatives: imports
  prebuild them once and cache them by attachment content hash.
- Because Firefox does not display HEIC/HEIF/AVIF directly, imports also cache
  2048 px WebP renditions for the fullscreen viewer while preserving the
  original attachment bytes in the archive.
  The repository includes a locally scoped libheif HEVC decoder plugin for
  HEIC thumbnails and uses FFmpeg as a fallback for newer HEIC sequences and
  AVIF variants; it does not alter system packages.
- Every audio attachment is cached as Opus/WebM for consistent Firefox
  playback, including CAF and AMR voice messages. Conversation and fullscreen
  players use this byte-range-capable cache rather than the source container.
- Every normalized sticker has a prebuilt transparent WebP. HEIC sequences
  merge Apple's color and alpha tracks and retain all animation frames.
- The service worker caches only the application shell, never message APIs or
  attachments.
- Tailscale is the authentication and network boundary.

## Working on this safely

This repository holds the code, never the archive. Every database, attachment
store and contact export is gitignored, and a pre-commit hook refuses anything
that still looks like real data: phone numbers, personal email addresses, Apple
handles, home directory paths, files over 1 MB, and anything named like a
database, backup or screenshot.

Turn the hook on once after cloning:

```bash
git config core.hooksPath .githooks
```

Use placeholders in docs and fixtures: `USER`, `555-0100`, `user@example.com`.

Repairs that name one exact message (a restored SMS whose sender a backup lost,
a reaction delivered before its target) are real conversation text, and so is
the owner's own number. They are read from `local_overrides.json`, which git
ignores; `local_overrides.example.json` shows the format and
`tools/local_overrides.py` loads it. Without the file the importers run with
no such repairs.
Screenshots are the easiest way to leak: any real screenshot of the reader shows
real conversations, so shoot against a scrubbed database or crop to structure.
