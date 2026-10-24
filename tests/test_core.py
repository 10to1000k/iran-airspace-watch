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


class SocialTextTests(unittest.TestCase):
    def test_hashtags_normalize_and_filter_generic_noise(self):
        from src.social import extract_hashtags

        self.assertEqual(
            extract_hashtags("#IranAirspace #حریم‌هوایی_ایران #airspace #news #12345"),
            ["iranairspace", "حریم_هوایی_ایران"],
        )

    def test_hashtags_expand_for_classifier(self):
        from src.social import expand_hashtags

        expanded = util.normalize(expand_hashtags("#IranAirspace #حریم_هوایی"))
        self.assertIn("iran airspace", expanded)
        self.assertIn("حریم هوایی", expanded)


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

    def test_sports_team_mentioning_an_airport_is_not_a_service_update(self):
        result = self.classifier.classify(
            self.item(
                "Iran football team reaches the final",
                "Fans and players arrive at the airport before the match.",
            )
        )
        self.assertFalse(result.relevant)

    def test_iran_airport_management_news_is_relevant_but_not_critical(self):
        result = self.classifier.classify(
            self.item(
                "عضو جدید هیئت‌مدیره شرکت شهر فرودگاهی امام خمینی منصوب شد",
                "در نشست امروز برنامه‌ی توسعه‌ی شهر فرودگاهی، سرمایه‌گذاری و مدیریت درآمد بررسی شد.",
                lang="fa",
                country="IR",
            )
        )
        self.assertTrue(result.relevant)
        self.assertEqual(result.category, "airport")
        self.assertEqual(result.severity, "important")

    def test_iran_civil_aviation_economy_is_relevant_without_flight_keywords(self):
        result = self.classifier.classify(
            self.item(
                "Iran's civil aviation industry attracts new investment",
                "The aviation sector is preparing a long-term business plan.",
            )
        )
        self.assertTrue(result.relevant)
        self.assertNotEqual(result.severity, "critical")

    def test_generic_cancellation_language_is_not_an_air_service_signal(self):
        result = self.classifier.classify(
            self.item(
                "Iranian football final cancelled after a dispute",
                "The tournament was cancelled, and the team will return tomorrow.",
            )
        )
        self.assertFalse(result.relevant)

    def test_military_flight_testing_is_not_civil_air_service(self):
        result = self.classifier.classify(
            self.item(
                "Iran Air Force conducts flight tests",
                "Military crews tested a new fighter aircraft.",
            )
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
        self.assertEqual(index.match("Iran airspace is closed"), [])
        self.assertNotIn("Iran Air", index.match("Iran Air Force performs a drill"))
        self.assertIn("Iran Air", index.match("Iran Air adds flights to Tehran"))

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

    def test_airline_airspace_news_must_explicitly_involve_iran(self):
        result = self.classifier.classify(
            self.item(
                "Emirates suspends flights across Middle East airspace",
                "The airline cancelled flights after airspace closures in the region.",
            )
        )
        self.assertFalse(result.relevant)

    def test_airline_airspace_news_that_mentions_iran_is_relevant(self):
        result = self.classifier.classify(
            self.item(
                "Emirates suspends flights across Middle East airspace",
                "Iranian airspace restrictions forced the airline to reroute flights.",
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

    def test_iranian_airline_alias_is_nearby_even_without_canonical_name(self):
        result = self.classifier.classify(
            self.item("ماهان پروازهای بیشتری به گوانگژو برقرار می‌کند")
        )
        self.assertTrue(result.relevant)
        self.assertIn("Mahan Air", result.airlines)

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

    def test_iran_and_flight_terms_far_apart_in_lead_are_rejected(self):
        filler = "The report discusses trade and budget negotiations. " * 7
        result = self.classifier.classify(
            self.item(
                "Iran announces a new economic policy",
                filler + "Airlines continue normal flights across Asia.",
            )
        )
        self.assertFalse(result.relevant)

    def test_irrelevant_indonesia_political_story_is_rejected(self):
        result = self.classifier.classify(
            self.item(
                "Big dreams, growing pains in Indonesia: Prabowo’s turbulent 2 years in power",
                "The political report mentions Iran sanctions and a military aircraft parade.",
            )
        )
        self.assertFalse(result.relevant)

    def test_iran_airspace_hashtags_are_expanded_for_matching(self):
        result = self.classifier.classify(
            self.item("#IranAirspace closes after #پرواز_ایران cancellations")
        )
        self.assertTrue(result.relevant)
        self.assertEqual(result.category, "airspace")

    def test_conflict_context_is_off_by_default(self):
        result = self.classifier.classify(
            self.item("Iran warns of stronger response to any new attack", "military drills")
        )
        self.assertFalse(result.relevant)

    def test_legacy_conflict_flag_does_not_bypass_aviation_scope(self):
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
        result = Classifier(cfg).classify(
            self.item("Iran warns of stronger response to any new attack", "military drills")
        )
        self.assertFalse(result.relevant)

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
            self.item("PIA suspends flights to Iran over airspace warning", "", country="PK")
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

    def test_telegram_public_preview_parses_recent_post_without_admin(self):
        from src.collectors import TelegramPublicPreviewCollector

        config = load_config()
        config.sources["telegram_channels"] = []
        source = {
            "id": "tg_test_baku",
            "name": "Baku Airport",
            "username": "bakuairport",
            "lang": "en",
            "country": "AZ",
            "tier": 0,
            "enabled": True,
        }
        config.sources["telegram_channels"] = [source]
        now = int(datetime.now(timezone.utc).timestamp())
        page = f"""
        <div class=\"tgme_widget_message_wrap\">
          <div class=\"tgme_widget_message js-widget_message\"
               data-post=\"bakuairport/1760\" data-post-date=\"{now}\">
            <div class=\"tgme_widget_message_bubble\">
              <div class=\"tgme_widget_message_text js-message_text\" dir=\"auto\">
                New flights to Tehran.<br/>OIIE route update.
              </div>
              <a class=\"tgme_widget_message_date\" href=\"https://t.me/bakuairport/1760\">
                <time datetime=\"{datetime.now(timezone.utc).isoformat()}\">now</time>
              </a>
            </div>
          </div>
        </div>
        """
        collector = TelegramPublicPreviewCollector(config, db=None)

        items = collector.parse_page(page, source)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "telegram")
        self.assertEqual(items[0].source_id, "tg_test_baku")
        self.assertEqual(items[0].url, "https://t.me/bakuairport/1760")
        self.assertEqual(items[0].title, "New flights to Tehran.")
        self.assertIn("OIIE route update.", items[0].body)
        self.assertEqual(items[0].country, "AZ")
        self.assertEqual(items[0].extra["telegram_collection"], "public_preview")

    def test_telegram_public_preview_ignores_old_posts_and_dedupes_seen_items(self):
        import tempfile
        from unittest import mock

        from src.collectors import TelegramPublicPreviewCollector
        from src.db import Database

        config = load_config()
        source = {
            "id": "tg_test_old",
            "name": "Test Channel",
            "username": "testchannel",
            "lang": "en",
            "country": "INTL",
            "enabled": True,
        }
        config.sources["telegram_channels"] = [source]
        now = int(datetime.now(timezone.utc).timestamp())
        recent_page = f"""
        <div class=\"tgme_widget_message\" data-post=\"testchannel/5\" data-post-date=\"{now}\">
          <div class=\"tgme_widget_message_text js-message_text\">Iran flight update</div>
        </div>
        """
        old_page = f"""
        <div class=\"tgme_widget_message\" data-post=\"testchannel/4\" data-post-date=\"{now - 86400}\">
          <div class=\"tgme_widget_message_text js-message_text\">Old Iran flight update</div>
        </div>
        """

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "preview.db")
            try:
                collector = TelegramPublicPreviewCollector(config, db)
                recent = collector.parse_page(recent_page, source)
                self.assertEqual(len(recent), 1)
                fingerprint = util.fingerprint(
                    recent[0].source_id, recent[0].url, recent[0].title, recent[0].body[:400]
                )
                self.assertTrue(db.add_item(recent[0], fingerprint))
                self.assertEqual(collector.parse_page(recent_page, source), [])
                self.assertEqual(collector.parse_page(old_page, source), [])
            finally:
                db.close()

    def test_telegram_public_preview_polls_public_web_page(self):
        import os
        from unittest import mock

        from src.collectors import TelegramPublicPreviewCollector

        with mock.patch.dict(os.environ, {"ENABLE_TELEGRAM_PUBLIC_PREVIEW": "true"}):
            config = load_config()
        source = {
            "id": "tg_poll_test",
            "name": "Test Channel",
            "username": "testchannel",
            "lang": "en",
            "country": "IR",
            "enabled": True,
        }
        config.sources["telegram_channels"] = [source]
        now = int(datetime.now(timezone.utc).timestamp())
        page = f"""
        <div class=\"tgme_widget_message\" data-post=\"testchannel/9\" data-post-date=\"{now}\">
          <div class=\"tgme_widget_message_text js-message_text\">Iran flight operations update</div>
        </div>
        """

        class FakeResponse:
            status_code = 200
            headers = {}
            encoding = "utf-8"

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size):
                yield page.encode("utf-8")

        collector = TelegramPublicPreviewCollector(config, db=None)
        with mock.patch("src.collectors.requests.get", return_value=FakeResponse()) as get:
            items = collector.poll()

        self.assertEqual(len(items), 1)
        self.assertEqual(get.call_args.args[0], "https://t.me/s/testchannel")
        self.assertTrue(get.call_args.kwargs["stream"])

    def test_build_jobs_skips_disabled_collectors(self):
        import os
        from unittest import mock

        from src.airlines import AirlineIndex
        from src.collectors import build_jobs

        env = {
            "ENABLE_TELEGRAM_PUBLIC_PREVIEW": "false",
            "ENABLE_TELEGRAM_CHANNELS": "false",
            "ENABLE_X": "false",
            "TELEGRAM_CHANNELS": "",
        }
        with mock.patch.dict(os.environ, env):
            config = load_config(dry_run=True)
            jobs = build_jobs(config, db=None, airline_index=AirlineIndex(config.airlines))
        self.assertEqual([job.name for job in jobs], ["rss"])

    def test_build_jobs_schedules_public_telegram_without_bot(self):
        import os
        from unittest import mock

        from src.airlines import AirlineIndex
        from src.collectors import build_jobs

        env = {
            "ENABLE_TELEGRAM_PUBLIC_PREVIEW": "true",
            "ENABLE_TELEGRAM_CHANNELS": "false",
            "ENABLE_X": "false",
            "TELEGRAM_CHANNELS": "",
        }
        with mock.patch.dict(os.environ, env):
            config = load_config(dry_run=True)
            jobs = build_jobs(config, db=None, airline_index=AirlineIndex(config.airlines))
        self.assertIn("telegram_public", [job.name for job in jobs])
        public_job = next(job for job in jobs if job.name == "telegram_public")
        self.assertEqual(public_job.interval, 300)

    def test_x_collector_uses_official_api_and_parses_recent_posts(self):
        import os
        from unittest import mock

        from src.airlines import AirlineIndex
        from src.collectors import XCollector

        with mock.patch.dict(
            os.environ,
            {"ENABLE_X": "true", "X_BEARER_TOKEN": "test-bearer"},
        ):
            config = load_config()
            config.settings["poll"]["x_max_queries"] = 1
            collector = XCollector(config, AirlineIndex(config.airlines))
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

            class FakeResponse:
                status_code = 200

                def raise_for_status(self):
                    return None

                def json(self):
                    return {
                        "data": [{
                            "id": "123",
                            "text": "Iranian airspace #IranAirspace flight notice",
                            "author_id": "u1",
                            "created_at": now,
                            "lang": "en",
                            "public_metrics": {"like_count": 7, "retweet_count": 2},
                        }],
                        "includes": {"users": [{"id": "u1", "username": "aviation_watch"}]},
                    }

            with mock.patch("src.collectors.requests.get", return_value=FakeResponse()) as get:
                items = collector.poll()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "x")
        self.assertEqual(items[0].source_id, "x_aviation_watch")
        self.assertEqual(items[0].url, "https://x.com/aviation_watch/status/123")
        self.assertEqual(items[0].extra["likes"], 7)
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer test-bearer")

    def test_telegram_bot_receives_posts_only_from_configured_channels(self):
        import os
        import tempfile
        from unittest import mock

        from src.db import Database
        from src.telegram_bot import TelegramBot

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "123456:test-token",
                "ENABLE_TELEGRAM_CHANNELS": "true",
                "TELEGRAM_CHANNELS": "iranaviation",
            },
        ):
            config = load_config()
            db = Database(Path(tmp) / "tg.db")
            try:
                bot = TelegramBot(config, db)
                sent = []
                bot.send = lambda text, chat_id=None, **kwargs: sent.append((text, chat_id))
                bot._handle_update({"message": {
                    "chat": {"id": 123},
                    "text": "/hashtags",
                }})
                self.assertEqual(sent[0][1], "123")
                self.assertIn("#IranAirspace", sent[0][0])
                now = int(datetime.now(timezone.utc).timestamp())
                bot._handle_update({"channel_post": {
                    "chat": {
                        "id": -100123,
                        "type": "channel",
                        "title": "Iran Aviation",
                        "username": "iranaviation",
                    },
                    "message_id": 42,
                    "date": now,
                    "text": "پروازها در تهران بررسی می‌شوند #پرواز_ایران",
                }})
                bot._handle_update({"channel_post": {
                    "chat": {"id": -100456, "type": "channel", "username": "other"},
                    "message_id": 43,
                    "date": now,
                    "text": "unconfigured channel post",
                }})
                db.close()
                db = Database(Path(tmp) / "tg.db")
                bot = TelegramBot(config, db)
                items = bot.poll_channel_posts()
                self.assertEqual(len(items), 1)
                bot.ack_channel_post(items[0].extra["telegram_inbox_id"])
                self.assertEqual(bot.poll_channel_posts(), [])
            finally:
                db.close()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source_id, "tg_iranaviation")
        self.assertEqual(items[0].url, "https://t.me/iranaviation/42")
        self.assertEqual(items[0].kind, "telegram")


    def test_channel_post_is_persisted_before_update_offset_advances(self):
        import os
        import tempfile
        from unittest import mock

        from src.db import Database
        from src.telegram_bot import TelegramBot

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "123456:test-token",
                "ENABLE_TELEGRAM_CHANNELS": "true",
                "TELEGRAM_CHANNELS": "iranaviation",
            },
        ):
            config = load_config()
            db = Database(Path(tmp) / "offset.db")
            try:
                bot = TelegramBot(config, db)
                update = {
                    "update_id": 77,
                    "channel_post": {
                        "chat": {"id": -100123, "type": "channel", "username": "iranaviation"},
                        "message_id": 42,
                        "date": int(datetime.now(timezone.utc).timestamp()),
                        "text": "Iran airspace restrictions affect flights #IranAirspace",
                    },
                }

                def fake_call(method, payload, **kwargs):
                    self.assertEqual(method, "getUpdates")
                    self.assertIn("channel_post", payload["allowed_updates"])
                    bot._stop.set()
                    return [update]

                with mock.patch.object(bot, "_call", side_effect=fake_call):
                    bot._listen_loop()

                self.assertEqual(db.kv_get("tg_offset"), "78")
                items = bot.poll_channel_posts()
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0].extra["telegram_inbox_id"], "tg-update:77")
            finally:
                db.close()


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
                urllib.request.urlopen(f"http://127.0.0.1:{server.port}/healthz", timeout=5)
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
        from src.formatter import format_publication_notice

        config = load_config()
        event = {
            "severity": "critical", "category": "airspace",
            "title_fa": "بستن حریم هوایی", "body_fa": "متن خبر",
            "confidence": "multi", "numbers_ok": 1,
            "primary_source": "Anadolu Ajansı",
            "primary_url": "https://example.com/a",
            "first_seen": util.iso(util.now_utc()),
            "airlines": "Turkish Airlines", "countries": "IR,TR,PK,TH,VN",
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


class SocialStorageTests(unittest.TestCase):
    def test_discovered_hashtag_requires_repeated_independent_sources(self):
        import tempfile

        from src.db import Database

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "tags.db")
            try:
                db.record_hashtags(["iranairspace"], "x_author_a", "fp-1")
                db.record_hashtags(["iranairspace"], "x_author_a", "fp-2")
                self.assertEqual(
                    db.discovered_hashtags(min_mentions=2, min_sources=2), []
                )
                db.record_hashtags(["iranairspace"], "tg_channel_b", "fp-3")
                self.assertEqual(
                    db.discovered_hashtags(min_mentions=2, min_sources=2),
                    ["iranairspace"],
                )
            finally:
                db.close()


    def test_x_queries_include_fixed_seeds_and_confirmed_hashtags(self):
        import tempfile

        from src.airlines import AirlineIndex
        from src.collectors import XCollector
        from src.db import Database

        with tempfile.TemporaryDirectory() as tmp:
            config = load_config()
            db = Database(Path(tmp) / "queries.db")
            try:
                db.record_hashtags(["iranaviationupdate"], "x_account_a", "fp-a")
                db.record_hashtags(["iranaviationupdate"], "tg_channel_b", "fp-b")
                queries = XCollector(config, AirlineIndex(config.airlines), db).queries()
            finally:
                db.close()

        self.assertIn("aviation", queries[0])
        self.assertIn("هوانوردی", queries[1])
        self.assertIn("#IranAirspace", queries[2])
        self.assertIn("#iranaviationupdate", queries[3])

    def test_pipeline_learns_tags_only_from_relevant_social_posts(self):
        import tempfile

        from src.db import Database
        from src.models import Translation
        from src.pipeline import Pipeline

        class FakeBot:
            def feedback_keyboard(self, event_id, url=""):
                return []

            def send(self, text, **kwargs):
                return None

            def chat_id(self):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(dry_run=True)
            db = Database(Path(tmp) / "pipeline.db")
            try:
                pipeline = Pipeline(config, db, FakeBot())
                pipeline.translator.translate_item = lambda **kwargs: Translation(
                    text_fa=kwargs["body"], engine="test", title_fa=kwargs["title"]
                )
                irrelevant = RawItem(
                    source_id="x_account_a",
                    source_name="@account_a",
                    source_url="https://x.com/account_a",
                    title="Iranian minister discusses regional policy #politicsupdate",
                    body="Officials discussed political relations and a new policy proposal.",
                    url="https://x.com/account_a/status/100",
                    kind="x",
                )
                relevant = RawItem(
                    source_id="x_account_a",
                    source_name="@account_a",
                    source_url="https://x.com/account_a",
                    title="Iran airspace restrictions affect flights #IranAirspace #SkyRouteUpdate",
                    body="Iran airspace restrictions affect flights.",
                    url="https://x.com/account_a/status/101",
                    kind="x",
                )
                self.assertEqual(pipeline.process(irrelevant), "irrelevant")
                self.assertIn(pipeline.process(relevant), {"published", "queued"})
                tags = db.discovered_hashtags(min_mentions=1, min_sources=1)
                self.assertEqual(set(tags), {"iranairspace", "skyrouteupdate"})
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
        client = KeepAliveClient("https://k8s.local", "tok", "demo-ns", verify=False, session=session)
        action = client.ensure_awake()
        self.assertEqual(action, "scaled-up")
        self.assertEqual(session.patches[0][1], {"spec": {"replicas": 1}})

    def test_recycles_pod_before_12h_sandbox_limit(self):
        from datetime import timedelta

        from src.keepalive import KeepAliveClient

        now = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
        old_start = util.iso(now - timedelta(hours=11, minutes=5))
        pods = [{
            "metadata": {"name": "airwatch-abc"},
            "status": {
                "phase": "Running",
                "startTime": old_start,
                "containerStatuses": [{"restartCount": 0}],
            },
        }]
        session = self._mock_session(deploy_replicas=1, pods=pods)
        client = KeepAliveClient("https://k8s.local", "tok", "demo-ns", verify=False, session=session)
        action = client.ensure_awake(now=now)
        self.assertEqual(action, "restarted")
        self.assertIn("restartedAt", str(session.patches[0][1]))

    def test_leaves_healthy_young_pod_untouched(self):
        from datetime import timedelta

        from src.keepalive import KeepAliveClient

        now = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
        fresh_start = util.iso(now - timedelta(hours=2))
        pods = [{
            "metadata": {"name": "airwatch-abc"},
            "status": {
                "phase": "Running",
                "startTime": fresh_start,
                "containerStatuses": [{"restartCount": 0}],
            },
        }]
        session = self._mock_session(deploy_replicas=1, pods=pods)
        client = KeepAliveClient("https://k8s.local", "tok", "demo-ns", verify=False, session=session)
        self.assertEqual(client.ensure_awake(now=now), "healthy")
        self.assertEqual(session.patches, [])


class ManifestValidationTests(unittest.TestCase):
    def test_openshift_manifests_are_valid_yaml(self):
        import yaml

        root = Path(__file__).resolve().parent.parent
        app_docs = [d for d in yaml.safe_load_all((root / "deploy/app.yaml").read_text("utf-8")) if d]
        kinds = [doc["kind"] for doc in app_docs]
        self.assertEqual(
            kinds,
            ["Deployment", "ServiceAccount", "Role", "RoleBinding", "CronJob"],
        )
        build_docs = [d for d in yaml.safe_load_all((root / "deploy/build.yaml").read_text("utf-8")) if d]
        self.assertEqual([doc["kind"] for doc in build_docs], ["ImageStream", "BuildConfig", "PersistentVolumeClaim"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
