#!/usr/bin/env python3
"""Import a BlueBubbles/OpenBubbles export into the TextView archive.

Container format: [4-byte big-endian JSON length][JSON][ (4-byte BE length)(bytes) ... ]
Blob N corresponds to the attachment whose bytes_id == N.

Messages already present (same guid, hence same dedup_key) are not duplicated;
their attachments are backfilled if the archive lacks the bytes.
"""
from __future__ import annotations
import argparse, hashlib, json, os, re, sqlite3, struct, sys, datetime

from imessage_semantics import (
    associated_type, clean_text, effective_direction, is_reaction_add,
    reaction_label, semantic_presentation,
)
from sticker_semantics import (
    ensure_sticker_schema, insert_sticker_event, is_animated_sticker,
    message_dedup, sticker_kind, target_guid as bare_target_guid,
)
from apple_message_features import (
    decode_app_payload, decode_bluebubbles_edit_history, ensure_feature_schema,
)

def as_int(v, default=0):
    try:
        if v is None or v == "": return default
        return int(v)
    except (TypeError, ValueError):
        return default


def canonical_address(address):
    if address is None: return "__self__", None, 1
    a = str(address).strip()
    if not a or a.lower() in {"insert-address-token","self","__self__"}: return "__self__", None, 1
    digits = re.sub(r"[^0-9+]", "", a).lstrip("+")
    if digits.isdigit() and len(digits) >= 7:
        if len(digits) == 11 and digits.startswith("1"): digits = digits[1:]
        return "tel:" + digits, a, 0
    return "addr:" + a.casefold().strip(), a, 0


def finalize_export_status(status, message, body, attachments):
    edit_history = decode_bluebubbles_edit_history(message.get("messageSummaryInfo"))
    if status is None and message.get("dateEdited") and (body or attachments or edit_history):
        status = "edited"
    if status is None and body is None and not attachments:
        status = "hidden"
    return status

def load_container(path):
    f = open(path, "rb")
    n = struct.unpack(">I", f.read(4))[0]
    doc = json.loads(f.read(n).decode("utf-8"))
    blobs = []
    pos = 4 + n
    total = os.path.getsize(path)
    f.seek(pos)
    while pos < total:
        h = f.read(4)
        if len(h) < 4: break
        ln = struct.unpack(">I", h)[0]
        blobs.append((pos + 4, ln))
        pos += 4 + ln
        f.seek(pos)
    return f, doc, blobs

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", required=True)
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--source-label", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--repair-existing", action="store_true")
    a = ap.parse_args()

    fh, doc, blobs = load_container(a.export)
    msgs, chats, atts = doc["messages"], doc["chats"], doc["atts"]
    print(f"container: {len(chats)} chats, {len(msgs):,} messages, {len(atts)} atts, {len(blobs)} blobs")

    db = sqlite3.connect(a.output)
    db.execute("PRAGMA journal_mode=WAL")
    sql = db.execute("select sql from sqlite_master where name='messages'").fetchone()[0]
    if "'imessage'" not in sql:
        sys.exit("run import_imessage.py first (it widens the transport CHECK constraint)")
    ensure_sticker_schema(db)
    ensure_feature_schema(db)

    label = a.source_label or os.path.basename(a.export)
    st = os.stat(a.export)
    h = hashlib.sha256()
    with open(a.export, "rb") as g:
        for c in iter(lambda: g.read(8*1024*1024), b""): h.update(c)
    row = db.execute("select id from backups where filename=?", (label,)).fetchone()
    if row: backup_id = row[0]
    else:
        backup_id = db.execute(
            "insert into backups(filename,file_size,file_sha256,backup_set,backup_date_ms,declared_count,"
            "backup_type,first_message_ms,last_message_ms,imported_at,root_metadata_json) values(?,?,?,?,?,?,?,?,?,?,?)",
            (label, st.st_size, h.hexdigest(), None, int(st.st_mtime*1000), len(msgs), "bluebubbles-export",
             None, None, datetime.datetime.now(datetime.UTC).isoformat(),
             json.dumps({"source":"BlueBubbles/OpenBubbles export","device":"Pixel 7"}))).lastrowid

    handles, chat_meta = {}, {}
    for c in chats:
        addrs = []
        for p in (c.get("participants") or []):
            if p.get("ROWID") is not None and p.get("address"):
                handles[p["ROWID"]] = p["address"]
            if p.get("address"): addrs.append(p["address"])
        chat_meta[c.get("guid")] = (c.get("style"), c.get("displayName"), addrs)

    att_by_msg = {}
    for at in atts:
        g = at.get("guid") or ""
        att_by_msg.setdefault(re.sub(r"_\d+$", "", g), []).append(at)

    pcache = {}
    def participant(addr, name=None):
        canon, disp, is_self = canonical_address(addr)
        if canon in pcache: return pcache[canon]
        r = db.execute("select id from participants where canonical_address=?", (canon,)).fetchone()
        pid = r[0] if r else db.execute(
            "insert into participants(canonical_address,display_address,is_self) values(?,?,?)",
            (canon, disp, is_self)).lastrowid
        pcache[canon] = pid
        return pid

    def store_blob(bid):
        if bid is None or bid < 0 or bid >= len(blobs): return None
        off, ln = blobs[bid]
        fh.seek(off); data = fh.read(ln)
        if not data: return None
        if len(data) == 0: return None
        sha = hashlib.sha256(data).hexdigest()
        r = db.execute(
            "select id from media where encoded_sha256=? or content_sha256=? "
            "order by id limit 1", (sha, sha)
        ).fetchone()
        if r: return r[0]
        return db.execute("insert into media(encoded_sha256,content_sha256,byte_size,data,"
                          "detected_content_type,validation_status) values(?,?,?,?,?,?)",
                          (sha, sha, len(data), data, None, "unannotated")).lastrowid

    stats = dict(new=0, existing=0, repaired=0, media_added=0, media_backfilled=0,
                 reactions=0, replies=0, hidden=0, announcements=0, no_blob=0)
    pending_reactions = []
    pending_replies = []
    for i, m in enumerate(msgs):
        guid = m.get("guid")
        if not guid: continue
        dedup = hashlib.sha256(("imessage:" + guid).encode()).hexdigest()
        ts = as_int(m.get("dateCreated"), 0)
        if not ts: continue
        existing = db.execute("select id from messages where dedup_key=?", (dedup,)).fetchone()
        cg = m.get("chat")
        style, dispname, addrs = chat_meta.get(cg, (None, None, []))
        is_group = 1 if (as_int(style, 0) == 43 or len(set(addrs)) > 1) else 0
        body = clean_text(m.get("text"))
        amt = associated_type(m.get("associatedMessageType"))
        bundle = str(m.get("balloonBundleId") or "").casefold()
        blue_attachments = att_by_msg.get(guid, [])
        has_sticker_attachment = bool(blue_attachments) and (
            "sticker" in bundle or "animoji" in bundle or
            any("sticker" in str(at.get("transferName") or "").casefold()
                for at in blue_attachments)
        )
        direction = effective_direction(
            is_from_me=m.get("isFromMe"), item_type=m.get("itemType"),
            other_handle=m.get("otherHandle"), share_direction=m.get("shareDirection"))
        body, status = semantic_presentation(
            body=body, item_type=m.get("itemType"),
            group_action_type=m.get("groupActionType"), group_title=m.get("groupTitle"),
            share_status=m.get("shareStatus"), direction=direction,
            balloon_bundle_id=None if has_sticker_attachment and amt == 0
                else m.get("balloonBundleId"),
            associated_message_type=amt,
        )
        status = finalize_export_status(status, m, body, blue_attachments)
        if status == "hidden": stats["hidden"] += 1
        if status == "announcement": stats["announcements"] += 1
        sender = handles.get(m.get("handleId"))

        if is_reaction_add(amt):
            pending_reactions.append((dedup, m, amt, sender))
        if m.get("threadOriginatorGuid"):
            pending_replies.append((dedup, m.get("threadOriginatorGuid"),
                                    m.get("threadOriginatorPart")))

        if existing:
            mid = existing[0]; stats["existing"] += 1
            backfill = True
            has_mac_source = db.execute(
                "select 1 from message_sources s join backups b on b.id=s.backup_id "
                "where s.message_id=? and b.backup_type='imessage-chatdb' limit 1", (mid,)).fetchone()
            if a.repair_existing and not a.dry_run and not has_mac_source:
                db.execute("update messages set direction=?,is_group=?,subject=?,body=?,read=?,status=? "
                           "where id=?", (direction, is_group, m.get("subject"), body,
                                          1 if m.get("dateRead") else 0, status, mid))
                stats["repaired"] += 1
        else:
            if a.dry_run: stats["new"] += 1; continue
            mid = db.execute(
                "insert into messages(dedup_key,timestamp_ms,sent_timestamp_ms,direction,transport,"
                "is_group,subject,body,read,status,source_occurrences) values(?,?,?,?,?,?,?,?,?,?,1)",
                (dedup, ts, as_int(m.get("dateDelivered"), None) or None, direction, "imessage", is_group,
                 m.get("subject"), body, 1 if m.get("dateRead") else 0, status)).lastrowid
            stats["new"] += 1
            backfill = False
            db.execute("insert or ignore into message_participants(message_id,participant_id,role,"
                       "source_address_type) values(?,?,?,?)",
                       (mid, participant(None if direction=="outgoing" else sender), "from", 137))
            if direction == "outgoing":
                for o in dict.fromkeys(addrs):
                    db.execute("insert or ignore into message_participants(message_id,participant_id,"
                               "role,source_address_type) values(?,?,?,?)", (mid, participant(o), "to", 151))
            else:
                db.execute("insert or ignore into message_participants(message_id,participant_id,role,"
                           "source_address_type) values(?,?,?,?)", (mid, participant(None), "to", 151))
            if body:
                db.execute("insert or ignore into message_parts(message_id,sequence,content_type,filename,"
                           "content_location,content_id,charset,text_content,media_id,metadata_json)"
                           " values(?,0,?,?,?,?,?,?,?,?)",
                           (mid, "text/plain", None, None, None, "utf-8", body, None, json.dumps({})))
        ordinal = m.get("ROWID") or i
        metadata = json.dumps({"guid":guid,"chat":cg,"chat_display_name":dispname,
                               "isFromMe":m.get("isFromMe"),"itemType":m.get("itemType"),
                               "groupActionType":m.get("groupActionType"),
                               "associatedMessageType":amt,
                               "associatedMessageGuid":m.get("associatedMessageGuid"),
                               "associatedMessageEmoji":m.get("associatedMessageEmoji"),
                               "balloonBundleId":m.get("balloonBundleId"),
                               "threadOriginatorGuid":m.get("threadOriginatorGuid"),
                               "threadOriginatorPart":m.get("threadOriginatorPart"),
                               "dateEdited":m.get("dateEdited"),"source":"bluebubbles-export"})
        db.execute("insert into message_sources(message_id,backup_id,source_ordinal,source_kind,"
                   "source_android_id,metadata_json) values(?,?,?,?,?,?) on conflict(backup_id,source_ordinal) "
                   "do update set message_id=excluded.message_id,source_android_id=excluded.source_android_id,"
                   "metadata_json=excluded.metadata_json",
                   (mid, backup_id, ordinal, "imessage", guid, metadata))

        for at in att_by_msg.get(guid, []):
            tn = at.get("transferName")
            have = db.execute("select id, media_id from message_parts where message_id=? and "
                              "ifnull(filename,'')=ifnull(?,'')", (mid, tn)).fetchone()
            if have and have[1] is not None:
                continue
            if a.dry_run: continue
            bid = at.get("bytes_id")
            bid = as_int(bid, -1) if bid is not None else -1
            mediaid = store_blob(bid) if bid >= 0 else None
            if mediaid is None: stats["no_blob"] += 1
            if have and have[1] is None and mediaid is not None:
                db.execute("update message_parts set media_id=? where id=?", (mediaid, have[0]))
                stats["media_backfilled"] += 1
                continue
            if have:
                continue
            seqrow = db.execute("select coalesce(max(sequence),-1)+1 from message_parts where message_id=?", (mid,)).fetchone()
            db.execute("insert into message_parts(message_id,sequence,content_type,filename,"
                       "content_location,content_id,charset,text_content,media_id,metadata_json)"
                       " values(?,?,?,?,?,?,?,?,?,?)",
                       (mid, seqrow[0], at.get("mimeType"), tn, None, None, None, None, mediaid,
                        json.dumps({"uti":at.get("uti"),"totalBytes":at.get("totalBytes"),
                                    "source":"bluebubbles-export"})))
            if mediaid: stats["media_backfilled" if backfill else "media_added"] += 1
        if (i+1) % 10000 == 0:
            db.commit(); print(f"  ... {i+1:,}/{len(msgs):,}", flush=True)

    if not a.dry_run:
        if a.repair_existing:
            # Do not erase the richer Mac interpretation of a reaction message
            # represented by both sources. Export-only reactions are rebuilt.
            db.execute(
                "delete from reaction_events where reaction_message_id in "
                "(select s.message_id from message_sources s where s.backup_id=? "
                "and not exists (select 1 from message_sources other "
                "where other.message_id=s.message_id and other.backup_id<>?))",
                (backup_id, backup_id),
            )
        for dedup, m, amt, sender in pending_reactions:
            rr = db.execute("select id from messages where dedup_key=?", (dedup,)).fetchone()
            if not rr: continue
            mid = rr[0]
            tgt = m.get("associatedMessageGuid") or ""
            tg = tgt.split("/")[-1] if "/" in tgt else tgt.replace("bp:","").replace("p:","")
            tmid = None
            if tg:
                rr = db.execute("select id from messages where dedup_key=?",
                                (hashlib.sha256(("imessage:"+tg).encode()).hexdigest(),)).fetchone()
                tmid = rr[0] if rr else None
            db.execute("insert or ignore into reaction_events(reaction_message_id,target_message_id,"
                       "reactor_participant_id,reaction,target_excerpt,target_kind,resolution_method,"
                       "resolution_confidence) values(?,?,?,?,?,?,?,?)",
                       (mid, tmid, participant(None if m.get("isFromMe") else sender),
                        reaction_label(amt, m.get("associatedMessageEmoji")) or f"type{amt}", None, "text",
                        "bluebubbles-associated-guid", "exact" if tmid else "unresolved"))
            stats["reactions"] += 1
        for dedup, target_guid, target_part in pending_replies:
            rr = db.execute("select id from messages where dedup_key=?", (dedup,)).fetchone()
            if not rr: continue
            target_guid = (target_guid or "").split("/")[-1].replace("bp:", "").replace("p:", "")
            target = db.execute("select id from messages where dedup_key=?",
                                (hashlib.sha256(("imessage:" + target_guid).encode()).hexdigest(),)).fetchone()
            db.execute("insert into message_replies(message_id,target_message_id,target_guid,target_part,"
                       "resolution_confidence) values(?,?,?,?,?) on conflict(message_id) do update set "
                       "target_message_id=coalesce(message_replies.target_message_id,excluded.target_message_id),"
                       "target_guid=coalesce(message_replies.target_guid,excluded.target_guid),"
                       "target_part=coalesce(message_replies.target_part,excluded.target_part),"
                       "resolution_confidence=case when message_replies.resolution_confidence='exact' "
                       "then 'exact' else excluded.resolution_confidence end",
                       (rr[0], target[0] if target else None, target_guid, target_part,
                        "exact" if target else "unresolved"))
            stats["replies"] += 1

        # BlueBubbles exports do not carry Apple's binary placement plist, but
        # they do identify sticker/stickerback messages and preserve the image.
        # Import those semantics for export-only messages; overlapping Mac
        # records remain authoritative when their richer metadata is present.
        if a.repair_existing:
            db.execute(
                "DELETE FROM sticker_events WHERE source_kind='bluebubbles' "
                "AND sticker_message_id IN "
                "(SELECT message_id FROM message_sources WHERE backup_id=?)",
                (backup_id,),
            )
        sticker_count = 0
        for m in msgs:
            guid = m.get("guid")
            if not guid:
                continue
            amt = associated_type(m.get("associatedMessageType"))
            bundle = str(m.get("balloonBundleId") or "").casefold()
            attachments = att_by_msg.get(guid, [])
            inferred_standalone = bool(attachments) and (
                "sticker" in bundle or "animoji" in bundle or
                any("sticker" in str(at.get("transferName") or "").casefold()
                    for at in attachments)
            )
            kind = sticker_kind(amt, inferred_standalone)
            if not kind:
                continue
            mapped = db.execute(
                "select id from messages where dedup_key=?", (message_dedup(guid),)
            ).fetchone()
            if not mapped:
                continue
            target = bare_target_guid(m.get("associatedMessageGuid"))
            target_row = db.execute(
                "select id from messages where dedup_key=?", (message_dedup(target),)
            ).fetchone() if target else None
            for at in attachments:
                name = at.get("transferName")
                part = db.execute(
                    """select id,media_id,content_type,filename from message_parts
                       where message_id=? and ifnull(filename,'')=ifnull(?,'')
                       order by media_id is not null desc,id limit 1""",
                    (mapped[0], name),
                ).fetchone()
                if not part or part[1] is None:
                    continue
                source_attachment_id = str(
                    at.get("guid") or at.get("ROWID") or at.get("originalROWID") or name
                )
                insert_sticker_event(db, {
                    "sticker_message_id": mapped[0],
                    "target_message_id": target_row[0] if target_row else None,
                    "part_id": part[0], "media_id": part[1],
                    "source_attachment_id": f"blue:{source_attachment_id}",
                    "source_kind": "bluebubbles", "kind": kind,
                    "target_guid": target,
                    "target_part": m.get("associatedMessageRangeLocation"),
                    "source_app": m.get("balloonBundleId"),
                    "short_description": m.get("associatedMessageEmoji"),
                    "is_animated": is_animated_sticker(part[2], part[3]),
                    "metadata_json": {
                        "uti": at.get("uti"), "width": at.get("width"),
                        "height": at.get("height"), "total_bytes": at.get("totalBytes"),
                    },
                })
                sticker_count += 1
        stats["stickers"] = sticker_count

        feature_stats = {"edits": 0, "effects": 0, "app_messages": 0, "shareplay": 0}
        for m in msgs:
            guid = m.get("guid")
            if not guid:
                continue
            mapped = db.execute(
                "select id from messages where dedup_key=?", (message_dedup(guid),)
            ).fetchone()
            if not mapped:
                continue
            message_id = mapped[0]
            history = decode_bluebubbles_edit_history(m.get("messageSummaryInfo"))
            if history and not db.execute(
                "select 1 from message_edits where message_id=? limit 1", (message_id,)
            ).fetchone():
                db.executemany(
                    "insert into message_edits(message_id,part_index,edit_order,timestamp_ms,body) "
                    "values(?,?,?,?,?)",
                    [(message_id, item["part_index"], item["edit_order"],
                      item["timestamp_ms"], item["body"]) for item in history],
                )
                feature_stats["edits"] += 1
            effect = m.get("expressiveSendStyleId")
            if effect:
                db.execute(
                    "insert into message_effects(message_id,effect_id) values(?,?) "
                    "on conflict(message_id) do update set effect_id=excluded.effect_id",
                    (message_id, effect),
                )
                feature_stats["effects"] += 1
            bundle = m.get("balloonBundleId")
            if bundle:
                card = decode_app_payload(bundle, m.get("payloadData"), m.get("text"))
                db.execute(
                    """insert into app_messages(message_id,bundle_id,app_name,title,subtitle,url,metadata_json)
                       values(?,?,?,?,?,?,?) on conflict(message_id) do update set
                       bundle_id=excluded.bundle_id,app_name=excluded.app_name,
                       title=coalesce(excluded.title,app_messages.title),
                       subtitle=coalesce(excluded.subtitle,app_messages.subtitle),
                       url=coalesce(excluded.url,app_messages.url),
                       metadata_json=excluded.metadata_json""",
                    (message_id, card["bundle_id"], card["app_name"], card["title"],
                     card["subtitle"], card["url"], card["metadata_json"]),
                )
                feature_stats["app_messages"] += 1
            if as_int(m.get("itemType"), 0) == 5:
                db.execute(
                    "update messages set body='SharePlay ended',status='announcement' where id=?",
                    (message_id,),
                )
                feature_stats["shareplay"] += 1
        stats.update(feature_stats)
        db.execute("update backups set first_message_ms=(select min(m.timestamp_ms) from messages m join "
                   "message_sources s on s.message_id=m.id where s.backup_id=?), last_message_ms=(select "
                   "max(m.timestamp_ms) from messages m join message_sources s on s.message_id=m.id where "
                   "s.backup_id=?) where id=?", (backup_id, backup_id, backup_id))
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
