# TextView Operations

This file is the maintenance runbook for the live installation in
`/home/USER/Documents/textview`.

## Services

```bash
systemctl --user status textview-user.service
systemctl --user status textview-imessage-sync.timer
systemctl --user status textview-health.timer
journalctl --user -u textview-user.service -n 100 --no-pager
journalctl --user -u textview-imessage-sync.service -n 100 --no-pager
```

`textview-user.service` serves localhost port 8787. Tailscale exposes that
listener to the private tailnet. `textview-imessage-sync.timer` performs the
hourly Mac sync; `textview-health.timer` checks responsiveness every minute.

**The host is not in this repo.** `tools/sync_imessage.sh` ships the
placeholder `your-mac.local`, because this repository is public. A real
install must set `MESSAGE_SOURCE_MAC`; the supported place is a systemd
drop-in at
`~/.config/systemd/user/textview-imessage-sync.service.d/host.conf`:

```
[Service]
Environment=MESSAGE_SOURCE_MAC=user@host-or-tailnet-address
```

Without it the hourly sync resolves nothing, and because the script treats an
unreachable Mac as an ordinary sleeping laptop it **exits 0 and logs success**.
That failure is silent by design and cost four days of missed messages once.
If the archive looks behind, check the newest message timestamp against
`journalctl`, not the exit status:

```bash
journalctl --user -u textview-imessage-sync.service -n 50 --no-pager | grep -i unreachable
```

To sync immediately, start the oneshot service and inspect its result:

```bash
systemctl --user start textview-imessage-sync.service
systemctl --user show textview-imessage-sync.service -p Result
```

The sync compares message, source, part, media, and sticker-event counts. Any
change triggers FTS/index rebuilding and derivative generation—even when a
newly observed Mac record deduplicates onto an older BlueBubbles message.
Before import, the same job updates `/mnt/Backups/message-sources`: the current
Mac checkpoint is published atomically and attachment/sticker trees are copied
append-only. An archive failure is logged and makes the oneshot unit fail
visibly, but it does not discard an otherwise successful live import.

## Routine validation

After schema/import changes, and periodically after large syncs:

```bash
python3 tools/finalize_archive.py --deep
python3 tools/audit_attachments.py
npm run check
```

Verify the original-source archive separately:

```bash
python3 tools/source_archive.py verify
python3 tools/source_archive.py verify --deep  # optional; redundant after a full rebuild
```

Success requires `validation.json` to report `healthy: true`. Deep validation
performs SQLite's full integrity check, checks foreign keys and every source
count, validates all stored JSON, recomputes every attachment SHA-256, and
checks the indexes needed by provenance audits. `attachment-audit.json`
separately records source MIME mismatches and source-container decoder warnings;
those do not imply database corruption when hashes and lengths still agree.

For a fast derivative check or to fill a missing cache entry:

```bash
python3 tools/build_thumbnails.py --archive-scan --workers 1 --prune
```

The command is idempotent. It builds only missing files, fails nonzero if a
generation fails, and removes content-hash files no longer referenced by the
current app index.

## Full rebuild

`tools/rebuild_full.sh` builds `messages-rebuilt.db` from the five canonical,
nonredundant sources and does not replace the live database. Set
`MESSAGE_SOURCE_ARCHIVE` only when validating a restored copy at another path.
Its stage order is required; do not run the individual importers in a different
order without reading `IMPORT_REPORT.md`.

```bash
./tools/rebuild_full.sh
python3 tools/finalize_archive.py messages-rebuilt.db \
  --output validation-rebuilt.json --deep
```

Before a live swap, stop the sync timer, retain a rollback copy, publish with a
same-filesystem rename, rebuild the disposable app index, and restart the
service/timer. Never copy a live WAL-mode SQLite database with a plain file
copy; use SQLite `.backup` or stop/checkpoint its writer first.

## Source coverage

The canonical source list, counts, hashes, and format schemas are in
`/mnt/Backups/message-sources`. TextView-specific coverage and
known historical gaps remain in `IMPORT_REPORT.md` and `validation.json`.
`messages.db` also needs its own backup: it contains normalization and import
decisions, while the original sources remain independently reusable.

The Mac sync obtains a consistent remote snapshot with SQLite `.backup`, then
mirrors `Attachments/` and `StickerCache/` locally and into the source archive.
An archived database checkpoint is replaced only when the new GUID set is a
superset. Otherwise the previous checkpoint is retained because it contains
unique records; later checkpoints that become pure subsets are pruned. A source row can legitimately
reference bytes that Apple or BlueBubbles had already deleted; those cases are
documented rather than synthesized.

## Failure handling

- A sync that cannot reach the Mac exits successfully without changing state;
  the next timer run retries.
- A source-archive failure is reported after the live import completes, so the
  service is visibly failed without withholding new messages from the viewer.
- Import, FTS, app-index, or derivative failures stop publication. The current
  app index continues serving.
- A missing derivative endpoint is an invariant failure, not a request-time
  generation trigger. Run the thumbnail builder and inspect its decoder error.
- If the web process stops responding, the health unit restarts only TextView;
  it does not reboot the host or mutate the database.
- `textview-index.db`, `data/thumbnails`, `data/previews`, `data/audio`, and
  `data/stickers` are
  rebuildable. `messages.db`, source files, contact data, and import evidence
  are not disposable.

## Repository hygiene

Do not commit database files, raw exports, mirrored attachment stores,
credentials, generated screenshots, Python bytecode, SQLite sidecars, or media
derivatives. Keep source code, tests, service definitions, schema/report docs,
and the small machine-readable validation reports. See `.gitignore` for the
exact boundaries.
