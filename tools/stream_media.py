#!/usr/bin/env python3
"""Stream a byte range from a SQLite media BLOB to stdout with bounded memory."""

from __future__ import annotations

import os
import sqlite3
import sys


def main() -> None:
    if len(sys.argv) != 5:
        raise SystemExit("usage: stream_media.py DATABASE MEDIA_ID START LENGTH")
    database, media_id, start, length = sys.argv[1:]
    remaining = int(length)
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    blob = connection.blobopen("media", "data", int(media_id), readonly=True)
    blob.seek(int(start))
    while remaining:
        chunk = blob.read(min(512 * 1024, remaining))
        if not chunk:
            raise RuntimeError("attachment ended before requested range")
        view = memoryview(chunk)
        while view:
            written = os.write(sys.stdout.fileno(), view)
            view = view[written:]
        remaining -= len(chunk)
    blob.close()
    connection.close()


if __name__ == "__main__":
    main()
