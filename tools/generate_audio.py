#!/usr/bin/env python3
"""Generate one browser-compatible Opus/WebM copy of an attachment BLOB."""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path


def generate(database: Path, media_id: int, output: Path) -> bool:
    if output.exists() and output.stat().st_size:
        return False
    output.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    if not connection.execute(
        "SELECT 1 FROM media WHERE id=?", (media_id,)
    ).fetchone():
        connection.close()
        raise RuntimeError(f"unknown media id {media_id}")
    # Source extraction is scratch space; only the atomic final WebM belongs in
    # the persistent content-addressed cache.
    with tempfile.NamedTemporaryFile(suffix=".source") as source:
        blob = connection.blobopen("media", "data", media_id, readonly=True)
        while chunk := blob.read(1024 * 1024):
            source.write(chunk)
        blob.close()
        source.flush()
        temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
        try:
            result = subprocess.run(
                [
                    "/usr/bin/ffmpeg", "-v", "error", "-threads", "1", "-y",
                    "-i", source.name, "-map", "0:a:0", "-vn", "-map_metadata", "-1",
                    "-c:a", "libopus", "-b:a", "96k", "-ac", "2", "-f", "webm",
                    str(temporary),
                ],
                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                timeout=180,
            )
            if result.returncode or not temporary.exists() or not temporary.stat().st_size:
                detail = result.stderr.decode("utf-8", "replace").strip()
                raise RuntimeError(detail or "ffmpeg produced no audio output")
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
    connection.close()
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("media_id", type=int)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    generate(args.database.resolve(), args.media_id, args.output.resolve())


if __name__ == "__main__":
    main()
