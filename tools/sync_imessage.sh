#!/bin/bash
# Pull new iMessages from a Mac into TextView's unified database.
# Idempotent: only messages whose guid is not already present are inserted.
#
# Retargeted from the Mac mini to the MacBook on 2026-09-06: the mini is being
# sold. The MacBook is usually awake but not always, and its chat.db only goes
# back to when it was set up -- neither matters, because the import dedupes on
# message guid and the mini's history is already in messages.db.
#
# Override the host with MESSAGE_SOURCE_MAC if this moves again.
set -uo pipefail

ROOT="$HOME/Documents/textview"
WORK="$ROOT/imessage-import"
SOURCE_ARCHIVE="${MESSAGE_SOURCE_ARCHIVE:-/mnt/Backups/message-sources}"
KEY="$HOME/.ssh/textview_mac"
# Set MESSAGE_SOURCE_MAC to the Mac holding the iMessage database,
# e.g. its hostname or tailnet address.
MAC="${MESSAGE_SOURCE_MAC:-user@your-mac.local}"   # override with MESSAGE_SOURCE_MAC
SSHOPT=(-i "$KEY" -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new)
LABEL="${MESSAGE_SOURCE_LABEL:-chat.db-macbook}"
archive_failed=0

log() { echo "[$(date '+%F %T')] $*"; }

cd "$ROOT" || { log "cannot cd to $ROOT"; exit 1; }

if ! ssh "${SSHOPT[@]}" "$MAC" true 2>/dev/null; then
  # A closed laptop is the normal case, not a failure. Exit clean so the timer
  # does not raise an alarm every hour the lid is shut.
  log "$MAC unreachable (asleep?); nothing to do."; exit 0
fi

log "checkpointing chat.db on the Mac"
ssh "${SSHOPT[@]}" "$MAC" 'rm -f /tmp/textview-sync.db; sqlite3 ~/Library/Messages/chat.db ".backup /tmp/textview-sync.db"' || {
  log "remote checkpoint failed"; exit 1; }

log "pulling database"
rsync -e "ssh ${SSHOPT[*]}" -a "$MAC:/tmp/textview-sync.db" "$WORK/chat-live.db" || { log "db pull failed"; exit 1; }
ssh "${SSHOPT[@]}" "$MAC" 'rm -f /tmp/textview-sync.db' 2>/dev/null

log "syncing attachments"
rsync -e "ssh ${SSHOPT[*]}" -a --quiet "$MAC:Library/Messages/Attachments/"   "$WORK/Attachments/"   2>/dev/null
rsync -e "ssh ${SSHOPT[*]}" -a --quiet "$MAC:Library/Messages/StickerCache/" "$WORK/StickerCache/" 2>/dev/null

log "archiving source checkpoint and newly observed assets"
python3 "$ROOT/tools/source_archive.py" --archive "$SOURCE_ARCHIVE" --workers 2 sync --work "$WORK" || {
  log "WARNING: source archiving failed; continuing the live import"
  archive_failed=1
}

read -r before_messages before_sources before_media before_parts before_stickers < <(
  python3 -c "import sqlite3;d=sqlite3.connect('$ROOT/messages.db');ts={r[0] for r in d.execute('select name from sqlite_master where type=\"table\"')};print(*(d.execute('select count(*) from '+t).fetchone()[0] if t in ts else 0 for t in ('messages','message_sources','media','message_parts','sticker_events')))"
)

log "importing"
python3 "$ROOT/tools/import_imessage.py" \
  --chat-db "$WORK/chat-live.db" \
  --attachments "$WORK/Attachments" \
  --sticker-cache "$WORK/StickerCache" \
  --fallback-root "$SOURCE_ARCHIVE/supplemental-attachments/BlueBubblesLegion" \
  -o "$ROOT/messages.db" --source-label "$LABEL" || { log "import failed"; exit 1; }

read -r after_messages after_sources after_media after_parts after_stickers < <(
  python3 -c "import sqlite3;d=sqlite3.connect('$ROOT/messages.db');print(*(d.execute('select count(*) from ' + t).fetchone()[0] for t in ('messages','message_sources','media','message_parts','sticker_events')))"
)
new_messages=$(( after_messages - before_messages ))
new_sources=$(( after_sources - before_sources ))
new_media=$(( after_media - before_media ))
new_parts=$(( after_parts - before_parts ))
new_stickers=$(( after_stickers - before_stickers ))
log "messages: $before_messages -> $after_messages (+$new_messages); sources +$new_sources; media +$new_media; parts +$new_parts; stickers +$new_stickers"

if [ "$new_messages" -ne 0 ] || [ "$new_sources" -ne 0 ] || [ "$new_media" -ne 0 ] || [ "$new_parts" -ne 0 ] || [ "$new_stickers" -ne 0 ]; then
  log "annotating new media containers"
  python3 "$ROOT/tools/annotate_media.py" "$ROOT/messages.db" || {
    log "media annotation failed"; exit 1; }
  log "rebuilding search index"
  python3 "$ROOT/tools/rebuild_search.py" "$ROOT/messages.db" || {
    log "FTS rebuild failed; refusing to publish a mismatched app index"; exit 1; }
  staged_index="$ROOT/textview-index.pending.db"
  log "building staged app index"
  TEXTVIEW_DATABASE="$ROOT/messages.db" TEXTVIEW_INDEX="$staged_index" \
    python3 "$ROOT/tools/build_app_index.py" || { log "WARNING: app index rebuild failed"; exit 1; }
  log "prebuilding browser media derivatives"
  # New HEIC files may require both a thumbnail and a fullscreen preview; one
  # worker prevents two full-resolution decodes of the same image overlapping.
  python3 "$ROOT/tools/build_thumbnails.py" --workers 1 --index "$staged_index" || {
    log "media derivative build failed; keeping the current index"; exit 1; }
  log "publishing app index"
  python3 - "$staged_index" "$ROOT/textview-index.db" <<'PY'
import os, sys
os.replace(sys.argv[1], sys.argv[2])
PY
  python3 "$ROOT/tools/build_thumbnails.py" --archive-scan --workers 1 --prune || {
    log "WARNING: refreshed index is live, but derivative cache pruning failed"; }
else
  log "no source, message, part, or media changes; skipping index rebuilds"
fi
if [ "$archive_failed" -ne 0 ]; then
  log "live import completed, but source archiving needs attention"
  exit 1
fi
log "done"
