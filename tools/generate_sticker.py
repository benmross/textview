#!/usr/bin/env python3
"""Prebuild a browser-safe WebP for one normalized sticker."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from generate_thumbnail import generate as generate_static


def _streams(path: str) -> list[dict]:
    result = subprocess.run(
        ["/usr/bin/ffprobe", "-v", "error", "-show_entries",
         "stream=index,pix_fmt,width,height,nb_frames,duration", "-of", "json", path],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
    )
    return json.loads(result.stdout).get("streams", [])


def _frames(stream: dict) -> int:
    try:
        return int(stream.get("nb_frames") or 0)
    except (TypeError, ValueError):
        return 0


def generate(database: Path, media_id: int, output: Path, animated: bool) -> bool:
    if output.exists() and output.stat().st_size:
        return False
    if not animated:
        return generate_static(database, media_id, output, max_size=600, quality=82)

    output.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    if not connection.execute("select 1 from media where id=?", (media_id,)).fetchone():
        raise RuntimeError(f"unknown media id {media_id}")
    with tempfile.NamedTemporaryFile(suffix=".heics") as source:
        blob = connection.blobopen("media", "data", media_id, readonly=True)
        while chunk := blob.read(1024 * 1024):
            source.write(chunk)
        blob.close()
        source.flush()
        streams = _streams(source.name)
        animated_streams = [stream for stream in streams if _frames(stream) > 1]
        color = max(
            (stream for stream in animated_streams
             if not str(stream.get("pix_fmt") or "").startswith("gray")),
            key=_frames, default=None,
        )
        if not color:
            # A mislabeled single-frame HEIC should still be served reliably.
            connection.close()
            return generate_static(database, media_id, output, max_size=600, quality=82)
        alpha = max(
            (stream for stream in animated_streams
             if str(stream.get("pix_fmt") or "").startswith("gray") and
                stream.get("width") == color.get("width") and
                stream.get("height") == color.get("height")),
            key=_frames, default=None,
        )
        temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
        try:
            if alpha:
                graph = (
                    f"[0:{color['index']}][0:{alpha['index']}]alphamerge,"
                    "scale=600:600:force_original_aspect_ratio=decrease:flags=lanczos[sticker]"
                )
                filters = ["-filter_complex", graph, "-map", "[sticker]"]
            else:
                filters = ["-map", f"0:{color['index']}", "-vf",
                           "scale=600:600:force_original_aspect_ratio=decrease:flags=lanczos"]
            subprocess.run(
                ["/usr/bin/ffmpeg", "-v", "error", "-threads", "1", "-y", "-i",
                 source.name, *filters, "-loop", "0", "-c:v", "libwebp_anim",
                 "-quality", "82", "-f", "webp", str(temporary)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120,
            )
            if not temporary.exists() or not temporary.stat().st_size:
                raise RuntimeError("ffmpeg produced no sticker output")
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
    connection.close()
    return True

