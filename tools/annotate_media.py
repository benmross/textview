#!/usr/bin/env python3
"""Annotate attachment bytes with independently detected container types."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def detect(head: bytes) -> str | None:
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith((b"#!AMR\n", b"#!AMR-WB\n")):
        return "audio/amr"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"PK"):
        return "application/zip"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "application/x-ole-storage"
    if head.startswith(b"ID3") or (
        len(head) > 1 and head[0] == 0xff and head[1] & 0xe0 == 0xe0
    ):
        return "audio/mpeg"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        compatible = {head[i:i + 4] for i in range(16, min(len(head), 64), 4)}
        all_brands = compatible | {brand}
        if all_brands & {b"avif", b"avis"}:
            return "image/avif"
        if all_brands & {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
            return "image/heic"
        if all_brands & {b"3gp4", b"3gp5", b"3gp6", b"3gp7", b"3ge6", b"3gg6"}:
            return "video/3gpp"
        if b"qt  " in all_brands:
            return "video/quicktime"
        return "video/mp4"
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path, nargs="?", default=Path("messages.db"))
    parser.add_argument("--all", action="store_true", help="recheck rows already annotated")
    args = parser.parse_args()
    db = sqlite3.connect(args.database)
    columns = {row[1] for row in db.execute("PRAGMA table_info(media)")}
    if "detected_content_type" not in columns:
        db.execute("ALTER TABLE media ADD COLUMN detected_content_type TEXT")
    if "validation_status" not in columns:
        db.execute("ALTER TABLE media ADD COLUMN validation_status TEXT")
    db.execute(
        "CREATE INDEX IF NOT EXISTS media_metadata_idx ON media("
        "id,content_sha256,detected_content_type,byte_size,validation_status)"
    )
    # This source APNG renders with ImageMagick, but Pillow reports a bad fcTL checksum.
    warnings = {"0133381a6672f6e9f808e1aeab1397dc0b395393bd5ee091b98582e910125c8b"}
    where = "" if args.all else (
        " WHERE validation_status IS NULL OR validation_status='unannotated'"
    )
    annotated = 0
    pending = db.execute(
        "SELECT id,content_sha256 FROM media INDEXED BY media_metadata_idx" + where
    ).fetchall()
    for media_id, content_hash in pending:
        # substr(BLOB) can materialize the full payload. Incremental BLOB I/O
        # reads only the signature bytes, keeping this proportional to row count.
        with db.blobopen("media", "data", media_id, readonly=True) as blob:
            head = blob.read(128)
        actual = detect(head)
        status = "source-container-warning" if content_hash in warnings else (
            "signature-verified" if actual else "hash-verified"
        )
        db.execute(
            """UPDATE media
               SET detected_content_type=?,validation_status=? WHERE id=?""",
            (actual, status, media_id),
        )
        annotated += 1
    db.execute(
        """INSERT OR REPLACE INTO archive_info(key,value)
           VALUES('media_validation_version','1')"""
    )
    db.commit()
    print(
        "annotated",
        annotated,
        "media rows; total",
        db.execute("SELECT count(*) FROM media").fetchone()[0],
    )


if __name__ == "__main__":
    main()
