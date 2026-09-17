import json
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from source_archive import (  # noqa: E402
    count_xml_records,
    publish_chat_database,
    verify_bluebubbles,
)


def make_chat_database(path, guids):
    database = sqlite3.connect(path)
    database.execute("CREATE TABLE message (guid TEXT)")
    database.executemany("INSERT INTO message(guid) VALUES(?)", [(guid,) for guid in guids])
    database.commit()
    database.close()


class SourceArchiveTest(unittest.TestCase):
    def test_database_snapshots_retain_only_non_subset_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            live = root / "live.db"
            current = root / "archive" / "chat.db"

            make_chat_database(live, ["A", "B"])
            result = publish_chat_database(live, current)
            self.assertTrue(result["changed"])
            self.assertFalse(result["retained_previous"])

            live.unlink()
            make_chat_database(live, ["A", "B", "C"])
            result = publish_chat_database(live, current)
            self.assertEqual(result["previous_only_guids"], 0)
            self.assertEqual(list((current.parent / "historical").glob("*.db")), [])

            live.unlink()
            make_chat_database(live, ["B", "C"])
            result = publish_chat_database(live, current)
            self.assertEqual(result["previous_only_guids"], 1)
            self.assertTrue(result["retained_previous"])
            self.assertEqual(len(list((current.parent / "historical").glob("*.db"))), 1)

            live.unlink()
            make_chat_database(live, ["A", "B", "C", "D"])
            result = publish_chat_database(live, current)
            self.assertEqual(len(result["pruned_subset_checkpoints"]), 1)
            self.assertEqual(list((current.parent / "historical").glob("*.db")), [])

    def test_xml_declared_count_is_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "messages.xml"
            path.write_text(
                '<smses count="2"><sms date="1"/><mms date="2"/></smses>',
                encoding="utf-8",
            )
            self.assertEqual(count_xml_records(path), (2, 2))

    def test_bluebubbles_blob_ids_must_resolve(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "export.bin"
            document = {"messages": [{"guid": "A"}], "chats": [], "atts": [{"bytes_id": 0}]}
            encoded = json.dumps(document).encode()
            blob = b"attachment"
            path.write_bytes(
                struct.pack(">I", len(encoded))
                + encoded
                + struct.pack(">I", len(blob))
                + blob
            )
            self.assertEqual(verify_bluebubbles(path), (1, 1))


if __name__ == "__main__":
    unittest.main()
