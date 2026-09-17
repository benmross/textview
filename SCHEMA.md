# Unified Message Database Schema

`messages.db` is TextView's self-contained source of truth, generated from three
SMS Backup & Restore XML exports, a macOS `chat.db`, and one
BlueBubbles/OpenBubbles export container. It contains normalized
messages, participants, full source provenance, a full-text search index, and
decoded attachment bytes. No external attachment directory is required.

## Design principles

- Unix timestamps are stored as integer milliseconds in UTC. Rendering code
  should apply the viewer's local time zone.
- `messages` is the clean logical view used by an application.
- `message_sources`, `source_addresses`, and `source_parts` preserve every XML
  occurrence and every original metadata field.
- Binary data is decoded from base64 and stored once in `media`. Repeated
  attachment content points to the same row.
- Phone numbers are normalized for identity matching while original spellings
  remain in provenance JSON and `display_address`.
- The owner has two equivalent representations: the SMS sentinel `__self__`
  and the confirmed number `tel:5555550100`. Both have `is_self = 1`.
- JSON metadata contains the complete set of source attributes present on its
  corresponding XML element, except a part's enormous `data` attribute. That
  payload is represented losslessly by its `media_id`.

## Relationship overview

```text
backups ──< message_sources >── messages ──< message_participants >── participants
                    │                │                                  │
                    ├──< source_parts│                                  └──< participant_names
                    │        │       └──< message_parts
                    │        └──────────────┐        │
                    └──< source_addresses   │        └── media
                                           └─────────── media

messages ──1:1 derived search document── message_search
message_sources ──< message_protocol_metadata
messages ──< reaction_events >── messages (target)
messages ──< sticker_events >── messages (target)
messages ──< message_edits
messages ──1:1 message_effects
messages ──1:1 app_messages
```

## Tables

### `archive_info`

Small archive-wide key/value settings.

| Column | Meaning |
|---|---|
| `key` | Setting name. |
| `value` | String value. |

Current keys include `schema_version`, `timestamps`, and
`owner_phone_e164`.

### `backups`

One row per imported source. Despite the name this covers every source kind,
not only XML: `backup_type` is `full` for SMS Backup & Restore XML,
`imessage-chatdb` for a macOS `chat.db`, and `bluebubbles-export` for a
BlueBubbles/OpenBubbles container. For non-XML sources `declared_count` is the
source's own message count and `root_metadata_json` records its provenance.

| Column | Meaning |
|---|---|
| `id` | Internal backup ID. |
| `filename` | Original basename. |
| `file_size` | Complete source size in bytes. |
| `file_sha256` | SHA-256 of the complete source. |
| `backup_set` | SMS Backup & Restore backup-set UUID. |
| `backup_date_ms` | Backup creation time. |
| `declared_count` | Root `smses.count`; checked against imported records. |
| `backup_type` | Root backup type, currently `full`. |
| `first_message_ms`, `last_message_ms` | Source coverage boundaries. |
| `imported_at` | UTC import time in ISO 8601 form. |
| `root_metadata_json` | Every root `<smses>` attribute. |

### `messages`

One row per consolidated logical message.

| Column | Meaning |
|---|---|
| `id` | Stable internal row ID for this build. |
| `dedup_key` | SHA-256 key used to merge restored/overlapping records. |
| `timestamp_ms` | Primary message timestamp. |
| `sent_timestamp_ms` | Source send timestamp when supplied. |
| `direction` | `incoming`, `outgoing`, `draft`, `outbox`, `failed`, `queued`, or `unknown`. |
| `transport` | `sms`, `mms`, positively identified `rcs`, or `imessage`. |
| `is_group` | True when more than one non-owner participant is present. |
| `subject` | Human-visible subject, excluding encoded RCS protocol markers. |
| `body` | SMS body or merged textual MMS/RCS parts. |
| `read` | Android read flag when supplied. |
| `status` | Original provider status or presentation state. `hidden` preserves source-only bookkeeping/tombstone rows without rendering empty messages. Other normalized states include `announcement`, `app`, and `edited`. |
| `source_occurrences` | Number of records across every imported source consolidated into this message. It must equal the related `message_sources` count. |

iMessage rows are classified from `chat.db`'s `service` column: `iMessage`
(and `iMessageLite`) become `imessage`, while messages Apple relayed as `SMS`
or `RCS` keep those transports. Their `dedup_key` is
`sha256("imessage:" || guid)`, so the same message arriving from both the Mac
and a BlueBubbles export consolidates into one row.

Transport detection for Android sources is conservative. An SMS `subject` beginning with
`proto:` or any element containing `rcs_*` attributes is classified as RCS.
When an exact message representation appeared as RCS in one backup and MMS
after restoration in another, it was consolidated as one logical RCS message.
Other records serialized indistinguishably from MMS remain labeled `mms`
rather than being guessed. Their text, participants, attachments, and raw
fields are still fully preserved.

### `participants`

Normalized identities.

| Column | Meaning |
|---|---|
| `canonical_address` | `tel:` plus normalized digits, `addr:` plus a case-folded non-phone address, or `__self__`. |
| `display_address` | A representative original address. |
| `is_self` | Owner identity flag. |

North American `+1XXXXXXXXXX` and `XXXXXXXXXX` forms normalize to the same
participant.

### `participant_names`

All contact-name variants seen for a participant, with occurrence counts.
Names are intentionally not collapsed to one value.

### `message_participants`

Participants in a logical message.

| Column | Meaning |
|---|---|
| `message_id`, `participant_id` | Relationship endpoints. |
| `role` | `from`, `to`, `cc`, `bcc`, or `other`. |
| `source_address_type` | Original MMS address code where relevant: 137 From, 151 To, 130 CC, or 129 BCC. |

### `message_parts`

The clean set of parts belonging to a logical MMS/RCS message.

| Column | Meaning |
|---|---|
| `sequence` | Source part order. |
| `content_type` | Original MIME type. Preserved verbatim from Android sources; for iMessage parts, `chat.db` frequently leaves `mime_type` NULL, in which case the type is inferred from the file extension so the UI can pick a renderer. |
| `filename` | Best available `name`, `fn`, or content-location filename. |
| `content_location`, `content_id` | Original MIME addressing fields. |
| `charset` | Original Android charset code. |
| `text_content` | Inline textual content, including SMIL where present. |
| `media_id` | Decoded binary payload, if the part had base64 data. |
| `metadata_json` | Every non-data `<part>` attribute. |

SMIL layout parts are retained but excluded from the assembled `messages.body`.
No MIME type is discarded.

### `media`

Content-addressed attachment storage.

| Column | Meaning |
|---|---|
| `encoded_sha256` | SHA-256 of the canonical source base64. Used for fast deduplication before decoding. |
| `content_sha256` | Unique SHA-256 of decoded bytes; this is the cross-source content identity. |
| `byte_size` | Decoded byte count. |
| `data` | Complete decoded attachment BLOB. |
| `detected_content_type` | Container type independently inferred from the bytes, when recognizable. |
| `validation_status` | `signature-verified`, `hash-verified`, or a recorded source-container warning. |

The detected type does not overwrite the source MIME in `message_parts` or
`source_parts`. The current audit records 43 attachments whose source MIME
label does not match their actual bytes; TextView renders from the detected
container while preserving the inaccurate source label for provenance.
`media_metadata_idx` covers metadata-only reads so derivative and app-index
builds never traverse BLOB pages.

### `message_sources`

One row for every top-level `<sms>` or `<mms>` record in every backup, even
when several rows resolve to one logical message.

| Column | Meaning |
|---|---|
| `message_id` | Consolidated logical message. |
| `backup_id` | Source XML. |
| `source_ordinal` | One-based top-level order within the XML. |
| `source_kind` | Literal source element: `sms`, `mms`, or `imessage`. |
| `source_android_id` | Original MMS `_id`, where supplied. |
| `metadata_json` | Every attribute of the original top-level element. |

This is where service-center values, SIM/subscription IDs, read/seen/locked
flags, delivery state, MMS protocol fields, Android provider IDs, encoded
`proto:` fields, and all `rcs_*` fields are preserved.

`message_sources_message_idx` supports logical-message-to-provenance lookups
and source-occurrence consistency audits. Import migrations must preserve it
when replacing `message_sources`.

### `message_protocol_metadata`

Normalized Google Messages metadata decoded from `proto:` source attributes.
The original base64 remains in `message_sources.metadata_json`.

| Column | Meaning |
|---|---|
| `message_source_id` | Exact source occurrence carrying the protobuf. |
| `source_attribute` | `subject` or `tr_id`. |
| `package_name` | Protocol namespace, normally `com.google.android.apps.messaging.` |
| `message_uuid` | Stable UUID carried by the message envelope, when present. |
| `conversation_uuid` | Stable conversation UUID, when present. |
| `remote_message_id` | Carrier/RCS message identifier such as an `Mx...` token or UUID. |
| `transaction_id` | MMS-style transaction ID where that variant supplies one. |
| `group_id`, `group_title`, `group_uri` | Decoded group-conversation identity. |
| `embedded_caption` | Caption from bodyless media-message variants. |
| `decoded_json` | Lossless wire-level field representation for future analysis. |

All 222,725 `proto:` values decoded successfully. The stable IDs are indexed
for future relationship resolution. No field in these backups could be
defensibly identified as a reply target. The database therefore does not
invent reply relationships.

### `reaction_events`

Normalized reaction fallback messages.

| Column | Meaning |
|---|---|
| `reaction_message_id` | Original fallback message such as `Liked “…”`. |
| `target_message_id` | Resolved earlier message, or null when ambiguous. |
| `reactor_participant_id` | Participant who sent the reaction. |
| `reaction` | Named reaction, emoji, or `photomoji`. |
| `target_excerpt` | Quoted target text when the fallback included it. |
| `target_kind` | `text`, `image`, `video`, `audio`, or `attachment`. |
| `resolution_method` | Evidence used to select the target. |
| `resolution_confidence` | `exact`, `high`, or `unresolved`. |

Only messages with the identical distinct non-owner participant set are
considered. Algorithmic matching considers earlier messages only; explicitly
recorded owner decisions may link known out-of-order deliveries.
`exact` means the quoted text identifies one target or the owner confirmed it.
`high` means the nearest of repeated exact text matches, or the sole compatible
recent media message. Unresolved events remain available without a guessed
target.

Both SMS Backup & Restore fallback families are normalized:

- English forms such as `Loved “…”` and `Reacted ❤️ to “…”`;
- direct Google Messages emoji forms such as `❤️ to “…”`, including their
  invisible Unicode wrapper characters.

Edit fallbacks such as `Edited to “…”` are explicitly excluded.

### `message_replies`

Normalized native iMessage reply relationships. `message_id` is the reply,
`target_message_id` is its resolved target when that GUID exists in the
archive, and `target_guid`/`target_part` preserve Apple's original reference.
`resolution_confidence` is `exact` or `unresolved`; TextView never guesses a
reply target from proximity or text.

### `sticker_events`

Normalized native iMessage stickers. `sticker_message_id` is the Apple source
record; `target_message_id` is null for a standalone sticker and the exact
associated GUID target for placed stickers and stickerbacks. `part_id` and
`media_id` identify the retained image. `position_x`, `position_y`, `scale`,
`rotation`, and `parent_width` preserve Apple's placement plist when present.
`source_attachment_id` keeps repeated imports idempotent, while Mac metadata
supersedes an overlapping BlueBubbles record because the latter does not
export the transform plist. `is_animated` identifies HEIC sequences that must
be converted with all frames and their separate alpha track.

The importer synchronizes this table across the complete source on every run,
not only newly inserted messages. Target, part, and media relationships are
validated by `finalize_archive.py`.

### `message_edits`

Available native iMessage edit events, ordered per original message part.
`timestamp_ms` is the time of that revision and `body` is the text decoded
from Apple's attributed-string event. Fully retracted messages that contain no
edit content remain normalized announcements rather than invented history.

### `message_effects`

One Apple expressive-send identifier per message. The UI maps known bubble and
screen effects to readable labels while retaining the original identifier.

### `app_messages`

Safe display fields decoded from Apple/BlueBubbles app-balloon archives:
bundle ID, app name, title, subtitle, and an optional HTTP(S) link.
`metadata_json` contains only bounded presentation facts. Opaque app state,
embedded images, data URLs, and private custom-scheme payloads are neither
serialized to the API nor treated as links.

### `dedup_decisions`

Human-adjudicated archive decisions. It records the eight confirmed
`UNKNOWN_SENDER` merges, the owner's explanation for exact RCS/MMS
cross-backup pairs, and acceptance of the earlier reviewed English-format
unresolved reactions. Newly discovered unresolved emoji-format fallbacks are
not covered by that acceptance decision.

### `source_addresses`

Every original `<addr>` child, in source order. It preserves raw attributes,
its normalized participant link, and interpreted role.
`source_addresses_participant_idx` supports participant integrity checks and
future provenance inspection without scanning all source addresses.

### `source_parts`

Every original `<part>` child, in source order. Its JSON retains every
non-data attribute and `media_id` replaces the base64 payload losslessly.
Unlike `message_parts`, this table deliberately retains redundant parts from
overlapping/restored records.
`source_parts_media_idx` supports media-reference validation and safe
content-address consolidation.

### `message_search`

An FTS5 index with one row per `messages.id` (`rowid`). Indexed fields are:

- message body;
- subject;
- all known participant/contact names;
- participant display addresses.

Example:

```sql
SELECT m.*
FROM message_search s
JOIN messages m ON m.id = s.rowid
WHERE message_search MATCH 'vacation photos'
ORDER BY m.timestamp_ms DESC;
```

### `import_stats`

Materialized audit counts such as source and logical messages, duplicates,
transports, groups, and unique media bytes.

## Deduplication

The archive uses message semantics rather than source database row IDs:

- SMS records match on millisecond timestamp, direction, subject, and body.
  This handles restored rows whose valid address became `UNKNOWN_SENDER`.
- MMS/RCS records preferentially match on timestamp, direction, and transport
  transaction ID; otherwise Message-ID is used.
- Records without a stable transport identifier use timestamp, direction,
  normalized participants, subject/body, ordered part MIME types, text, and
  attachment hashes.
- Duplicate representations merge participants, useful text, and unique
  parts. Every representation remains independently inspectable through the
  source tables.

## Rebuilding

Use `tools/rebuild_full.sh`, which runs every stage in the correct order and
builds to `messages-rebuilt.db` without disturbing the live archive. The order
is load-bearing and is explained in `IMPORT_REPORT.md`; in particular
`normalize_protocol.py` begins with `DELETE FROM reaction_events`, so it must
run before the iMessage importers, and it requires cross-backup MMS/RCS twins
to have been merged first.

```bash
./tools/rebuild_full.sh
# verify, then:
mv messages.db messages.db.prev && mv messages-rebuilt.db messages.db
npm run index
```

Incremental iMessage imports do not require a rebuild:
`tools/import_imessage.py` and `tools/import_bluebubbles.py` are idempotent
(keyed on message GUID), and `tools/rebuild_search.py` refreshes the FTS index
afterwards. `tools/sync_imessage.sh` does this hourly under systemd.

Every importer records its source's declared/current row count. Run
`python3 tools/finalize_archive.py --deep` to regenerate `validation.json` and
verify that all five source counts match, every foreign key and JSON value is valid,
the FTS index covers every logical message, provenance counters agree, and
every stored attachment hashes to its recorded content identity.
`IMPORT_REPORT.md` describes coverage and the limits of the available sources.
