#!/usr/bin/env python3
"""Prebuild TextView browser media derivatives with bounded parallelism."""

from __future__ import annotations

import argparse
import fcntl
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from generate_audio import generate as generate_audio
from generate_sticker import generate as generate_sticker
from generate_thumbnail import generate


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--database", type=Path, default=Path("messages.db"))
    parser.add_argument("--index", type=Path, default=Path("textview-index.db"))
    parser.add_argument("--archive-scan", action="store_true",
                        help="discover images from the archive instead of the app index")
    parser.add_argument("--output", type=Path, default=Path("data/thumbnails"))
    parser.add_argument("--preview-output", type=Path, default=Path("data/previews"))
    parser.add_argument("--audio-output", type=Path, default=Path("data/audio"))
    parser.add_argument("--sticker-output", type=Path, default=Path("data/stickers"))
    parser.add_argument(
        "--prune", action="store_true",
        help="remove derivatives no longer referenced by the selected index/database",
    )
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    lock_file = (args.output.parent / ".derivatives.lock").open("a+")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another derivative build is active; waiting for its cache lock.", flush=True)
        fcntl.flock(lock_file, fcntl.LOCK_EX)
    if not args.archive_scan:
        index = sqlite3.connect(f"file:{args.index.resolve()}?mode=ro", uri=True)
        columns = {row[1] for row in index.execute("PRAGMA table_info(attachments)")}
        if "content_sha256" not in columns:
            raise RuntimeError("app index is too old; rebuild it before thumbnails")
        rows = index.execute(
            """SELECT media_id,content_sha256,detected_content_type,content_type,category
               FROM attachments WHERE category IN ('image','video','audio')"""
        ).fetchall()
        index.close()
    else:
        archive = sqlite3.connect(f"file:{args.database.resolve()}?mode=ro", uri=True)
        media_ids = [row[0] for row in archive.execute(
            """SELECT DISTINCT media_id FROM message_parts
               WHERE media_id IS NOT NULL
                 AND (lower(coalesce(content_type,'')) LIKE 'image/%'
                      OR lower(coalesce(content_type,'')) LIKE 'video/%'
                      OR lower(coalesce(content_type,'')) LIKE 'audio/%')
                 AND lower(coalesce(filename,'')) NOT LIKE '%.pluginpayloadattachment'"""
        )]
        # The media table holds multi-gigabyte BLOBs. Read identity metadata
        # from a covering index so discovery never walks BLOB pages.
        wanted_media = set(media_ids)
        detected = [
            (media_id, content_hash, detected_type)
            for media_id, content_hash, detected_type in archive.execute(
                "SELECT id,content_sha256,detected_content_type "
                "FROM media INDEXED BY media_metadata_idx"
            )
            if media_id in wanted_media
        ]
        part_types = dict(archive.execute(
            """SELECT media_id,max(content_type) FROM message_parts
               WHERE media_id IS NOT NULL AND
                     (lower(coalesce(content_type,'')) LIKE 'image/%'
                      OR lower(coalesce(content_type,'')) LIKE 'video/%'
                      OR lower(coalesce(content_type,'')) LIKE 'audio/%')
               GROUP BY media_id"""
        ))
        rows = []
        for media_id, content_hash, detected_type in detected:
            declared_type = part_types.get(media_id) or ""
            effective_type = detected_type or declared_type
            if declared_type.startswith("audio/"):
                category = "audio"
            elif effective_type.startswith("video/"):
                category = "video"
            else:
                category = "image"
            rows.append((media_id, content_hash, detected_type, declared_type, category))
        archive.close()
    media = {}
    for media_id, content_hash, detected_type, content_type, category in rows:
        current = media.get(media_id)
        media[media_id] = (content_hash, detected_type or content_type or (current[1] if current else None), category)
    unsupported = {"image/heic", "image/heif", "image/heic-sequence", "image/avif"}
    image_jobs = [
        (media_id, args.output / f"{content_hash}.webp", 480, 76, "thumbnail")
        for media_id, (content_hash, _content_type, category) in media.items()
        if category in {"image", "video"}
    ]
    image_jobs += [
        (media_id, args.preview_output / f"{content_hash}.webp", 2048, 82, "preview")
        for media_id, (content_hash, content_type, category) in media.items()
        if category == "image" and (content_type or "").lower() in unsupported
    ]
    audio_jobs = [
        (media_id, args.audio_output / f"{content_hash}.webm", "audio")
        for media_id, (content_hash, _content_type, category) in media.items()
        if category == "audio"
    ]
    archive = sqlite3.connect(f"file:{args.database.resolve()}?mode=ro", uri=True)
    has_stickers = archive.execute(
        "select 1 from sqlite_master where type='table' and name='sticker_events'"
    ).fetchone()
    sticker_rows = archive.execute(
        """SELECT DISTINCT se.media_id,m.content_sha256,se.is_animated
           FROM sticker_events se JOIN media m ON m.id=se.media_id
           WHERE se.media_id IS NOT NULL"""
    ).fetchall() if has_stickers else []
    archive.close()
    sticker_jobs = [
        (media_id, args.sticker_output / f"{content_hash}.webp", bool(animated), "sticker")
        for media_id, content_hash, animated in sticker_rows
    ]
    jobs = image_jobs + audio_jobs + sticker_jobs
    missing = [job for job in jobs if not job[1].exists() or not job[1].stat().st_size]
    thumbnails = sum(job[-1] == "thumbnail" for job in jobs)
    previews = sum(job[-1] == "preview" for job in jobs)
    audio = sum(job[-1] == "audio" for job in jobs)
    stickers = sum(job[-1] == "sticker" for job in jobs)
    print(f"Derivative cache: {len(jobs) - len(missing):,} ready; {len(missing):,} missing "
          f"({thumbnails:,} thumbnails, {previews:,} image previews, "
          f"{audio:,} audio copies, {stickers:,} stickers).", flush=True)
    completed = failures = 0

    def build(job):
        media_id, path, *settings, kind = job
        if kind == "audio":
            return generate_audio(args.database.resolve(), media_id, path)
        if kind == "sticker":
            return generate_sticker(args.database.resolve(), media_id, path, settings[0])
        size, quality = settings
        return generate(args.database.resolve(), media_id, path, size, quality)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(build, job): (job[0], job[-1]) for job in missing
        }
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as error:
                failures += 1
                print(f"warning: media {futures[future]}: {error}", flush=True)
            completed += 1
            if completed % 100 == 0 or completed == len(missing):
                print(
                    f"Processed {completed:,}/{len(missing):,}; failures {failures:,}.",
                    flush=True,
                )
    print(f"Derivative build complete with {failures:,} failures.", flush=True)
    if failures:
        raise SystemExit(1)
    if args.prune:
        expected = {
            args.output: {job[1] for job in image_jobs if job[-1] == "thumbnail"},
            args.preview_output: {job[1] for job in image_jobs if job[-1] == "preview"},
            args.audio_output: {job[1] for job in audio_jobs},
            args.sticker_output: {job[1] for job in sticker_jobs},
        }
        removed = 0
        for directory, keep in expected.items():
            if not directory.exists():
                continue
            for candidate in directory.iterdir():
                if candidate.is_file() and candidate not in keep:
                    candidate.unlink()
                    removed += 1
        print(f"Pruned {removed:,} unreferenced derivative files.", flush=True)


if __name__ == "__main__":
    main()
