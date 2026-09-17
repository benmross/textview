#!/bin/bash
# Full unified-message database rebuild from every known source, then atomic swap.
set -euo pipefail
ROOT="$HOME/Documents/textview"; cd "$ROOT"
NEW="$ROOT/messages-rebuilt.db"
SOURCE_ARCHIVE="${MESSAGE_SOURCE_ARCHIVE:-/mnt/Backups/message-sources}"
LOG(){ echo "[$(date '+%F %T')] $*"; }

if [ ! -d "$SOURCE_ARCHIVE" ]; then
  LOG "source archive is unavailable: $SOURCE_ARCHIVE"
  exit 1
fi

LOG "=== 1/10 importing 3 nonredundant Android SMS/MMS/RCS backups ==="
if [ "${TEXTVIEW_REBUILD_RESUME:-0}" = "1" ] && [ -f "$NEW" ]; then
  LOG "resuming from existing $NEW"
else
  rm -f "$NEW" "$NEW"-wal "$NEW"-shm
  python3 tools/import_archive.py -o "$NEW" \
    "$SOURCE_ARCHIVE/android/sms-backup-restore/sms-20250212171642.xml" \
    "$SOURCE_ARCHIVE/android/sms-backup-restore/sms-20250619153114.xml" \
    "$SOURCE_ARCHIVE/android/sms-backup-restore/sms-20260610030051.xml" || { LOG "XML import FAILED"; exit 1; }
fi

LOG "=== 2/10 merging cross-backup MMS/RCS duplicates ==="
# merge_cross_backup_duplicates only repoints rows in reaction_events; the table is
# created by normalize_protocol, which must run AFTER the merge so its owner-confirmed
# overrides see a de-duplicated archive. Create it empty so the merge can run first.
python3 - "$NEW" <<'PYEOF'
import sqlite3, sys
db=sqlite3.connect(sys.argv[1])
db.executescript("""
CREATE TABLE IF NOT EXISTS reaction_events (
  id INTEGER PRIMARY KEY,
  reaction_message_id INTEGER NOT NULL UNIQUE REFERENCES messages(id) ON DELETE CASCADE,
  target_message_id INTEGER REFERENCES messages(id),
  reactor_participant_id INTEGER REFERENCES participants(id),
  reaction TEXT NOT NULL,
  target_excerpt TEXT,
  target_kind TEXT NOT NULL,
  resolution_method TEXT,
  resolution_confidence TEXT NOT NULL
    CHECK(resolution_confidence IN ('exact','high','unresolved'))
);
CREATE INDEX IF NOT EXISTS reactions_target_idx ON reaction_events(target_message_id);
""")
db.commit()
PYEOF
python3 tools/merge_cross_backup_duplicates.py "$NEW" || { LOG "merge FAILED"; exit 1; }

LOG "=== 3/10 normalising protocol metadata + Android reactions ==="
# NOTE: this DELETEs reaction_events, so it must run BEFORE the iMessage imports.
python3 tools/normalize_protocol.py "$NEW" || { LOG "normalize_protocol FAILED"; exit 1; }

LOG "=== 4/10 importing Mac chat.db ==="
CHAT_DB="$SOURCE_ARCHIVE/apple/messages/chat.db"
python3 tools/import_imessage.py --repair-existing --chat-db "$CHAT_DB" \
  --attachments "$SOURCE_ARCHIVE/apple/messages/Attachments" \
  --sticker-cache "$SOURCE_ARCHIVE/apple/messages/StickerCache" \
  --fallback-root "$SOURCE_ARCHIVE/supplemental-attachments/BlueBubblesLegion" \
  --fallback-root "$SOURCE_ARCHIVE/supplemental-attachments/LegionMessagesAttachments" \
  -o "$NEW" --source-label "chat.db-macmini" || { LOG "mac import FAILED"; exit 1; }

LOG "=== 5/10 importing the nonredundant BlueBubbles export ==="
python3 tools/import_bluebubbles.py --export "$SOURCE_ARCHIVE/bluebubbles/exports/BlueBubbles-chats-2026822_2117.json" \
  -o "$NEW" --source-label "BlueBubbles-chats-2026822_2117.json" --repair-existing || { LOG "August BlueBubbles import FAILED"; exit 1; }

LOG "=== 6/10 backfilling attachment bytes from every known store ==="
python3 tools/backfill_attachments.py "$NEW" \
  --root "$SOURCE_ARCHIVE/supplemental-attachments/LegionMessagesAttachments" \
  --root "$SOURCE_ARCHIVE/supplemental-attachments/BlueBubblesLegion" \
  --root "$SOURCE_ARCHIVE/apple/messages/Attachments" \
  --root "$SOURCE_ARCHIVE/apple/messages/StickerCache" \
  --root "$SOURCE_ARCHIVE/supplemental-attachments/PixelMessages" \
  --immich-map "/usr/src/app/upload=/mnt/medialibrary/photos" || { LOG "attachment backfill FAILED"; exit 1; }

LOG "=== 7/10 annotating media container types ==="
python3 tools/annotate_media.py "$NEW" || { LOG "media annotation FAILED"; exit 1; }

LOG "=== 8/10 inferring missing content types ==="
python3 - "$NEW" <<'PY'
import sqlite3, sys
db=sqlite3.connect(sys.argv[1])
M={"caf":"audio/x-caf","m4a":"audio/mp4","amr":"audio/amr","heic":"image/heic","heics":"image/heic-sequence",
   "jpg":"image/jpeg","jpeg":"image/jpeg","png":"image/png","gif":"image/gif","webp":"image/webp",
   "mov":"video/quicktime","mp4":"video/mp4","m4v":"video/x-m4v","pdf":"application/pdf","vcf":"text/vcard"}
n=0
for pid,fn in db.execute("""select mp.id,mp.filename from message_parts mp join messages m on m.id=mp.message_id
                            where m.transport='imessage' and mp.content_type is null and mp.filename is not null""").fetchall():
    t=M.get(fn.rsplit(".",1)[-1].lower() if "." in fn else "")
    if t: db.execute("update message_parts set content_type=? where id=?", (t,pid)); n+=1
db.commit(); print(f"   inferred {n} content types")
PY

LOG "=== 9/10 building search index ==="
python3 tools/rebuild_search.py "$NEW" || { LOG "FTS FAILED"; exit 1; }

LOG "=== 10/10 prebuilding browser media derivatives ==="
python3 tools/build_thumbnails.py --database "$NEW" --archive-scan --workers 3 || {
  LOG "media derivative build FAILED"; exit 1; }

LOG "=== verification (no swap yet) ==="
python3 tools/finalize_archive.py "$NEW" --output validation-rebuilt.json --deep || {
  LOG "deep database validation FAILED"; exit 1; }
python3 tools/audit_attachments.py "$NEW" --output attachment-audit-rebuilt.json || {
  LOG "attachment container audit FAILED"; exit 1; }
python3 - "$NEW" <<'PY'
import sqlite3, sys
d=sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
q=lambda s: d.execute(s).fetchall()
print("   messages:", q("select count(*) from messages")[0][0])
for r in q("select transport,count(*) from messages group by 1 order by 2 desc"): print("     ",r)
print("   media:", q("select count(*) from media")[0][0], "blobs")
print("   parts with media:", q("select count(*) from message_parts where media_id is not null")[0][0])
print("   range:", q("select datetime(min(timestamp_ms)/1000,'unixepoch'),datetime(max(timestamp_ms)/1000,'unixepoch') from messages")[0])
PY
LOG "REBUILD COMPLETE - awaiting verification before swap"
