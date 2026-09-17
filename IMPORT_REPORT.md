# Import and Coverage Report

Last rebuilt and validated: **2026-08-23**.

## Sources

The canonical archive contains five nonredundant primary sources:

| Source | Records | Coverage |
|---|---:|---|
| `sms-20250212171642.xml` | 377,015 | 2021-08-07–2025-02-12 |
| `sms-20250619153114.xml` | 111,991 | 2021-08-07–2022-12-25 |
| `sms-20260610030051.xml` | 118,986 | 2025-02-13–2026-06-09 |
| `chat.db-macmini` | 89,232 | 2026-01-30–live |
| `BlueBubbles-chats-2026822_2117.json` | 56,377 | 2026-01-30–2026-06-14 |

Every source contributes logical messages found in no other retained source:
271,351; 35,391; 118,942; 33,595; and 740 respectively. The three XMLs also
contribute 5,855, 4,763, and 4,076 source-exclusive media payloads. No primary
source or attachment tree is a subset of another.

## Result

- Source records: **753,601**
- Logical messages: **592,242**
- SMS: **146,905**; MMS: **305,669**; RCS: **49,828**; iMessage: **89,840**
- Message parts: **667,009**; parts carrying media: **18,313**
- Unique stored media: **16,998** (**21,337,889,956 bytes**)
- Participants: **633**
- Reactions: **16,164**; native replies: **27,704**
- Stickers: **168**; edit revisions: **1,273**; effects: **30**; app cards: **455**
- Coverage: **2021-08-07–live**

The database retains every source occurrence in provenance tables while
deduplicating overlapping logical messages and attachment content.

## Verification

Confidence comes from independent checks at each layer:

1. `MANIFEST.sha256` verifies every archived source and attachment file.
2. The importers parsed the exact declared row count of all five sources.
3. The clean rebuild plus immediate Mac sync preserved all **753,601** source
   rows and matched every declared source count.
4. Full SQLite integrity, foreign keys, JSON, provenance counters, search
   coverage, sticker relationships, BLOB lengths, and content hashes passed.
5. All stored media have prebuilt browser derivatives; request handling never
   generates thumbnails or previews.

`validation.json` and `attachment-audit.json` contain the machine-readable
results. `tools/finalize_archive.py --deep` and `tools/audit_attachments.py`
reproduce them.

## Fidelity and unavailable data

The importers normalize replies, reactions, edits, announcements, app cards,
effects, and standalone or positioned stickers. They retain opaque source
metadata so future decoders can improve presentation without re-exporting.
TextView does not reproduce arbitrary third-party app interfaces or Apple's
screen animations; polls are display-only.

There are **114** iMessage attachment records without bytes: 65 internal
`.pluginPayloadAttachment` records and 49 photos, videos, or audio files no
available source retains. Five of the 49 have conflicting same-name fallback
candidates; they remain unavailable rather than displaying guessed content.

No available source contains messages during **2024-03-27–2024-04-11**. The
other empty days in the covered range are 2021-08-18, 2021-08-20, 2021-11-20,
2023-07-14, and 2026-07-26. No available iMessage source predates 2026-01-30.

## Rebuild and ongoing import

`tools/rebuild_full.sh` is the authoritative pipeline. Its important ordering
constraints are: merge Android cross-backup duplicates before reaction
normalization; normalize Android reactions before importing iMessage sources;
and annotate media, rebuild search, and prebuild derivatives only after all
sources and attachment stores are loaded.

The hourly Mac job checkpoints `chat.db`, archives a GUID-superset snapshot and
new files, imports new records idempotently, repairs previously unavailable
attachments when bytes arrive, and atomically publishes rebuilt indexes only
after derivative generation succeeds.
