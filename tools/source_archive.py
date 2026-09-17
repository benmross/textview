#!/usr/bin/env python3
"""Maintain and verify the application-neutral messaging source archive.

The archive is intentionally independent of TextView.  TextView is one known
consumer, but the raw formats, paths, checksums, and schema documentation are
kept useful to any future importer.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
import xml.parsers.expat


DEFAULT_ARCHIVE = Path("/mnt/Backups/message-sources")
DEFAULT_WORK = Path(__file__).resolve().parents[1] / "imessage-import"
DATA_DIRECTORIES = (
    "android",
    "apple",
    "bluebubbles",
    "supplemental-attachments",
)
TREE_MAP = {
    "Attachments": "apple/messages/Attachments",
    "StickerCache": "apple/messages/StickerCache",
}
PRIMARY_SOURCES = (
    ("android/sms-backup-restore/sms-20250212171642.xml", "sms-backup-restore-xml"),
    ("android/sms-backup-restore/sms-20250619153114.xml", "sms-backup-restore-xml"),
    ("android/sms-backup-restore/sms-20260610030051.xml", "sms-backup-restore-xml"),
    ("apple/messages/chat.db", "apple-messages-sqlite"),
    (
        "bluebubbles/exports/BlueBubbles-chats-2026822_2117.json",
        "bluebubbles-length-prefixed-container",
    ),
)


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_text(path: Path, text: str) -> None:
    pending = path.with_name(path.name + ".pending")
    pending.write_text(text, encoding="utf-8")
    os.replace(pending, path)


def require_archive(archive: Path) -> None:
    resolved = archive.resolve()
    backup_root = Path("/mnt/Backups").resolve()
    if resolved != backup_root and backup_root not in resolved.parents:
        raise RuntimeError(f"refusing archive path outside {backup_root}: {resolved}")
    if not os.path.ismount("/mnt"):
        raise RuntimeError("/mnt is not mounted; refusing to write to the root filesystem")
    if not archive.is_dir():
        raise RuntimeError(f"archive directory does not exist: {archive}")


def sqlite_open(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)


def sqlite_quick_check(path: Path) -> None:
    with sqlite_open(path) as database:
        result = database.execute("PRAGMA quick_check").fetchone()[0]
    if result != "ok":
        raise RuntimeError(f"SQLite quick_check failed for {path}: {result}")


def message_guids(path: Path) -> set[str]:
    with sqlite_open(path) as database:
        return {
            row[0]
            for row in database.execute("SELECT guid FROM message WHERE guid IS NOT NULL")
        }


def copy_sqlite_snapshot(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    pending = destination.with_name(destination.name + ".pending")
    pending.unlink(missing_ok=True)
    # chat-live.db is already the output of SQLite's backup API on the Mac and
    # this command runs only after its rsync has completed. Preserve that exact
    # source file rather than producing a second, byte-different SQLite backup.
    shutil.copy2(source, pending)
    sqlite_quick_check(pending)
    return pending


def prune_subset_checkpoints(current: Path, historical: Path) -> list[str]:
    """Remove any historical DB whose GUID set is contained in another DB."""
    removed: list[str] = []
    if not historical.is_dir():
        return removed
    paths = [current, *sorted(historical.glob("*.db"))]
    guid_sets = {path: message_guids(path) for path in paths}
    for candidate in paths[1:]:
        if any(
            candidate != other and guid_sets[candidate] <= guid_sets[other]
            for other in paths
        ):
            candidate.unlink()
            removed.append(candidate.name)
    return removed


def publish_chat_database(
    source: Path, destination: Path, known_destination_hash: str | None = None
) -> dict[str, object]:
    # A checkpoint is a self-contained database. SQLite runtime sidecars are
    # neither source data nor portable and must not enter the archive.
    for suffix in ("-wal", "-shm"):
        destination.with_name(destination.name + suffix).unlink(missing_ok=True)
    new_hash = sha256_file(source)
    if destination.exists() and known_destination_hash == new_hash:
        return {"changed": False, "sha256": new_hash, "retained_previous": False}
    if destination.exists() and known_destination_hash is None and sha256_file(destination) == new_hash:
        return {"changed": False, "sha256": new_hash, "retained_previous": False}

    pending = copy_sqlite_snapshot(source, destination)
    if sha256_file(pending) != new_hash:
        pending.unlink(missing_ok=True)
        raise RuntimeError("Mac Messages checkpoint changed while it was being copied")

    historical = destination.parent / "historical"
    retained = False
    previous_only = 0
    if destination.exists():
        old_guids = message_guids(destination)
        new_guids = message_guids(pending)
        previous_only = len(old_guids - new_guids)
        if previous_only:
            historical.mkdir(parents=True, exist_ok=True)
            existing_sets = [message_guids(path) for path in historical.glob("*.db")]
            if not any(old_guids <= existing for existing in existing_sets):
                old_hash = sha256_file(destination)
                stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
                shutil.move(
                    destination,
                    historical / f"chat-{stamp}-{old_hash[:12]}.db",
                )
                retained = True
    os.replace(pending, destination)
    removed = prune_subset_checkpoints(destination, historical)
    return {
        "changed": True,
        "sha256": new_hash,
        "previous_only_guids": previous_only,
        "retained_previous": retained,
        "pruned_subset_checkpoints": removed,
    }


def conflicting_files(source: Path, destination: Path) -> list[tuple[Path, str]]:
    conflicts: list[tuple[Path, str]] = []
    if not destination.exists():
        return conflicts
    for candidate in source.rglob("*"):
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(source)
        archived = destination / relative
        if not archived.is_file():
            continue
        source_stat, archive_stat = candidate.stat(), archived.stat()
        if (
            source_stat.st_size == archive_stat.st_size
            and source_stat.st_mtime_ns == archive_stat.st_mtime_ns
        ):
            continue
        source_hash = sha256_file(candidate)
        if source_hash != sha256_file(archived):
            conflicts.append((relative, source_hash))
    return conflicts


def sync_append_only(source: Path, destination: Path, conflict_root: Path) -> dict[str, int]:
    destination.mkdir(parents=True, exist_ok=True)
    conflicts = conflicting_files(source, destination)
    for relative, digest in conflicts:
        target = conflict_root / destination.name / relative.parent
        target.mkdir(parents=True, exist_ok=True)
        name = relative.name + f".{digest[:16]}"
        shutil.copy2(source / relative, target / name)
    result = subprocess.run(
        ["rsync", "-a", "--ignore-existing", f"{source}/", f"{destination}/"],
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"rsync failed for {source} with status {result.returncode}")
    return {"path_conflicts_preserved": len(conflicts)}


def data_files(archive: Path) -> list[Path]:
    paths: list[Path] = []
    for name in DATA_DIRECTORIES:
        root = archive / name
        if root.is_dir():
            paths.extend(path for path in root.rglob("*") if path.is_file())
    return sorted(paths)


def load_checksum_cache(archive: Path) -> dict[str, dict[str, object]]:
    path = archive / "documentation" / ".checksum-cache.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def refresh_checksums(archive: Path, workers: int) -> dict[str, dict[str, object]]:
    old_cache = load_checksum_cache(archive)
    new_cache: dict[str, dict[str, object]] = {}
    needs_hash: list[tuple[str, Path, os.stat_result]] = []
    for path in data_files(archive):
        relative = path.relative_to(archive).as_posix()
        stat = path.stat()
        cached = old_cache.get(relative)
        if cached and cached.get("size") == stat.st_size and cached.get("mtime_ns") == stat.st_mtime_ns:
            new_cache[relative] = cached
        else:
            needs_hash.append((relative, path, stat))

    def calculate(item: tuple[str, Path, os.stat_result]):
        relative, path, stat = item
        return relative, {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": sha256_file(path),
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        for relative, record in executor.map(calculate, needs_hash):
            new_cache[relative] = record

    documentation = archive / "documentation"
    documentation.mkdir(parents=True, exist_ok=True)
    atomic_text(
        documentation / ".checksum-cache.json",
        json.dumps(new_cache, indent=2, sort_keys=True) + "\n",
    )
    manifest = "".join(
        f"{record['sha256']}  {relative}\n"
        for relative, record in sorted(new_cache.items())
    )
    atomic_text(archive / "MANIFEST.sha256", manifest)
    return new_cache


def xml_root(path: Path) -> dict[str, str]:
    for _event, element in ET.iterparse(path, events=("start",)):
        if element.tag != "smses":
            raise RuntimeError(f"unexpected XML root {element.tag!r} in {path}")
        return dict(element.attrib)
    raise RuntimeError(f"empty XML source: {path}")


def bluebubbles_header(path: Path) -> dict[str, object]:
    with path.open("rb") as stream:
        header = stream.read(4)
        if len(header) != 4:
            raise RuntimeError(f"truncated BlueBubbles header: {path}")
        json_length = struct.unpack(">I", header)[0]
        document = json.loads(stream.read(json_length))
    return {
        "json_bytes": json_length,
        "messages": len(document.get("messages", [])),
        "chats": len(document.get("chats", [])),
        "attachments": len(document.get("atts", [])),
    }


def apple_metadata(path: Path) -> dict[str, object]:
    with sqlite_open(path) as database:
        messages = database.execute("SELECT COUNT(*) FROM message").fetchone()[0]
        attachments = database.execute("SELECT COUNT(*) FROM attachment").fetchone()[0]
        chats = database.execute("SELECT COUNT(*) FROM chat").fetchone()[0]
        schema_version = database.execute("PRAGMA schema_version").fetchone()[0]
    return {
        "messages": messages,
        "attachments": attachments,
        "chats": chats,
        "sqlite_schema_version": schema_version,
    }


def write_apple_schema(archive: Path) -> None:
    database_path = archive / "apple/messages/chat.db"
    with sqlite_open(database_path) as database:
        user_version = database.execute("PRAGMA user_version").fetchone()[0]
        rows = database.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL "
            "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END,name"
        ).fetchall()
    output = [
        "-- Exact SQLite schema extracted from apple/messages/chat.db.\n",
        "-- This records structure only; it contains no message content.\n",
        f"-- Generated: {utc_now()}\n",
        f"PRAGMA user_version = {user_version};\n\n",
    ]
    for kind, name, sql in rows:
        output.append(f"-- {kind}: {name}\n{sql.rstrip(';')};\n\n")
    atomic_text(
        archive / "documentation/schemas/apple-messages-schema.sql",
        "".join(output),
    )


def source_record(archive: Path, cache: dict[str, dict[str, object]], relative: str, kind: str):
    path = archive / relative
    record: dict[str, object] = {
        "path": relative,
        "format": kind,
        "bytes": path.stat().st_size,
        "sha256": cache[relative]["sha256"],
    }
    if kind == "sms-backup-restore-xml":
        root = xml_root(path)
        record.update(
            declared_records=int(root["count"]),
            backup_set=root.get("backup_set"),
            backup_date_ms=int(root["backup_date"]),
            backup_type=root.get("type"),
        )
    elif kind == "apple-messages-sqlite":
        record.update(apple_metadata(path))
    elif kind == "bluebubbles-length-prefixed-container":
        record.update(bluebubbles_header(path))
    return record


def write_inventory(archive: Path, cache: dict[str, dict[str, object]]) -> None:
    previous_by_path: dict[str, dict[str, object]] = {}
    try:
        previous = json.loads((archive / "INVENTORY.json").read_text(encoding="utf-8"))
        previous_by_path = {
            item["path"]: item for item in previous.get("primary_sources", [])
        }
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError):
        pass
    sources = []
    for relative, kind in PRIMARY_SOURCES:
        old = previous_by_path.get(relative)
        # The JSON prefix expands substantially in memory. Its structural counts
        # cannot change while the complete container hash is unchanged.
        if (
            kind == "bluebubbles-length-prefixed-container"
            and old
            and old.get("sha256") == cache[relative]["sha256"]
        ):
            sources.append(old)
        else:
            sources.append(source_record(archive, cache, relative, kind))
    trees = []
    for relative in (
        "apple/messages/Attachments",
        "apple/messages/StickerCache",
        "supplemental-attachments/BlueBubblesLegion",
        "supplemental-attachments/LegionMessagesAttachments",
        "supplemental-attachments/PixelMessages",
    ):
        prefix = relative + "/"
        records = [value for path, value in cache.items() if path.startswith(prefix)]
        trees.append(
            {
                "path": relative,
                "files": len(records),
                "bytes": sum(int(value["size"]) for value in records),
                "unique_content_sha256": len({value["sha256"] for value in records}),
            }
        )
    inventory = {
        "archive_format_version": 1,
        "generated_at": utc_now(),
        "description": "Original messaging exports, databases, and attachment stores",
        "known_consumers": {
            "TextView": "A local web application that normalizes and presents these sources"
        },
        "primary_sources": sources,
        "attachment_trees": trees,
    }
    atomic_text(archive / "INVENTORY.json", json.dumps(inventory, indent=2) + "\n")


def refresh_metadata(archive: Path, workers: int) -> None:
    require_archive(archive)
    cache = refresh_checksums(archive, workers)
    write_inventory(archive, cache)
    write_apple_schema(archive)
    print(f"metadata refreshed: {len(cache):,} files")


def sync_live(archive: Path, work: Path, workers: int) -> None:
    require_archive(archive)
    source_database = work / "chat-live.db"
    if not source_database.is_file():
        raise RuntimeError(f"live Messages checkpoint is missing: {source_database}")
    destination_database = archive / "apple/messages/chat.db"
    cache = load_checksum_cache(archive)
    cached_database = cache.get("apple/messages/chat.db")
    known_destination_hash = None
    if cached_database and destination_database.exists():
        stat = destination_database.stat()
        if (
            cached_database.get("size") == stat.st_size
            and cached_database.get("mtime_ns") == stat.st_mtime_ns
        ):
            known_destination_hash = str(cached_database["sha256"])
    database_result = publish_chat_database(
        source_database, destination_database, known_destination_hash
    )
    conflict_root = archive / "supplemental-attachments/path-conflicts"
    tree_results = {}
    for source_name, destination in TREE_MAP.items():
        tree_results[source_name] = sync_append_only(
            work / source_name,
            archive / destination,
            conflict_root,
        )
    refresh_metadata(archive, workers)
    print(json.dumps({"chat_database": database_result, "trees": tree_results}, indent=2))


def read_manifest(archive: Path) -> dict[str, str]:
    result = {}
    for line in (archive / "MANIFEST.sha256").read_text(encoding="utf-8").splitlines():
        digest, relative = line.split("  ", 1)
        result[relative] = digest
    return result


def verify_manifest(archive: Path, workers: int) -> None:
    expected = read_manifest(archive)
    actual_paths = {path.relative_to(archive).as_posix(): path for path in data_files(archive)}
    missing = sorted(set(expected) - set(actual_paths))
    unlisted = sorted(set(actual_paths) - set(expected))
    if missing or unlisted:
        raise RuntimeError(f"manifest path mismatch: missing={missing}, unlisted={unlisted}")

    def verify(item: tuple[str, Path]):
        relative, path = item
        return relative, sha256_file(path)

    mismatches = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        for relative, digest in executor.map(verify, actual_paths.items()):
            if digest != expected[relative]:
                mismatches.append(relative)
    if mismatches:
        raise RuntimeError(f"checksum mismatch: {mismatches}")


def count_xml_records(path: Path) -> tuple[int, int]:
    declared = None
    observed = 0
    parser = xml.parsers.expat.ParserCreate()

    def start(name: str, attributes: dict[str, str]) -> None:
        nonlocal declared, observed
        if declared is None:
            if name != "smses" or "count" not in attributes:
                raise RuntimeError(f"unexpected XML root in {path}")
            declared = int(attributes["count"])
        elif name in {"sms", "mms"}:
            observed += 1

    parser.StartElementHandler = start
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            parser.Parse(chunk, False)
    parser.Parse(b"", True)
    return int(declared), observed


def verify_bluebubbles(path: Path) -> tuple[int, int]:
    with path.open("rb") as stream:
        json_length = struct.unpack(">I", stream.read(4))[0]
        document = json.loads(stream.read(json_length))
        blobs = 0
        while header := stream.read(4):
            if len(header) != 4:
                raise RuntimeError(f"truncated blob header in {path}")
            length = struct.unpack(">I", header)[0]
            if len(stream.read(length)) != length:
                raise RuntimeError(f"truncated blob in {path}")
            blobs += 1
    ids = [int(att["bytes_id"]) for att in document.get("atts", []) if att.get("bytes_id") is not None]
    if ids and max(ids) >= blobs:
        raise RuntimeError(f"attachment bytes_id exceeds blob count in {path}")
    return len(document.get("messages", [])), blobs


def verify_archive(archive: Path, workers: int, deep: bool) -> None:
    require_archive(archive)
    verify_manifest(archive, workers)
    sqlite_quick_check(archive / "apple/messages/chat.db")
    if deep:
        for relative, kind in PRIMARY_SOURCES:
            path = archive / relative
            if kind == "sms-backup-restore-xml":
                declared, observed = count_xml_records(path)
                if declared != observed:
                    raise RuntimeError(
                        f"XML count mismatch in {relative}: declared {declared}, observed {observed}"
                    )
            elif kind == "bluebubbles-length-prefixed-container":
                verify_bluebubbles(path)
    print(f"archive verification passed ({'deep' if deep else 'checksums + SQLite'})")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    result.add_argument("--workers", type=int, default=1)
    commands = result.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync", help="archive the rolling Mac database and new assets")
    sync.add_argument("--work", type=Path, default=DEFAULT_WORK)
    commands.add_parser("refresh", help="refresh cached checksums, inventory, and extracted schema")
    verify = commands.add_parser("verify", help="verify every manifest checksum and SQLite")
    verify.add_argument("--deep", action="store_true", help="also parse every XML record and blob")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "sync":
            sync_live(args.archive, args.work, args.workers)
        elif args.command == "refresh":
            refresh_metadata(args.archive, args.workers)
        elif args.command == "verify":
            verify_archive(args.archive, args.workers, args.deep)
    except (OSError, RuntimeError, sqlite3.Error, subprocess.SubprocessError, ValueError) as error:
        print(f"source archive error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
