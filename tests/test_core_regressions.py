"""Regression tests for translation failure safety and Telegram/X language tracking."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import util
from src.config import load_config
from src.models import RawItem


class TranslationSafetyTests(unittest.TestCase):
    def test_translation_failure_never_publishes_raw_english(self):
        """ترجمة ناموفق هرگز رویداد با متن انگلیسی خام منتشر نمی‌کند."""
        from src.pipeline import Pipeline
        from src.db import Database
        import tempfile

        config = load_config()
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            try:
                bot = Mock()
                bot.configured = True
                bot.chat_id.return_value = "123"
                bot.send.side_effect = RuntimeError("fail")

                pipeline = Pipeline(config, db, bot)
                with patch.object(
                    pipeline.translator,
                    "translate_item",
                    side_effect=RuntimeError("LLM down"),
                ):
                    item = RawItem(
                        source_id="tr_fail",
                        source_name="test",
                        source_url="",
                        title="Iran airspace closed: all flights cancelled",
                        body="Airspace closed. NOTAM A1234/26 issued.",
                        url="",
                        lang="en",
                        country="TR",
                    )
                    outcome = pipeline.process(item)
                self.assertEqual(outcome, "translation-failed")
                events = db.recent_events(hours=1)
                for ev in events:
                    body = ev.get("body_fa") or ""
                    self.assertNotIn("Flight cancelled", body)
                    self.assertNotIn("Airspace closed. NOTAM", body)
            finally:
                db.close()


class TelegramLangMetadataTests(unittest.TestCase):
    def test_events_carry_lang_field(self):
        """رویدادها باید فیلد lang داشته باشند تا تلگرام/X آن را بفرستد."""
        from src.formatter import format_event

        config = load_config()
        event = {
            "title_fa": "لغو پرواز",
            "body_fa": "متن",
            "primary_source": "test",
            "primary_url": "",
            "lang": "tr",
            "severity": "critical",
            "category": "airspace",
            "first_seen": util.iso(util.now_utc()),
            "confidence": "single",
            "numbers_ok": 1,
            "airlines": "",
            "countries": "TR",
        }
        text = format_event(event, config)
        self.assertIn("لغو پرواز", text)
        self.assertLess(len(text), 4096)


if __name__ == "__main__":
    unittest.main(verbosity=2)
