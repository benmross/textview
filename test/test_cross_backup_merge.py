import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from merge_cross_backup_duplicates import merge  # noqa: E402


SCHEMA = """
CREATE TABLE messages (
  id INTEGER PRIMARY KEY, timestamp_ms INTEGER, direction TEXT, subject TEXT,
  body TEXT, transport TEXT, source_occurrences INTEGER
);
CREATE TABLE participants (
  id INTEGER PRIMARY KEY, canonical_address TEXT, is_self INTEGER
);
CREATE TABLE message_participants (
  message_id INTEGER, participant_id INTEGER, role TEXT,
  source_address_type INTEGER,
  UNIQUE(message_id,participant_id,role,source_address_type)
);
CREATE TABLE media (id INTEGER PRIMARY KEY, content_sha256 TEXT);
CREATE TABLE message_parts (
  id INTEGER PRIMARY KEY, message_id INTEGER, sequence INTEGER,
  content_type TEXT, text_content TEXT, media_id INTEGER
);
CREATE TABLE message_sources (
  id INTEGER PRIMARY KEY, message_id INTEGER, backup_id INTEGER,
  source_android_id TEXT
);
CREATE TABLE reaction_events (
  reaction_message_id INTEGER UNIQUE, target_message_id INTEGER
);
"""


class CrossBackupMergeTest(unittest.TestCase):
    def database(self):
        database = sqlite3.connect(":memory:")
        database.executescript(SCHEMA)
        database.execute("INSERT INTO participants VALUES(1,'tel:1',0)")
        for message_id in range(1, 5):
            database.execute(
                "INSERT INTO messages VALUES(?,1000,'incoming',NULL,'YUM','mms',1)",
                (message_id,),
            )
            database.execute(
                "INSERT INTO message_participants VALUES(?,1,'from',137)",
                (message_id,),
            )
            database.execute(
                "INSERT INTO message_parts VALUES(?, ?,0,'text/plain','YUM',NULL)",
                (message_id, message_id),
            )
        # Two provider records occur in both backup 4 and backup 5.
        for message_id, backup_id, android_id in (
            (1, 4, "87366"), (2, 4, "87367"),
            (3, 5, "87366"), (4, 5, "87367"),
        ):
            database.execute(
                "INSERT INTO message_sources(message_id,backup_id,source_android_id) VALUES(?,?,?)",
                (message_id, backup_id, android_id),
            )
        return database

    def test_complex_group_pairs_cross_backup_rows_by_android_id(self):
        database = self.database()
        result = merge(database)
        self.assertEqual(result["cross_backup_messages_merged"], 2)
        self.assertEqual(database.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
        self.assertEqual(
            database.execute(
                "SELECT group_concat(source_android_id),COUNT(*) FROM message_sources "
                "GROUP BY message_id ORDER BY message_id"
            ).fetchall(),
            [("87366,87366", 2), ("87367,87367", 2)],
        )


if __name__ == "__main__":
    unittest.main()
