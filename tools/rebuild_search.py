#!/usr/bin/env python3
"""Rebuild the message_search FTS5 index over every message in the archive.

Mirrors the index built by tools/import_archive.py so incremental importers
(import_imessage.py, import_bluebubbles.py) can refresh search after adding rows.
"""
import argparse, sqlite3, time

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("database")
    a = ap.parse_args()
    db = sqlite3.connect(a.database)
    t0 = time.time()
    n = db.execute("select count(*) from messages").fetchone()[0]
    print(f"indexing {n:,} messages ...", flush=True)
    db.execute("DROP TABLE IF EXISTS message_search")
    # remove any orphaned FTS5 shadow tables left by an interrupted build
    for (t,) in db.execute(
            "select name from sqlite_master where type='table' and name like 'message_search%'").fetchall():
        db.execute(f'DROP TABLE IF EXISTS "{t}"')
    db.execute("""CREATE VIRTUAL TABLE message_search USING fts5(
                    body, subject, participant_names, participant_addresses,
                    tokenize='unicode61 remove_diacritics 2')""")
    db.execute("""INSERT INTO message_search(
                    rowid,body,subject,participant_names,participant_addresses)
                  SELECT m.id,coalesce(m.body,''),coalesce(m.subject,''),
                    coalesce(group_concat(DISTINCT pn.name),''),
                    coalesce(group_concat(DISTINCT p.display_address),'')
                  FROM messages m
                  LEFT JOIN message_participants mp ON mp.message_id=m.id
                  LEFT JOIN participants p ON p.id=mp.participant_id
                  LEFT JOIN participant_names pn ON pn.participant_id=p.id
                  GROUP BY m.id""")
    db.commit()
    got = db.execute("select count(*) from message_search").fetchone()[0]
    print(f"indexed {got:,} rows in {time.time()-t0:.1f}s")
    if got != n:
        raise SystemExit(f"FTS row count {got} != messages {n}")

if __name__ == "__main__":
    main()
