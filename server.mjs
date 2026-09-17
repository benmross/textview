import { createReadStream, existsSync, mkdirSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { spawn } from "node:child_process";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";
import { DatabaseSnapshot } from "./lib/database-snapshot.mjs";

const root = fileURLToPath(new URL(".", import.meta.url));
const publicDir = join(root, "public");
const dataDir = join(root, "data");
const thumbnailDir = join(dataDir, "thumbnails");
const previewDir = join(dataDir, "previews");
const audioDir = join(dataDir, "audio");
const stickerDir = join(dataDir, "stickers");
const archivePath = process.env.TEXTVIEW_DATABASE || join(root, "messages.db");
const indexPath = process.env.TEXTVIEW_INDEX || join(root, "textview-index.db");
const host = process.env.TEXTVIEW_HOST || "127.0.0.1";
const port = Number(process.env.TEXTVIEW_PORT || 8787);

if (!existsSync(archivePath) || !existsSync(indexPath)) {
  console.error("Missing messages.db or textview-index.db. Run: npm run index");
  process.exit(1);
}
mkdirSync(thumbnailDir, { recursive: true });
mkdirSync(previewDir, { recursive: true });
mkdirSync(audioDir, { recursive: true });
mkdirSync(stickerDir, { recursive: true });

const databases = new DatabaseSnapshot(archivePath, indexPath);

const json = (res, value, status = 200, headers = {}) => {
  const body = JSON.stringify(value);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "private, no-store",
    ...headers,
  });
  res.end(body);
};

const fail = (res, status, message) => json(res, { error: message }, status);
const int = (value, fallback = null) => {
  const parsed = Number.parseInt(value, 10);
  return Number.isSafeInteger(parsed) ? parsed : fallback;
};
const ftsQuery = (query) =>
  query.trim().split(/\s+/).filter(Boolean).slice(0, 12)
    .map((word) => {
      const quoted = `"${word.replaceAll('"', '""')}"`;
      // Prefix expansion on one- and two-character tokens can match most of
      // the archive and monopolize SQLite. Keep short searches exact.
      return word.length >= 3 ? `${quoted}*` : quoted;
    }).join(" AND ");

function thumbnail(mediaId, res) {
  return cachedImage(mediaId, thumbnailDir, "Thumbnail", ["image", "video"], res);
}

function preview(mediaId, res) {
  return cachedImage(mediaId, previewDir, "Preview", ["image"], res);
}

function sticker(mediaId, res) {
  const info = databases.archive.prepare(
    `SELECT m.content_sha256 FROM sticker_events s
     JOIN media m ON m.id=s.media_id WHERE s.media_id=? LIMIT 1`
  ).get(mediaId);
  if (!info) return fail(res, 404, "Sticker not found");
  const path = join(stickerDir, `${info.content_sha256}.webp`);
  if (!existsSync(path) || !statSync(path).size) {
    return fail(res, 404, "Browser-compatible sticker has not been prebuilt");
  }
  return sendThumbnail(path, res);
}

function browserAudio(mediaId, req, res) {
  const info = databases.appIndex.prepare(
    "SELECT content_sha256 FROM attachments WHERE media_id=? AND category='audio' LIMIT 1"
  ).get(mediaId);
  if (!info) return fail(res, 404, "Audio not found");
  const path = join(audioDir, `${info.content_sha256}.webm`);
  if (!existsSync(path) || !statSync(path).size) {
    return fail(res, 404, "Browser-compatible audio has not been prebuilt");
  }
  const total = statSync(path).size;
  let start = 0;
  let end = total - 1;
  let status = 200;
  const range = req.headers.range?.match(/^bytes=(\d*)-(\d*)$/);
  if (range) {
    if (range[1]) start = Number(range[1]);
    if (range[2]) end = Math.min(end, Number(range[2]));
    if (!range[1] && range[2]) start = Math.max(0, total - Number(range[2]));
    if (start > end || start >= total) {
      res.writeHead(416, { "content-range": `bytes */${total}` });
      return res.end();
    }
    status = 206;
  }
  const headers = {
    "content-type": "audio/webm; codecs=opus",
    "content-length": end - start + 1,
    "accept-ranges": "bytes",
    "cache-control": "private, max-age=31536000, immutable",
    "content-disposition": `inline; filename="audio-${mediaId}.webm"`,
    "x-content-type-options": "nosniff",
  };
  if (status === 206) headers["content-range"] = `bytes ${start}-${end}/${total}`;
  res.writeHead(status, headers);
  if (req.method === "HEAD") return res.end();
  createReadStream(path, { start, end }).pipe(res);
}

function cachedImage(mediaId, directory, label, categories, res) {
  const placeholders = categories.map(() => "?").join(",");
  const info = databases.appIndex.prepare(
    `SELECT content_sha256 FROM attachments
     WHERE media_id=? AND category IN (${placeholders}) LIMIT 1`
  ).get(mediaId, ...categories);
  if (!info) return fail(res, 404, "Image not found");
  const path = join(directory, `${info.content_sha256}.webp`);
  if (!existsSync(path) || !statSync(path).size) {
    return fail(res, 404, `${label} has not been prebuilt`);
  }
  return sendThumbnail(path, res);
}

function sendThumbnail(path, res) {
  res.writeHead(200, {
    "content-type": "image/webp",
    "content-length": statSync(path).size,
    "cache-control": "private, max-age=31536000, immutable",
    "x-content-type-options": "nosniff",
  });
  createReadStream(path).pipe(res);
}

function conversations(url, res) {
  const limit = Math.min(100, Math.max(1, int(url.searchParams.get("limit"), 40)));
  const before = int(url.searchParams.get("before"));
  const q = url.searchParams.get("q")?.trim();
  const params = [];
  let where = "WHERE c.last_message_ms IS NOT NULL";
  let join = "";
  if (q) {
    join = "JOIN conversation_search s ON s.rowid=c.id";
    where += " AND conversation_search MATCH ?";
    params.push(ftsQuery(q));
  }
  if (before) {
    where += " AND c.last_message_ms < ?";
    params.push(before);
  }
  params.push(limit + 1);
  const rows = databases.appIndex.prepare(
    `SELECT c.* FROM conversations c ${join} ${where}
     ORDER BY c.last_message_ms DESC,c.id DESC LIMIT ?`
  ).all(...params);
  const hasMore = rows.length > limit;
  if (hasMore) rows.pop();
  json(res, {
    items: rows,
    next: hasMore ? rows.at(-1)?.last_message_ms : null,
  });
}

function conversationDetail(id, res) {
  const conversation = databases.appIndex.prepare(
    "SELECT * FROM conversations WHERE id=?"
  ).get(id);
  if (!conversation) return fail(res, 404, "Conversation not found");
  const participants = databases.appIndex.prepare(
    `SELECT participant_id,display_name,display_address
     FROM conversation_participants WHERE conversation_id=?
     ORDER BY display_name`
  ).all(id);
  json(res, { ...conversation, participants });
}

function hydrateMessages(ids) {
  if (!ids.length) return [];
  const placeholders = ids.map(() => "?").join(",");
  const messages = databases.archive.prepare(
    `SELECT id,timestamp_ms,sent_timestamp_ms,direction,transport,is_group,
            subject,body,read,status,source_occurrences
     FROM messages WHERE id IN (${placeholders})`
  ).all(...ids);
  const byId = new Map(messages.map((message) => [message.id, {
    ...message, parts: [], reactions: [], stickers: [],
  }]));
  for (const part of databases.archive.prepare(
    `SELECT mp.message_id,mp.id,mp.sequence,mp.content_type,mp.filename,
            mp.text_content,mp.media_id,m.content_sha256,m.byte_size,m.detected_content_type,
            m.validation_status,
            EXISTS(SELECT 1 FROM sticker_events se
                   WHERE se.part_id=mp.id OR
                     (se.sticker_message_id=mp.message_id AND se.kind='standalone'
                      AND lower(coalesce(mp.content_type,'')) LIKE 'image/%')) is_sticker
     FROM message_parts mp LEFT JOIN media m ON m.id=mp.media_id
     WHERE mp.message_id IN (${placeholders})
       AND lower(coalesce(mp.content_type,'')) != 'application/smil'
       AND lower(coalesce(mp.filename,'')) NOT LIKE '%.pluginpayloadattachment'
     ORDER BY mp.message_id,mp.sequence,mp.id`
  ).all(...ids)) {
    byId.get(part.message_id)?.parts.push(part);
  }
  for (const reaction of databases.archive.prepare(
    `SELECT r.target_message_id,r.reaction,r.resolution_confidence,
            p.display_address,
            (SELECT pn.name FROM participant_names pn
             WHERE pn.participant_id=p.id
             ORDER BY pn.occurrence_count DESC LIMIT 1) reactor_name
     FROM reaction_events r
     LEFT JOIN participants p ON p.id=r.reactor_participant_id
     WHERE r.target_message_id IN (${placeholders})
       AND lower(r.reaction) != 'sticker'`
  ).all(...ids)) {
    byId.get(reaction.target_message_id)?.reactions.push(reaction);
  }
  for (const stickerEvent of databases.archive.prepare(
    `SELECT se.sticker_message_id,se.target_message_id,se.part_id,se.media_id,se.kind,
            se.position_x,se.position_y,se.scale,se.rotation,se.parent_width,
            se.short_description,se.is_animated,m.content_sha256,m.byte_size,
            mp.content_type,mp.filename,
            p.display_address,
            (SELECT pn.name FROM participant_names pn
             WHERE pn.participant_id=p.id
             ORDER BY pn.occurrence_count DESC LIMIT 1) reactor_name
     FROM sticker_events se
     JOIN media m ON m.id=se.media_id
     LEFT JOIN message_parts mp ON mp.id=se.part_id
     LEFT JOIN reaction_events r ON r.reaction_message_id=se.sticker_message_id
     LEFT JOIN participants p ON p.id=r.reactor_participant_id
     WHERE se.target_message_id IN (${placeholders})
        OR (se.target_message_id IS NULL AND se.sticker_message_id IN (${placeholders}))
     ORDER BY se.id`
  ).all(...ids, ...ids)) {
    const owner = stickerEvent.target_message_id || stickerEvent.sticker_message_id;
    byId.get(owner)?.stickers.push(stickerEvent);
  }
  for (const fallback of databases.archive.prepare(
    `SELECT reaction_message_id,target_message_id,resolution_confidence
     FROM reaction_events
     WHERE reaction_message_id IN (${placeholders})`
  ).all(...ids)) {
    Object.assign(byId.get(fallback.reaction_message_id) || {}, {
      reaction_fallback_target_id: fallback.target_message_id,
      reaction_resolution: fallback.resolution_confidence,
    });
  }
  for (const reply of databases.archive.prepare(
    `SELECT r.message_id,r.target_message_id,r.resolution_confidence,
            target.timestamp_ms target_timestamp_ms,
            coalesce(target.body,target.subject) target_body
     FROM message_replies r
     LEFT JOIN messages target ON target.id=r.target_message_id
     WHERE r.message_id IN (${placeholders})`
  ).all(...ids)) {
    Object.assign(byId.get(reply.message_id) || {}, { reply });
  }
  for (const edit of databases.archive.prepare(
    `SELECT message_id,part_index,edit_order,timestamp_ms,body
     FROM message_edits WHERE message_id IN (${placeholders})
     ORDER BY message_id,part_index,edit_order`
  ).all(...ids)) {
    const message = byId.get(edit.message_id);
    if (message) (message.edit_history ||= []).push(edit);
  }
  for (const effect of databases.archive.prepare(
    `SELECT message_id,effect_id FROM message_effects
     WHERE message_id IN (${placeholders})`
  ).all(...ids)) {
    const message = byId.get(effect.message_id);
    if (message) message.effect_id = effect.effect_id;
  }
  for (const appMessage of databases.archive.prepare(
    `SELECT message_id,bundle_id,app_name,title,subtitle,url
     FROM app_messages WHERE message_id IN (${placeholders})`
  ).all(...ids)) {
    const message = byId.get(appMessage.message_id);
    if (message) message.app_message = appMessage;
  }
  return ids.map((id) => byId.get(id)).filter(Boolean);
}

function messages(id, url, res) {
  const limit = Math.min(100, Math.max(10, int(url.searchParams.get("limit"), 50)));
  const around = int(url.searchParams.get("around"));
  const beforeMs = int(url.searchParams.get("beforeMs"));
  const beforeId = int(url.searchParams.get("beforeId"));
  const afterMs = int(url.searchParams.get("afterMs"));
  const afterId = int(url.searchParams.get("afterId"));
  if (around) {
    const target = databases.appIndex.prepare(
      `SELECT timestamp_ms,message_id FROM conversation_messages
       WHERE conversation_id=? AND message_id=?`
    ).get(id, around);
    if (!target) return fail(res, 404, "Message not found in conversation");
    const olderSize = Math.floor(limit / 2);
    const older = databases.appIndex.prepare(
      `SELECT message_id,timestamp_ms FROM conversation_messages
       WHERE conversation_id=?
         AND (timestamp_ms < ? OR (timestamp_ms=? AND message_id<=?))
       ORDER BY timestamp_ms DESC,message_id DESC LIMIT ?`
    ).all(id, target.timestamp_ms, target.timestamp_ms, target.message_id, olderSize + 1);
    const hasOlder = older.length > olderSize;
    if (hasOlder) older.pop();
    const newerSize = limit - older.length;
    const newer = databases.appIndex.prepare(
      `SELECT message_id,timestamp_ms FROM conversation_messages
       WHERE conversation_id=?
         AND (timestamp_ms > ? OR (timestamp_ms=? AND message_id>?))
       ORDER BY timestamp_ms ASC,message_id ASC LIMIT ?`
    ).all(id, target.timestamp_ms, target.timestamp_ms, target.message_id, newerSize + 1);
    const hasNewer = newer.length > newerSize;
    if (hasNewer) newer.pop();
    const rows = [...older.toReversed(), ...newer];
    const oldest = older.at(-1);
    const newest = newer.at(-1) || older.at(0);
    return json(res, {
      items: hydrateMessages(rows.map((row) => row.message_id)),
      next: hasOlder && oldest
        ? { beforeMs: oldest.timestamp_ms, beforeId: oldest.message_id }
        : null,
      newer: hasNewer && newest
        ? { afterMs: newest.timestamp_ms, afterId: newest.message_id }
        : null,
    });
  }
  if (afterMs && afterId) {
    const rows = databases.appIndex.prepare(
      `SELECT message_id,timestamp_ms FROM conversation_messages
       WHERE conversation_id=?
         AND (timestamp_ms > ? OR (timestamp_ms=? AND message_id>?))
       ORDER BY timestamp_ms ASC,message_id ASC LIMIT ?`
    ).all(id, afterMs, afterMs, afterId, limit + 1);
    const hasMore = rows.length > limit;
    if (hasMore) rows.pop();
    const newest = rows.at(-1);
    return json(res, {
      items: hydrateMessages(rows.map((row) => row.message_id)),
      next: null,
      newer: hasMore && newest
        ? { afterMs: newest.timestamp_ms, afterId: newest.message_id }
        : null,
    });
  }
  const params = [id];
  let cursor = "";
  if (beforeMs && beforeId) {
    cursor = "AND (cm.timestamp_ms < ? OR (cm.timestamp_ms=? AND cm.message_id<?))";
    params.push(beforeMs, beforeMs, beforeId);
  }
  params.push(limit + 1);
  const rows = databases.appIndex.prepare(
    `SELECT cm.message_id,cm.timestamp_ms FROM conversation_messages cm
     WHERE cm.conversation_id=? ${cursor}
     ORDER BY cm.timestamp_ms DESC,cm.message_id DESC LIMIT ?`
  ).all(...params);
  const hasMore = rows.length > limit;
  if (hasMore) rows.pop();
  const ordered = rows.toReversed();
  const items = hydrateMessages(ordered.map((row) => row.message_id));
  const oldest = rows.at(-1);
  json(res, {
    items,
    next: hasMore && oldest
      ? { beforeMs: oldest.timestamp_ms, beforeId: oldest.message_id }
      : null,
    newer: null,
  });
}

function search(url, res) {
  const q = url.searchParams.get("q")?.trim();
  if (!q) return json(res, { total_matches: 0, conversations: [] });
  const match = ftsQuery(q);
  try {
    const total = databases.archive.prepare(
      "SELECT count(*) count FROM message_search WHERE message_search MATCH ?"
    ).get(match).count;
    const grouped = databases.archive.prepare(
      `WITH hits AS (
         SELECT m.id,m.timestamp_ms,m.direction,m.transport
         FROM message_search
         JOIN messages m ON m.id=message_search.rowid
         WHERE message_search MATCH ?
       ), grouped AS (
         SELECT cm.conversation_id,h.id,h.timestamp_ms,h.direction,h.transport,
                count(*) occurrences,
                max(printf('%013d:%010d',h.timestamp_ms,h.id)) latest_key
         FROM hits h
         JOIN app.conversation_messages cm ON cm.message_id=h.id
         GROUP BY cm.conversation_id
       )
       SELECT g.conversation_id,g.id latest_message_id,g.timestamp_ms,
              g.direction,g.transport,
              snippet(message_search,0,'<mark>','</mark>',' … ',32) snippet,
              g.occurrences,c.title,c.subtitle,c.is_group,c.avatar_path,
              c.message_count
       FROM grouped g
       JOIN message_search ON message_search.rowid=g.id
                          AND message_search MATCH ?
       JOIN app.conversations c ON c.id=g.conversation_id
       ORDER BY g.timestamp_ms DESC,g.conversation_id DESC`
    ).all(match, match);
    return json(res, {
      query: q,
      total_matches: total,
      conversation_count: grouped.length,
      conversations: grouped,
    });
  } catch (error) {
    return fail(res, 400, `Search query could not be parsed: ${error.message}`);
  }
}

function conversationSearch(id, url, res) {
  const q = url.searchParams.get("q")?.trim();
  if (!q) return json(res, { total: 0, items: [], next_offset: null });
  const limit = Math.min(200, Math.max(10, int(url.searchParams.get("limit"), 100)));
  const offset = Math.max(0, int(url.searchParams.get("offset"), 0));
  const match = ftsQuery(q);
  try {
    const total = databases.archive.prepare(
      `SELECT count(*) count FROM message_search
       WHERE message_search MATCH ?
         AND rowid IN (
           SELECT message_id FROM app.conversation_messages
           WHERE conversation_id=?
         )`
    ).get(match, id).count;
    const items = databases.archive.prepare(
      `SELECT m.id,m.timestamp_ms,m.direction,m.transport,
              snippet(message_search,0,'<mark>','</mark>',' … ',32) snippet
       FROM message_search
       JOIN messages m ON m.id=message_search.rowid
       WHERE message_search MATCH ?
         AND m.id IN (
           SELECT message_id FROM app.conversation_messages
           WHERE conversation_id=?
         )
       ORDER BY m.timestamp_ms DESC,m.id DESC LIMIT ? OFFSET ?`
    ).all(match, id, limit, offset);
    return json(res, {
      query: q,
      total,
      items,
      next_offset: offset + items.length < total ? offset + items.length : null,
    });
  } catch (error) {
    return fail(res, 400, `Search query could not be parsed: ${error.message}`);
  }
}

function attachments(url, res) {
  const conversationId = int(url.searchParams.get("conversation"));
  const category = url.searchParams.get("type") || "all";
  if (!["all", "image", "video", "audio", "file"].includes(category)) {
    return fail(res, 400, "Unknown attachment type");
  }
  const limit = Math.min(100, Math.max(12, int(url.searchParams.get("limit"), 48)));
  const catalog = url.searchParams.get("catalog") === "1";
  const beforeMs = int(url.searchParams.get("beforeMs"));
  const beforePart = int(url.searchParams.get("beforePart"));
  const params = [];
  let where = "WHERE 1=1";
  if (conversationId) {
    where += " AND conversation_id=?";
    params.push(conversationId);
  }
  if (category !== "all") {
    where += " AND category=?";
    params.push(category);
  }
  if (beforeMs && beforePart) {
    where += " AND (timestamp_ms < ? OR (timestamp_ms=? AND part_id<?))";
    params.push(beforeMs, beforeMs, beforePart);
  }
  if (!catalog) params.push(limit + 1);
  const rows = databases.appIndex.prepare(
    `SELECT * FROM attachments ${where}
     ORDER BY timestamp_ms DESC,part_id DESC ${catalog ? "" : "LIMIT ?"}`
  ).all(...params);
  if (catalog) return json(res, { items: rows, next: null });
  const hasMore = rows.length > limit;
  if (hasMore) rows.pop();
  const last = rows.at(-1);
  return json(res, {
    items: rows,
    next: hasMore && last
      ? { beforeMs: last.timestamp_ms, beforePart: last.part_id }
      : null,
  });
}

async function media(id, req, res) {
  const info = databases.archive.prepare(
    `SELECT m.byte_size,m.detected_content_type,
      (SELECT mp.content_type FROM message_parts mp
       WHERE mp.media_id=m.id ORDER BY mp.id LIMIT 1) declared_type,
      (SELECT mp.filename FROM message_parts mp
       WHERE mp.media_id=m.id AND mp.filename IS NOT NULL
       ORDER BY mp.id LIMIT 1) filename
     FROM media m WHERE m.id=?`
  ).get(id);
  if (!info) return fail(res, 404, "Attachment not found");
  const total = info.byte_size;
  let start = 0;
  let end = total - 1;
  let status = 200;
  const range = req.headers.range?.match(/^bytes=(\d*)-(\d*)$/);
  if (range) {
    if (range[1]) start = Number(range[1]);
    if (range[2]) end = Math.min(end, Number(range[2]));
    if (!range[1] && range[2]) start = Math.max(0, total - Number(range[2]));
    if (start > end || start >= total) {
      res.writeHead(416, { "content-range": `bytes */${total}` });
      return res.end();
    }
    status = 206;
  }
  let mime = info.detected_content_type || info.declared_type || "application/octet-stream";
  if (
    info.declared_type?.startsWith("audio/") &&
    ["audio/mp4", "audio/x-m4a"].includes(info.declared_type) &&
    info.detected_content_type === "video/mp4"
  ) {
    mime = info.declared_type;
  } else if (
    info.declared_type?.startsWith("application/vnd.") &&
    info.detected_content_type === "application/zip"
  ) {
    mime = info.declared_type;
  }
  const headers = {
    "content-type": mime,
    "content-length": end - start + 1,
    "accept-ranges": "bytes",
    "cache-control": "private, max-age=31536000, immutable",
    "content-disposition": `inline; filename*=UTF-8''${encodeURIComponent(info.filename || `attachment-${id}`)}`,
  };
  if (status === 206) headers["content-range"] = `bytes ${start}-${end}/${total}`;
  res.writeHead(status, headers);
  if (req.method === "HEAD") return res.end();
  const child = spawn("python3", [
    join(root, "tools/stream_media.py"),
    archivePath,
    String(id),
    String(start),
    String(end - start + 1),
  ], { stdio: ["ignore", "pipe", "pipe"] });
  child.stdout.pipe(res);
  let error = "";
  child.stderr.on("data", (chunk) => { error += chunk; });
  child.on("error", (cause) => res.destroy(cause));
  child.on("close", (code) => {
    if (code && !res.destroyed) {
      console.error(`Attachment ${id} stream failed: ${error}`);
      res.destroy();
    }
  });
  req.on("aborted", () => child.kill());
  res.on("close", () => {
    if (!res.writableEnded && !child.killed) child.kill();
  });
}

const types = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".webmanifest": "application/manifest+json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
};

function staticFile(pathname, res) {
  if (pathname.startsWith("/contact-photos/")) {
    const path = normalize(join(dataDir, pathname.slice(1)));
    if (!path.startsWith(dataDir) || !existsSync(path) || !statSync(path).isFile()) {
      return false;
    }
    res.writeHead(200, {
      "content-type": "image/jpeg",
      "cache-control": "private, max-age=31536000, immutable",
      "x-content-type-options": "nosniff",
    });
    createReadStream(path).pipe(res);
    return true;
  }
  const relative = pathname === "/" ? "index.html" : pathname.slice(1);
  const path = normalize(join(publicDir, relative));
  if (!path.startsWith(publicDir) || !existsSync(path) || !statSync(path).isFile()) {
    return false;
  }
  res.writeHead(200, {
    "content-type": types[extname(path)] || "application/octet-stream",
    "cache-control": "no-cache, must-revalidate",
    "content-security-policy": "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
  });
  createReadStream(path).pipe(res);
  return true;
}

const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url, "http://localhost");
    if (req.method !== "GET" && req.method !== "HEAD") {
      return fail(res, 405, "Method not allowed");
    }
    try {
      if (databases.refreshIfReplaced()) {
        console.log(`Loaded TextView index generation ${databases.indexIdentity}`);
      }
    } catch (error) {
      // Keep serving the last known-good snapshot if a replacement cannot be
      // opened. A later atomic replacement will be retried on the next request.
      console.error(`Could not load replacement TextView index: ${error.message}`);
    }
    if (url.pathname === "/api/conversations") return conversations(url, res);
    if (url.pathname === "/api/search") return search(url, res);
    if (url.pathname === "/api/attachments") return attachments(url, res);
    if (url.pathname === "/api/health") {
      return json(res, {
        ok: true,
        archive_messages: databases.archive.prepare(
          "SELECT count(*) count FROM messages"
        ).get().count,
        indexed_messages: databases.appIndex.prepare(
          "SELECT count(*) count FROM conversation_messages"
        ).get().count,
      });
    }
    let match = url.pathname.match(/^\/api\/conversations\/(\d+)$/);
    if (match) return conversationDetail(Number(match[1]), res);
    match = url.pathname.match(/^\/api\/conversations\/(\d+)\/messages$/);
    if (match) return messages(Number(match[1]), url, res);
    match = url.pathname.match(/^\/api\/conversations\/(\d+)\/search$/);
    if (match) return conversationSearch(Number(match[1]), url, res);
    match = url.pathname.match(/^\/api\/media\/(\d+)$/);
    if (match) return await media(Number(match[1]), req, res);
    match = url.pathname.match(/^\/api\/thumbnail\/(\d+)$/);
    if (match) return thumbnail(Number(match[1]), res);
    match = url.pathname.match(/^\/api\/preview\/(\d+)$/);
    if (match) return preview(Number(match[1]), res);
    match = url.pathname.match(/^\/api\/sticker\/(\d+)$/);
    if (match) return sticker(Number(match[1]), res);
    match = url.pathname.match(/^\/api\/audio\/(\d+)$/);
    if (match) return browserAudio(Number(match[1]), req, res);
    if (staticFile(url.pathname, res)) return;
    if (!url.pathname.startsWith("/api/")) return staticFile("/", res);
    return fail(res, 404, "Not found");
  } catch (error) {
    console.error(error);
    return fail(res, 500, "Internal server error");
  }
});

server.listen(port, host, () => {
  console.log(`TextView listening at http://${host}:${port}`);
});

server.on("close", () => databases.close());
