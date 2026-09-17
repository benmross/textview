#!/usr/bin/env python3
"""Normalize Google Messages proto metadata, captions, and reaction events."""

from __future__ import annotations

import argparse
import base64
import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from pathlib import Path

from local_overrides import DEDUP_DECISIONS, OUT_OF_ORDER_REACTIONS

PROTO_SCHEMA = """
CREATE TABLE IF NOT EXISTS message_protocol_metadata (
  id INTEGER PRIMARY KEY,
  message_source_id INTEGER NOT NULL REFERENCES message_sources(id) ON DELETE CASCADE,
  source_attribute TEXT NOT NULL,
  package_name TEXT,
  message_uuid TEXT,
  conversation_uuid TEXT,
  remote_message_id TEXT,
  transaction_id TEXT,
  group_id TEXT,
  group_title TEXT,
  group_uri TEXT,
  embedded_caption TEXT,
  decoded_json TEXT NOT NULL,
  UNIQUE(message_source_id, source_attribute)
);
CREATE INDEX IF NOT EXISTS protocol_message_uuid_idx
  ON message_protocol_metadata(message_uuid);
CREATE INDEX IF NOT EXISTS protocol_conversation_uuid_idx
  ON message_protocol_metadata(conversation_uuid);
CREATE INDEX IF NOT EXISTS protocol_remote_id_idx
  ON message_protocol_metadata(remote_message_id);

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
CREATE INDEX IF NOT EXISTS reactions_target_idx
  ON reaction_events(target_message_id);
CREATE INDEX IF NOT EXISTS reactions_reactor_idx
  ON reaction_events(reactor_participant_id);

CREATE TABLE IF NOT EXISTS dedup_decisions (
  id INTEGER PRIMARY KEY,
  decision_key TEXT NOT NULL UNIQUE,
  decision TEXT NOT NULL,
  rationale TEXT NOT NULL,
  decided_by TEXT NOT NULL,
  decided_at TEXT NOT NULL
);
"""

CLASSIC_REACTION = re.compile(
    r"^(Liked|Loved|Laughed at|Emphasized|Questioned|Disliked) “(.*)”$",
    re.DOTALL,
)
CLASSIC_MEDIA = re.compile(
    r"^(Liked|Loved|Laughed at|Emphasized|Questioned|Disliked) "
    r"(an image|a photo|a video|a movie|an audio message|an attachment)\s*$",
    re.IGNORECASE,
)
EMOJI_REACTION = re.compile(r"^Reacted (.+?) to “(.*)”$", re.DOTALL)
EMOJI_MEDIA_REACTION = re.compile(
    r"^Reacted (.+?) to (an image|a photo|a video|a movie|an audio message|an attachment)$",
    re.IGNORECASE,
)
PHOTOMOJI_REACTION = re.compile(
    r'^Reacted with a photomoji to ["“](.*)["”]$', re.DOTALL
)
PHOTOMOJI_STANDALONE = re.compile(r"^Reacted with a photomoji$")
DIRECT_EMOJI_REACTION = re.compile(r'^(.+?) to ["“](.*)["”]$', re.DOTALL)
INVISIBLE_REACTION_CHARS = str.maketrans("", "", "\u200a\u200b\ufeff")

# The archive owner confirmed these messages arrived out of order during poor
# service. Each target has the exact quoted body in the same conversation. The
# bodies are real messages, so the list is read from local_overrides.json.
OWNER_CONFIRMED_OUT_OF_ORDER_REACTIONS = OUT_OF_ORDER_REACTIONS


def read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
        shift += 7
        if shift > 70:
            break
    raise ValueError("invalid protobuf varint")


def parse_wire(data: bytes) -> list[tuple[int, int, int | bytes]]:
    fields = []
    offset = 0
    while offset < len(data):
        key, offset = read_varint(data, offset)
        number, wire = key >> 3, key & 7
        if number == 0:
            raise ValueError("invalid protobuf field zero")
        if wire == 0:
            value, offset = read_varint(data, offset)
        elif wire == 1:
            value = data[offset:offset + 8]
            offset += 8
        elif wire == 2:
            length, offset = read_varint(data, offset)
            value = data[offset:offset + length]
            offset += length
        elif wire == 5:
            value = data[offset:offset + 4]
            offset += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        if offset > len(data):
            raise ValueError("truncated protobuf field")
        fields.append((number, wire, value))
    return fields


def first_bytes(fields, number: int) -> bytes | None:
    return next(
        (value for field, wire, value in fields
         if field == number and wire == 2 and isinstance(value, bytes)),
        None,
    )


def text(value: bytes | None) -> str | None:
    if value is None:
        return None
    try:
        decoded = value.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return decoded if all(c.isprintable() or c in "\r\n\t" for c in decoded) else None


def uuid_hex(value: bytes | None) -> str | None:
    if value is None or len(value) != 16:
        return None
    h = value.hex()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def json_value(value: int | bytes):
    if isinstance(value, int):
        return value
    decoded = text(value)
    return {"text": decoded} if decoded is not None else {"hex": value.hex()}


def decode_proto(encoded: str) -> dict:
    raw = base64.b64decode(encoded[6:], validate=True)
    root = parse_wire(raw)
    app_bytes = first_bytes(root, 1)
    if app_bytes is None:
        raise ValueError("missing proto application envelope")
    app = parse_wire(app_bytes)
    package = text(first_bytes(app, 1))
    payload_bytes = first_bytes(app, 2)
    if payload_bytes is None:
        raise ValueError("missing proto payload")
    payload = parse_wire(payload_bytes)

    branch_number = next(
        (number for number in (2, 3) if first_bytes(payload, number) is not None),
        None,
    )
    branch = parse_wire(first_bytes(payload, branch_number)) if branch_number else []
    context = []
    for number in (2, 3):
        candidate = first_bytes(branch, number)
        if candidate:
            try:
                parsed = parse_wire(candidate)
            except ValueError:
                continue
            # Context messages carry identifiers/titles/URIs in fields 1..4.
            if parsed and all(field <= 4 for field, _, _ in parsed):
                context = parsed
                break

    result = {
        "package_name": package,
        "message_uuid": uuid_hex(first_bytes(payload, 5)),
        "conversation_uuid": uuid_hex(first_bytes(payload, 6)),
        "remote_message_id": text(first_bytes(branch, 5)),
        "transaction_id": text(first_bytes(branch, 1)),
        "group_id": text(first_bytes(context, 1)),
        "group_title": text(first_bytes(context, 2)),
        "group_uri": text(first_bytes(context, 4)),
        # In this corpus field 7 occurs only on bodyless media records and is
        # the caption displayed with that media.
        "embedded_caption": text(first_bytes(branch, 7)),
        "decoded_json": json.dumps({
            "root": [[n, w, json_value(v)] for n, w, v in root],
            "application": [[n, w, json_value(v)] for n, w, v in app],
            "payload": [[n, w, json_value(v)] for n, w, v in payload],
            "branch": [[n, w, json_value(v)] for n, w, v in branch],
            "context": [[n, w, json_value(v)] for n, w, v in context],
        }, separators=(",", ":"), ensure_ascii=False),
    }
    return result


def reaction(body: str | None):
    if not body:
        return None
    body = body.translate(INVISIBLE_REACTION_CHARS).strip()
    match = CLASSIC_REACTION.match(body)
    if match:
        return match.group(1), match.group(2), "text"
    match = CLASSIC_MEDIA.match(body)
    if match:
        kind = {
            "an image": "image", "a photo": "image", "a video": "video",
            "a movie": "video", "an audio message": "audio",
            "an attachment": "attachment",
        }[match.group(2).lower()]
        return match.group(1), None, kind
    match = PHOTOMOJI_REACTION.match(body)
    if match:
        return "photomoji", match.group(1), "text"
    if PHOTOMOJI_STANDALONE.match(body):
        return "photomoji", None, "attachment"
    match = EMOJI_REACTION.match(body)
    if match:
        return match.group(1), match.group(2), "text"
    match = EMOJI_MEDIA_REACTION.match(body)
    if match:
        kind = {
            "an image": "image", "a photo": "image", "a video": "video",
            "a movie": "video", "an audio message": "audio",
            "an attachment": "attachment",
        }[match.group(2).lower()]
        return match.group(1), None, kind
    match = DIRECT_EMOJI_REACTION.match(body)
    if match:
        label = match.group(1).replace("\ufe0f", "").strip()
        # "Edited to …" is a Google Messages edit fallback, not a reaction.
        # Direct reaction labels contain an emoji; reject other prose.
        if label != "Edited" and any(ord(character) > 127 for character in label):
            return label, match.group(2).strip(), "text"
    return None


def normalized_text(value: str | None) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = value.replace("\u200b", "").replace("\u200a", " ")
    value = value.replace("“", '"').replace("”", '"')
    value = value.replace("‘", "'").replace("’", "'")
    return " ".join(value.split()).strip().casefold()


def conversation_members(db: sqlite3.Connection) -> dict[int, tuple[int, ...]]:
    members: dict[int, list[int]] = defaultdict(list)
    for message_id, participant_id in db.execute(
        """SELECT DISTINCT mp.message_id,mp.participant_id
           FROM message_participants mp
           JOIN participants p ON p.id=mp.participant_id
           WHERE p.is_self=0 ORDER BY mp.message_id,mp.participant_id"""
    ):
        members[message_id].append(participant_id)
    return {message_id: tuple(ids) for message_id, ids in members.items()}


def populate(db: sqlite3.Connection) -> dict[str, int]:
    db.executescript(PROTO_SCHEMA)
    db.execute("DELETE FROM message_protocol_metadata")
    db.execute("DELETE FROM reaction_events")

    proto_count = captions = invalid = 0
    for row in db.execute(
        """SELECT id,message_id,metadata_json FROM message_sources
           WHERE metadata_json LIKE '%proto:%'"""
    ).fetchall():
        metadata = json.loads(row[2])
        for attribute in ("subject", "tr_id"):
            encoded = metadata.get(attribute)
            if not isinstance(encoded, str) or not encoded.startswith("proto:"):
                continue
            try:
                decoded = decode_proto(encoded)
            except (ValueError, IndexError):
                invalid += 1
                continue
            db.execute(
                """INSERT INTO message_protocol_metadata(
                  message_source_id,source_attribute,package_name,message_uuid,
                  conversation_uuid,remote_message_id,transaction_id,group_id,
                  group_title,group_uri,embedded_caption,decoded_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row[0], attribute, decoded["package_name"],
                 decoded["message_uuid"], decoded["conversation_uuid"],
                 decoded["remote_message_id"], decoded["transaction_id"],
                 decoded["group_id"], decoded["group_title"],
                 decoded["group_uri"], decoded["embedded_caption"],
                 decoded["decoded_json"])
            )
            proto_count += 1
            caption = decoded["embedded_caption"]
            if caption:
                current = db.execute(
                    "SELECT body FROM messages WHERE id=?", (row[1],)
                ).fetchone()[0]
                if current is None or current == "":
                    db.execute(
                        "UPDATE messages SET body=? WHERE id=?", (caption, row[1])
                    )
                    captions += 1

    members = conversation_members(db)
    messages = db.execute(
        """SELECT id,timestamp_ms,direction,body
           FROM messages ORDER BY timestamp_ms,id"""
    ).fetchall()
    by_conversation: dict[tuple[int, ...], list[sqlite3.Row]] = defaultdict(list)
    reaction_rows = []
    for message in messages:
        parsed = reaction(message[3])
        conversation = members.get(message[0], ())
        if parsed:
            label, excerpt, target_kind = parsed
            target_id = None
            method = None
            confidence = "unresolved"
            candidates = by_conversation[conversation]
            if excerpt is not None:
                exact = [m for m in reversed(candidates) if m[3] == excerpt]
                if exact:
                    target_id = exact[0][0]
                    method = "same-conversation exact body; nearest earlier"
                    confidence = "exact" if len(exact) == 1 else "high"
                else:
                    normalized_excerpt = normalized_text(excerpt)
                    normalized = [
                        m for m in reversed(candidates)
                        if normalized_text(m[3]) == normalized_excerpt
                    ]
                    if normalized:
                        target_id = normalized[0][0]
                        method = (
                            "same-conversation normalized body; nearest earlier"
                        )
                        confidence = (
                            "exact" if len(normalized) == 1 else "high"
                        )
                    elif normalized_excerpt.endswith("..."):
                        prefix = normalized_excerpt.rstrip(".").strip()
                        prefix_matches = [
                            m for m in reversed(candidates)
                            if len(prefix) >= 8
                            and normalized_text(m[3]).startswith(prefix)
                        ]
                        if prefix_matches:
                            target_id = prefix_matches[0][0]
                            method = (
                                "same-conversation truncated quoted prefix; "
                                "nearest earlier"
                            )
                            confidence = (
                                "exact" if len(prefix_matches) == 1 else "high"
                            )
            else:
                # Media reactions contain no target ID or caption in the
                # fallback row. Select only when one recent compatible media
                # message is unambiguous.
                compatible = []
                for candidate in reversed(candidates):
                    if message[1] - candidate[1] > 7 * 86_400_000:
                        break
                    types = {
                        r[0].split("/", 1)[0] for r in db.execute(
                            """SELECT content_type FROM message_parts
                               WHERE message_id=? AND media_id IS NOT NULL""",
                            (candidate[0],)
                        ) if r[0] and "/" in r[0]
                    }
                    wanted = target_kind if target_kind in {
                        "image", "video", "audio",
                    } else None
                    if types and (wanted is None or wanted in types):
                        compatible.append(candidate)
                        if len(compatible) > 1:
                            break
                if len(compatible) == 1:
                    target_id = compatible[0][0]
                    method = "only compatible media in prior seven days"
                    confidence = "high"

            sender = db.execute(
                """SELECT mp.participant_id FROM message_participants mp
                   JOIN participants p ON p.id=mp.participant_id
                   WHERE mp.message_id=? AND mp.role='from'
                   ORDER BY p.is_self LIMIT 1""", (message[0],)
            ).fetchone()
            reaction_rows.append((
                message[0], target_id, sender[0] if sender else None, label,
                excerpt, target_kind, method, confidence,
            ))
        by_conversation[conversation].append(message)

    db.executemany(
        """INSERT INTO reaction_events(
          reaction_message_id,target_message_id,reactor_participant_id,reaction,
          target_excerpt,target_kind,resolution_method,resolution_confidence
        ) VALUES(?,?,?,?,?,?,?,?)""", reaction_rows
    )
    for reaction_ms, reaction_body, target_ms, target_body in (
        OWNER_CONFIRMED_OUT_OF_ORDER_REACTIONS
    ):
        reactions = db.execute(
            """SELECT r.reaction_message_id
               FROM reaction_events r
               JOIN messages m ON m.id=r.reaction_message_id
               WHERE m.timestamp_ms=? AND m.body=?""",
            (reaction_ms, reaction_body)
        ).fetchall()
        targets = db.execute(
            "SELECT id FROM messages WHERE timestamp_ms=? AND body=?",
            (target_ms, target_body)
        ).fetchall()
        linked = 0
        for reaction_row in reactions:
            reaction_message_id = reaction_row[0]
            compatible = [
                row[0] for row in targets
                if members.get(reaction_message_id, ())
                    == members.get(row[0], ())
            ]
            if len(compatible) != 1:
                continue
            db.execute(
                """UPDATE reaction_events SET
                     target_message_id=?,
                     resolution_method='owner-confirmed out-of-order delivery',
                     resolution_confidence='exact'
                   WHERE reaction_message_id=?""",
                (compatible[0], reaction_message_id)
            )
            linked += 1
        if linked == 0:
            raise RuntimeError(
                f"out-of-order reaction override no longer resolves: "
                f"{reaction_ms} {reaction_body!r}"
            )

    # The owner's adjudications of specific restore rows (for example a set of
    # UNKNOWN_SENDER duplicates) are recorded from local_overrides.json.
    db.executemany(
        """INSERT OR REPLACE INTO dedup_decisions(
          decision_key,decision,rationale,decided_by,decided_at
        ) VALUES(?,?,?,?,?)""", DEDUP_DECISIONS
    )
    db.execute(
        """INSERT OR REPLACE INTO dedup_decisions(
          decision_key,decision,rationale,decided_by,decided_at
        ) VALUES(?,?,?,?,?)""",
        ("unresolved-reactions-accepted",
         "retain reviewed English-format unresolved reactions without links",
         "Owner reviewed the earlier unresolved English-format reaction list. "
         "This decision does not adjudicate subsequently discovered direct "
         "emoji-format reaction fallbacks.",
         "archive owner", "2026-07-27")
    )

    # Cross-backup merging may repoint provenance after the initial importer
    # calculated this materialized value. Normalize it before building FTS;
    # later source-specific importers repeat the calculation for their rows.
    db.execute(
        """UPDATE messages SET source_occurrences=(
             SELECT count(*) FROM message_sources s
             WHERE s.message_id=messages.id
           )"""
    )

    # Some carrier/provider exports retain bookkeeping rows with no text and
    # no usable part. Preserve their provenance, but never render an empty
    # message bubble in TextView.
    db.execute(
        """UPDATE messages SET status='hidden'
           WHERE trim(coalesce(body,''))=''
             AND trim(coalesce(subject,''))=''
             AND NOT EXISTS (
               SELECT 1 FROM message_parts p
               WHERE p.message_id=messages.id
                 AND (p.media_id IS NOT NULL
                      OR trim(coalesce(p.text_content,''))!=''
                      OR (trim(coalesce(p.filename,''))!=''
                          AND lower(coalesce(p.content_type,''))
                              NOT IN ('application/smil','text/smil')))
             )"""
    )

    db.execute("DELETE FROM message_search")
    db.execute(
        """INSERT INTO message_search(
          rowid,body,subject,participant_names,participant_addresses
        )
        SELECT m.id,coalesce(m.body,''),coalesce(m.subject,''),
          coalesce(group_concat(DISTINCT pn.name),''),
          coalesce(group_concat(DISTINCT p.display_address),'')
        FROM messages m
        LEFT JOIN message_participants mp ON mp.message_id=m.id
        LEFT JOIN participants p ON p.id=mp.participant_id
        LEFT JOIN participant_names pn ON pn.participant_id=p.id
        GROUP BY m.id"""
    )
    db.execute(
        "INSERT OR REPLACE INTO archive_info VALUES('protocol_schema_version','1')"
    )
    db.commit()
    resolved_counts = dict(db.execute(
        """SELECT resolution_confidence,count(*)
           FROM reaction_events GROUP BY resolution_confidence"""
    ))
    return {
        "proto_payloads": proto_count,
        "invalid_proto_payloads": invalid,
        "captions_applied": captions,
        "reaction_events": len(reaction_rows),
        "reaction_targets_resolved":
            resolved_counts.get("exact", 0) + resolved_counts.get("high", 0),
        "reaction_targets_exact": resolved_counts.get("exact", 0),
        "reaction_targets_high": resolved_counts.get("high", 0),
        "reaction_targets_unresolved": resolved_counts.get("unresolved", 0),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path, nargs="?", default=Path("messages.db"))
    args = parser.parse_args()
    db = sqlite3.connect(args.database)
    db.row_factory = sqlite3.Row
    try:
        stats = populate(db)
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = db.execute("PRAGMA foreign_key_check").fetchall()
        if integrity != "ok" or foreign_keys:
            raise RuntimeError(
                f"validation failed: integrity={integrity}, "
                f"foreign_keys={len(foreign_keys)}"
            )
        print(json.dumps(stats, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
