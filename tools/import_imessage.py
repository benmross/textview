#!/usr/bin/env python3
"""Import a macOS Messages chat.db into the TextView archive.

Adds 'imessage' as a transport alongside the existing sms/mms/rcs Android data.
Idempotent: re-running only inserts messages whose guid is not already present.
"""
from __future__ import annotations
import argparse, hashlib, json, os, re, sqlite3, sys, datetime
from local_overrides import IMESSAGE_SOURCE_HOST

from attachment_resolution import choose_fallback

from imessage_semantics import (
    associated_type, clean_text, effective_direction, fully_unsent,
    is_reaction_add, reaction_label, semantic_presentation,
)
from sticker_semantics import (
    decode_sticker_info, ensure_sticker_schema, insert_sticker_event,
    is_animated_sticker, message_dedup, safe_float, sticker_kind,
    target_guid as bare_target_guid,
)
from apple_message_features import (
    decode_app_payload, decode_edit_history, ensure_feature_schema,
)

APPLE_EPOCH = 978307200  # 2001-01-01 UTC

# ---------- attributedBody (NSArchiver typedstream) ----------
def _read_len(b, i):
    if i >= len(b): return None, i
    n = b[i]
    if n == 0x81: return int.from_bytes(b[i+1:i+3], "little"), i+3
    if n == 0x82: return int.from_bytes(b[i+1:i+4], "little"), i+4
    if n == 0x83: return int.from_bytes(b[i+1:i+5], "little"), i+5
    if n == 0x84: return int.from_bytes(b[i+1:i+9], "little"), i+9
    return n, i+1

def decode_attributed(blob):
    if not blob: return None
    b = bytes(blob)
    for m in re.finditer(b"NSString", b):
        k = b.find(b"+", m.end(), m.end() + 24)
        if k == -1: continue
        ln, p = _read_len(b, k + 1)
        if not ln or ln <= 0 or p + ln > len(b): continue
        try: return b[p:p+ln].decode("utf-8")
        except UnicodeDecodeError:
            try: return b[p:p+ln].decode("utf-16-le")
            except UnicodeDecodeError: continue
    return None

# ---------- participant identity (mirrors tools/import_archive.py) ----------
PHONE_RE = re.compile(r"[^0-9+]")
def canonical_address(address):
    if address is None: return "__self__", None, 1
    a = address.strip()
    if not a or a.lower() in {"insert-address-token", "self", "__self__"}:
        return "__self__", None, 1
    digits = PHONE_RE.sub("", a).lstrip("+")
    if digits.isdigit() and len(digits) >= 7:
        if len(digits) == 11 and digits.startswith("1"): digits = digits[1:]
        return "tel:" + digits, a, 0
    return "addr:" + a.casefold().strip(), a, 0

def apple_ms(v):
    if not v: return None
    v = int(v)
    sec = v / 1e9 if v > 10**11 else float(v)
    return int((sec + APPLE_EPOCH) * 1000)

EXT2MIME = {"caf":"audio/x-caf","m4a":"audio/mp4","amr":"audio/amr","heic":"image/heic",
            "heics":"image/heic-sequence","jpg":"image/jpeg","jpeg":"image/jpeg","png":"image/png",
            "gif":"image/gif","webp":"image/webp","mov":"video/quicktime","mp4":"video/mp4",
            "m4v":"video/x-m4v","pdf":"application/pdf","vcf":"text/vcard"}


def guess_type(declared, filename):
    """chat.db often leaves mime_type NULL; infer it so the UI can render a player."""
    if declared: return declared
    if not filename or "." not in filename: return declared
    return EXT2MIME.get(filename.rsplit(".", 1)[-1].lower(), declared)


def resolve_attachment(fn, attachments_root, sticker_root):
    """Map a chat.db attachment path onto the local mirrors."""
    if fn.startswith("~/Library/Messages/Attachments/"):
        return os.path.join(attachments_root, fn[len("~/Library/Messages/Attachments/"):])
    if fn.startswith("~/Library/Messages/StickerCache/"):
        if not sticker_root: return None
        return os.path.join(sticker_root, fn[len("~/Library/Messages/StickerCache/"):])
    if "/Attachments/" in fn:
        return os.path.join(attachments_root, fn.split("/Attachments/", 1)[1])
    return None


def find_part(db, message_id, sequence, content_type, filename, text_content):
    """UNIQUE(...) does not dedupe when columns are NULL, so check explicitly."""
    row = db.execute(
        "select id from message_parts where message_id=? and sequence=?"
        " and ifnull(content_type,'')=ifnull(?,'') and ifnull(filename,'')=ifnull(?,'')"
        " and ifnull(text_content,'')=ifnull(?,'') limit 1",
        (message_id, sequence, content_type, filename, text_content)).fetchone()
    return row[0] if row else None

def migrate_checks(db):
    """Widen the transport / source_kind CHECK constraints if needed."""
    sql = db.execute("select sql from sqlite_master where name='messages'").fetchone()[0]
    if "'imessage'" in sql:
        return False
    db.executescript("""
    PRAGMA foreign_keys=off;
    BEGIN;
    CREATE TABLE messages_new (
      id INTEGER PRIMARY KEY,
      dedup_key TEXT NOT NULL UNIQUE,
      timestamp_ms INTEGER NOT NULL,
      sent_timestamp_ms INTEGER,
      direction TEXT NOT NULL,
      transport TEXT NOT NULL CHECK(transport IN ('sms','mms','rcs','imessage')),
      is_group INTEGER NOT NULL DEFAULT 0 CHECK(is_group IN (0,1)),
      subject TEXT, body TEXT, read INTEGER, status TEXT,
      source_occurrences INTEGER NOT NULL DEFAULT 1
    );
    INSERT INTO messages_new SELECT * FROM messages;
    DROP TABLE messages;
    ALTER TABLE messages_new RENAME TO messages;
    CREATE INDEX messages_time_idx ON messages(timestamp_ms);
    CREATE INDEX messages_transport_idx ON messages(transport);

    CREATE TABLE message_sources_new (
      id INTEGER PRIMARY KEY,
      message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
      backup_id INTEGER NOT NULL REFERENCES backups(id),
      source_ordinal INTEGER NOT NULL,
      source_kind TEXT NOT NULL CHECK(source_kind IN ('sms','mms','imessage')),
      source_android_id TEXT,
      metadata_json TEXT NOT NULL,
      UNIQUE(backup_id, source_ordinal)
    );
    INSERT INTO message_sources_new SELECT * FROM message_sources;
    DROP TABLE message_sources;
    ALTER TABLE message_sources_new RENAME TO message_sources;
    CREATE INDEX message_sources_message_idx ON message_sources(message_id);
    COMMIT;
    PRAGMA foreign_keys=on;
    """)
    return True


def migrate_semantics(db):
    """Add normalized reply relationships without coupling them to a source."""
    # Old builds permitted the same decoded bytes when different sources used
    # different encoded forms. Canonicalize those rows before enforcing the
    # content-addressed invariant.
    duplicates = db.execute(
        "select content_sha256,min(id),group_concat(id) from media "
        "group by content_sha256 having count(*)>1"
    ).fetchall()
    for _, keep, ids_csv in duplicates:
        for duplicate in (int(value) for value in ids_csv.split(',')):
            if duplicate == keep:
                continue
            db.execute(
                "update message_parts set media_id=? where media_id=?",
                (keep, duplicate)
            )
            db.execute(
                "update source_parts set media_id=? where media_id=?",
                (keep, duplicate)
            )
            db.execute("delete from media where id=?", (duplicate,))
    db.executescript("""
    CREATE INDEX IF NOT EXISTS message_sources_message_idx
      ON message_sources(message_id);
    CREATE UNIQUE INDEX IF NOT EXISTS media_content_sha_unique
      ON media(content_sha256);
    CREATE INDEX IF NOT EXISTS media_metadata_idx
      ON media(id,content_sha256,detected_content_type,byte_size,validation_status);
    CREATE INDEX IF NOT EXISTS source_addresses_participant_idx
      ON source_addresses(participant_id);
    CREATE INDEX IF NOT EXISTS source_parts_media_idx
      ON source_parts(media_id);
    CREATE INDEX IF NOT EXISTS reactions_reactor_idx
      ON reaction_events(reactor_participant_id);
    CREATE TABLE IF NOT EXISTS message_replies (
      message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
      target_message_id INTEGER REFERENCES messages(id),
      target_guid TEXT,
      target_part TEXT,
      resolution_confidence TEXT NOT NULL
        CHECK(resolution_confidence IN ('exact','unresolved'))
    );
    CREATE INDEX IF NOT EXISTS message_replies_target_idx
      ON message_replies(target_message_id);
    """)
    ensure_sticker_schema(db)
    ensure_feature_schema(db)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat-db", required=True)
    ap.add_argument("--attachments", required=True, help="local copy of ~/Library/Messages/Attachments")
    ap.add_argument("--sticker-cache", default=None, help="local copy of ~/Library/Messages/StickerCache")
    ap.add_argument("--fallback-root", action="append", default=[],
                    help="extra directory searched by basename when the exact path is absent (repeatable)")
    ap.add_argument("-o", "--output", required=True, help="messages.db to import into")
    ap.add_argument("--source-label", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--repair-existing", action="store_true",
                    help="reinterpret and update rows already imported from this source")
    a = ap.parse_args()

    # The input is a completed checkpoint and cannot change during import.
    # immutable=1 avoids creating WAL/SHM sidecars beside source files.
    src = sqlite3.connect(f"file:{a.chat_db}?mode=ro&immutable=1", uri=True)
    src.row_factory = sqlite3.Row
    db = sqlite3.connect(a.output)
    db.execute("PRAGMA journal_mode=WAL")

    changed = migrate_checks(db)
    migrate_semantics(db)
    print(f"schema migration applied: {changed}")

    label = a.source_label or os.path.basename(a.chat_db)
    st = os.stat(a.chat_db)
    h = hashlib.sha256()
    with open(a.chat_db, "rb") as f:
        for chunk in iter(lambda: f.read(8*1024*1024), b""): h.update(chunk)
    source_hash = h.hexdigest()
    source_count = src.execute("select count(*) from message").fetchone()[0]
    imported_at = datetime.datetime.now(datetime.UTC).isoformat()
    source_metadata = json.dumps({"source":"macOS Messages chat.db","host":IMESSAGE_SOURCE_HOST})
    row = db.execute("select id from backups where filename=?", (label,)).fetchone()
    if row:
        backup_id = row[0]
        if not a.dry_run:
            # This row describes a rolling live source, unlike the immutable
            # export rows. Keep its census and checkpoint provenance current.
            db.execute(
                "update backups set file_size=?,file_sha256=?,backup_date_ms=?,"
                "declared_count=?,imported_at=?,root_metadata_json=? where id=?",
                (st.st_size, source_hash, int(st.st_mtime * 1000), source_count,
                 imported_at, source_metadata, backup_id),
            )
    else:
        backup_id = db.execute(
            "insert into backups(filename,file_size,file_sha256,backup_set,backup_date_ms,"
            "declared_count,backup_type,first_message_ms,last_message_ms,imported_at,root_metadata_json)"
            " values(?,?,?,?,?,?,?,?,?,?,?)",
            (label, st.st_size, source_hash, None, int(st.st_mtime*1000),
             source_count, "imessage-chatdb", None, None, imported_at,
             source_metadata)).lastrowid

    # caches
    pcache = {}
    def participant(addr, name=None):
        canon, disp, is_self = canonical_address(addr)
        if canon in pcache: pid = pcache[canon]
        else:
            r = db.execute("select id from participants where canonical_address=?", (canon,)).fetchone()
            pid = r[0] if r else db.execute(
                "insert into participants(canonical_address,display_address,is_self) values(?,?,?)",
                (canon, disp, is_self)).lastrowid
            pcache[canon] = pid
        if name:
            db.execute("insert into participant_names(participant_id,name) values(?,?)"
                       " on conflict(participant_id,name) do update set occurrence_count=occurrence_count+1",
                       (pid, name))
        return pid

    mcache = {}
    def store_media(path):
        try:
            with open(path, "rb") as f: data = f.read()
        except OSError: return None, None
        if not data:
            # macOS leaves zero-byte placeholders for attachments it never fetched;
            # storing them would dedupe every stub onto one empty blob.
            return None, 0
        sha = hashlib.sha256(data).hexdigest()
        if sha in mcache: return mcache[sha], len(data)
        # Raw iMessage attachments use their byte hash as both hashes. Match
        # either column so an identical payload already imported from an
        # Android backup is reused even when that backup's base64 encoding had
        # a different encoded_sha256 value.
        r = db.execute(
            "select id from media where encoded_sha256=? or content_sha256=? "
            "order by id limit 1", (sha, sha)
        ).fetchone()
        if r: mid = r[0]
        else:
            mid = db.execute("insert into media(encoded_sha256,content_sha256,byte_size,data,"
                             "detected_content_type,validation_status) values(?,?,?,?,?,?)",
                             (sha, sha, len(data), data, None, "unannotated")).lastrowid
        mcache[sha] = mid
        return mid, len(data)

    existing = {r[0]: r[1] for r in db.execute(
        "select source_ordinal,message_id from message_sources where backup_id=?", (backup_id,))}
    needs_media_repair = {
        row[0] for row in db.execute(
            "select distinct s.source_ordinal from message_sources s "
            "join message_parts p on p.message_id=s.message_id "
            "where s.backup_id=? and p.media_id is null and p.filename is not null",
            (backup_id,),
        )
    }
    print(f"already imported from this source: {len(existing):,}")

    # chat participants
    chat_handles = {}
    for r in src.execute("select chat_id, handle_id from chat_handle_join"):
        chat_handles.setdefault(r["chat_id"], []).append(r["handle_id"])
    handles = {r["ROWID"]: r["id"] for r in src.execute("select ROWID,id from handle")}
    msg_chat = {r["message_id"]: r["chat_id"] for r in src.execute("select message_id,chat_id from chat_message_join")}
    chat_style = {r["ROWID"]: (r["style"], r["display_name"], r["guid"])
                  for r in src.execute("select ROWID,style,display_name,guid from chat")}

    atts = {}
    for r in src.execute("select message_id,attachment_id from message_attachment_join"):
        atts.setdefault(r["message_id"], []).append(r["attachment_id"])
    # Keep the complete attachment row. Recent macOS versions add sticker
    # fields over time; selecting only the historical six columns silently
    # discarded the placement plist and made future stickers irrecoverable.
    attinfo = {r["ROWID"]: r for r in src.execute("select * from attachment")}

    fallback_index = {}
    for root in a.fallback_root:
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                fallback_index.setdefault(f.casefold(), []).append(os.path.join(dirpath, f))
    print(f"fallback index: {sum(map(len, fallback_index.values())):,} files "
          f"under {len(fallback_index):,} names from {len(a.fallback_root)} root(s)")

    stats = dict(inserted=0, repaired=0, skipped=0, media=0, media_missing=0,
                 media_fallback=0, media_ambiguous=0, reactions=0, replies=0,
                 hidden=0, announcements=0, empty=0)
    fallback_hashes = {}
    guid_to_mid = {}
    pending_reactions = []
    pending_replies = []

    rows = src.execute("select * from message order by ROWID").fetchall()
    for m in rows:
        rid = m["ROWID"]
        repair_row = a.repair_existing or rid in needs_media_repair
        if rid in existing and not repair_row:
            stats["skipped"] += 1
            continue
        body = clean_text(m["text"] or decode_attributed(m["attributedBody"]))
        ts = apple_ms(m["date"])
        if ts is None: continue
        svc = (m["service"] or "").lower()
        transport = "imessage" if "imessage" in svc else ("rcs" if svc == "rcs" else "sms")
        direction = effective_direction(
            is_from_me=m["is_from_me"], item_type=m["item_type"],
            other_handle=m["other_handle"], share_direction=m["share_direction"])
        cid = msg_chat.get(rid)
        style, dispname, chat_guid = chat_style.get(cid, (None, None, None))
        others = [handles.get(h) for h in chat_handles.get(cid, []) if handles.get(h)]
        is_group = 1 if (style == 43 or len(set(others)) > 1) else 0
        dedup = hashlib.sha256(("imessage:" + (m["guid"] or f"rowid:{rid}")).encode()).hexdigest()

        amt = associated_type(m["associated_message_type"])
        has_sticker_attachment = any(
            bool(attinfo[aid]["is_sticker"])
            for aid in atts.get(rid, [])
            if aid in attinfo and "is_sticker" in attinfo[aid].keys()
        )
        body, status = semantic_presentation(
            body=body, item_type=m["item_type"], group_action_type=m["group_action_type"],
            group_title=m["group_title"], share_status=m["share_status"],
            direction=direction,
            balloon_bundle_id=None if has_sticker_attachment and amt == 0 else m["balloon_bundle_id"],
            associated_message_type=amt,
            is_fully_unsent=fully_unsent(m["message_summary_info"]),
        )
        if status is None and m["date_edited"]:
            status = "edited"
        if status is None and body is None and rid not in atts:
            # Some CloudKit/app bookkeeping rows contain no renderable payload.
            status = "hidden"
        if status == "hidden": stats["hidden"] += 1
        if status == "announcement": stats["announcements"] += 1
        if is_reaction_add(amt):
            pending_reactions.append((dedup, m, amt))
        if m["thread_originator_guid"]:
            pending_replies.append((dedup, m["thread_originator_guid"], m["thread_originator_part"]))

        if a.dry_run:
            stats["inserted"] += 1; continue

        try:
            mid = db.execute(
                "insert into messages(dedup_key,timestamp_ms,sent_timestamp_ms,direction,transport,"
                "is_group,subject,body,read,status,source_occurrences) values(?,?,?,?,?,?,?,?,?,?,1)",
                (dedup, ts, apple_ms(m["date_delivered"]), direction, transport, is_group,
                 m["subject"], body, 1 if m["is_read"] else 0, status)).lastrowid
        except sqlite3.IntegrityError:
            r = db.execute("select id from messages where dedup_key=?", (dedup,)).fetchone()
            mid = r[0]
            db.execute("update messages set direction=?,transport=?,is_group=?,subject=?,body=?,"
                       "read=?,status=? where id=?",
                       (direction, transport, is_group, m["subject"], body,
                        1 if m["is_read"] else 0, status, mid))
            stats["repaired"] += 1
        guid_to_mid[m["guid"]] = mid
        if body is None and rid not in atts and status != "hidden": stats["empty"] += 1

        metadata = json.dumps({"guid":m["guid"],"service":m["service"],"chat_id":cid,
                               "chat_guid":chat_guid,"chat_display_name":dispname,
                               "is_from_me":m["is_from_me"],"item_type":m["item_type"],
                               "group_action_type":m["group_action_type"],
                               "associated_message_type":amt,
                               "associated_message_guid":m["associated_message_guid"],
                               "associated_message_emoji":m["associated_message_emoji"],
                               "balloon_bundle_id":m["balloon_bundle_id"],
                               "thread_originator_guid":m["thread_originator_guid"],
                               "thread_originator_part":m["thread_originator_part"],
                               "date_edited":m["date_edited"],
                               "expressive_send_style_id":m["expressive_send_style_id"]})
        db.execute("insert into message_sources(message_id,backup_id,source_ordinal,source_kind,"
                   "source_android_id,metadata_json) values(?,?,?,?,?,?)"
                   " on conflict(backup_id,source_ordinal) do update set message_id=excluded.message_id,"
                   "source_android_id=excluded.source_android_id,metadata_json=excluded.metadata_json",
                   (mid, backup_id, rid, "imessage", m["guid"], metadata))

        if rid in existing and repair_row:
            db.execute("delete from message_participants where message_id=?", (mid,))
            db.execute("delete from message_parts where message_id=? and media_id is null and "
                       "lower(coalesce(content_type,''))='text/plain'", (mid,))

        sender = handles.get(m["handle_id"])
        if direction == "outgoing":
            db.execute("insert or ignore into message_participants(message_id,participant_id,role,"
                       "source_address_type) values(?,?,?,?)", (mid, participant(None), "from", 137))
            for o in dict.fromkeys(others):
                db.execute("insert or ignore into message_participants(message_id,participant_id,role,"
                           "source_address_type) values(?,?,?,?)", (mid, participant(o), "to", 151))
        else:
            db.execute("insert or ignore into message_participants(message_id,participant_id,role,"
                       "source_address_type) values(?,?,?,?)", (mid, participant(sender), "from", 137))
            db.execute("insert or ignore into message_participants(message_id,participant_id,role,"
                       "source_address_type) values(?,?,?,?)", (mid, participant(None), "to", 151))
            for o in dict.fromkeys(others):
                if o != sender:
                    db.execute("insert or ignore into message_participants(message_id,participant_id,"
                               "role,source_address_type) values(?,?,?,?)", (mid, participant(o), "to", 151))

        seq = 0
        if body:
            if not find_part(db, mid, seq, "text/plain", None, body):
              db.execute("insert or ignore into message_parts(message_id,sequence,content_type,filename,"
                       "content_location,content_id,charset,text_content,media_id,metadata_json)"
                       " values(?,?,?,?,?,?,?,?,?,?)",
                       (mid, seq, "text/plain", None, None, None, "utf-8", body, None, json.dumps({})))
            seq += 1
        for aid in atts.get(rid, []):
            info = attinfo.get(aid)
            if not info: continue
            fn = info["filename"]
            mediaid = None
            if fn:
                p = resolve_attachment(fn, a.attachments, a.sticker_cache)
                if not p or not os.path.exists(p):
                    p, reason = choose_fallback(
                        fallback_index.get((info["transfer_name"] or "").casefold(), []),
                        info["total_bytes"], hash_cache=fallback_hashes,
                    )
                    if p:
                        stats["media_fallback"] += 1
                    elif reason == "ambiguous":
                        stats["media_ambiguous"] += 1
                mediaid, _ = store_media(p) if p else (None, None)
                if mediaid: stats["media"] += 1
                else: stats["media_missing"] += 1
            content_type = guess_type(info["mime_type"], info["transfer_name"])
            part_metadata = json.dumps({
                "uti": info["uti"], "total_bytes": info["total_bytes"],
                "is_sticker": info["is_sticker"] if "is_sticker" in info.keys() else 0,
                "sticker_info": decode_sticker_info(info["sticker_user_info"])
                    if "sticker_user_info" in info.keys() else {},
                "width": info["width"] if "width" in info.keys() else None,
                "height": info["height"] if "height" in info.keys() else None,
                "emoji_image_content_identifier":
                    info["emoji_image_content_identifier"]
                    if "emoji_image_content_identifier" in info.keys() else None,
                "emoji_image_short_description":
                    info["emoji_image_short_description"]
                    if "emoji_image_short_description" in info.keys() else None,
            })
            part_id = find_part(db, mid, seq, content_type, info["transfer_name"], None)
            if part_id and a.repair_existing:
                # A requested full repair includes clearing a formerly guessed
                # media link when available source files are now ambiguous.
                db.execute(
                    "update message_parts set content_location=?,media_id=?,metadata_json=? where id=?",
                    (fn, mediaid, part_metadata, part_id),
                )
            elif part_id and repair_row:
                # Routine sync revisits messages with unavailable parts, but
                # must never clear or replace bytes recovered by another source.
                db.execute(
                    "update message_parts set content_location=?,"
                    "media_id=coalesce(media_id,?),metadata_json=? where id=?",
                    (fn, mediaid, part_metadata, part_id),
                )
            elif not part_id:
                db.execute("insert into message_parts(message_id,sequence,content_type,filename,"
                       "content_location,content_id,charset,text_content,media_id,metadata_json)"
                       " values(?,?,?,?,?,?,?,?,?,?)",
                       (mid, seq, content_type,
                        info["transfer_name"], fn, None, None, None,
                        mediaid, part_metadata))
            seq += 1
        stats["inserted"] += rid not in existing
        processed = stats["inserted"] + stats["repaired"]
        if processed and processed % 5000 == 0:
            db.commit(); print(f"  ... {processed:,} processed", flush=True)

    if not a.dry_run:
        if a.repair_existing:
            db.execute("delete from reaction_events where reaction_message_id in "
                       "(select message_id from message_sources where backup_id=?)", (backup_id,))
        # Reactions are records attached to another message, not timeline bubbles.
        for dedup, m, amt in pending_reactions:
            tgt = m["associated_message_guid"] or ""
            tg = tgt.split("/")[-1] if "/" in tgt else tgt.replace("bp:", "").replace("p:", "")
            r = db.execute("select id from messages where dedup_key=?", (dedup,)).fetchone()
            if not r: continue
            rmid = r[0]
            tmid = guid_to_mid.get(tg)
            if tmid is None and tg:
                rr = db.execute("select id from messages where dedup_key=?",
                                (hashlib.sha256(("imessage:"+tg).encode()).hexdigest(),)).fetchone()
                tmid = rr[0] if rr else None
            reactor = participant(None if m["is_from_me"] else handles.get(m["handle_id"]))
            db.execute("insert or ignore into reaction_events(reaction_message_id,target_message_id,"
                       "reactor_participant_id,reaction,target_excerpt,target_kind,resolution_method,"
                       "resolution_confidence) values(?,?,?,?,?,?,?,?)",
                       (rmid, tmid, reactor,
                        reaction_label(amt, m["associated_message_emoji"]) or f"type{amt}", None, "text",
                        "chatdb-associated-guid", "exact" if tmid else "unresolved"))
            stats["reactions"] += 1
        for dedup, target_guid, target_part in pending_replies:
            r = db.execute("select id from messages where dedup_key=?", (dedup,)).fetchone()
            if not r: continue
            target_guid = (target_guid or "").split("/")[-1].replace("bp:", "").replace("p:", "")
            target = db.execute("select id from messages where dedup_key=?",
                                (hashlib.sha256(("imessage:" + target_guid).encode()).hexdigest(),)).fetchone()
            db.execute("insert into message_replies(message_id,target_message_id,target_guid,target_part,"
                       "resolution_confidence) values(?,?,?,?,?) on conflict(message_id) do update set "
                       "target_message_id=excluded.target_message_id,target_guid=excluded.target_guid,"
                       "target_part=excluded.target_part,resolution_confidence=excluded.resolution_confidence",
                       (r[0], target[0] if target else None, target_guid, target_part,
                        "exact" if target else "unresolved"))
            stats["replies"] += 1

        # Sticker semantics are synchronized source-wide, even when every
        # message was already imported. This makes schema upgrades repair old
        # rows and makes the hourly live import cover every future sticker.
        attachment_columns = {row[1] for row in src.execute("pragma table_info(attachment)")}
        sticker_flag = "coalesce(a.is_sticker,0)" if "is_sticker" in attachment_columns else "0"
        sticker_rows = src.execute(f"""
            SELECT m.ROWID message_rowid,m.associated_message_type,
                   m.associated_message_guid,m.associated_message_emoji,
                   m.associated_message_range_location,m.associated_message_range_length,
                   m.balloon_bundle_id,a.*
            FROM message m
            JOIN message_attachment_join j ON j.message_id=m.ROWID
            JOIN attachment a ON a.ROWID=j.attachment_id
            WHERE {sticker_flag}=1
               OR cast(coalesce(m.associated_message_type,0) AS INTEGER) IN (1000,2007)
            ORDER BY m.ROWID,a.ROWID
        """).fetchall()
        sticker_count = 0
        for row in sticker_rows:
            mapped = db.execute(
                "select message_id from message_sources where backup_id=? and source_ordinal=?",
                (backup_id, row["message_rowid"]),
            ).fetchone()
            if not mapped:
                continue
            sticker_message_id = mapped[0]
            part = db.execute(
                """select id,media_id,content_type,filename from message_parts
                   where message_id=? and
                     (ifnull(content_location,'')=ifnull(?,'') or
                      ifnull(filename,'')=ifnull(?,''))
                   order by media_id is not null desc,id limit 1""",
                (sticker_message_id, row["filename"], row["transfer_name"]),
            ).fetchone()
            if not part:
                continue
            info = decode_sticker_info(
                row["sticker_user_info"] if "sticker_user_info" in row.keys() else None
            )
            is_sticker = bool(row["is_sticker"]) if "is_sticker" in row.keys() else False
            kind = sticker_kind(row["associated_message_type"], is_sticker)
            if not kind:
                continue
            guid = bare_target_guid(row["associated_message_guid"])
            target = db.execute(
                "select id from messages where dedup_key=?",
                (message_dedup(guid),),
            ).fetchone() if guid else None
            insert_sticker_event(db, {
                "sticker_message_id": sticker_message_id,
                "target_message_id": target[0] if target else None,
                "part_id": part[0], "media_id": part[1],
                "source_attachment_id": f"mac:{row['ROWID']}", "source_kind": "mac",
                "kind": kind, "target_guid": guid,
                "target_part": row["associated_message_range_location"],
                "position_x": safe_float(info.get("sxs")),
                "position_y": safe_float(info.get("sys")),
                "scale": safe_float(info.get("ssa")),
                "rotation": safe_float(info.get("sro")),
                "parent_width": safe_float(info.get("spw")),
                "source_app": info.get("pid") or row["balloon_bundle_id"],
                "short_description":
                    (row["emoji_image_short_description"]
                     if "emoji_image_short_description" in row.keys() else None)
                    or row["associated_message_emoji"],
                "is_animated": is_animated_sticker(part[2], part[3]),
                "metadata_json": {
                    "placement": info,
                    "range_location": row["associated_message_range_location"],
                    "range_length": row["associated_message_range_length"],
                    "uti": row["uti"],
                },
            })
            sticker_count += 1
        stats["stickers"] = sticker_count

        feature_rows = src.execute("""
            SELECT ROWID,message_summary_info,expressive_send_style_id,
                   balloon_bundle_id,payload_data,item_type
            FROM message
            WHERE date_edited!=0
               OR (expressive_send_style_id IS NOT NULL AND expressive_send_style_id!='')
               OR (balloon_bundle_id IS NOT NULL AND balloon_bundle_id!='')
               OR item_type=5
        """).fetchall()
        feature_stats = {"edits": 0, "effects": 0, "app_messages": 0, "shareplay": 0}
        for row in feature_rows:
            mapped = db.execute(
                "select message_id from message_sources where backup_id=? and source_ordinal=?",
                (backup_id, row["ROWID"]),
            ).fetchone()
            if not mapped:
                continue
            message_id = mapped[0]
            history = decode_edit_history(row["message_summary_info"], decode_attributed, apple_ms)
            if history:
                db.execute("delete from message_edits where message_id=?", (message_id,))
                db.executemany(
                    "insert into message_edits(message_id,part_index,edit_order,timestamp_ms,body) "
                    "values(?,?,?,?,?)",
                    [(message_id, item["part_index"], item["edit_order"],
                      item["timestamp_ms"], item["body"]) for item in history],
                )
                feature_stats["edits"] += 1
            effect = row["expressive_send_style_id"]
            if effect:
                db.execute(
                    "insert into message_effects(message_id,effect_id) values(?,?) "
                    "on conflict(message_id) do update set effect_id=excluded.effect_id",
                    (message_id, effect),
                )
                feature_stats["effects"] += 1
            bundle = row["balloon_bundle_id"]
            if bundle:
                fallback_body = db.execute(
                    "select body from messages where id=?", (message_id,)
                ).fetchone()[0]
                card = decode_app_payload(bundle, row["payload_data"], fallback_body)
                db.execute(
                    """insert into app_messages(message_id,bundle_id,app_name,title,subtitle,url,metadata_json)
                       values(?,?,?,?,?,?,?) on conflict(message_id) do update set
                       bundle_id=excluded.bundle_id,app_name=excluded.app_name,
                       title=excluded.title,subtitle=excluded.subtitle,url=excluded.url,
                       metadata_json=excluded.metadata_json""",
                    (message_id, card["bundle_id"], card["app_name"], card["title"],
                     card["subtitle"], card["url"], card["metadata_json"]),
                )
                feature_stats["app_messages"] += 1
            if int(row["item_type"] or 0) == 5:
                db.execute(
                    "update messages set body='SharePlay ended',status='announcement' where id=?",
                    (message_id,),
                )
                feature_stats["shareplay"] += 1
        stats.update(feature_stats)
        db.execute("update backups set first_message_ms=(select min(m.timestamp_ms) from messages m "
                   "join message_sources s on s.message_id=m.id where s.backup_id=?),"
                   "last_message_ms=(select max(m.timestamp_ms) from messages m "
                   "join message_sources s on s.message_id=m.id where s.backup_id=?) where id=?",
                   (backup_id, backup_id, backup_id))
        # Messages may be shared by Android, Mac, and BlueBubbles sources.
        # Recalculate the materialized provenance count instead of assuming
        # that only an XML overlap can increment it.
        db.execute(
            "update messages set source_occurrences=(select count(*) from "
            "message_sources s where s.message_id=messages.id) where id in "
            "(select message_id from message_sources where backup_id=?)",
            (backup_id,)
        )
        db.commit()
    print(json.dumps(stats, indent=2))

if __name__ == "__main__":
    main()
