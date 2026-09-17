#!/usr/bin/env python3
"""Generate one bounded WebP thumbnail from an attachment BLOB."""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path


LOCAL_HEIF_PLUGINS = (
    Path(__file__).resolve().parent.parent
    / "vendor/libheif-plugin/extracted/usr/lib/x86_64-linux-gnu/libheif/plugins"
)


def generate(
    database: Path, media_id: int, output: Path,
    max_size: int = 480, quality: int = 76,
) -> bool:
    if output.exists() and output.stat().st_size:
        return False
    output.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    size = connection.execute(
        "SELECT byte_size FROM media WHERE id=?", (media_id,)
    ).fetchone()
    if not size:
        raise RuntimeError(f"unknown media id {media_id}")
    # Keep decoded source scratch files out of the persistent derivative cache.
    # An interrupted backfill can then never leave files that look cache-owned.
    with tempfile.NamedTemporaryFile(suffix=".source") as source:
        blob = connection.blobopen("media", "data", media_id, readonly=True)
        while chunk := blob.read(1024 * 1024):
            source.write(chunk)
        blob.close()
        source.flush()
        temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
        try:
            environment = os.environ.copy()
            environment["MAGICK_THREAD_LIMIT"] = "1"
            environment["OMP_NUM_THREADS"] = "1"
            if LOCAL_HEIF_PLUGINS.exists():
                environment["LIBHEIF_PLUGIN_PATH"] = str(LOCAL_HEIF_PLUGINS)
            command = [
                    "/usr/bin/magick",
                    "-limit", "thread", "1",
                    "-limit", "memory", "96MiB",
                    "-limit", "map", "192MiB",
                    f"{source.name}[0]",
                    "-auto-orient",
                    "-thumbnail", f"{max_size}x{max_size}>",
                    "-strip",
                    "-quality", str(quality),
                    f"webp:{temporary}",
                ]
            errors = []
            try:
                result = subprocess.run(
                    command, check=True, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, timeout=90, env=environment,
                )
                if not temporary.exists() or not temporary.stat().st_size:
                    errors.append("ImageMagick produced no output")
            except (subprocess.SubprocessError, OSError) as error:
                detail = getattr(error, "stderr", b"")
                errors.append(detail.decode("utf-8", "replace").strip() or str(error))
            if not temporary.exists() or not temporary.stat().st_size:
                # ffmpeg's AV1/HEVC/video decoders cover formats and partial
                # containers that the installed ImageMagick stack cannot open.
                temporary.unlink(missing_ok=True)
                fallback = [
                    "/usr/bin/ffmpeg", "-v", "error", "-err_detect", "ignore_err",
                    "-threads", "1", "-y", "-i", source.name,
                    "-frames:v", "1", "-vf",
                    f"scale={max_size}:{max_size}:force_original_aspect_ratio=decrease",
                    "-c:v", "libwebp", "-quality", str(quality), "-f", "webp",
                    str(temporary),
                ]
                try:
                    subprocess.run(
                        fallback, check=True, stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE, timeout=90,
                    )
                except (subprocess.SubprocessError, OSError) as error:
                    detail = getattr(error, "stderr", b"")
                    errors.append(detail.decode("utf-8", "replace").strip() or str(error))
            if not temporary.exists() or not temporary.stat().st_size:
                # Preserve a stable, cacheable tile even when the archived
                # source itself is truncated or corrupt.
                temporary.unlink(missing_ok=True)
                subprocess.run(
                    ["/usr/bin/magick", "-limit", "thread", "1", "-size",
                     f"{max_size}x{max_size}", "radial-gradient:#46504e-#151918",
                     "-strip", "-quality", str(quality), f"webp:{temporary}"],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    timeout=30, env=environment,
                )
                print(f"warning: media {media_id}: compatibility placeholder used: "
                      + "; ".join(filter(None, errors)), file=sys.stderr, flush=True)
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
    parser.add_argument("--size", type=int, default=480)
    parser.add_argument("--quality", type=int, default=76)
    args = parser.parse_args()
    generate(
        args.database.resolve(), args.media_id, args.output.resolve(),
        max_size=args.size, quality=args.quality,
    )


if __name__ == "__main__":
    main()
