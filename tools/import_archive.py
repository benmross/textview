#!/usr/bin/env python3
"""Consolidate SMS Backup & Restore XML files into one archival SQLite database."""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import mmap
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

from local_overrides import OWNER_PHONE, SMS_ADDRESS_OVERRIDES

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE archive_info (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE backups (
  id INTEGER PRIMARY KEY,
  filename TEXT NOT NULL UNIQUE,
  file_size INTEGER NOT NULL,
  file_sha256 TEXT NOT NULL,
  backup_set TEXT,
  backup_date_ms INTEGER,
  declared_count INTEGER,
  backup_type TEXT,
  first_message_ms INTEGER,
  last_message_ms INTEGER,
  imported_at TEXT NOT NULL,
  root_metadata_json TEXT NOT NULL
);
CREATE TABLE participants (
  id INTEGER PRIMARY KEY,
  canonical_address TEXT NOT NULL UNIQUE,
  display_address TEXT,
  is_self INTEGER NOT NULL DEFAULT 0 CHECK(is_self IN (0,1))
);
CREATE TABLE participant_names (
  participant_id INTEGER NOT NULL REFERENCES participants(id),
  name TEXT NOT NULL,
  occurrence_count INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY(participant_id, name)
);
CREATE TABLE media (
  id INTEGER PRIMARY KEY,
  encoded_sha256 TEXT NOT NULL UNIQUE,
  content_sha256 TEXT NOT NULL,
  byte_size INTEGER NOT NULL,
  data BLOB NOT NULL,
  detected_content_type TEXT,
  validation_status TEXT
);
CREATE UNIQUE INDEX media_content_sha_unique ON media(content_sha256);
CREATE INDEX media_metadata_idx
  ON media(id,content_sha256,detected_content_type,byte_size,validation_status);
CREATE TABLE messages (
  id INTEGER PRIMARY KEY,
  dedup_key TEXT NOT NULL UNIQUE,
  timestamp_ms INTEGER NOT NULL,
  sent_timestamp_ms INTEGER,
  direction TEXT NOT NULL,
  transport TEXT NOT NULL CHECK(transport IN ('sms','mms','rcs')),
  is_group INTEGER NOT NULL DEFAULT 0 CHECK(is_group IN (0,1)),
  subject TEXT,
  body TEXT,
  read INTEGER,
  status TEXT,
  source_occurrences INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX messages_time_idx ON messages(timestamp_ms);
CREATE INDEX messages_transport_idx ON messages(transport);
CREATE TABLE message_participants (
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  participant_id INTEGER NOT NULL REFERENCES participants(id),
  role TEXT NOT NULL,
  source_address_type INTEGER,
  PRIMARY KEY(message_id, participant_id, role)
);
CREATE INDEX message_participants_participant_idx
  ON message_participants(participant_id, message_id);
CREATE TABLE message_parts (
  id INTEGER PRIMARY KEY,
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  sequence INTEGER NOT NULL,
  content_type TEXT,
  filename TEXT,
  content_location TEXT,
  content_id TEXT,
  charset TEXT,
  text_content TEXT,
  media_id INTEGER REFERENCES media(id),
  metadata_json TEXT NOT NULL,
  UNIQUE(message_id, sequence, content_type, filename, content_location, content_id)
);
CREATE INDEX message_parts_message_idx ON message_parts(message_id, sequence);
CREATE INDEX message_parts_media_idx ON message_parts(media_id);
CREATE TABLE message_sources (
  id INTEGER PRIMARY KEY,
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  backup_id INTEGER NOT NULL REFERENCES backups(id),
  source_ordinal INTEGER NOT NULL,
  source_kind TEXT NOT NULL CHECK(source_kind IN ('sms','mms')),
  source_android_id TEXT,
  metadata_json TEXT NOT NULL,
  UNIQUE(backup_id, source_ordinal)
);
CREATE INDEX message_sources_message_idx ON message_sources(message_id);
CREATE TABLE source_addresses (
  id INTEGER PRIMARY KEY,
  message_source_id INTEGER NOT NULL REFERENCES message_sources(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  participant_id INTEGER REFERENCES participants(id),
  role TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  UNIQUE(message_source_id, ordinal)
);
CREATE INDEX source_addresses_participant_idx ON source_addresses(participant_id);
CREATE TABLE source_parts (
  id INTEGER PRIMARY KEY,
  message_source_id INTEGER NOT NULL REFERENCES message_sources(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  media_id INTEGER REFERENCES media(id),
  metadata_json TEXT NOT NULL,
  UNIQUE(message_source_id, ordinal)
);
CREATE INDEX source_parts_media_idx ON source_parts(media_id);
CREATE VIRTUAL TABLE message_search USING fts5(
  body, subject, participant_names, participant_addresses,
  tokenize='unicode61 remove_diacritics 2'
);
CREATE TABLE import_stats (
  key TEXT PRIMARY KEY,
  value INTEGER NOT NULL
);
"""

NULLISH = {"", "null", "NULL"}
PHONE_RE = re.compile(r"[^0-9+]")
ROLE_MAP = {"137": "from", "151": "to", "130": "cc", "129": "bcc"}
# Owner-adjudicated restored SMS rows: one backup used UNKNOWN_SENDER while
# another retained the correct counterpart. Timestamp plus body identifies each
# exact case and avoids a broad address-blind SMS deduplication rule. The cases
# are real messages, so they are read from the untracked local_overrides.json.


def clean(value: str | None) -> str | None:
    return None if value is None or value in NULLISH else value


def as_int(value: str | None) -> int | None:
    try:
        return int(value) if clean(value) is not None else None
    except ValueError:
        return None


def xml_value(raw: bytes) -> str:
    return html.unescape(raw.decode("utf-8", "replace"))


def canonical_address(address: str | None) -> tuple[str, str | None, int]:
    address = clean(address)
    if address is None or address.lower() in {
        "insert-address-token", "self", "__self__",
    }:
        return "__self__", None, 1
    compact = PHONE_RE.sub("", address)
    digits = compact.lstrip("+")
    if digits.isdigit() and len(digits) >= 7:
        # North American +1 and bare ten-digit forms should identify one person.
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        return "tel:" + digits, address, 0
    return "addr:" + address.casefold().strip(), address, 0


def scan_attrs(mm: mmap.mmap, start: int, end: int) -> dict[str, str | tuple[int, int]]:
    """Parse one start tag. The base64 data value is returned as an mmap span."""
    out: dict[str, str | tuple[int, int]] = {}
    space = mm.find(b" ", start, end)
    if space < 0:
        return out
    i = space + 1
    while i < end:
        while i < end and mm[i] in b" \t\r\n/":
            i += 1
        k0 = i
        while i < end and mm[i] not in b"= \t\r\n>/":
            i += 1
        if i == k0:
            break
        key = mm[k0:i].decode("ascii", "replace")
        while i < end and mm[i] in b" \t\r\n":
            i += 1
        if i >= end or mm[i] != ord("="):
            continue
        i += 1
        while i < end and mm[i] in b" \t\r\n":
            i += 1
        if i >= end or mm[i] not in (ord('"'), ord("'")):
            continue
        quote = bytes((mm[i],))
        i += 1
        v0 = i
        i = mm.find(quote, i, end)
        if i < 0:
            raise ValueError(f"unterminated attribute {key}")
        out[key] = (v0, i) if key == "data" else xml_value(mm[v0:i])
        i += 1
    return out


def iter_tags(mm: mmap.mmap):
    pos = 0
    size = len(mm)
    while pos < size:
        a = mm.find(b"<", pos)
        if a < 0:
            return
        i = a + 1
        while True:
            candidates = [
                p for p in (
                    mm.find(b">", i),
                    mm.find(b'"', i),
                    mm.find(b"'", i),
                ) if p >= 0
            ]
            if not candidates:
                raise ValueError("unterminated XML tag")
            b = min(candidates)
            if mm[b] == ord(">"):
                break
            quote = bytes((mm[b],))
            i = mm.find(quote, b + 1)
            if i < 0:
                raise ValueError("unterminated quoted XML attribute")
            i += 1
        p = a + 1
        closing = mm[p] == ord("/")
        if closing:
            p += 1
        if mm[p:p+1] in (b"?", b"!"):
            pos = b + 1
            continue
        n = p
        while n < b and mm[n] not in b" \t\r\n/>":
            n += 1
        yield mm[p:n].decode("ascii", "replace"), closing, a, b
        pos = b + 1


class Importer:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.participant_cache: dict[str, int] = {}
        self.media_cache: dict[str, int] = {}
        self.stats = Counter()

    def participant(self, address: str | None, name: str | None = None) -> int:
        canonical, display, is_self = canonical_address(address)
        if canonical not in self.participant_cache:
            row = self.db.execute(
                "SELECT id FROM participants WHERE canonical_address=?", (canonical,)
            ).fetchone()
            if row:
                pid = row[0]
            else:
                pid = self.db.execute(
                    "INSERT INTO participants(canonical_address,display_address,is_self)"
                    " VALUES(?,?,?)", (canonical, display, is_self)
                ).lastrowid
            self.participant_cache[canonical] = pid
        pid = self.participant_cache[canonical]
        name = clean(name)
        if name:
            self.db.execute(
                "INSERT INTO participant_names(participant_id,name) VALUES(?,?)"
                " ON CONFLICT(participant_id,name) DO UPDATE SET"
                " occurrence_count=occurrence_count+1", (pid, name)
            )
        return pid

    def media(self, mm: mmap.mmap, span: tuple[int, int]) -> tuple[int, str]:
        start, end = span
        encoded = hashlib.sha256()
        for i in range(start, end, 8 * 1024 * 1024):
            encoded.update(mm[i:min(i + 8 * 1024 * 1024, end)])
        encoded_hash = encoded.hexdigest()
        if encoded_hash in self.media_cache:
            self.stats["media_reuses"] += 1
            media_id = self.media_cache[encoded_hash]
            content_hash = self.db.execute(
                "SELECT content_sha256 FROM media WHERE id=?", (media_id,)
            ).fetchone()[0]
            return media_id, content_hash
        row = self.db.execute(
            "SELECT id,content_sha256 FROM media WHERE encoded_sha256=?", (encoded_hash,)
        ).fetchone()
        if row:
            self.media_cache[encoded_hash] = row[0]
            self.stats["media_reuses"] += 1
            return row[0], row[1]

        encoded_len = end - start
        pad = (1 if encoded_len and mm[end - 1] == ord("=") else 0) + (
            1 if encoded_len > 1 and mm[end - 2] == ord("=") else 0
        )
        decoded_len = encoded_len * 3 // 4 - pad
        cursor = self.db.execute(
            "INSERT INTO media(encoded_sha256,content_sha256,byte_size,data)"
            " VALUES(?,?,?,zeroblob(?))", (encoded_hash, "pending", decoded_len, decoded_len)
        )
        media_id = cursor.lastrowid
        blob = self.db.blobopen("media", "data", media_id, readonly=False)
        digest = hashlib.sha256()
        carry = b""
        try:
            for i in range(start, end, 4 * 1024 * 1024):
                raw = carry + mm[i:min(i + 4 * 1024 * 1024, end)]
                usable = len(raw) - (len(raw) % 4)
                if usable:
                    decoded = base64.b64decode(raw[:usable], validate=False)
                    blob.write(decoded)
                    digest.update(decoded)
                carry = raw[usable:]
            if carry:
                decoded = base64.b64decode(carry, validate=False)
                blob.write(decoded)
                digest.update(decoded)
        finally:
            blob.close()
        content_hash = digest.hexdigest()
        try:
            self.db.execute(
                "UPDATE media SET content_sha256=? WHERE id=?", (content_hash, media_id)
            )
        except sqlite3.IntegrityError:
            # Different base64 serialization (line wrapping/padding) can encode
            # the same bytes. Keep one content-addressed BLOB and remember the
            # resolved ID for the rest of this import.
            existing = self.db.execute(
                "SELECT id FROM media WHERE content_sha256=?", (content_hash,)
            ).fetchone()
            if existing is None:
                raise
            self.db.execute("DELETE FROM media WHERE id=?", (media_id,))
            media_id = existing[0]
            self.media_cache[encoded_hash] = media_id
            self.stats["media_reuses"] += 1
            return media_id, content_hash
        self.media_cache[encoded_hash] = media_id
        self.stats["unique_media"] += 1
        self.stats["unique_media_bytes"] += decoded_len
        return media_id, content_hash

    def import_file(self, path: Path) -> None:
        started = time.monotonic()
        file_hash = hashlib.sha256()
        with path.open("rb") as fh:
            while chunk := fh.read(16 * 1024 * 1024):
                file_hash.update(chunk)
        with path.open("rb") as fh:
            mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
            try:
                root: dict[str, str] | None = None
                backup_id = None
                current: dict | None = None
                ordinal = 0
                first_ms = None
                last_ms = None
                for tag, closing, a, b in iter_tags(mm):
                    if closing:
                        if tag == "mms" and current is not None:
                            ordinal += 1
                            stamp = self.store_message(
                                backup_id, ordinal, current, mm
                            )
                            first_ms = stamp if first_ms is None else min(first_ms, stamp)
                            last_ms = stamp if last_ms is None else max(last_ms, stamp)
                            current = None
                            if ordinal % 10_000 == 0:
                                self.db.commit()
                                print(
                                    f"  {path.name}: {ordinal:,} records",
                                    file=sys.stderr, flush=True,
                                )
                        continue
                    if tag == "smses":
                        root = {k: v for k, v in scan_attrs(mm, a + 1, b).items()
                                if isinstance(v, str)}
                        backup_id = self.db.execute(
                            """INSERT INTO backups(
                              filename,file_size,file_sha256,backup_set,backup_date_ms,
                              declared_count,backup_type,imported_at,root_metadata_json
                            ) VALUES(?,?,?,?,?,?,?,?,?)""",
                            (path.name, path.stat().st_size, file_hash.hexdigest(),
                             clean(root.get("backup_set")), as_int(root.get("backup_date")),
                             as_int(root.get("count")), clean(root.get("type")),
                             time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                             json.dumps(root, separators=(",", ":"), ensure_ascii=False))
                        ).lastrowid
                    elif tag == "sms":
                        record = {"kind": "sms", "attrs": scan_attrs(mm, a + 1, b),
                                  "parts": [], "addrs": []}
                        ordinal += 1
                        stamp = self.store_message(backup_id, ordinal, record, mm)
                        first_ms = stamp if first_ms is None else min(first_ms, stamp)
                        last_ms = stamp if last_ms is None else max(last_ms, stamp)
                        if ordinal % 10_000 == 0:
                            self.db.commit()
                            print(f"  {path.name}: {ordinal:,} records",
                                  file=sys.stderr, flush=True)
                    elif tag == "mms":
                        current = {"kind": "mms", "attrs": scan_attrs(mm, a + 1, b),
                                   "parts": [], "addrs": []}
                    elif tag == "part" and current is not None:
                        current["parts"].append(scan_attrs(mm, a + 1, b))
                    elif tag == "addr" and current is not None:
                        current["addrs"].append(scan_attrs(mm, a + 1, b))
                if backup_id is None:
                    raise ValueError(f"{path}: no smses root")
                self.db.execute(
                    "UPDATE backups SET first_message_ms=?,last_message_ms=? WHERE id=?",
                    (first_ms, last_ms, backup_id)
                )
                self.db.commit()
                declared = as_int((root or {}).get("count"))
                if declared != ordinal:
                    raise ValueError(
                        f"{path}: root count {declared} != imported count {ordinal}"
                    )
                print(
                    f"  {path.name}: complete ({ordinal:,} records, "
                    f"{time.monotonic()-started:.1f}s)", file=sys.stderr, flush=True
                )
            finally:
                mm.close()

    def store_message(
        self, backup_id: int, ordinal: int, record: dict, mm: mmap.mmap
    ) -> int:
        a: dict[str, str] = record["attrs"]
        kind = record["kind"]
        stamp = as_int(a.get("date")) or 0
        sent_stamp = as_int(a.get("date_sent"))
        type_value = a.get("type") if kind == "sms" else a.get("msg_box")
        direction = {
            "1": "incoming", "2": "outgoing", "3": "draft", "4": "outbox",
            "5": "failed", "6": "queued",
        }.get(type_value, "unknown")
        is_rcs = (
            (kind == "sms" and (a.get("subject") or "").startswith("proto:"))
            or any(k.startswith("rcs_") for k in a)
            or any(any(k.startswith("rcs_") for k in p) for p in record["parts"])
        )
        transport = "rcs" if is_rcs else kind
        subject = clean(a.get("sub") if kind == "mms" else (
            None if is_rcs else a.get("subject")
        ))
        body = clean(a.get("body")) if kind == "sms" else None

        participant_specs: list[tuple[str, str | None, str | None, int | None]] = []
        if kind == "sms":
            counterpart = a.get("address")
            contact = clean(a.get("contact_name"))
            if direction == "incoming":
                participant_specs = [("from", counterpart, contact, None),
                                     ("to", None, None, None)]
            elif direction == "outgoing":
                participant_specs = [("from", None, None, None),
                                     ("to", counterpart, contact, None)]
            else:
                participant_specs = [("other", counterpart, contact, None)]
        else:
            for addr in record["addrs"]:
                av = addr.get("address")
                at = addr.get("type")
                participant_specs.append(
                    (ROLE_MAP.get(at, "other"), av, None, as_int(at))
                )
            if not participant_specs and clean(a.get("address")):
                role = "from" if direction == "incoming" else "to"
                participant_specs.append(
                    (role, a.get("address"), clean(a.get("contact_name")), None)
                )

        participant_rows = []
        counterpart_ids = []
        for role, address, name, addr_type in participant_specs:
            pid = self.participant(address, name)
            canonical = self.db.execute(
                "SELECT canonical_address FROM participants WHERE id=?", (pid,)
            ).fetchone()[0]
            participant_rows.append((pid, role, addr_type, canonical))
            if canonical != "__self__":
                counterpart_ids.append(canonical)
        unique_counterparts = set(counterpart_ids)
        is_group = len(unique_counterparts) > 1

        part_rows = []
        text_fragments = []
        part_signature = []
        for index, part in enumerate(record["parts"]):
            text = clean(part.get("text") if isinstance(part.get("text"), str) else None)
            ct = clean(part.get("ct") if isinstance(part.get("ct"), str) else None)
            if text and ct != "application/smil":
                text_fragments.append(text)
            media_id = None
            content_hash = None
            data = part.get("data")
            if isinstance(data, tuple) and data[1] > data[0]:
                # The literal "null" occasionally appears instead of base64.
                if data[1] - data[0] != 4 or mm[data[0]:data[1]] != b"null":
                    media_id, content_hash = self.media(mm, data)
            metadata = {
                k: v for k, v in part.items()
                if k != "data" and isinstance(v, str)
            }
            filename = clean(
                next((part.get(k) for k in ("name", "fn", "cl")
                      if isinstance(part.get(k), str) and clean(part.get(k))), None)
            )
            seq = as_int(part.get("seq") if isinstance(part.get("seq"), str) else None)
            seq = index if seq is None else seq
            part_rows.append((
                seq, ct, filename,
                clean(part.get("cl") if isinstance(part.get("cl"), str) else None),
                clean(part.get("cid") if isinstance(part.get("cid"), str) else None),
                clean(part.get("chset") if isinstance(part.get("chset"), str) else None),
                text, media_id,
                json.dumps(metadata, separators=(",", ":"), ensure_ascii=False),
            ))
            if ct != "application/smil":
                part_signature.append((seq, ct, filename, text, content_hash))
        if body is None and text_fragments:
            body = "\n".join(dict.fromkeys(text_fragments))

        transaction_id = clean(a.get("tr_id")) if kind == "mms" else None
        message_id = clean(a.get("m_id")) if kind == "mms" else None
        if kind == "sms":
            sms_participants = sorted(
                set((role, canonical)
                    for _, role, _, canonical in participant_rows)
            )
            override = SMS_ADDRESS_OVERRIDES.get((stamp, body))
            if override:
                sms_participants = [
                    (role, override if canonical != "__self__" else canonical)
                    for role, canonical in sms_participants
                ]
            signature = {
                "kind": "sms",
                "timestamp_ms": stamp,
                "direction": direction,
                "subject": subject,
                "body": body,
                "participants": sms_participants,
            }
        elif transaction_id:
            # Duplicate MMS provider rows frequently contain an empty shell and
            # a populated row sharing this transport identity.
            signature = {
                "kind": "mms-transaction",
                "timestamp_ms": stamp,
                "direction": direction,
                "transaction_id": transaction_id,
            }
        elif message_id:
            signature = {
                "kind": "mms-message-id",
                "timestamp_ms": stamp,
                "direction": direction,
                "message_id": message_id,
            }
        else:
            signature = {
                "kind": "mms-content",
                "timestamp_ms": stamp,
                "direction": direction,
                "participants": sorted(set((r, c) for _, r, _, c in participant_rows)),
                "subject": subject,
                "body": body,
                "parts": part_signature,
            }
        dedup_key = hashlib.sha256(
            json.dumps(signature, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False).encode()
        ).hexdigest()
        existing = self.db.execute(
            "SELECT id FROM messages WHERE dedup_key=?", (dedup_key,)
        ).fetchone()
        if existing:
            message_id = existing[0]
            self.db.execute(
                """UPDATE messages SET
                  source_occurrences=source_occurrences+1,
                  transport=CASE WHEN ?='rcs' THEN 'rcs' ELSE transport END,
                  body=CASE
                    WHEN body IS NULL OR body='' THEN ?
                    WHEN ? IS NOT NULL AND length(?)>length(body) THEN ?
                    ELSE body END,
                  subject=coalesce(subject,?)
                WHERE id=?""",
                (transport, body, body, body, body, subject, message_id)
            )
            for pid, role, addr_type, _ in set(participant_rows):
                self.db.execute(
                    "INSERT OR IGNORE INTO message_participants"
                    "(message_id,participant_id,role,source_address_type)"
                    " VALUES(?,?,?,?)", (message_id, pid, role, addr_type)
                )
            for p in part_rows:
                media_id = p[7]
                text_content = p[6]
                already = self.db.execute(
                    """SELECT 1 FROM message_parts
                       WHERE message_id=? AND (
                         (? IS NOT NULL AND media_id=?)
                         OR (? IS NOT NULL AND content_type=? AND text_content=?)
                       ) LIMIT 1""",
                    (message_id, media_id, media_id, text_content, p[1], text_content)
                ).fetchone()
                if not already:
                    self.db.execute(
                        """INSERT OR IGNORE INTO message_parts(
                          message_id,sequence,content_type,filename,content_location,
                          content_id,charset,text_content,media_id,metadata_json
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)""", (message_id, *p)
                    )
            self.stats["duplicate_messages"] += 1
        else:
            message_id = self.db.execute(
                """INSERT INTO messages(
                  dedup_key,timestamp_ms,sent_timestamp_ms,direction,transport,
                  is_group,subject,body,read,status
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (dedup_key, stamp, sent_stamp, direction, transport, int(is_group),
                 subject, body, as_int(a.get("read")), clean(a.get("status")))
            ).lastrowid
            for pid, role, addr_type, _ in set(participant_rows):
                self.db.execute(
                    "INSERT OR IGNORE INTO message_participants"
                    "(message_id,participant_id,role,source_address_type)"
                    " VALUES(?,?,?,?)", (message_id, pid, role, addr_type)
                )
            for p in part_rows:
                self.db.execute(
                    """INSERT OR IGNORE INTO message_parts(
                      message_id,sequence,content_type,filename,content_location,
                      content_id,charset,text_content,media_id,metadata_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)""", (message_id, *p)
                )
            self.stats["unique_messages"] += 1
            self.stats["transport_" + transport] += 1
            if is_group:
                self.stats["group_messages"] += 1
        source_metadata = {
            k: v for k, v in a.items() if isinstance(v, str)
        }
        source_id = self.db.execute(
            """INSERT INTO message_sources(
              message_id,backup_id,source_ordinal,source_kind,source_android_id,
              metadata_json
            ) VALUES(?,?,?,?,?,?)""",
            (message_id, backup_id, ordinal, kind, clean(a.get("_id")),
             json.dumps(source_metadata, separators=(",", ":"), ensure_ascii=False))
        ).lastrowid
        for index, addr in enumerate(record["addrs"]):
            role = ROLE_MAP.get(addr.get("type"), "other")
            pid = self.participant(addr.get("address"))
            self.db.execute(
                """INSERT INTO source_addresses(
                  message_source_id,ordinal,participant_id,role,metadata_json
                ) VALUES(?,?,?,?,?)""",
                (source_id, index, pid, role,
                 json.dumps(addr, separators=(",", ":"), ensure_ascii=False))
            )
        for index, (part, part_row) in enumerate(zip(record["parts"], part_rows)):
            metadata = {
                k: v for k, v in part.items()
                if k != "data" and isinstance(v, str)
            }
            self.db.execute(
                """INSERT INTO source_parts(
                  message_source_id,ordinal,media_id,metadata_json
                ) VALUES(?,?,?,?)""",
                (source_id, index, part_row[7],
                 json.dumps(metadata, separators=(",", ":"), ensure_ascii=False))
            )
        self.stats["source_messages"] += 1
        return stamp


def finalize(db: sqlite3.Connection, importer: Importer) -> None:
    db.execute(
        """DELETE FROM message_participants
           WHERE participant_id IN (
             SELECT id FROM participants
             WHERE canonical_address LIKE '%unknown_sender%'
           )
           AND EXISTS (
             SELECT 1
             FROM message_participants other
             JOIN participants p ON p.id=other.participant_id
             WHERE other.message_id=message_participants.message_id
               AND p.is_self=0
               AND p.canonical_address NOT LIKE '%unknown_sender%'
           )"""
    )
    # MMS address lists use the device's actual number rather than the synthetic
    # self token. A participant that is the From address on outgoing messages
    # and never the From address on incoming messages is an owner identity.
    db.execute(
        """UPDATE participants SET is_self=1 WHERE id IN (
          SELECT p.id
          FROM participants p
          JOIN message_participants mp ON mp.participant_id=p.id
          JOIN messages m ON m.id=mp.message_id
          GROUP BY p.id
          HAVING sum(m.direction='outgoing' AND mp.role='from') > 0
             AND sum(m.direction='incoming' AND mp.role='from') = 0
        )"""
    )
    db.execute(
        """UPDATE messages SET is_group=(
          SELECT count(DISTINCT CASE WHEN p.is_self=0 THEN p.id END) > 1
          FROM message_participants mp
          JOIN participants p ON p.id=mp.participant_id
          WHERE mp.message_id=messages.id
        )"""
    )
    # Recreate this derived index so finalization can safely be resumed.
    db.execute("DROP TABLE IF EXISTS message_search")
    db.execute(
        """CREATE VIRTUAL TABLE message_search USING fts5(
          body, subject, participant_names, participant_addresses,
          tokenize='unicode61 remove_diacritics 2'
        )"""
    )
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
    derived_stats = {
        "source_messages": "SELECT count(*) FROM message_sources",
        "unique_messages": "SELECT count(*) FROM messages",
        "duplicate_messages":
            "SELECT count(*)-(SELECT count(*) FROM messages) FROM message_sources",
        "unique_media": "SELECT count(*) FROM media",
        "unique_media_bytes": "SELECT coalesce(sum(byte_size),0) FROM media",
        "media_reuses":
            "SELECT count(*)-(SELECT count(*) FROM media) FROM source_parts WHERE media_id IS NOT NULL",
        "group_messages": "SELECT count(*) FROM messages WHERE is_group=1",
        "transport_sms": "SELECT count(*) FROM messages WHERE transport='sms'",
        "transport_mms": "SELECT count(*) FROM messages WHERE transport='mms'",
        "transport_rcs": "SELECT count(*) FROM messages WHERE transport='rcs'",
    }
    for key, sql in derived_stats.items():
        importer.stats[key] = db.execute(sql).fetchone()[0]
    for key, value in importer.stats.items():
        db.execute("INSERT OR REPLACE INTO import_stats(key,value) VALUES(?,?)",
                   (key, value))
    db.execute("INSERT OR REPLACE INTO archive_info VALUES('schema_version','1')")
    db.execute("INSERT OR REPLACE INTO archive_info VALUES('timestamps','Unix milliseconds UTC')")
    db.execute("ANALYZE")
    db.commit()
    result = db.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise RuntimeError(f"SQLite integrity check failed: {result}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("-o", "--output", type=Path, default=Path("messages.db"))
    parser.add_argument(
        "--owner-phone",
        default=OWNER_PHONE,
        help="Owner number stored in archive_info (prefer E.164, e.g. "
             "+15555550100). Defaults to owner_phone in local_overrides.json.",
    )
    args = parser.parse_args()
    for path in args.files:
        if not path.is_file():
            parser.error(f"not a file: {path}")
    temp = args.output.with_suffix(args.output.suffix + ".building")
    if temp.exists():
        temp.unlink()
    db = sqlite3.connect(temp)
    try:
        db.execute("PRAGMA page_size=32768")
        db.execute("PRAGMA journal_mode=OFF")
        db.execute("PRAGMA synchronous=OFF")
        db.execute("PRAGMA temp_store=MEMORY")
        db.executescript(SCHEMA)
        importer = Importer(db)
        importer.participant(None)
        for path in args.files:
            print(f"Importing {path}", file=sys.stderr, flush=True)
            importer.import_file(path)
        if args.owner_phone:
            db.execute(
                "INSERT OR REPLACE INTO archive_info(key,value)"
                " VALUES('owner_phone_e164',?)", (args.owner_phone,)
            )
            owner_canonical = canonical_address(args.owner_phone)[0]
            db.execute(
                "UPDATE participants SET is_self=1 WHERE canonical_address=?",
                (owner_canonical,)
            )
        print("Building search index and validating", file=sys.stderr, flush=True)
        finalize(db, importer)
    except Exception:
        db.close()
        raise
    db.close()
    os.replace(temp, args.output)
    print(f"Created {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
