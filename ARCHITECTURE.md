# TextView Architecture

TextView is a unified, read-only message viewer. It presents every available
SMS, MMS, RCS, and iMessage source through one normalized model while retaining
the original source occurrences for auditability.

## Data flow

```text
3 Android XML exports ─┐
Mac Messages chat.db ──┼─> messages.db ─> textview-index.db ─> server.mjs ─> PWA
1 BlueBubbles export ──┘        │                 │
attachment fallback stores ─────┘                 └─> prebuilt media cache
```

Those are the five nonredundant inputs in `/mnt/Backups/message-sources`.
Earlier overlapping snapshots can remain as provenance in an existing build,
but a clean rebuild omits any snapshot proven to contribute no message identity
or attachment hash.

`messages.db` is the source of truth. It owns normalized messages, complete
source provenance, reactions/replies, stickers, edit histories, app balloons,
effects, FTS, and original attachment bytes.
`textview-index.db` is disposable: it denormalizes conversation membership and
attachment metadata for low-latency API queries. `data/` is also derivative;
its filenames are attachment content hashes, so URLs are immutable.

The Node process opens both databases read-only through
`lib/database-snapshot.mjs`. A newly built app index is published with
`os.replace`; the server notices the new file generation and switches both
SQLite views together between requests.

## Import invariants

- Every source record gets exactly one `message_sources` row. The unique
  `(backup_id, source_ordinal)` key makes incremental imports idempotent.
- `backups.declared_count` equals the imported `message_sources` count for
  every source. For the live Mac source it is refreshed from the current
  `chat.db` on every sync.
- Every logical message has at least one source. `messages.source_occurrences`
  is a materialized count of those rows, not merely the number of XML copies.
- Deduplication changes presentation identity, never provenance: overlapping
  source records remain independently inspectable.
- `media.content_sha256` is unique. Byte-identical payloads from different
  source encodings share one BLOB, while the source part metadata is retained.
- Attachment recovery uses the exact source path first. Basename fallbacks
  must be unique by recorded size or byte hash; ambiguous candidates remain
  unavailable instead of being guessed.
- Contentless provider bookkeeping and Apple tombstones remain in the source
  tables with `status='hidden'`; they never become empty timeline bubbles.
- Apple placement plists, edit summaries, app archives, and effects are
  normalized source-wide on every import. A schema upgrade therefore repairs
  existing rows as well as covering every future message.
- Import metadata columns contain valid JSON, foreign keys are enabled during
  validation, and FTS contains one row per logical message.

`tools/finalize_archive.py --deep` enforces these rules and recomputes every
attachment hash. Add a new invariant there whenever a future bug reveals a
state that the schema alone cannot reject.

## Request and memory model

Conversation and attachment lists use indexed keyset pagination; they never
use deep SQL offsets. Message hydration is limited to at most 100 IDs per
request. Common index orderings are documented in `SCHEMA.md` and mirrored in
the schema inside `tools/build_app_index.py`.

The gallery keeps all fetched metadata so its carousel remains coherent, but
renders a moving window of at most 360 tiles. An `IntersectionObserver` assigns
thumbnail URLs only near the viewport. This bounds DOM nodes, decoded images,
and concurrent HTTP work even after thousands of attachments have been
fetched.

Original BLOBs are streamed with SQLite's incremental BLOB API and support
HTTP byte ranges. The server never loads a large attachment into Node memory.
Search terms are bounded and one- or two-character prefixes are kept exact to
avoid pathological FTS expansion. systemd supplies an additional process
memory limit and a one-minute health check.

## Media contract

Media generation belongs to imports and maintenance, never request handling:

- every image and video gets a 480 px WebP gallery/thread thumbnail;
- HEIC, HEIF, and AVIF images get a 2048 px WebP fullscreen preview for
  browsers such as Firefox;
- every audio attachment, including CAF and AMR, gets an Opus/WebM copy;
- every sticker gets a transparent 600 px WebP; HEIC sequences retain their
  animation by combining Apple's color and alpha tracks;
- originals remain unchanged and downloadable from `messages.db`;
- a derivative failure prevents a staged app index from being published.

`server.mjs` returns 404 when a derivative is unexpectedly absent. It does not
spawn FFmpeg, ImageMagick, or Python in response to scrolling. This separation
is deliberate: it keeps request latency and memory predictable. Cache pruning
is explicit (`tools/build_thumbnails.py --prune`) and serialized by
`data/.derivatives.lock`. Media metadata and derivative discovery use a
covering index and never scan stored BLOBs.

## Client state rules

- A conversation jump requests a page around the target and carries both
  older and newer cursors; either direction can continue paginating.
- Gallery pages use `(timestamp_ms, part_id)` cursors. A tile, its viewer item,
  and its thumbnail URL all come from the same immutable part record.
- Audio uses the same browser-compatible endpoint in conversation and
  fullscreen views. Gallery audio tiles use a music icon rather than trying to
  decode an audio stream as an image.
- Sticker tapbacks attach to their exact target GUID. Positioned stickers use
  Apple's x/y/scale/rotation metadata; stickerbacks use a compact target
  decoration, and standalone stickers remain gallery-addressable.
- Service-worker caching is shell-only. APIs, originals, and derivatives are
  governed by their HTTP cache headers, preventing stale JSON from mixing with
  a new database generation.

## Change checklist

When adding a source or message type:

1. preserve the complete source metadata and a stable source ordinal;
2. define deduplication evidence conservatively;
3. decide whether the record has visible text, an announcement, media, or is a
   non-displayable source artifact;
4. add indexes for every new foreign-key lookup and pagination ordering;
5. update both the full rebuild and incremental path;
6. rebuild the app index and all required derivatives before publication;
7. run the checks in `OPERATIONS.md` and update `IMPORT_REPORT.md`.
