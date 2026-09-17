#!/usr/bin/env python3
"""Merge only semantically identical messages originating in different backups."""

from __future__ import annotations

import collections
import hashlib
import json
import sqlite3
from pathlib import Path


AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS dedup_decisions (
  id INTEGER PRIMARY KEY,
  decision_key TEXT NOT NULL UNIQUE,
  decision TEXT NOT NULL,
  rationale TEXT NOT NULL,
  decided_by TEXT NOT NULL,
  decided_at TEXT NOT NULL
);
"""


def semantic_groups(db: sqlite3.Connection) -> list[list[int]]:
    members: dict[int, set[str]] = collections.defaultdict(set)
    for message_id, address in db.execute(
        """SELECT mp.message_id,p.canonical_address
           FROM message_participants mp
           JOIN participants p ON p.id=mp.participant_id
           WHERE p.is_self=0"""
    ):
        members[message_id].add(address)
    parts: dict[int, list[tuple]] = collections.defaultdict(list)
    for row in db.execute(
        """SELECT mp.message_id,mp.sequence,mp.content_type,mp.text_content,
                  me.content_sha256
           FROM message_parts mp
           LEFT JOIN media me ON me.id=mp.media_id
           WHERE coalesce(mp.content_type,'')!='application/smil'
           ORDER BY mp.message_id,mp.sequence,mp.id"""
    ):
        parts[row[0]].append(tuple(row[1:]))
    groups: dict[bytes, list[int]] = collections.defaultdict(list)
    for row in db.execute(
        """SELECT id,timestamp_ms,direction,subject,body
           FROM messages ORDER BY id"""
    ):
        signature = (
            row[1], row[2], row[3], row[4],
            tuple(sorted(members[row[0]])),
            tuple(parts[row[0]]),
        )
        digest = hashlib.sha256(
            json.dumps(
                signature, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).digest()
        groups[digest].append(row[0])
    return [ids for ids in groups.values() if len(ids) > 1]


def merge(db: sqlite3.Connection) -> dict[str, int]:
    db.executescript(AUDIT_SCHEMA)
    groups = semantic_groups(db)
    merged = skipped_same_backup = skipped_complex = 0
    candidate_pairs: list[tuple[int, int]] = []
    for ids in groups:
        if len(ids) == 2:
            candidate_pairs.append((ids[0], ids[1]))
            continue

        # The same provider record can occur in several exports while a source
        # itself contains multiple semantically identical records. Pair those
        # cross-export occurrences by Android provider _id instead of rejecting
        # the entire 3+-row semantic group as ambiguous.
        by_android_id: dict[str, list[int]] = collections.defaultdict(list)
        for message_id in ids:
            android_ids = {
                str(row[0])
                for row in db.execute(
                    """SELECT DISTINCT source_android_id FROM message_sources
                       WHERE message_id=? AND source_android_id IS NOT NULL""",
                    (message_id,),
                )
            }
            if len(android_ids) == 1:
                by_android_id[next(iter(android_ids))].append(message_id)
        found_pair = False
        for matching_ids in by_android_id.values():
            if len(matching_ids) == 2:
                candidate_pairs.append((matching_ids[0], matching_ids[1]))
                found_pair = True
            elif len(matching_ids) > 2:
                skipped_complex += 1
        if not found_pair:
            skipped_complex += 1

    for ids in candidate_pairs:
        backup_sets = [
            {
                row[0] for row in db.execute(
                    """SELECT DISTINCT backup_id FROM message_sources
                       WHERE message_id=?""", (message_id,)
                )
            }
            for message_id in ids
        ]
        # Only merge independent representations from different backups.
        if not backup_sets[0].isdisjoint(backup_sets[1]):
            skipped_same_backup += 1
            continue
        keeper, loser = min(ids), max(ids)
        transports = {
            row[0] for row in db.execute(
                "SELECT transport FROM messages WHERE id IN (?,?)",
                (keeper, loser)
            )
        }
        db.execute(
            """INSERT OR IGNORE INTO message_participants(
                 message_id,participant_id,role,source_address_type
               )
               SELECT ?,participant_id,role,source_address_type
               FROM message_participants WHERE message_id=?""",
            (keeper, loser)
        )
        db.execute(
            "UPDATE message_sources SET message_id=? WHERE message_id=?",
            (keeper, loser)
        )
        # Preserve any derived reaction references until protocol normalization
        # rebuilds the reaction table.
        db.execute(
            "UPDATE reaction_events SET target_message_id=? WHERE target_message_id=?",
            (keeper, loser)
        )
        keeper_reaction = db.execute(
            "SELECT 1 FROM reaction_events WHERE reaction_message_id=?",
            (keeper,)
        ).fetchone()
        loser_reaction = db.execute(
            "SELECT 1 FROM reaction_events WHERE reaction_message_id=?",
            (loser,)
        ).fetchone()
        if loser_reaction:
            if keeper_reaction:
                db.execute(
                    "DELETE FROM reaction_events WHERE reaction_message_id=?",
                    (loser,)
                )
            else:
                db.execute(
                    """UPDATE reaction_events SET reaction_message_id=?
                       WHERE reaction_message_id=?""", (keeper, loser)
                )
        db.execute("DELETE FROM messages WHERE id=?", (loser,))
        db.execute(
            """UPDATE messages SET
                 source_occurrences=(
                   SELECT count(*) FROM message_sources WHERE message_id=?
                 ),
                 transport=CASE WHEN ? THEN 'rcs' ELSE transport END
               WHERE id=?""",
            (keeper, int("rcs" in transports), keeper)
        )
        merged += 1
        if merged % 1000 == 0:
            db.commit()
    db.execute(
        """INSERT OR REPLACE INTO dedup_decisions(
             decision_key,decision,rationale,decided_by,decided_at
           ) VALUES(?,?,?,?,?)""",
        (
            "cross-backup-identical-mms-rcs",
            "merge exact cross-backup representations; preserve RCS transport",
            "Exact timestamp, direction, text, participants, and ordered "
            "attachment hashes matched. In semantic groups containing multiple "
            "same-source twins, matching Android provider IDs pair the same "
            "record across exports. Owner explained that restored RCS messages "
            "appeared as MMS after SMS Backup & Restore.",
            "archive owner",
            "2026-07-27",
        ),
    )
    db.commit()
    return {
        "duplicate_groups_found": len(groups),
        "candidate_pairs": len(candidate_pairs),
        "cross_backup_messages_merged": merged,
        "same_backup_groups_left_separate": skipped_same_backup,
        "complex_groups_left_separate": skipped_complex,
    }


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path, nargs="?", default=Path("messages.db"))
    args = parser.parse_args()
    db = sqlite3.connect(args.database)
    db.execute("PRAGMA foreign_keys=ON")
    try:
        result = merge(db)
        print(json.dumps(result, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
