#!/usr/bin/env python3
"""Decode durable, user-visible features from Apple message metadata."""

from __future__ import annotations

import json
import plistlib
from urllib.parse import urlparse


SCHEMA = """
CREATE TABLE IF NOT EXISTS message_edits (
  id INTEGER PRIMARY KEY,
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  part_index TEXT NOT NULL,
  edit_order INTEGER NOT NULL,
  timestamp_ms INTEGER,
  body TEXT,
  UNIQUE(message_id,part_index,edit_order)
);
CREATE INDEX IF NOT EXISTS message_edits_message_idx ON message_edits(message_id);
CREATE TABLE IF NOT EXISTS message_effects (
  message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
  effect_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_messages (
  message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
  bundle_id TEXT NOT NULL,
  app_name TEXT,
  title TEXT,
  subtitle TEXT,
  url TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
"""


def ensure_feature_schema(db) -> None:
    db.executescript(SCHEMA)


def unarchive(blob):
    """Resolve the Foundation collection types used in NSKeyedArchiver plists."""
    plist = plistlib.loads(bytes(blob))
    if not isinstance(plist, dict) or "$objects" not in plist:
        return plist
    objects = plist["$objects"]
    seen = {}

    def resolve(value):
        if isinstance(value, plistlib.UID):
            index = value.data
            if index in seen:
                return seen[index]
            seen[index] = None
            seen[index] = resolve(objects[index])
            return seen[index]
        if isinstance(value, list):
            return [resolve(item) for item in value]
        if not isinstance(value, dict):
            return value
        if "NS.keys" in value and "NS.objects" in value:
            return {
                str(resolve(key)): resolve(item)
                for key, item in zip(value["NS.keys"], value["NS.objects"])
            }
        if "NS.relative" in value:
            return resolve(value["NS.relative"])
        if "NS.objects" in value:
            return resolve(value["NS.objects"])
        return {
            key: resolve(item) for key, item in value.items()
            if not key.startswith("$") and key != "NS.base"
        }

    top = plist.get("$top", {})
    return resolve(top.get("root"))


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def decode_app_payload(bundle_id, blob, fallback_text=None) -> dict:
    """Extract a safe display card; opaque app data and embedded images stay out."""
    try:
        root = blob if isinstance(blob, dict) else unarchive(blob)
    except (plistlib.InvalidFileException, IndexError, KeyError, TypeError, ValueError):
        root = {}
    if not isinstance(root, dict):
        root = {}
    # OpenBubbles exports the already-unarchived app dictionary.
    app_data = root.get("appData")
    if isinstance(app_data, list) and app_data and isinstance(app_data[0], dict):
        root = {**root, **app_data[0]}
    url_data = root.get("urlData")
    if isinstance(url_data, dict):
        root = {**root, **url_data}
    bundle = str(bundle_id or "")
    lower = bundle.casefold()
    rich = root.get("richLinkMetadata")
    rich = rich if isinstance(rich, dict) else {}
    user_info = root.get("userInfo")
    user_info = user_info if isinstance(user_info, dict) else {}
    app_name = _text(root.get("an"))
    title = _text(root.get("ldtext"))
    subtitle = _text(user_info.get("caption"))
    candidate_url = _text(rich.get("URL")) or _text(rich.get("originalURL"))
    if "urlballoonprovider" in lower:
        app_name = _text(rich.get("siteName")) or "Link"
        fallback_url = _text(fallback_text)
        fallback_parsed = urlparse(fallback_url or "")
        if fallback_parsed.scheme not in {"http", "https"}:
            fallback_url = None
        candidate_url = candidate_url or fallback_url
        title = _text(rich.get("title")) or candidate_url
        subtitle = _text(rich.get("summary")) or candidate_url
    elif "poll" in lower:
        app_name = app_name or "Polls"
        title = "Poll"
        subtitle = _text(root.get("ldtext")) or subtitle
    elif "gamepigeon" in lower:
        app_name = app_name or "GamePigeon"
    elif "findmy" in lower:
        app_name = app_name or "Find My"
    elif "peerpayment" in lower:
        app_name = app_name or "Apple Cash"
    parsed = urlparse(candidate_url or "")
    url = candidate_url if parsed.scheme in {"http", "https"} else None
    return {
        "bundle_id": bundle,
        "app_name": app_name,
        "title": title,
        "subtitle": subtitle,
        "url": url,
        "metadata_json": json.dumps({
            "layout_class": _text(root.get("layoutClass")),
            "has_embedded_image": any(isinstance(value, bytes) and value.startswith(
                (b"\xff\xd8\xff", b"\x89PNG")) for value in root.values()),
        }, separators=(",", ":"), sort_keys=True),
    }


def decode_edit_history(summary, decode_attributed, apple_ms) -> list[dict]:
    try:
        value = plistlib.loads(bytes(summary)) if summary else {}
    except (plistlib.InvalidFileException, TypeError, ValueError, OverflowError):
        return []
    edits = value.get("ec") if isinstance(value, dict) else None
    if not isinstance(edits, dict):
        return []
    result = []
    for part_index, events in edits.items():
        if not isinstance(events, list):
            continue
        for order, event in enumerate(events):
            if not isinstance(event, dict):
                continue
            result.append({
                "part_index": str(part_index), "edit_order": order,
                "timestamp_ms": apple_ms(event.get("d")),
                "body": decode_attributed(event.get("t")),
            })
    return result


def decode_bluebubbles_edit_history(summary) -> list[dict]:
    if not isinstance(summary, list):
        return []
    result = []
    for group in summary:
        edited = group.get("editedContent") if isinstance(group, dict) else None
        if not isinstance(edited, dict):
            continue
        for part_index, events in edited.items():
            for order, event in enumerate(events if isinstance(events, list) else []):
                values = ((event.get("text") or {}).get("values") or [])
                body = "".join(
                    str(value.get("string") or "") for value in values
                    if isinstance(value, dict)
                ).strip() or None
                timestamp = event.get("date")
                try:
                    timestamp = int(float(timestamp)) if timestamp is not None else None
                except (TypeError, ValueError):
                    timestamp = None
                result.append({
                    "part_index": str(part_index), "edit_order": order,
                    "timestamp_ms": timestamp, "body": body,
                })
    return result
