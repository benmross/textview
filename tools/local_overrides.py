#!/usr/bin/env python3
"""Load the archive owner's own corrections from an untracked file.

Some repairs cannot be expressed as a rule: a restored SMS whose sender one
backup lost, a reaction that arrived before the message it reacts to. Each is
identified by an exact timestamp and body, which is real conversation text, so
none of it can live in a public repository. It lives in `local_overrides.json`
at the checkout root (ignored by git; `local_overrides.example.json` shows the
format), or wherever TEXTVIEW_LOCAL_OVERRIDES points. Without the file every
table is empty and the importers run as pure rules.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "local_overrides.json"


def load() -> dict:
    path = Path(os.environ.get("TEXTVIEW_LOCAL_OVERRIDES") or DEFAULT_PATH)
    if not path.is_file():
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


_DATA = load()

# E.164 number of the archive owner, used when --owner-phone is not given.
OWNER_PHONE: str | None = _DATA.get("owner_phone")

# Hostname recorded in the provenance of an imported macOS chat.db.
IMESSAGE_SOURCE_HOST: str = _DATA.get("imessage_source_host", "mac")

# (timestamp_ms, body) -> canonical address of the real counterpart.
SMS_ADDRESS_OVERRIDES: dict[tuple[int, str], str] = {
    (int(stamp), body): address
    for stamp, body, address in _DATA.get("sms_address_overrides", ())
}

# (reaction_ms, reaction_body, target_ms, target_body)
OUT_OF_ORDER_REACTIONS: tuple[tuple[int, str, int, str], ...] = tuple(
    (int(reaction_ms), reaction_body, int(target_ms), target_body)
    for reaction_ms, reaction_body, target_ms, target_body
    in _DATA.get("out_of_order_reactions", ())
)

# (decision_key, decision, rationale, decided_by, decided_at) rows recording
# the adjudications above in the archive's own dedup_decisions table.
DEDUP_DECISIONS: tuple[tuple[str, str, str, str, str], ...] = tuple(
    tuple(row) for row in _DATA.get("dedup_decisions", ())
)
