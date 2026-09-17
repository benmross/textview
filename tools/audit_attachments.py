#!/usr/bin/env python3
"""Validate stored attachment containers without materializing the archive."""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import sqlite3
import warnings
import zipfile
from collections import Counter
from pathlib import Path

from PIL import Image
from annotate_media import detect


def brands(head: bytes) -> set[bytes]:
    if len(head) < 12 or head[4:8] != b"ftyp":
        return set()
    return {head[8:12], *(head[16:64][i:i + 4] for i in range(0, 48, 4))}


def signature_ok(mime: str, head: bytes, tail: bytes) -> bool | None:
    b = brands(head)
    checks = {
        # Samsung/Google sometimes append auxiliary data after the JPEG EOI.
        "image/jpeg": head.startswith(b"\xff\xd8\xff"),
        "image/png": head.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/gif": head.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": head.startswith(b"RIFF") and head[8:12] == b"WEBP",
        "image/heic": bool(b & {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}),
        "image/heic-sequence": bool(b & {b"hevc", b"hevx", b"msf1", b"heic", b"mif1"}),
        "image/avif": bool(b & {b"avif", b"avis"}),
        "video/mp4": bool(b),
        "video/3gpp": bool(b & {b"3gp4", b"3gp5", b"3gp6", b"3gp7", b"3ge6", b"3gg6"}),
        "video/quicktime": b"qt  " in b,
        "audio/mp4": bool(b),
        "audio/x-m4a": bool(b),
        "audio/amr": head.startswith((b"#!AMR\n", b"#!AMR-WB\n")),
        "audio/mpeg": head.startswith(b"ID3") or (len(head) > 1 and head[0] == 0xff and head[1] & 0xe0 == 0xe0),
        "audio/x-wav": head.startswith(b"RIFF") and head[8:12] == b"WAVE",
        "audio/vnd.wave": head.startswith(b"RIFF") and head[8:12] == b"WAVE",
        "audio/x-caf": head.startswith(b"caff"),
        "application/pdf": head.startswith(b"%PDF-") and b"%%EOF" in tail,
        "application/zip": head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            head.startswith(b"PK"),
        "application/vnd.openxmlformats-officedocument.presentationml.presentation":
            head.startswith(b"PK"),
        "application/msword": head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),
    }
    return checks.get(mime)


class BlobReader:
    """Give Pillow the small file API surface sqlite3.Blob omits."""

    def __init__(self, blob: sqlite3.Blob):
        self.blob = blob

    def read(self, size: int = -1) -> bytes:
        return self.blob.read(size)

    def seek(self, offset: int, whence: int = 0) -> int:
        return self.blob.seek(offset, whence)

    def tell(self) -> int:
        return self.blob.tell()

    def readline(self, size: int = -1) -> bytes:
        result = bytearray()
        while size < 0 or len(result) < size:
            char = self.blob.read(1)
            if not char:
                break
            result += char
            if char == b"\n":
                break
        return bytes(result)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path, nargs="?", default=Path("messages.db"))
    parser.add_argument("--output", type=Path, default=Path("attachment-audit.json"))
    args = parser.parse_args()
    db = sqlite3.connect(args.database)
    db.row_factory = sqlite3.Row
    mime_rows = db.execute(
        """SELECT DISTINCT media_id,mime FROM (
             SELECT mp.media_id,lower(mp.content_type) mime
             FROM message_parts mp WHERE mp.media_id IS NOT NULL
             UNION
             SELECT sp.media_id,lower(json_extract(sp.metadata_json,'$.ct')) mime
             FROM source_parts sp WHERE sp.media_id IS NOT NULL
           )"""
    ).fetchall()
    by_media: dict[int, set[str]] = {}
    for row in mime_rows:
        if row["mime"]:
            by_media.setdefault(row["media_id"], set()).add(row["mime"])

    failures: list[dict] = []
    mime_mismatches: list[dict] = []
    decoder_warnings: list[dict] = []
    unverifiable = Counter()
    checked = Counter()
    decoded = Counter()
    zip_checked = 0
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        for row in db.execute("SELECT id,byte_size FROM media ORDER BY id"):
            media_id, size = row
            blob = db.blobopen("media", "data", media_id, readonly=True)
            head = blob.read(min(128, size))
            blob.seek(max(0, size - 128))
            tail = blob.read(min(128, size))
            for mime in sorted(by_media.get(media_id, ())):
                result = signature_ok(mime, head, tail)
                if result is None:
                    unverifiable[mime] += 1
                elif result:
                    checked[mime] += 1
                else:
                    mime_mismatches.append(
                        {"media_id": media_id, "declared_mime": mime,
                         "detected_mime": detect(head)}
                    )

            image_mimes = by_media.get(media_id, set()) & {
                "image/jpeg", "image/png", "image/gif", "image/webp"
            }
            if image_mimes:
                try:
                    blob.seek(0)
                    with Image.open(BlobReader(blob)) as image:
                        image.verify()
                    for mime in image_mimes:
                        decoded[mime] += 1
                except Exception as exc:
                    decoder_warnings.append(
                        {"media_id": media_id, "mime": sorted(image_mimes),
                         "reason": f"image decoder: {type(exc).__name__}: {exc}"}
                    )
            blob.close()

            zip_mimes = by_media.get(media_id, set()) & {
                "application/zip",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            }
            if zip_mimes:
                data = db.execute("SELECT data FROM media WHERE id=?", (media_id,)).fetchone()[0]
                try:
                    with zipfile.ZipFile(io.BytesIO(data)) as archive:
                        bad = archive.testzip()
                        if bad:
                            raise zipfile.BadZipFile(f"bad member {bad}")
                    zip_checked += 1
                except Exception as exc:
                    failures.append(
                        {"media_id": media_id, "mime": sorted(zip_mimes),
                         "reason": f"zip decoder: {type(exc).__name__}: {exc}"}
                    )

    payloadless = [
        dict(row) for row in db.execute(
            """SELECT lower(content_type) content_type,count(*) occurrences
               FROM message_parts
               WHERE media_id IS NULL AND text_content IS NULL
                 AND lower(content_type) NOT IN ('application/smil','text/plain')
               GROUP BY 1 ORDER BY 2 DESC"""
        )
    ]
    report = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "stored_media_rows": db.execute("SELECT count(*) FROM media").fetchone()[0],
        "media_rows_with_declared_mime": len(by_media),
        "signature_checked_by_mime": dict(sorted(checked.items())),
        "decoder_checked_by_mime": dict(sorted(decoded.items())),
        "zip_containers_checked": zip_checked,
        "signature_not_defined_by_mime": dict(sorted(unverifiable.items())),
        "source_mime_mismatches": mime_mismatches,
        "decoder_warnings": decoder_warnings,
        "failures": failures,
        "source_parts_declared_without_payload": payloadless,
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
