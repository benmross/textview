#!/usr/bin/env python3
"""Normalized iMessage sticker semantics shared by all importers.

Apple stores sticker placement as a binary plist on the attachment and stores
the target message on the message row.  Keeping those facts in a normalized
table means the web application never needs to understand a source-specific
plist or regenerate a sticker while serving a request.
"""

from __future__ import annotations

import hashlib
import json
import plistlib


SCHEMA = """
CREATE TABLE IF NOT EXISTS sticker_events (
  id INTEGER PRIMARY KEY,
  sticker_message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  target_message_id INTEGER REFERENCES messages(id),
  part_id INTEGER REFERENCES message_parts(id) ON DELETE SET NULL,
  media_id INTEGER REFERENCES media(id),
  source_attachment_id TEXT NOT NULL,
  source_kind TEXT NOT NULL CHECK(source_kind IN ('mac','bluebubbles')),
  kind TEXT NOT NULL CHECK(kind IN ('standalone','placed','stickerback')),
  target_guid TEXT,
  target_part TEXT,
  position_x REAL,
  position_y REAL,
  scale REAL,
  rotation REAL,
  parent_width REAL,
  source_app TEXT,
  short_description TEXT,
  is_animated INTEGER NOT NULL DEFAULT 0 CHECK(is_animated IN (0,1)),
  metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(sticker_message_id, source_attachment_id),
  UNIQUE(sticker_message_id, media_id)
);
CREATE INDEX IF NOT EXISTS sticker_events_target_idx
  ON sticker_events(target_message_id);
CREATE INDEX IF NOT EXISTS sticker_events_message_idx
  ON sticker_events(sticker_message_id);
CREATE INDEX IF NOT EXISTS sticker_events_media_idx
  ON sticker_events(media_id);
"""


def ensure_sticker_schema(db) -> None:
    db.executescript(SCHEMA)


def target_guid(value) -> str | None:
    """Return the bare Apple message GUID from p:/bp:/part-qualified forms."""
    value = str(value or "").strip()
    if not value:
        return None
    value = value.split("/")[-1]
    while value.startswith(("p:", "bp:")):
        value = value.split(":", 1)[1]
    return value or None


def message_dedup(guid: str) -> str:
    return hashlib.sha256(("imessage:" + guid).encode()).hexdigest()


def safe_float(value):
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _json_value(value):
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def decode_sticker_info(blob) -> dict:
    """Decode Apple's binary/XML sticker plist without leaking opaque bytes."""
    if not blob:
        return {}
    try:
        value = plistlib.loads(bytes(blob))
    except (plistlib.InvalidFileException, TypeError, ValueError, OverflowError):
        return {}
    return _json_value(value) if isinstance(value, dict) else {}


def sticker_kind(associated_message_type, is_sticker=False):
    try:
        value = int(associated_message_type or 0)
    except (TypeError, ValueError):
        value = 0
    if value == 1000:
        return "placed"
    if value == 2007:
        return "stickerback"
    return "standalone" if is_sticker else None


def is_animated_sticker(content_type, filename) -> bool:
    content_type = str(content_type or "").lower()
    filename = str(filename or "").lower()
    return content_type == "image/heic-sequence" or filename.endswith(".heics")


def insert_sticker_event(db, event: dict) -> None:
    """Upsert an event, preferring richer Mac placement data over exports."""
    metadata = event.get("metadata_json", {})
    if not isinstance(metadata, str):
        metadata = json.dumps(metadata, separators=(",", ":"), sort_keys=True)
    values = (
        event["sticker_message_id"], event.get("target_message_id"), event.get("part_id"),
        event.get("media_id"), event["source_attachment_id"], event["source_kind"],
        event["kind"], event.get("target_guid"), event.get("target_part"),
        event.get("position_x"), event.get("position_y"), event.get("scale"),
        event.get("rotation"), event.get("parent_width"), event.get("source_app"),
        event.get("short_description"), 1 if event.get("is_animated") else 0, metadata,
    )
    existing = db.execute(
        """SELECT id,source_kind FROM sticker_events
           WHERE sticker_message_id=? AND
             (source_attachment_id=? OR (? IS NOT NULL AND media_id=?))
           ORDER BY source_kind='mac' DESC LIMIT 1""",
        (event["sticker_message_id"], event["source_attachment_id"],
         event.get("media_id"), event.get("media_id")),
    ).fetchone()
    if existing:
        # A Mac chat.db contains Apple's transform plist and is authoritative;
        # an overlapping BlueBubbles row only fills fields the Mac lacks.
        prefer_new = event["source_kind"] == "mac" or existing[1] != "mac"
        if prefer_new:
            db.execute(
                """UPDATE sticker_events SET
                     target_message_id=coalesce(?,target_message_id),part_id=coalesce(?,part_id),
                     media_id=coalesce(?,media_id),source_attachment_id=?,source_kind=?,kind=?,
                     target_guid=coalesce(?,target_guid),target_part=coalesce(?,target_part),
                     position_x=coalesce(?,position_x),position_y=coalesce(?,position_y),
                     scale=coalesce(?,scale),rotation=coalesce(?,rotation),
                     parent_width=coalesce(?,parent_width),source_app=coalesce(?,source_app),
                     short_description=coalesce(?,short_description),
                     is_animated=max(?,is_animated),metadata_json=? WHERE id=?""",
                (event.get("target_message_id"), event.get("part_id"), event.get("media_id"),
                 event["source_attachment_id"], event["source_kind"], event["kind"],
                 event.get("target_guid"), event.get("target_part"), event.get("position_x"),
                 event.get("position_y"), event.get("scale"), event.get("rotation"),
                 event.get("parent_width"), event.get("source_app"),
                 event.get("short_description"), 1 if event.get("is_animated") else 0,
                 metadata, existing[0]),
            )
        else:
            db.execute(
                """UPDATE sticker_events SET
                     target_message_id=coalesce(target_message_id,?),
                     part_id=coalesce(part_id,?),media_id=coalesce(media_id,?),
                     target_guid=coalesce(target_guid,?),target_part=coalesce(target_part,?),
                     source_app=coalesce(source_app,?),
                     short_description=coalesce(short_description,?),
                     is_animated=max(?,is_animated) WHERE id=?""",
                (event.get("target_message_id"), event.get("part_id"), event.get("media_id"),
                 event.get("target_guid"), event.get("target_part"), event.get("source_app"),
                 event.get("short_description"), 1 if event.get("is_animated") else 0,
                 existing[0]),
            )
        return
    db.execute(
        """INSERT INTO sticker_events(
             sticker_message_id,target_message_id,part_id,media_id,
             source_attachment_id,source_kind,kind,target_guid,target_part,
             position_x,position_y,scale,rotation,parent_width,source_app,
             short_description,is_animated,metadata_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        values,
    )
