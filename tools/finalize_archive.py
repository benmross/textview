#!/usr/bin/env python3
"""Validate TextView's database and write a reproducible JSON health report."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sqlite3
from pathlib import Path


TABLES = (
    "backups", "messages", "message_sources", "message_parts",
    "source_parts", "source_addresses", "media", "participants",
    "message_search", "message_protocol_metadata", "reaction_events",
    "message_replies", "sticker_events", "message_edits", "message_effects",
    "app_messages",
)


def scalar(db: sqlite3.Connection, sql: str, parameters=()):
    return db.execute(sql, parameters).fetchone()[0]


def grouped(db: sqlite3.Connection, sql: str) -> dict[str, int]:
    return {str(key): value for key, value in db.execute(sql)}


def hash_media(db: sqlite3.Connection) -> dict:
    """Stream every BLOB through SHA-256 without loading it into Python RAM."""
    mismatches = []
    checked_bytes = 0
    rows = db.execute(
        "SELECT id,content_sha256,byte_size FROM media ORDER BY id"
    ).fetchall()
    for media_id, expected, byte_size in rows:
        digest = hashlib.sha256()
        blob = db.blobopen("media", "data", media_id, readonly=True)
        try:
            while chunk := blob.read(8 * 1024 * 1024):
                digest.update(chunk)
        finally:
            blob.close()
        checked_bytes += byte_size
        actual = digest.hexdigest()
        if actual != expected:
            mismatches.append({
                "media_id": media_id,
                "expected": expected,
                "actual": actual,
            })
    return {
        "rows_checked": len(rows),
        "bytes_checked": checked_bytes,
        "hash_mismatches": mismatches,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path, nargs="?", default=Path("messages.db"))
    parser.add_argument("--output", type=Path, default=Path("validation.json"))
    parser.add_argument(
        "--deep", action="store_true",
        help="also recompute SHA-256 over every stored attachment byte",
    )
    args = parser.parse_args()

    db = sqlite3.connect(f"file:{args.database.resolve()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    integrity = scalar(db, "PRAGMA integrity_check")
    foreign_keys = [dict(row) for row in db.execute("PRAGMA foreign_key_check")]
    counts = {table: scalar(db, f"SELECT count(*) FROM {table}") for table in TABLES}

    sources = []
    for row in db.execute(
        """SELECT b.id,b.filename,b.backup_type,b.declared_count,
                  count(s.id) imported_count,min(s.source_ordinal) first_ordinal,
                  max(s.source_ordinal) last_ordinal,b.file_size,b.file_sha256,
                  b.first_message_ms,b.last_message_ms
           FROM backups b LEFT JOIN message_sources s ON s.backup_id=b.id
           GROUP BY b.id ORDER BY b.id"""
    ):
        item = dict(row)
        item["count_matches_declaration"] = (
            item["declared_count"] == item["imported_count"]
        )
        sources.append(item)

    json_columns = {
        "backups.root_metadata_json":
            "SELECT count(*) FROM backups WHERE NOT json_valid(root_metadata_json)",
        "message_sources.metadata_json":
            "SELECT count(*) FROM message_sources WHERE NOT json_valid(metadata_json)",
        "message_parts.metadata_json":
            "SELECT count(*) FROM message_parts WHERE NOT json_valid(metadata_json)",
        "source_parts.metadata_json":
            "SELECT count(*) FROM source_parts WHERE NOT json_valid(metadata_json)",
        "source_addresses.metadata_json":
            "SELECT count(*) FROM source_addresses WHERE NOT json_valid(metadata_json)",
        "message_protocol_metadata.decoded_json":
            "SELECT count(*) FROM message_protocol_metadata WHERE NOT json_valid(decoded_json)",
        "sticker_events.metadata_json":
            "SELECT count(*) FROM sticker_events WHERE NOT json_valid(metadata_json)",
        "app_messages.metadata_json":
            "SELECT count(*) FROM app_messages WHERE NOT json_valid(metadata_json)",
    }
    invalid_json = {name: scalar(db, sql) for name, sql in json_columns.items()}
    invariants = {
        "source_count_mismatches": sum(
            not source["count_matches_declaration"] for source in sources
        ),
        "messages_without_source": scalar(
            db, "SELECT count(*) FROM messages m WHERE NOT EXISTS "
                "(SELECT 1 FROM message_sources s WHERE s.message_id=m.id)"
        ),
        "source_occurrence_mismatches": scalar(
            db, "SELECT count(*) FROM messages m WHERE source_occurrences != "
                "(SELECT count(*) FROM message_sources s WHERE s.message_id=m.id)"
        ),
        "fts_coverage_mismatch": abs(counts["messages"] - counts["message_search"]),
        "blob_length_mismatches": scalar(
            db, "SELECT count(*) FROM media WHERE byte_size != length(data)"
        ),
        "duplicate_media_content_hashes": scalar(
            db, "SELECT count(*) FROM (SELECT 1 FROM media "
                "GROUP BY content_sha256 HAVING count(*)>1)"
        ),
        "invalid_media_hashes": scalar(
            db, "SELECT count(*) FROM media WHERE length(encoded_sha256)!=64 "
                "OR length(content_sha256)!=64 OR encoded_sha256 GLOB '*[^0-9a-f]*' "
                "OR content_sha256 GLOB '*[^0-9a-f]*'"
        ),
        "invalid_json_values": sum(invalid_json.values()),
        "visible_contentless_messages": scalar(
            db, """SELECT count(*) FROM messages m
                    WHERE coalesce(status,'')!='hidden'
                      AND trim(coalesce(body,''))=''
                      AND trim(coalesce(subject,''))=''
                      AND NOT EXISTS (
                        SELECT 1 FROM message_parts p WHERE p.message_id=m.id
                          AND (p.media_id IS NOT NULL
                            OR trim(coalesce(p.text_content,''))!=''
                            OR (trim(coalesce(p.filename,''))!=''
                              AND lower(coalesce(p.content_type,''))
                                  NOT IN ('application/smil','text/smil')))
                      )"""
        ),
        "stickers_without_media": scalar(
            db, "SELECT count(*) FROM sticker_events WHERE media_id IS NULL"
        ),
        "targeted_stickers_without_target": scalar(
            db, "SELECT count(*) FROM sticker_events "
                "WHERE kind!='standalone' AND target_message_id IS NULL"
        ),
        "sticker_part_mismatches": scalar(
            db, """SELECT count(*) FROM sticker_events s
                    LEFT JOIN message_parts p ON p.id=s.part_id
                    WHERE p.id IS NULL OR p.message_id!=s.sticker_message_id
                       OR p.media_id!=s.media_id"""
        ),
        "missing_required_indexes": sum(
            scalar(
                db,
                "SELECT count(*) FROM sqlite_master WHERE type='index' AND name=?",
                (name,),
            ) == 0
            for name in (
                "message_sources_message_idx", "media_content_sha_unique",
                "media_metadata_idx",
                "source_addresses_participant_idx", "source_parts_media_idx",
                "reactions_reactor_idx",
                "sticker_events_target_idx", "sticker_events_message_idx",
                "sticker_events_media_idx",
            )
        ),
    }

    deep = hash_media(db) if args.deep else {"performed": False}
    if args.deep:
        deep["performed"] = True
        invariants["media_hash_mismatches"] = len(deep["hash_mismatches"])

    report = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "database": str(args.database),
        "file_bytes": os.path.getsize(args.database),
        "integrity_check": integrity,
        "foreign_key_violations": foreign_keys,
        "healthy": integrity == "ok" and not foreign_keys and not any(invariants.values()),
        "counts": counts,
        "sources": sources,
        "invariants": invariants,
        "invalid_json_by_column": invalid_json,
        "messages_by_transport": grouped(
            db, "SELECT transport,count(*) FROM messages GROUP BY transport"
        ),
        "messages_by_direction": grouped(
            db, "SELECT direction,count(*) FROM messages GROUP BY direction"
        ),
        "messages_by_status": grouped(
            db, "SELECT coalesce(status,'null'),count(*) FROM messages GROUP BY status"
        ),
        "reactions_by_resolution": grouped(
            db, "SELECT resolution_confidence,count(*) FROM reaction_events GROUP BY 1"
        ),
        "replies_by_resolution": grouped(
            db, "SELECT resolution_confidence,count(*) FROM message_replies GROUP BY 1"
        ),
        "stickers_by_kind": grouped(
            db, "SELECT kind,count(*) FROM sticker_events GROUP BY 1"
        ),
        "unique_media_bytes": scalar(db, "SELECT coalesce(sum(byte_size),0) FROM media"),
        "deep_media_validation": deep,
        "sqlite_pages": {
            "page_size": scalar(db, "PRAGMA page_size"),
            "page_count": scalar(db, "PRAGMA page_count"),
            "freelist_count": scalar(db, "PRAGMA freelist_count"),
        },
    }
    db.close()

    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n")
    temporary.replace(args.output)
    print(json.dumps({
        "healthy": report["healthy"],
        "integrity_check": integrity,
        "foreign_key_violations": len(foreign_keys),
        "counts": counts,
        "invariants": invariants,
        "deep_media_validation": {
            key: value for key, value in deep.items() if key != "hash_mismatches"
        },
    }, indent=2))
    if not report["healthy"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
