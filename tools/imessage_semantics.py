"""Shared interpretation of Apple's semantic message records.

Messages/chat.db stores reactions, announcements, app payloads, and location
sharing changes in the message table.  These helpers keep both import paths
from mistaking those records for ordinary (and often empty) text bubbles.
"""

from __future__ import annotations

import plistlib


REACTION_TYPES = {
    1000: "sticker",
    2000: "loved",
    2001: "liked",
    2002: "disliked",
    2003: "laughed",
    2004: "emphasized",
    2005: "questioned",
    2006: "emoji",
    2007: "sticker",
}

ASSOCIATED_TYPE_NAMES = {
    "sticker": 1000,
    "stickerback": 2007,
    "love": 2000,
    "like": 2001,
    "dislike": 2002,
    "laugh": 2003,
    "emphasize": 2004,
    "question": 2005,
    "emoji": 2006,
    "-love": 3000,
    "-like": 3001,
    "-dislike": 3002,
    "-laugh": 3003,
    "-emphasize": 3004,
    "-question": 3005,
    "-emoji": 3006,
    "-stickerback": 3007,
}


def associated_type(value) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, str):
        named = ASSOCIATED_TYPE_NAMES.get(value.strip().casefold())
        if named is not None:
            return named
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def is_reaction_add(value) -> bool:
    return associated_type(value) in REACTION_TYPES


def is_reaction_removal(value) -> bool:
    return 3000 <= associated_type(value) <= 3007


def reaction_label(value, emoji=None) -> str | None:
    kind = REACTION_TYPES.get(associated_type(value))
    if kind == "emoji":
        return (emoji or "emoji").strip() or "emoji"
    return kind


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    # U+FFFC is Apple's attachment placeholder. U+FFFD is also emitted by
    # Messages for app-only payloads and must not become a visible glyph.
    value = value.replace("\ufffc", "").replace("\ufffd", "").strip()
    return value or None


def fully_unsent(summary) -> bool:
    if not summary:
        return False
    try:
        info = plistlib.loads(bytes(summary))
    except (plistlib.InvalidFileException, TypeError, ValueError, OverflowError):
        return False
    if not isinstance(info, dict) or not info.get("ust"):
        return False
    original_parts = info.get("otr")
    retracted_parts = info.get("rp")
    if not isinstance(original_parts, dict) or not isinstance(retracted_parts, list):
        return False
    original = {str(key) for key in original_parts}
    retracted = {str(key) for key in retracted_parts}
    return bool(original) and original <= retracted


def effective_direction(*, is_from_me, item_type=0, other_handle=0, share_direction=0) -> str:
    # is_from_me is not meaningful for legacy location-sharing records.
    if int(item_type or 0) == 4:
        outgoing = bool(other_handle) and not bool(share_direction)
        return "outgoing" if outgoing else "incoming"
    return "outgoing" if is_from_me else "incoming"


def app_label(balloon_bundle_id: str | None) -> str:
    bundle = (balloon_bundle_id or "").casefold()
    if "urlballoonprovider" in bundle:
        return "Link"
    if "gamepigeon" in bundle:
        return "GamePigeon message"
    if "poll" in bundle:
        return "Poll"
    if "findmy" in bundle:
        return "Find My message"
    if "applepay" in bundle or "money" in bundle:
        return "Apple Pay message"
    if "digitaltouch" in bundle or "handwriting" in bundle:
        return "Digital Touch message"
    if "fitness" in bundle:
        return "Fitness message"
    if "checkin" in bundle:
        return "Check In"
    return "App message"


def semantic_presentation(
    *, body=None, item_type=0, group_action_type=0, group_title=None,
    share_status=0, direction="incoming", balloon_bundle_id=None,
    associated_message_type=0, is_fully_unsent=False,
) -> tuple[str | None, str | None]:
    """Return (body, status); status='hidden' excludes a record from timelines."""
    text = clean_text(body)
    associated = associated_type(associated_message_type)
    if is_reaction_add(associated) or is_reaction_removal(associated) or associated == 4000:
        return text, "hidden"
    if is_fully_unsent:
        return "You unsent a message" if direction == "outgoing" else "Unsent a message", "announcement"

    item = int(item_type or 0)
    action = int(group_action_type or 0)
    if item == 1:
        return ("Removed someone from the conversation" if action == 1
                else "Added someone to the conversation"), "announcement"
    if item == 2:
        title = clean_text(group_title)
        return (f'Named the conversation “{title}”' if title
                else "Changed the conversation name"), "announcement"
    if item == 3:
        labels = {
            0: "Left the conversation",
            1: "Changed the conversation photo",
            2: "Removed the conversation photo",
            4: "Changed the conversation background",
            6: "Removed the conversation background",
        }
        return labels.get(action, "Changed the conversation"), "announcement"
    if item == 4:
        verb = "stopped" if int(share_status or 0) == 1 else "started"
        if direction == "outgoing":
            return f"You {verb} sharing your location", "announcement"
        return f"{verb.capitalize()} sharing their location with you", "announcement"
    if item == 5:
        return "SharePlay ended", "announcement"

    if balloon_bundle_id and not text:
        return app_label(balloon_bundle_id), "app"
    return text, None
