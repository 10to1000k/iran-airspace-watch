"""تست‌های هسته — بدون شبکه و بدون کلید API."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import util
from src.classify import Classifier
from src.config import load_config
from src.formatter import format_event
from src.models import RawItem
from src.notam import decode_notam, parse_notam

SAMPLE_NOTAM = (
    "A1234/26 NOTAMN Q) OIIX/QFALC/IV/NBO/A/000/999/3535N05121E "
    "A) OIIE B) 2601011200 C) 2601011800 E) AD CLSD DUE TO WIP"
)


class NotamTests(unittest.TestCase):
    def test_parse(self):
        notam = parse_notam(SAMPLE_NOTAM)
        self.assertIsNotNone(notam)
        self.assertEqual(notam.number, "A1234/26")
        self.assertEqual(notam.location, "OIIE")
        self.assertEqual(notam.qcode, "QFALC")
        self.assertEqual(notam.start, datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(notam.end, datetime(2026, 1, 1, 18, 0, tzinfo=timezone.utc))

    def test_decode(self):
        glossary = load_config().glossary
        text = decode_notam(parse_notam(SAMPLE_NOTAM), glossary)
        self.assertIn("فرودگاه بین‌المللی امام خمینی", text)
        self.assertIn("بسته شدن کامل فرودگاه", text)
        self.assertIn("فرودگاه بسته است", text)
        self.assertNotIn("AD CLSD", text)

    def test_garbage_returns_none(self):
        self.assertIsNone(parse_notam("سلام دنیا"))


class UtilTests(unittest.TestCase):
    def test_fingerprint_is_stable(self):
        a = util.fingerprint("منبع", "https://x.com/1", "تیتر")
        b = util.fingerprint("منبع", "https://x.com/1", "تیتر")
        self.assertEqual(a, b)
        self.assertNotEqual(a, util.fingerprint("منبع", "https://x.com/2", "تیتر"))

    def test_persian_normalization(self):
        self.assertEqual(util.normalize("ايران"), util.normalize("ایران"))

    def test_numbers_preserved(self):
        self.assertTrue(util.numbers_preserved("باند ۲۹L از 09:10", "باند 29L از 09:10"))
        self.assertFalse(util.numbers_preserved("باند 29L", "باند 92L"))

    def test_jalali(self):
        self.assertEqual(util.g2j(2026, 3, 21), (1405, 1, 1))
        self.assertEqual(util.g2j(2025, 3, 20), (1403, 12, 30))


class ClassifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config()
        cls.classifier = Classifier(cls.config)

    def item(self, title: str, body: str = "", lang: str = "en", country: str = "INTL"):
        return RawItem(
            source_id="t", source_name="test", source_url="", title=title,
            body=body, url="", lang=lang, country=country,
        )

    def test_relevant_airspace(self):
        result = self.classifier.classify(
            self.item("Iran says airspace closed over Tehran FIR", "OIIX NOTAM issued")
        )
        self.assertTrue(result.relevant)
        self.assertEqual(result.category, "airspace")
        self.assertEqual(result.severity, "critical")

    def test_relevant_airline_route(self):
        result = self.classifier.classify(
            self.item("Turkish Airlines suspends flights to Iran", "", country="TR")
        )
        self.assertTrue(result.relevant)
        self.assertIn("Turkish Airlines", result.airlines)
        self.assertIn("TR", result.countries)

    def test_irrelevant(self):
        result = self.classifier.classify(self.item("Weather forecast for Brazil", "rain expected"))
        self.assertFalse(result.relevant)

    def test_airport_code_not_matched_inside_words(self):
        result = self.classifier.classify(self.item("Walking through the park"))
        self.assertFalse(result.relevant)

    def test_sports_flight_word_is_not_relevant(self):
        # «پرواز» تنها، خبر ورزشی را رد نمی‌کند
        result = self.classifier.classify(
            self.item("پرواز تیم ملی ایران به فینال جام ملت‌ها")
        )
        self.assertFalse(result.relevant)

    def test_political_iran_news_without_aviation_is_irrelevant(self):
        result = self.classifier.classify(
            self.item(
                "Iran accused of stoking French unrest through influence operations",
                "Tehran and Moscow were accused of exploiting student protests.",
            )
        )
        self.assertFalse(result.relevant)

    def test_generic_airspace_news_without_iran_is_irrelevant(self):
        result = self.classifier.classify(
            self.item(
                "FAA chief promises AI-powered ATC overhaul",
                "The agency wants to modernise airspace management and aviation safety.",
            )
        )
        self.assertFalse(result.relevant)

    def test_airline_codes_do_not_match_plain_words(self):
        from src.airlines import AirlineIndex

        index = AirlineIndex(self.config.airlines)
        self.assertEqual(index.match("What I saw today is nothing new"), [])
        self.assertEqual(index.match("THY cancelled its flights"), ["Turkish Airlines"])

    def test_airline_route_news_outside_region_is_not_relevant(self):
        """مثبت کاذب واقعی: قطر ایرویز به سیدنی — نه ایران، نه حریم هوایی."""
        result = self.classifier.classify(
            self.item(
                "Qatar Airways to fly to new Western Sydney airport in 2027",
                "The airline will launch three weekly flights from Doha to Western Sydney "
                "International airport using Boeing 777-300ER aircraft. It is the first "
                "Middle East carrier to serve the new airport.",
            )
        )
        self.assertFalse(result.relevant)

    def test_airline_airspace_suspension_in_region_is_relevant(self):
        result = self.classifier.classify(
            self.item(
                "Emirates suspends flights across Middle East airspace",
                "The airline cancelled flights after airspace closures in the region.",
            )
        )
        self.assertTrue(result.relevant)

    def test_gulf_air_name_alone_is_not_a_region_signal(self):
        result = self.classifier.classify(
            self.item(
                "Gulf Air adds a new route to Athens",
                "The airline will operate four weekly flights with an Airbus A321neo aircraft.",
            )
        )
        self.assertFalse(result.relevant)

    def test_conflict_context_is_off_by_default(self):
        result = self.classifier.classify(
            self.item("Iran warns of stronger response to any new attack", "military drills")
        )
        self.assertFalse(result.relevant)

    def test_conflict_context_can_be_enabled(self):
        import copy

        from src.config import Config

        settings = copy.deepcopy(self.config.settings)
        settings["relevance"]["include_iran_conflict_context"] = True
        cfg = Config(
            settings=settings,
            sources=self.config.sources,
            airlines=self.config.airlines,
            glossary=self.config.glossary,
        )
        classifier = Classifier(cfg)
        result = classifier.classify(
            self.item("Iran warns of stronger response to any new attack", "military drills")
        )
        self.assertTrue(result.relevant)
        self.assertEqual(result.category, "context")

    def test_persian_cancellation_is_relevant(self):
        result = self.classifier.classify(
            self.item("لغو پروازها به تهران به‌دلیل بسته شدن حریم هوایی")
        )
        self.assertTrue(result.relevant)
        self.assertEqual(result.severity, "critical")


class HealthTests(unittest.TestCase):
    def test_touch_keeps_state_healthy(self):
        from src.health import HealthState

        state = HealthState(stale_seconds=5)
        state.touch()
        self.assertTrue(state.healthy())
        state.mark_publish()
        self.assertEqual(state.snapshot()["published"], 1)

    def test_state_goes_stale(self):
        import time

        from src.health import HealthState

        state = HealthState(stale_seconds=0)
        time.sleep(0.02)
        self.assertFalse(state.healthy())

    def test_http_endpoint(self):
        import json
        import urllib.request

        from src.health import HealthServer, HealthState

        state = HealthState(stale_seconds=5)
        state.touch()
        server = HealthServer(state, port=0, host="127.0.0.1")
        server.start()
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.port}/healthz", timeout=5
            ) as response:
                payload = json.loads(response.read().decode())
                self.assertEqual(response.status, 200)
                self.assertEqual(payload["status"], "ok")
        finally:
            server.stop()


class TranslatorTests(unittest.TestCase):
    def test_falls_back_when_model_is_overloaded(self):
        import os
        from unittest import mock

        import requests as requests_module

        from src.translator import Translator

        config = load_config()
        translator = Translator(config, db=None)
        translator.api_key = "test-key"
        translator.model = "busy-model"
        translator.fallbacks = ["working-model"]

        class FakeResponse:
            def __init__(self, status_code, payload=None):
                self.status_code = status_code
                self._payload = payload or {}

            def json(self):
                return self._payload

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise requests_module.HTTPError(str(self.status_code))

        calls = []

        def fake_post(url, json=None, headers=None, timeout=None):
            calls.append(json["model"])
            if json["model"] == "busy-model":
                return FakeResponse(503)
            return FakeResponse(200, {"choices": [{"message": {"content": "تیتر\n---\nمتن"}}]})

        with mock.patch("src.translator.requests.post", side_effect=fake_post):
            output = translator._chat("hello", "system")

        self.assertIn("تیتر", output)
        self.assertEqual(translator.active_model, "working-model")
        self.assertIn("busy-model", calls)
        self.assertIn("working-model", calls)

    def test_falls_back_when_model_is_removed(self):
        from unittest import mock

        import requests as requests_module

        from src.translator import Translator

        config = load_config()
        translator = Translator(config, db=None)
        translator.api_key = "test-key"
        translator.model = "gone-model"
        translator.fallbacks = ["working-model"]

        class FakeResponse:
            def __init__(self, status_code, payload=None):
                self.status_code = status_code
                self._payload = payload or {}

            def json(self):
                return self._payload

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise requests_module.HTTPError(str(self.status_code))

        def fake_post(url, json=None, headers=None, timeout=None):
            if json["model"] == "gone-model":
                return FakeResponse(404)
            return FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]})

        with mock.patch("src.translator.requests.post", side_effect=fake_post):
            self.assertEqual(translator._chat("hello", "system"), "ok")

    def test_persian_passthrough_needs_no_network(self):
        from src.translator import Translator

        translator = Translator(load_config(), db=None)
        result = translator.translate_item("لغو پرواز", "متن فارسی", "fa")
        self.assertEqual(result.engine, "passthrough-fa")


class FormatterTests(unittest.TestCase):
    def test_format_contains_source_and_badge(self):
        config = load_config()
        event = {
            "severity": "critical", "category": "airspace",
            "title_fa": "بستن حریم هوایی", "body_fa": "متن خبر",
            "confidence": "multi", "numbers_ok": 1,
            "primary_source": "Anadolu Ajansı",
            "primary_url": "https://example.com/a",
            "first_seen": util.iso(util.now_utc()),
            "airlines": "Turkish Airlines", "countries": "IR,TR",
        }
        text = format_event(event, config)
        self.assertIn("🔴", text)
        self.assertIn("Anadolu Ajansı", text)
        self.assertIn("✅", text)
        self.assertIn("https://example.com/a", text)
        self.assertLess(len(text), 4096)


if __name__ == "__main__":
    unittest.main(verbosity=2)
