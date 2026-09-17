#!/usr/bin/env python3
"""Build the small, disposable conversation index used by TextView."""

from __future__ import annotations

import os
import sqlite3
import hashlib
import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


SOURCE = Path(os.environ.get("TEXTVIEW_DATABASE", "messages.db"))
TARGET = Path(os.environ.get("TEXTVIEW_INDEX", "textview-index.db"))
TEMP = TARGET.with_suffix(TARGET.suffix + ".building")
VCARD = Path("contacts.vcf")
PHOTO_DIR = Path("data/contact-photos")


SCHEMA = """
PRAGMA page_size=16384;
PRAGMA journal_mode=OFF;
PRAGMA synchronous=OFF;
CREATE TABLE conversations (
  id INTEGER PRIMARY KEY,
  participant_key TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  subtitle TEXT,
  is_group INTEGER NOT NULL,
  message_count INTEGER NOT NULL DEFAULT 0,
  first_message_ms INTEGER,
  last_message_ms INTEGER,
  last_message_id INTEGER,
  last_body TEXT,
  last_direction TEXT,
  attachment_count INTEGER NOT NULL DEFAULT 0
  ,avatar_path TEXT
);
CREATE TABLE conversation_participants (
  conversation_id INTEGER NOT NULL REFERENCES conversations(id),
  participant_id INTEGER NOT NULL,
  display_name TEXT NOT NULL,
  display_address TEXT,
  avatar_path TEXT,
  PRIMARY KEY(conversation_id,participant_id)
);
CREATE TABLE conversation_messages (
  conversation_id INTEGER NOT NULL REFERENCES conversations(id),
  message_id INTEGER NOT NULL UNIQUE,
  timestamp_ms INTEGER NOT NULL,
  PRIMARY KEY(conversation_id,message_id)
);
CREATE INDEX conversation_messages_page_idx
  ON conversation_messages(conversation_id,timestamp_ms DESC,message_id DESC);
CREATE INDEX conversations_recent_idx
  ON conversations(last_message_ms DESC,id DESC);
CREATE TABLE attachments (
  part_id INTEGER PRIMARY KEY,
  message_id INTEGER NOT NULL,
  conversation_id INTEGER NOT NULL,
  media_id INTEGER NOT NULL,
  content_sha256 TEXT NOT NULL,
  content_type TEXT,
  detected_content_type TEXT,
  category TEXT NOT NULL,
  filename TEXT,
  byte_size INTEGER NOT NULL,
  validation_status TEXT,
  timestamp_ms INTEGER NOT NULL,
  direction TEXT NOT NULL,
  conversation_title TEXT NOT NULL,
  avatar_path TEXT,
  is_sticker INTEGER NOT NULL DEFAULT 0 CHECK(is_sticker IN (0,1))
);
CREATE INDEX attachments_recent_idx
  ON attachments(timestamp_ms DESC,part_id DESC);
CREATE INDEX attachments_media_idx ON attachments(media_id);
CREATE INDEX attachments_category_recent_idx
  ON attachments(category,timestamp_ms DESC,part_id DESC);
CREATE INDEX attachments_conversation_recent_idx
  ON attachments(conversation_id,timestamp_ms DESC,part_id DESC);
CREATE INDEX attachments_conversation_category_recent_idx
  ON attachments(conversation_id,category,timestamp_ms DESC,part_id DESC);
CREATE VIRTUAL TABLE conversation_search USING fts5(
  title,subtitle,content='conversations',content_rowid='id',
  tokenize='unicode61 remove_diacritics 2'
);
"""


def clean_name(name: str | None) -> str | None:
    if not name or name.strip().casefold() in {"(unknown)", "unknown", "null"}:
        return None
    return name.strip()


class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, value: str) -> str:
        self.parent.setdefault(value, value)
        if self.parent[value] != value:
            self.parent[value] = self.find(self.parent[value])
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


def apple_chat_identity(source: sqlite3.Connection):
    """Resolve stable group-chat IDs and bridge source-specific IDs by GUID."""
    union = UnionFind()
    refs_by_message: dict[int, list[str]] = {}
    names_by_ref: dict[str, list[str]] = {}
    for row in source.execute(
        """SELECT s.message_id,b.backup_type,s.metadata_json
           FROM message_sources s JOIN backups b ON b.id=s.backup_id
           JOIN messages m ON m.id=s.message_id
           WHERE m.transport='imessage' AND m.is_group=1"""
    ):
        try:
            meta = json.loads(row["metadata_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if row["backup_type"] == "imessage-chatdb":
            raw = meta.get("chat_guid") or meta.get("chat_id")
            prefix = "mac"
        elif row["backup_type"] == "bluebubbles-export":
            raw = meta.get("chat")
            prefix = "bluebubbles"
        else:
            continue
        if raw is None or raw == "":
            continue
        ref = f"{prefix}:{raw}"
        refs_by_message.setdefault(row["message_id"], []).append(ref)
        name = clean_name(meta.get("chat_display_name"))
        if name:
            names_by_ref.setdefault(ref, []).append(name)
    for refs in refs_by_message.values():
        for ref in refs[1:]:
            union.union(refs[0], ref)
    roots_by_message = {
        mid: union.find(refs[0]) for mid, refs in refs_by_message.items()
    }
    names_by_root: dict[str, list[str]] = {}
    for ref, names in names_by_ref.items():
        names_by_root.setdefault(union.find(ref), []).extend(names)
    return roots_by_message, names_by_root


def canonical_phone(value: str) -> str:
    digits = "".join(character for character in value if character.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return f"tel:{digits}"


def read_contacts() -> dict[str, tuple[str, str | None]]:
    if not VCARD.exists():
        return {}
    raw_lines = VCARD.read_text(errors="replace").replace("\r\n", "\n").splitlines()
    lines: list[str] = []
    for line in raw_lines:
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    cards: list[dict[str, list[str]]] = []
    card: dict[str, list[str]] | None = None
    for line in lines:
        if line == "BEGIN:VCARD":
            card = {}
        elif line == "END:VCARD" and card is not None:
            cards.append(card)
            card = None
        elif card is not None and ":" in line:
            key, value = line.split(":", 1)
            card.setdefault(key.split(";", 1)[0], []).append(value)
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    photos = {
        url: f"/contact-photos/{hashlib.sha256(url.encode()).hexdigest()[:24]}.jpg"
        for entry in cards for url in entry.get("PHOTO", []) if url.startswith("http")
    }

    def download(item: tuple[str, str]) -> None:
        url, public_path = item
        target = Path("data") / public_path.lstrip("/")
        if target.exists() and target.stat().st_size:
            return
        request = urllib.request.Request(url, headers={"User-Agent": "TextView/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                data = response.read()
            if data:
                target.write_bytes(data)
        except Exception as exc:
            print(f"warning: could not cache contact photo: {exc}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(download, photos.items()))

    result: dict[str, tuple[str, str | None]] = {}
    for entry in cards:
        name = (entry.get("FN") or ["Unknown"])[0].replace("\\,", ",").strip()
        photo_url = (entry.get("PHOTO") or [None])[0]
        photo = photos.get(photo_url) if photo_url else None
        if photo and not (Path("data") / photo.lstrip("/")).exists():
            photo = None
        for phone in entry.get("TEL", []):
            result[canonical_phone(phone)] = (name, photo)
    return result


def main() -> None:
    if TEMP.exists():
        TEMP.unlink()
    source = sqlite3.connect(f"file:{SOURCE}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    target = sqlite3.connect(TEMP)
    target.executescript(SCHEMA)
    contacts = read_contacts()
    chat_root_by_message, chat_names = apple_chat_identity(source)

    participants: dict[int, tuple[str, str | None]] = {}
    for row in source.execute(
        """SELECT p.id,p.canonical_address,p.display_address,
             (SELECT pn.name FROM participant_names pn
              WHERE pn.participant_id=p.id
              ORDER BY (lower(pn.name) IN ('(unknown)','unknown','null')) ASC,
                       pn.occurrence_count DESC,length(pn.name) DESC LIMIT 1) name
           FROM participants p WHERE p.is_self=0"""
    ):
        contact = contacts.get(row["canonical_address"])
        name = contact[0] if contact else clean_name(row["name"])
        address = row["display_address"] or ""
        participants[row["id"]] = (
            name or address or "Unknown", address or None, contact[1] if contact else None
        )

    conversation_by_key: dict[str, int] = {}
    conversation_people: dict[int, set[int]] = {}
    conversation_chat_root: dict[int, str] = {}
    next_conversation_id = 1
    batch: list[tuple[int, int, int]] = []
    cursor = source.execute(
        """SELECT m.id,m.timestamp_ms,m.direction,m.body,m.subject,m.is_group,
                  EXISTS(SELECT 1 FROM message_parts x
                         WHERE x.message_id=m.id AND x.media_id IS NOT NULL
                           AND lower(coalesce(x.filename,''))
                               NOT LIKE '%.pluginpayloadattachment') has_media,
                  group_concat(DISTINCT CASE WHEN p.is_self=0 THEN p.id END) participant_ids
           FROM messages m
           LEFT JOIN message_participants mp ON mp.message_id=m.id
           LEFT JOIN participants p ON p.id=mp.participant_id
           WHERE coalesce(m.status,'') != 'hidden'
             AND NOT EXISTS(SELECT 1 FROM reaction_events r
                            WHERE r.reaction_message_id=m.id)
           GROUP BY m.id ORDER BY m.id"""
    )
    for row in cursor:
        ids = sorted(
            int(value) for value in (row["participant_ids"] or "").split(",") if value
        )
        chat_root = chat_root_by_message.get(row["id"]) if row["is_group"] else None
        key = f"chat:{chat_root}" if chat_root else (
            "participants:" + (",".join(map(str, ids)) or "unknown"))
        conversation_id = conversation_by_key.get(key)
        if conversation_id is None:
            conversation_id = next_conversation_id
            next_conversation_id += 1
            conversation_by_key[key] = conversation_id
            conversation_people[conversation_id] = set()
            if chat_root:
                conversation_chat_root[conversation_id] = chat_root
            people = [participants.get(pid, ("Unknown", None, None)) for pid in ids]
            names = [person[0] for person in people]
            addresses = [person[1] for person in people if person[1]]
            avatar = people[0][2] if len(people) == 1 else None
            title = ", ".join(names[:4])
            if len(names) > 4:
                title += f" +{len(names) - 4}"
            subtitle = " · ".join(addresses[:3]) or None
            target.execute(
                """INSERT INTO conversations(
                     id,participant_key,title,subtitle,is_group
                     ,avatar_path
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    conversation_id, key, title or "Unknown", subtitle,
                    int(len(ids) > 1), avatar,
                ),
            )
        conversation_people[conversation_id].update(ids)
        batch.append((conversation_id, row["id"], row["timestamp_ms"]))
        body = row["body"] or row["subject"] or (
            "Attachment" if row["has_media"] else "Message"
        )
        # Messages are streamed in id order, which is not chronological once an
        # out-of-band source (iMessage, BlueBubbles) has been imported. Track the
        # true minimum/maximum instead of trusting arrival order.
        target.execute(
            """UPDATE conversations SET
                 message_count=message_count+1,
                 first_message_ms=min(coalesce(first_message_ms,?),?),
                 last_message_ms=max(coalesce(last_message_ms,?),?),
                 last_message_id=CASE WHEN ?>=coalesce(last_message_ms,-1)
                                      THEN ? ELSE last_message_id END,
                 last_body=CASE WHEN ?>=coalesce(last_message_ms,-1)
                                THEN ? ELSE last_body END,
                 last_direction=CASE WHEN ?>=coalesce(last_message_ms,-1)
                                     THEN ? ELSE last_direction END,
                 attachment_count=attachment_count+?
               WHERE id=?""",
            (
                row["timestamp_ms"], row["timestamp_ms"],
                row["timestamp_ms"], row["timestamp_ms"],
                row["timestamp_ms"], row["id"],
                row["timestamp_ms"], body[:500],
                row["timestamp_ms"], row["direction"],
                row["has_media"], conversation_id,
            ),
        )
        if len(batch) >= 10_000:
            target.executemany(
                "INSERT INTO conversation_messages VALUES(?,?,?)", batch
            )
            target.commit()
            batch.clear()
    if batch:
        target.executemany("INSERT INTO conversation_messages VALUES(?,?,?)", batch)
    # Membership can change during a group thread. Build its participant roster
    # from the full history, and prefer Apple's explicit group name when present.
    for conversation_id, ids_set in conversation_people.items():
        ids = sorted(ids_set)
        people = [participants.get(pid, ("Unknown", None, None)) for pid in ids]
        names = [person[0] for person in people]
        addresses = [person[1] for person in people if person[1]]
        root = conversation_chat_root.get(conversation_id)
        custom_names = chat_names.get(root, []) if root else []
        title = max(set(custom_names), key=custom_names.count) if custom_names else ", ".join(names[:4])
        if not custom_names and len(names) > 4:
            title += f" +{len(names) - 4}"
        avatar = people[0][2] if len(people) == 1 else None
        target.execute("UPDATE conversations SET title=?,subtitle=?,is_group=?,avatar_path=? WHERE id=?",
                       (title or "Unknown", " · ".join(addresses[:3]) or None,
                        int(bool(root) or len(ids) > 1), avatar, conversation_id))
        target.executemany(
            """INSERT INTO conversation_participants(
                 conversation_id,participant_id,display_name,display_address,avatar_path
               ) VALUES(?,?,?,?,?)""",
            [(conversation_id, pid, *participants.get(pid, ("Unknown", None, None))) for pid in ids],
        )
    target.execute(
        """INSERT INTO conversation_search(rowid,title,subtitle)
           SELECT id,title,coalesce(subtitle,'') FROM conversations"""
    )
    target.execute(
        f"ATTACH DATABASE 'file:{SOURCE.resolve()}?mode=ro' AS archive"
    )
    target.execute(
        """INSERT INTO attachments
           SELECT mp.id,mp.message_id,cm.conversation_id,mp.media_id,
                  md.content_sha256,mp.content_type,md.detected_content_type,
                  CASE
                    WHEN lower(coalesce(mp.content_type,md.detected_content_type,'')) LIKE 'image/%' THEN 'image'
                    WHEN lower(coalesce(mp.content_type,md.detected_content_type,'')) LIKE 'video/%' THEN 'video'
                    WHEN lower(coalesce(mp.content_type,md.detected_content_type,'')) LIKE 'audio/%' THEN 'audio'
                    ELSE 'file'
                  END,
                  mp.filename,md.byte_size,md.validation_status,m.timestamp_ms,
                  m.direction,c.title,c.avatar_path,
                  EXISTS(SELECT 1 FROM archive.sticker_events se
                         WHERE se.part_id=mp.id)
           FROM archive.message_parts mp
           JOIN archive.media AS md INDEXED BY media_metadata_idx ON md.id=mp.media_id
           JOIN archive.messages m ON m.id=mp.message_id
           JOIN conversation_messages cm ON cm.message_id=m.id
           JOIN conversations c ON c.id=cm.conversation_id
           WHERE mp.media_id IS NOT NULL
             AND lower(coalesce(mp.filename,''))
                 NOT LIKE '%.pluginpayloadattachment'
             AND (NOT EXISTS(
                    SELECT 1 FROM archive.sticker_events standalone
                    WHERE standalone.sticker_message_id=mp.message_id
                      AND standalone.kind='standalone'
                  ) OR EXISTS(
                    SELECT 1 FROM archive.sticker_events canonical
                    WHERE canonical.part_id=mp.id
                  ))"""
    )
    target.commit()
    target.execute("DETACH DATABASE archive")
    target.execute("PRAGMA optimize")
    target.commit()
    source_count = source.execute(
        """SELECT count(*) FROM messages m WHERE coalesce(m.status,'') != 'hidden'
           AND NOT EXISTS(SELECT 1 FROM reaction_events r
                          WHERE r.reaction_message_id=m.id)"""
    ).fetchone()[0]
    indexed_count = target.execute(
        "SELECT count(*) FROM conversation_messages"
    ).fetchone()[0]
    if source_count != indexed_count:
        raise RuntimeError(f"indexed {indexed_count} of {source_count} messages")
    integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(integrity)
    conversations = target.execute("SELECT count(*) FROM conversations").fetchone()[0]
    target.close()
    source.close()
    os.replace(TEMP, TARGET)
    print(f"Indexed {indexed_count:,} messages in {conversations:,} conversations.")


if __name__ == "__main__":
    main()
