import plistlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from imessage_semantics import (  # noqa: E402
    associated_type, clean_text, effective_direction, fully_unsent,
    reaction_label, semantic_presentation,
)
from sticker_semantics import (  # noqa: E402
    decode_sticker_info, is_animated_sticker, sticker_kind, target_guid,
)
from apple_message_features import decode_app_payload, unarchive  # noqa: E402
from import_bluebubbles import finalize_export_status  # noqa: E402


class IMessageSemanticsTest(unittest.TestCase):
    def test_bluebubbles_named_reactions_are_normalized(self):
        self.assertEqual(associated_type("love"), 2000)
        self.assertEqual(associated_type("-like"), 3001)
        self.assertEqual(reaction_label("emoji", "🥳"), "🥳")

    def test_location_direction_uses_share_fields(self):
        self.assertEqual(effective_direction(
            is_from_me=False, item_type=4, other_handle=55, share_direction=0), "outgoing")
        self.assertEqual(effective_direction(
            is_from_me=True, item_type=4, other_handle=0, share_direction=1), "incoming")

    def test_location_record_becomes_announcement(self):
        self.assertEqual(semantic_presentation(
            item_type=4, share_status=0, direction="incoming"),
            ("Started sharing their location with you", "announcement"))

    def test_reactions_and_poll_votes_are_hidden(self):
        self.assertEqual(semantic_presentation(associated_message_type=2001)[1], "hidden")
        self.assertEqual(semantic_presentation(associated_message_type=3001)[1], "hidden")
        self.assertEqual(semantic_presentation(associated_message_type=4000)[1], "hidden")

    def test_replacement_characters_are_not_content(self):
        self.assertIsNone(clean_text("\ufffc\ufffd"))

    def test_complete_retraction_is_detected(self):
        blob = plistlib.dumps({"ust": True, "otr": {"0": {}}, "rp": [0]}, fmt=plistlib.FMT_BINARY)
        self.assertTrue(fully_unsent(blob))

    def test_sticker_plist_and_target_are_normalized(self):
        blob = plistlib.dumps({"sxs": "0.75", "sys": "0.2", "ssa": "0.367"},
                               fmt=plistlib.FMT_BINARY)
        self.assertEqual(decode_sticker_info(blob)["sxs"], "0.75")
        self.assertEqual(target_guid("bp:3/p:0/ABC-123"), "ABC-123")
        self.assertEqual(sticker_kind(1000), "placed")
        self.assertEqual(sticker_kind("2007"), "stickerback")
        self.assertEqual(sticker_kind(0, True), "standalone")

    def test_heic_sequences_are_animated_stickers(self):
        self.assertTrue(is_animated_sticker("image/heic-sequence", "sticker.heics"))
        self.assertFalse(is_animated_sticker("image/heic", "still.heic"))

    def test_keyed_archive_app_card_is_decoded(self):
        objects = ["$null", {
            "NS.keys": [plistlib.UID(2), plistlib.UID(3)],
            "NS.objects": [plistlib.UID(4), plistlib.UID(5)],
        }, "an", "ldtext", "GamePigeon", "Cup Pong"]
        archive = plistlib.dumps({
            "$archiver": "NSKeyedArchiver", "$version": 100000,
            "$top": {"root": plistlib.UID(1)}, "$objects": objects,
        }, fmt=plistlib.FMT_BINARY)
        self.assertEqual(unarchive(archive)["ldtext"], "Cup Pong")
        card = decode_app_payload("com.gamerdelights.gamepigeon.ext", archive)
        self.assertEqual(card["app_name"], "GamePigeon")
        self.assertEqual(card["title"], "Cup Pong")

    def test_empty_failed_export_edit_is_hidden(self):
        message = {"dateEdited": 1781135621522, "messageSummaryInfo": None}
        self.assertEqual(
            finalize_export_status(None, message, None, []),
            "hidden",
        )

    def test_real_edit_remains_visible(self):
        message = {"dateEdited": 1781135621522, "messageSummaryInfo": None}
        self.assertEqual(
            finalize_export_status(None, message, "corrected", []),
            "edited",
        )


if __name__ == "__main__":
    unittest.main()
