#!/usr/bin/env python3
"""Fill in attachment bytes that the message sources referenced but did not carry.

Candidate files are supplied as one or more search roots (and optionally the
Immich library). Basename matches are narrowed by the source byte count. If
several remain, they are accepted only when their bytes are identical.
"""
from __future__ import annotations
import argparse, hashlib, json, os, sqlite3, sys

from attachment_resolution import choose_fallback

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("database")
    ap.add_argument("--root", action="append", default=[], help="directory to search (repeatable)")
    ap.add_argument("--allow-size-mismatch", action="store_true",
                    help="accept a name match even when the recorded size differs (use with care)")
    ap.add_argument("--immich-map", default=None,
                    help="resolve candidates through Immich, e.g. /usr/src/app/upload=/mnt/medialibrary/photos")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    db = sqlite3.connect(a.database)
    missing = db.execute(
        """select mp.id, mp.filename, mp.metadata_json
             from message_parts mp join messages m on m.id=mp.message_id
            where mp.media_id is null and mp.filename is not null
              and m.transport='imessage'""").fetchall()
    want = {}
    for pid, fn, mj in missing:
        ext = (fn.rsplit(".", 1)[-1] if "." in fn else "").lower()
        if ext not in ("heic","heics","jpeg","jpg","png","gif","webp","tiff","mov","mp4","m4v","avi","caf","m4a"):
            continue
        try:
            j = json.loads(mj or "{}")
            size = j.get("total_bytes") or j.get("totalBytes") or 0
        except Exception:
            size = 0
        want.setdefault(fn.casefold(), []).append((pid, int(size or 0)))
    print(f"attachment rows needing bytes: {sum(len(v) for v in want.values())} "
          f"({len(want)} distinct filenames)")

    index = {}
    for root in a.root:
        for dirpath, _d, files in os.walk(root):
            for f in files:
                k = f.casefold()
                if k in want:
                    index.setdefault(k, []).append(os.path.join(dirpath, f))
    print(f"candidate files located: {sum(len(v) for v in index.values())} for {len(index)} names")

    # Immich stores originals under UUID filenames, so basename search cannot find
    # them; resolve through its database and translate the container path instead.
    if a.immich_map:
        import subprocess
        cpath, hpath = a.immich_map.split("=", 1)
        names = list(want.keys())
        out = subprocess.run(
            ["docker","exec","immich_postgres","psql","-U","postgres","-d","immich","-tAF","\t","-c",
             "select lower(\"originalFileName\"), \"originalPath\" from asset where \"deletedAt\" is null"],
            capture_output=True, text=True, timeout=180)
        found = 0
        for line in out.stdout.splitlines():
            if "\t" not in line: continue
            nm, op = line.split("\t", 1)
            nm = nm.strip().casefold()
            if nm in want:
                local = op.strip().replace(cpath, hpath, 1)
                if os.path.exists(local):
                    index.setdefault(nm, []).append(local)
                    found += 1
        print(f"Immich supplied {found} candidate file(s)")

    filled = skipped = ambiguous = 0
    hash_cache = {}
    for name, rows in want.items():
        cands = index.get(name)
        if not cands: continue
        for pid, size in rows:
            chosen, reason = choose_fallback(
                cands, size, allow_size_mismatch=a.allow_size_mismatch,
                hash_cache=hash_cache,
            )
            if chosen is None:
                skipped += 1
                ambiguous += reason == "ambiguous"
                continue
            if a.dry_run:
                filled += 1
                continue
            with open(chosen, "rb") as fh: data = fh.read()
            if not data:
                skipped += 1
                continue
            sha = hashlib.sha256(data).hexdigest()
            r = db.execute(
                "select id from media where encoded_sha256=? or content_sha256=? "
                "order by id limit 1", (sha, sha)
            ).fetchone()
            mid = r[0] if r else db.execute(
                "insert into media(encoded_sha256,content_sha256,byte_size,data,"
                "detected_content_type,validation_status) values(?,?,?,?,?,?)",
                (sha, sha, len(data), data, None, "unannotated")).lastrowid
            db.execute("update message_parts set media_id=? where id=?", (mid, pid))
            filled += 1
    if not a.dry_run: db.commit()
    print(json.dumps({"filled": filled, "skipped": skipped, "ambiguous": ambiguous}, indent=2))

if __name__ == "__main__":
    main()
