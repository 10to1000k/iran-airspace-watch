"""تست‌های هسته — بدون شبکه و بدون کلید API."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
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
        self.assertTrue(
            util.numbers_preserved("باند ۲۹L از 09:10", "باند 29L از 09:10")
        )
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
            source_id="t",
            source_name="test",
            source_url="",
            title=title,
            body=body,
            url="",
            lang=lang,
            country=country,
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
        result = self.classifier.classify(
            self.item("Weather forecast for Brazil", "rain expected")
        )
        self.assertFalse(result.relevant)

    def test_airport_code_not_matched_inside_words(self):
        result = self.classifier.classify(self.item("Walking through the park"))
        self.assertFalse(result.relevant)

    def test_airport_code_does_not_match_a_person_name(self):
        """مثبت کاذب واقعی: «Ika Kartika» با کد فرودگاه IKA قاطی شد."""
        result = self.classifier.classify(
            self.item(
                "Big dreams, growing pains in Indonesia: Prabowo's turbulent 2 years",
                "Bekasi resident Ika Kartika voted in Indonesia's 2024 election "
                "and hoped for a more stable economy.",
            )
        )
        self.assertFalse(result.relevant)

    def test_uppercase_airport_code_still_matches(self):
        result = self.classifier.classify(
            self.item("Flights resume at IKA after runway repairs", "")
        )
        self.assertTrue(result.relevant)

    def test_persian_suffix_does_not_match_an_airline(self):
        """«ماهانه» (monthly) نباید «ماهان» (Mahan Air) شمرده شود."""
        result = self.classifier.classify(
            self.item(
                "تولید ماهانه ۷ تن نان سبوس‌دار در حرم شاهچراغ(ع)",
                "این کارگاه ماهانه هفت تن نان تولید می‌کند.",
            )
        )
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

    def test_iranian_airline_news_without_the_word_iran_is_relevant(self):
        """خبر «ماهان ایر در گوانگژو» لزوماً کلمه‌ی ایران را ندارد."""
        result = self.classifier.classify(
            self.item(
                "Mahan Air flight lands in Guangzhou despite sanctions",
                "Flightradar24 tracked the aircraft.",
            )
        )
        self.assertTrue(result.relevant)

    def test_foreign_airline_route_without_iran_stays_irrelevant(self):
        result = self.classifier.classify(
            self.item(
                "Emirates adds a new route to Athens",
                "The airline will operate four weekly flights with an A350.",
            )
        )
        self.assertFalse(result.relevant)

    def test_russia_country_name_is_not_an_airline(self):
        """«Россия» نام کشور است؛ نباید با Rossiya Airlines قاطی شود."""
        result = self.classifier.classify(
            self.item(
                "Путин: Россия готова содействовать урегулированию кризиса вокруг Ирана",
                "",
            )
        )
        self.assertFalse(result.relevant)

    def test_persian_sky_word_is_not_an_airline(self):
        """«آسمان» یعنی sky؛ ایران آسمان نام ایرلاین است."""
        result = self.classifier.classify(
            self.item(
                "ایران: آسمان تهران امروز صاف است",
                "دمای هوا در پایتخت به ۲۸ درجه می‌رسد.",
            )
        )
        self.assertFalse(result.relevant)

    def test_context_item_with_war_keyword_is_not_critical(self):
        result = self.classifier.classify(
            self.item(
                "Iran says war rhetoric is rising in the region",
                "Officials spoke about the war and attacks but flights operate normally.",
            )
        )
        self.assertTrue(result.relevant)
        self.assertNotEqual(result.severity, "critical")

    def test_long_live_page_does_not_match_by_accident(self):
        """صفحه‌های بلند کلیدواژه‌های ایران و هوانوردی را تصادفی کنار هم می‌آورند."""
        filler = "The weather in the city was mild and traffic was normal. " * 40
        result = self.classifier.classify(
            self.item(
                "Morning update",
                filler + "Iran airspace is closed and all flights are cancelled.",
            )
        )
        self.assertFalse(result.relevant)

    def test_conflict_context_is_off_by_default(self):
        result = self.classifier.classify(
            self.item(
                "Iran warns of stronger response to any new attack", "military drills"
            )
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
            self.item(
                "Iran warns of stronger response to any new attack", "military drills"
            )
        )
        self.assertTrue(result.relevant)
        self.assertEqual(result.category, "context")

    def test_incidental_body_mention_is_not_publishable(self):
        """شکایت کاربر: خبر خودرو که در بدنه‌اش اتفاقی فرودگاه/مسافر آمده پوش می‌شد."""
        result = self.classifier.classify(
            self.item(
                "Iran unveils new domestic car model",
                "The minister said flights of fancy aside, the airport city project "
                "and passenger terminal were discussed at the trade fair.",
            )
        )
        self.assertTrue(result.relevant)       # ثبت می‌شود
        self.assertFalse(result.publishable)   # ولی برای کاربر نمی‌رود

    def test_airline_livery_with_incidental_iran_is_not_publishable(self):
        result = self.classifier.classify(
            self.item(
                "Turkish Airlines unveils new livery",
                "The airline painted an aircraft. Iran was mentioned in a list "
                "of destinations served.",
            )
        )
        self.assertFalse(result.publishable)

    def test_main_topic_is_publishable(self):
        result = self.classifier.classify(
            self.item("Iran says airspace closed over Tehran FIR", "OIIX NOTAM issued")
        )
        self.assertTrue(result.publishable)

    def test_geopolitics_with_generic_flight_mention_is_not_publishable(self):
        result = self.classifier.classify(
            self.item(
                "Iran says war rhetoric is rising in the region",
                "Officials spoke about the war and attacks but flights operate normally.",
            )
        )
        self.assertFalse(result.publishable)

    def test_hashtag_pair_is_on_topic(self):
        result = self.classifier.classify(
            self.item("فوری #ایران #NOTAM حریم هوایی بسته شد", "")
        )
        self.assertTrue(result.relevant)
        self.assertTrue(result.publishable)

    def test_persian_cancellation_is_relevant(self):
        result = self.classifier.classify(
            self.item("لغو پروازها به تهران به‌دلیل بسته شدن حریم هوایی")
        )
        self.assertTrue(result.relevant)
        self.assertEqual(result.severity, "critical")

    def test_uppercase_airline_code_matched_inside_classifier(self):
        """کدهای ۳حرفی بزرگ مثل THY و PIA باید داخل Classifier هم تشخیص داده شوند."""
        tr_item = self.classifier.classify(
            self.item("THY İran seferlerini iptal etti", "", lang="tr", country="TR")
        )
        self.assertTrue(tr_item.relevant)
        self.assertIn("Turkish Airlines", tr_item.airlines)

        pk_item = self.classifier.classify(
            self.item(
                "PIA suspends flights to Iran over airspace warning", "", country="PK"
            )
        )
        self.assertTrue(pk_item.relevant)
        self.assertIn("Pakistan International Airlines", pk_item.airlines)
        self.assertIn("PK", pk_item.countries)


class CollectorTests(unittest.TestCase):
    def test_live_blog_and_index_urls_are_skipped_but_fr24_articles_kept(self):
        from src.collectors import RssCollector

        collector = RssCollector(load_config(), db=None)
        self.assertTrue(
            collector._should_skip(
                "https://www.middleeasteye.net/live-blog/live-blog-update/morning-update-647"
            )
        )
        self.assertTrue(collector._should_skip("https://example.com/tag/iran/"))
        self.assertTrue(collector._should_skip("https://ops.group/blog/"))
        self.assertFalse(
            collector._should_skip(
                "https://www.flightradar24.com/blog/flight-tracking-news/iran-airspace-rerouting/"
            )
        )
        self.assertFalse(
            collector._should_skip(
                "https://ops.group/blog/tehran-fir-airspace-risk-warning/"
            )
        )
        self.assertFalse(
            collector._should_skip(
                "https://www.aa.com.tr/en/middle-east/iran-closes-airspace/4095206"
            )
        )

    def test_build_jobs_skips_disabled_collectors(self):
        from src.airlines import AirlineIndex
        from src.collectors import build_jobs

        config = load_config(dry_run=True)
        jobs = build_jobs(config, db=None, airline_index=AirlineIndex(config.airlines))
        # فقط rss و gdelt (متن‌باز، بدون کلید) پیش‌فرض فعال‌اند
        self.assertEqual([job.name for job in jobs], ["rss", "gdelt"])

    def test_gdelt_articles_become_items(self):
        from src.collectors import GdeltCollector

        collector = GdeltCollector(load_config())
        items = collector._to_items({
            "articles": [
                {
                    "url": "https://example.com/a1",
                    "title": "Iran closes airspace over Tehran",
                    "domain": "example.com",
                    "language": "English",
                    "seendate": "20261026T120000Z",
                },
                {"url": "https://example.com/a1", "title": "duplicate", "domain": "example.com"},
                {"url": "", "title": "no url", "domain": "x"},
            ]
        })
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "gdelt")
        self.assertEqual(items[0].source_id, "gdelt_example_com")
        self.assertIsNotNone(items[0].published_at)

    def test_x_queries_include_hashtags(self):
        from src.airlines import AirlineIndex
        from src.collectors import XCollector

        config = load_config()
        collector = XCollector(config, AirlineIndex(config.airlines))
        queries = collector.queries()
        self.assertTrue(any("#" in q for q in queries))


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

    def test_touch_alone_cannot_mask_dead_collector_or_thread(self):
        import time

        from src.health import HealthState

        state = HealthState(stale_seconds=60)
        state.expect_poll("rss", 0.02)
        time.sleep(0.03)
        state.touch()  # حتی اگر حلقه‌ی اصلی زنده باشد، عقب‌افتادن کالکتور باید ناسالم شود
        self.assertFalse(state.healthy())
        self.assertTrue(any("poll-stale:rss" in r for r in state.snapshot()["reasons"]))

        state.mark_poll("rss")
        self.assertTrue(state.healthy())

        state.add_check("telegram_listener", lambda: False)
        state.touch()
        self.assertFalse(state.healthy())

    def test_http_endpoint(self):
        import json
        import urllib.error
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

            state.add_check("broken_worker", lambda: False)
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{server.port}/healthz", timeout=5
                )
            self.assertEqual(ctx.exception.code, 503)
            ctx.exception.close()
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
            return FakeResponse(
                200, {"choices": [{"message": {"content": "تیتر\n---\nمتن"}}]}
            )

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

    def _free_chain_translator(self, mock_env):
        from unittest import mock

        from src.translator import Translator

        with mock.patch.dict("os.environ", mock_env, clear=False):
            return Translator(load_config(), db=None)

    def test_free_providers_are_ordered_and_skip_missing_keys(self):
        translator = self._free_chain_translator(
            {"OPENROUTER_API_KEY": "or-key", "GEMINI_API_KEY": "gm-key"}
        )
        chain = translator._provider_chain()
        self.assertEqual([p.name for p in chain], ["gemini", "openrouter"])
        self.assertEqual(chain[0].models[0], "gemini-2.5-flash")
        self.assertTrue(translator.enabled)
        self.assertEqual(translator.active_provider, "gemini")

    def test_falls_back_across_providers_on_rate_limit(self):
        from unittest import mock

        import requests as requests_module

        from src.translator import Translator  # noqa: F401

        translator = self._free_chain_translator(
            {"GEMINI_API_KEY": "gm-key", "GROQ_API_KEY": "gq-key"}
        )

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
            calls.append((url, headers["Authorization"]))
            if "generativelanguage" in url:
                return FakeResponse(429)  # سهمیه‌ی روزانه‌ی رایگان تمام شده
            return FakeResponse(
                200, {"choices": [{"message": {"content": "تیتر\n---\nمتن"}}]}
            )

        with mock.patch("src.translator.requests.post", side_effect=fake_post):
            with mock.patch("src.translator.time.sleep"):
                output = translator._chat("hello", "system")

        self.assertIn("تیتر", output)
        self.assertEqual(translator.active_provider, "groq")
        self.assertEqual(translator.active_model, "llama-3.3-70b-versatile")
        self.assertTrue(any("generativelanguage" in u for u, _ in calls))
        self.assertTrue(any("groq.com" in u for u, _ in calls))
        # هر سرویس با کلید خودش صدا زده شده
        self.assertIn(("https://api.groq.com/openai/v1/chat/completions",
                       "Bearer gq-key"), calls)

    def test_bad_key_skips_whole_provider_without_retry(self):
        from unittest import mock

        import requests as requests_module

        translator = self._free_chain_translator(
            {"GEMINI_API_KEY": "bad-key", "OPENROUTER_API_KEY": "or-key"}
        )

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
            calls.append(url)
            if "generativelanguage" in url:
                return FakeResponse(401)
            return FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]})

        with mock.patch("src.translator.requests.post", side_effect=fake_post):
            self.assertEqual(translator._chat("hello", "system"), "ok")

        self.assertEqual(translator.active_provider, "openrouter")
        # کلید بد فقط یک بار امتحان شده، نه برای هر مدل/تلاش
        self.assertEqual(sum("generativelanguage" in u for u in calls), 1)

    def test_persian_passthrough_needs_no_network(self):
        from src.translator import Translator

        translator = Translator(load_config(), db=None)
        result = translator.translate_item("لغو پرواز", "متن فارسی", "fa")
        self.assertEqual(result.engine, "passthrough-fa")


class TelegramListenerTests(unittest.TestCase):
    def test_conflict_marks_health_so_pod_is_not_restarted(self):
        """409 یعنی نمونه‌ی خارجی تداخل دارد؛ نخ زنده است و نباید
        سلامت‌سنج به‌خاطر نرسیدن getUpdates موفق، پاد را ری‌استارت کند."""
        from unittest import mock

        from src.health import HealthState
        from src.telegram_bot import TelegramBot, TelegramError

        class FakeDb:
            def kv_get(self, key, default=None):
                return "0"

            def kv_set(self, key, value):
                pass

        class FakeStop:
            def __init__(self):
                self.stopped = False

            def is_set(self):
                return self.stopped

            def wait(self, _seconds):
                self.stopped = True
                return True

            def set(self):
                self.stopped = True

        config = load_config()
        health = HealthState(stale_seconds=300)
        health.expect_poll("telegram_bot", 300)
        bot = TelegramBot(config, FakeDb(), health=health)
        bot._stop = FakeStop()

        def conflict(*_args, **_kwargs):
            raise TelegramError("Conflict", status_code=409)

        with mock.patch.object(bot, "_call", side_effect=conflict):
            bot._listen_loop()

        self.assertEqual(bot._conflict_count, 1)
        # با وجود خطا، poll ثبت شده و وضعیت سالم می‌ماند
        self.assertTrue(health.healthy())
        self.assertIn("telegram_bot", health.snapshot()["last_poll_utc"])


class MultiRecipientTests(unittest.TestCase):
    def _msg(self, chat_id, text):
        return {"message": {"chat": {"id": chat_id}, "text": text}}

    def test_second_start_does_not_evict_first_subscriber(self):
        import tempfile
        from unittest import mock

        from src.db import Database
        from src.telegram_bot import TelegramBot

        config = load_config()
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            try:
                bot = TelegramBot(config, db)
                with mock.patch.object(bot, "send", return_value=1) as send:
                    bot._handle_update(self._msg(111, "/start"))
                    bot._handle_update(self._msg(222, "/start"))

                self.assertEqual(bot.recipients(), ["111", "222"])
                self.assertEqual(bot.chat_id(), "111")  # گیرنده‌ی اصلی همان اولی

                with mock.patch.object(bot, "send", return_value=1):
                    bot._handle_update(self._msg(111, "/stop"))
                self.assertEqual(bot.recipients(), ["222"])
                # /stop نفر دوم را دست نمی‌زند
                self.assertEqual(send.call_count, 2)
            finally:
                db.close()

    def test_event_is_broadcast_to_every_recipient(self):
        import tempfile

        from src.db import Database
        from src.pipeline import Pipeline

        config = load_config(dry_run=False)
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            try:

                class MultiBot:
                    configured = True

                    def __init__(self):
                        self.sent: list[tuple[str, int]] = []

                    def recipients(self):
                        return ["111", "222"]

                    def chat_id(self):
                        return "111"

                    def feedback_keyboard(self, event_id, url=""):
                        return []

                    def send(self, text, chat_id=None, **kwargs):
                        message_id = 1000 + int(chat_id)
                        self.sent.append((str(chat_id), message_id))
                        return message_id

                    def edit(self, *args, **kwargs):
                        pass

                bot = MultiBot()
                pipeline = Pipeline(config, db, bot)
                item = RawItem(
                    source_id="ir_irna",
                    source_name="IRNA",
                    source_url="https://irna.ir/rss",
                    title="لغو پروازها در فرودگاه مهرآباد تهران",
                    body="پروازهای مهرآباد موقتاً متوقف شد.",
                    url="https://irna.ir/news/2001",
                    lang="fa",
                    country="IR",
                )
                outcome = pipeline.process(item)
                self.assertEqual(outcome, "published")
                self.assertEqual(bot.sent, [("111", 1111), ("222", 1222)])

                event = db.latest_events(1)[0]
                refs = db.message_refs(int(event["id"]))
                self.assertEqual(refs, {"111": 1111, "222": 1222})
                # /wrong هر گیرنده روی پیام خودش کار می‌کند
                self.assertIsNotNone(db.find_event_by_message("222", 1222))
                self.assertIsNotNone(db.find_event_by_message("111", 1111))
            finally:
                db.close()


class ChatAboutNewsTests(unittest.TestCase):
    def _make_bot(self, tmp):
        from unittest import mock

        from src.db import Database
        from src.telegram_bot import TelegramBot

        db = Database(Path(tmp) / "test.db")
        event_id, _ = db.create_event({
            "fingerprint": "chat-test-1",
            "tokens": {"tehran"},
            "entity_key": "",
            "category": "airspace",
            "severity": "critical",
            "countries": ["IR"],
            "airlines": [],
            "title_fa": "بسته‌شدن حریم هوایی تهران",
            "body_fa": "حریم هوایی تهران موقتاً بسته شد.",
            "confidence": "single",
            "numbers_ok": 1,
            "primary_url": "https://example.com/n1",
            "primary_source": "Example",
        })
        bot = TelegramBot(load_config(), db)
        sent = []
        bot.send = lambda text, chat_id=None, **kw: sent.append((chat_id, text)) or 1
        return db, bot, event_id, sent

    def test_keyboard_has_chat_button(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db, bot, event_id, _sent = self._make_bot(tmp)
            try:
                rows = bot.feedback_keyboard(event_id, "https://example.com/n1")
                flat = [btn for row in rows for btn in row]
                self.assertTrue(any(b.get("callback_data") == f"ask:{event_id}" for b in flat))
                self.assertTrue(any("گفت‌وگو" in b.get("text", "") for b in flat))
            finally:
                db.close()

    def test_button_starts_chat_and_questions_go_to_responder(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db, bot, event_id, sent = self._make_bot(tmp)
            try:
                seen = {}

                def responder(event, question, history):
                    seen["event_id"] = int(event["id"])
                    seen["question"] = question
                    seen["history"] = history
                    return "پاسخ تستی درباره‌ی خبر"

                bot.set_chat_responder(responder)
                bot._handle_callback({
                    "id": "cb1",
                    "data": f"ask:{event_id}",
                    "from": {"id": 555},
                    "message": {"chat": {"id": 111}},
                })
                self.assertEqual(bot._active_chat_event("111"), event_id)

                bot._handle_update(
                    {"message": {"chat": {"id": 111}, "text": "علت بسته‌شدن چه بود؟"}}
                )
                self.assertEqual(seen["event_id"], event_id)
                self.assertEqual(seen["question"], "علت بسته‌شدن چه بود؟")
                self.assertTrue(any("پاسخ تستی" in t for _c, t in sent))

                # پرسش دوم حافظه‌ی مکالمه را هم می‌گیرد
                bot._handle_update(
                    {"message": {"chat": {"id": 111}, "text": "تا کی ادامه دارد؟"}}
                )
                self.assertEqual(len(seen["history"]), 1)

                bot._handle_update(
                    {"message": {"chat": {"id": 111}, "text": "/endchat"}}
                )
                self.assertIsNone(bot._active_chat_event("111"))

                # بعد از پایان، پیام عادی دیگر به responder نمی‌رود
                seen.clear()
                bot._handle_update(
                    {"message": {"chat": {"id": 111}, "text": "سؤال بی‌ربط"}}
                )
                self.assertEqual(seen, {})
            finally:
                db.close()

    def test_translator_answers_from_event_context(self):
        from unittest import mock

        from src.translator import Translator

        translator = Translator(load_config(), db=None)
        translator.api_key = "test-key"
        event = {
            "title_fa": "بسته‌شدن حریم هوایی تهران",
            "body_fa": "حریم هوایی تهران تا ساعت ۱۸ بسته است.",
            "category": "airspace",
            "severity": "critical",
            "first_seen": "2026-01-01T12:00:00+00:00",
            "primary_source": "Example",
            "primary_url": "https://example.com/n1",
            "sources_count": 2,
            "airlines": "",
            "countries": "IR",
        }
        with mock.patch.object(
            translator, "_chat", return_value="بر اساس خبر، تا ساعت ۱۸ بسته است."
        ) as chat:
            answer = translator.answer_about_event(event, [], "تا کی بسته است؟", [])

        self.assertIn("۱۸", answer)
        user_content = chat.call_args.args[0]
        self.assertIn("بسته‌شدن حریم هوایی تهران", user_content)
        self.assertIn("تا کی بسته است؟", user_content)


class FormatterTests(unittest.TestCase):
    def test_format_contains_source_and_badge(self):
        from src.formatter import format_publication_notice

        config = load_config()
        event = {
            "severity": "critical",
            "category": "airspace",
            "title_fa": "بستن حریم هوایی",
            "body_fa": "متن خبر",
            "confidence": "multi",
            "numbers_ok": 1,
            "primary_source": "Anadolu Ajansı",
            "primary_url": "https://example.com/a",
            "first_seen": util.iso(util.now_utc()),
            "airlines": "Turkish Airlines",
            "countries": "IR,TR,PK,TH,VN",
        }
        text = format_event(event, config)
        self.assertIn("🔴", text)
        self.assertIn("Anadolu Ajansı", text)
        self.assertIn("✅", text)
        self.assertIn("پاکستان", text)
        self.assertIn("تایلند", text)
        self.assertIn("ویتنام", text)
        self.assertIn("https://example.com/a", text)
        self.assertLess(len(text), 4096)

        notice = format_publication_notice(event)
        self.assertNotIn("##", notice)
        self.assertIn("#هشدار", notice)


class PipelineRecoveryTests(unittest.TestCase):
    def test_unsent_event_is_queued_and_flushed_later(self):
        import tempfile

        from src.db import Database
        from src.pipeline import Pipeline

        config = load_config(dry_run=False)
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            try:

                class FlakyBot:
                    configured = True

                    def __init__(self):
                        self.should_fail = True
                        self._chat_id = "123456"

                    def chat_id(self):
                        return self._chat_id

                    def feedback_keyboard(self, event_id, url=""):
                        return []

                    def send(self, text, **kwargs):
                        if self.should_fail:
                            raise RuntimeError("temporary network timeout")
                        return 777

                bot = FlakyBot()
                pipeline = Pipeline(config, db, bot)
                item = RawItem(
                    source_id="ir_irna",
                    source_name="IRNA",
                    source_url="https://irna.ir/rss",
                    title="لغو پروازها در فرودگاه مهرآباد تهران",
                    body="پروازهای مهرآباد موقتاً متوقف شد.",
                    url="https://irna.ir/news/1001",
                    lang="fa",
                    country="IR",
                )
                outcome = pipeline.process(item)
                self.assertEqual(outcome, "queued")
                self.assertEqual(len(db.unsent_events()), 1)

                bot.should_fail = False
                sent = pipeline.flush_unsent()
                self.assertEqual(sent, 1)
                self.assertEqual(len(db.unsent_events()), 0)
            finally:
                db.close()


class OffTopicGateTests(unittest.TestCase):
    def test_off_topic_event_is_stored_but_not_sent(self):
        import tempfile

        from src.db import Database
        from src.pipeline import Pipeline

        config = load_config(dry_run=False)
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            try:

                class CountingBot:
                    configured = True

                    def __init__(self):
                        self.sent = []

                    def recipients(self):
                        return ["111"]

                    def chat_id(self):
                        return "111"

                    def feedback_keyboard(self, event_id, url=""):
                        return []

                    def send(self, text, **kwargs):
                        self.sent.append(text)
                        return 42

                    def edit(self, *args, **kwargs):
                        pass

                bot = CountingBot()
                pipeline = Pipeline(config, db, bot)
                item = RawItem(
                    source_id="en_example",
                    source_name="Example",
                    source_url="https://example.com/rss",
                    title="Iran unveils new domestic car model",
                    body=(
                        "The minister said flights of fancy aside, the airport city "
                        "project and passenger terminal were discussed."
                    ),
                    url="https://example.com/car",
                    lang="en",
                    country="INTL",
                )
                outcome = pipeline.process(item)
                self.assertEqual(outcome, "off-topic")
                self.assertEqual(bot.sent, [])
                self.assertEqual(db.stats()["events"], 1)
            finally:
                db.close()


class KeepAliveTests(unittest.TestCase):
    def _mock_session(self, deploy_replicas: int, pods: list[dict]):
        class Resp:
            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                return None

            def json(self):
                return self._data

        class Session:
            def __init__(self):
                self.headers = {}
                self.patches: list[tuple[str, dict]] = []

            def get(self, url, **kwargs):
                if url.endswith("/deployments/airwatch"):
                    return Resp({"spec": {"replicas": deploy_replicas}})
                return Resp({"items": pods})

            def patch(self, url, json=None, **kwargs):
                self.patches.append((url, json))
                return Resp({})

        return Session()

    def test_scales_up_when_idled_to_zero(self):
        from src.keepalive import KeepAliveClient

        session = self._mock_session(deploy_replicas=0, pods=[])
        client = KeepAliveClient(
            "https://k8s.local", "tok", "demo-ns", verify=False, session=session
        )
        action = client.ensure_awake()
        self.assertEqual(action, "scaled-up")
        self.assertEqual(session.patches[0][1], {"spec": {"replicas": 1}})

    def test_recycles_pod_before_12h_sandbox_limit(self):
        from datetime import timedelta

        from src.keepalive import KeepAliveClient

        now = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
        old_start = util.iso(now - timedelta(hours=11, minutes=5))
        pods = [
            {
                "metadata": {"name": "airwatch-abc"},
                "status": {
                    "phase": "Running",
                    "startTime": old_start,
                    "containerStatuses": [{"restartCount": 0}],
                },
            }
        ]
        session = self._mock_session(deploy_replicas=1, pods=pods)
        client = KeepAliveClient(
            "https://k8s.local", "tok", "demo-ns", verify=False, session=session
        )
        action = client.ensure_awake(now=now)
        self.assertEqual(action, "restarted")
        self.assertIn("restartedAt", str(session.patches[0][1]))

    def test_leaves_healthy_young_pod_untouched(self):
        from datetime import timedelta

        from src.keepalive import KeepAliveClient

        now = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
        fresh_start = util.iso(now - timedelta(hours=2))
        pods = [
            {
                "metadata": {"name": "airwatch-abc"},
                "status": {
                    "phase": "Running",
                    "startTime": fresh_start,
                    "containerStatuses": [{"restartCount": 0}],
                },
            }
        ]
        session = self._mock_session(deploy_replicas=1, pods=pods)
        client = KeepAliveClient(
            "https://k8s.local", "tok", "demo-ns", verify=False, session=session
        )
        self.assertEqual(client.ensure_awake(now=now), "healthy")
        self.assertEqual(session.patches, [])


class ManifestValidationTests(unittest.TestCase):
    def test_openshift_manifests_are_valid_yaml(self):
        import yaml

        root = Path(__file__).resolve().parent.parent
        app_docs = [
            d
            for d in yaml.safe_load_all((root / "deploy/app.yaml").read_text("utf-8"))
            if d
        ]
        kinds = [doc["kind"] for doc in app_docs]
        self.assertEqual(
            kinds,
            ["Deployment", "ServiceAccount", "Role", "RoleBinding", "CronJob"],
        )
        build_docs = [
            d
            for d in yaml.safe_load_all((root / "deploy/build.yaml").read_text("utf-8"))
            if d
        ]
        self.assertEqual(
            [doc["kind"] for doc in build_docs],
            ["ImageStream", "BuildConfig", "PersistentVolumeClaim"],
        )

    def test_keepalive_cronjob_always_pulls_latest_image(self):
        """تگ ایمیج ثابت (:latest) است؛ با IfNotPresent نود ایمیج قدیمیِ
        کش‌شده را اجرا می‌کند — همان باگی که keepalive را Error کرد."""
        import yaml

        root = Path(__file__).resolve().parent.parent
        app_docs = [
            d
            for d in yaml.safe_load_all((root / "deploy/app.yaml").read_text("utf-8"))
            if d
        ]
        cronjob = next(d for d in app_docs if d["kind"] == "CronJob")
        container = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"][
            "containers"
        ][0]
        self.assertEqual(container["imagePullPolicy"], "Always")


if __name__ == "__main__":
    unittest.main(verbosity=2)
