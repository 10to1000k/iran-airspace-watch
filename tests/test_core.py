"""تست‌های هسته — بدون شبکه و بدون کلید API."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import util
from src.article import ArticleFetcher
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


class DatabaseMigrationTests(unittest.TestCase):
    def test_old_database_migrates_and_uses_earliest_published_source(self):
        import sqlite3
        import tempfile

        from src.db import Database

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.sqlite"
            connection = sqlite3.connect(path)
            connection.executescript(
                """CREATE TABLE events (
                       id INTEGER PRIMARY KEY,
                       primary_source TEXT,
                       primary_url TEXT,
                       first_seen TEXT NOT NULL,
                       status TEXT NOT NULL DEFAULT 'open'
                   );
                   CREATE TABLE items (
                       id INTEGER PRIMARY KEY,
                       source_name TEXT,
                       url TEXT,
                       published_at TEXT,
                       event_id INTEGER
                   );
                   INSERT INTO events(id, primary_source, primary_url, first_seen)
                   VALUES(1, 'later publisher', 'https://later.example/story',
                          '2026-10-24T00:00:00+00:00');
                   INSERT INTO items(id, source_name, url, published_at, event_id)
                   VALUES(1, 'later publisher', 'https://later.example/story',
                          '2026-10-22T12:00:00+00:00', 1);
                   INSERT INTO items(id, source_name, url, published_at, event_id)
                   VALUES(2, 'earlier publisher', 'https://earlier.example/story',
                          '2026-10-22T10:00:00+00:00', 1);"""
            )
            connection.commit()
            connection.close()

            db = Database(path)
            try:
                event = db.get_event(1)
                self.assertEqual(event["primary_source"], "earlier publisher")
                self.assertEqual(event["primary_url"], "https://earlier.example/story")
                self.assertEqual(
                    event["primary_published_at"], "2026-10-22T10:00:00+00:00"
                )
                columns = {
                    row["name"]
                    for row in db.conn.execute("PRAGMA table_info(events)").fetchall()
                }
                self.assertIn("telegram_sent_at", columns)
            finally:
                db.close()

    def test_news_chat_sessions_are_per_user_persistent_and_expire(self):
        import tempfile
        from datetime import timedelta

        from src.db import Database

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "chat.sqlite")
            try:
                db.save_news_chat_session(
                    "100", "100", 7,
                    [{"role": "user", "content": "چرا پرواز لغو شد؟"}],
                )
                session = db.get_news_chat_session("100", "100", max_age_minutes=120)
                self.assertEqual(session["event_id"], 7)
                self.assertEqual(session["history"][0]["content"], "چرا پرواز لغو شد؟")
                self.assertIsNone(db.get_news_chat_session("100", "200"))

                old_time = util.iso(util.now_utc() - timedelta(hours=3))
                db.conn.execute(
                    "UPDATE telegram_news_chats SET updated_at=? WHERE chat_id='100' AND user_id='100'",
                    (old_time,),
                )
                db.conn.commit()
                self.assertIsNone(db.get_news_chat_session("100", "100", max_age_minutes=60))
                self.assertIsNone(db.get_news_chat_session("100", "100", max_age_minutes=120))

                db.save_news_chat_session("200", "200", 8, [])
                db.conn.execute(
                    "UPDATE telegram_news_chats SET updated_at=? WHERE chat_id='200' AND user_id='200'",
                    (old_time,),
                )
                db.conn.commit()
                self.assertEqual(db.prune_news_chat_sessions(max_age_minutes=60), 1)
                self.assertIsNone(db.get_news_chat_session("200", "200"))
            finally:
                db.close()


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

    def test_turkish_iranian_arrest_at_foreign_airport_is_relevant(self):
        result = self.classifier.classify(
            self.item(
                "İranlı yolcular İstanbul Havalimanı'nda uyuşturucu suçundan yakalandı",
                "İranlı iki yolcu, İstanbul Havalimanı'nda yapılan arama sonrası tutuklandı.",
                lang="tr",
                country="TR",
            )
        )

        self.assertTrue(result.relevant)
        self.assertEqual(result.category, "airport")
        self.assertIn("IR", result.countries)
        self.assertIn("TR", result.countries)


class CollectorTests(unittest.TestCase):
    def test_public_turkish_news_feeds_are_enabled(self):
        sources = {source["id"]: source for source in load_config().rss_sources}
        expected = {"tr_haberler", "tr_ntv", "tr_sabah", "tr_milliyet"}

        self.assertTrue(expected.issubset(sources))
        self.assertEqual(sources["tr_haberler"]["lang"], "tr")
        self.assertEqual(sources["tr_ntv"]["country"], "TR")
        self.assertTrue(
            ArticleFetcher.can_fetch(
                "https://www.haberler.com/story",
                sources["tr_haberler"]["url"],
            )
        )

    def test_dynamic_telegram_channel_language_defaults_to_auto(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"TELEGRAM_CHANNELS": "arabicnews"}):
            config = load_config()
            source = next(
                channel for channel in config.telegram_channels
                if channel["username"] == "arabicnews"
            )
        self.assertEqual(source["lang"], "auto")

    def test_rss_body_keeps_full_content_and_paragraph_breaks(self):
        from src.collectors import _entry_body

        paragraphs = [f"بند شماره {index} و متن کامل گزارش؛ پایان گزارش {index}." for index in range(1, 45)]
        entry = {"content": [{"value": "".join(f"<p>{paragraph}</p>" for paragraph in paragraphs)}]}

        body = _entry_body(entry)

        self.assertGreater(len(body), 1200)
        self.assertEqual(body.count("\n\n"), len(paragraphs) - 1)
        self.assertIn("پایان گزارش 44.", body)

    def test_rss_body_uses_the_longest_available_content_field(self):
        from src.collectors import _entry_body

        entry = {
            "content": [{"value": "<p>Short preview.</p>"}],
            "summary": "<p>" + "Full summary detail. " * 80 + "</p>",
        }

        body = _entry_body(entry)

        self.assertGreater(len(body), 1000)
        self.assertIn("Full summary detail.", body)

    def test_article_extractor_ignores_navigation_and_keeps_paragraphs(self):
        from src.article import extract_article_text

        article = (
            "<html><body><nav>navigation must not be included</nav>"
            "<main><article><h1>Iran airport development</h1>"
            "<div class='article-content'><p>First complete article paragraph.</p>"
            "<p>Second paragraph contains the full report and all details.</p></div>"
            "</article></main><footer>footer must not be included</footer></body></html>"
        )

        text = extract_article_text(article)

        self.assertIn("First complete article paragraph.", text)
        self.assertIn("Second paragraph contains the full report", text)
        self.assertIn("\n\n", text)
        self.assertNotIn("navigation must not be included", text)
        self.assertNotIn("footer must not be included", text)

    def test_article_extractor_reads_structured_article_body(self):
        import json

        from src.article import extract_article_text

        structured = json.dumps({
            "@context": "https://schema.org",
            "@type": "NewsArticle",
            "articleBody": (
                "Structured full article body with all source details and numbers 47. "
                "It preserves every paragraph and the complete original report as provided."
            ),
        })
        text = extract_article_text(
            f"<html><head><script type='application/ld+json'>{structured}</script></head></html>"
        )

        self.assertIn("Structured full article body", text)
        self.assertIn("47", text)

    def test_rss_collector_fetches_full_article_for_short_relevant_feed_text(self):
        from unittest import mock

        from src.collectors import RssCollector

        config = load_config()
        source = {
            "id": "test_rss",
            "name": "Example News",
            "url": "https://example.com/rss",
            "lang": "en",
            "country": "INTL",
            "tier": 1,
        }
        paragraphs = "".join(
            f"<p>Paragraph {index}: the full report covers Iran airport operations and "
            "the complete aviation development plan, including every detail.</p>"
            for index in range(1, 25)
        )
        feed = (
            "<?xml version='1.0' encoding='utf-8'?>"
            "<rss version='2.0'><channel><title>Example</title><item>"
            "<title>Iran airport aviation development plan</title>"
            "<link>https://example.com/news/iran-airport-plan</link>"
            "<description><![CDATA[<p>Iran airport development update.</p>]]></description>"
            "</item></channel></rss>"
        ).encode()
        page = (
            "<html><body><nav>ignore nav</nav><article>"
            f"<div class='article-content'>{paragraphs}</div>"
            "</article></body></html>"
        ).encode()

        class FakeResponse:
            status_code = 200
            encoding = "utf-8"
            apparent_encoding = "utf-8"

            def __init__(self, body, content_type):
                self.body = body
                self.headers = {"Content-Type": content_type}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size=32768):
                for start in range(0, len(self.body), chunk_size):
                    yield self.body[start:start + chunk_size]

        def fake_get(url, **_kwargs):
            if url == source["url"]:
                return FakeResponse(feed, "application/rss+xml")
            if url == "https://example.com/news/iran-airport-plan":
                return FakeResponse(page, "text/html; charset=utf-8")
            raise AssertionError(f"Unexpected URL requested: {url}")

        collector = RssCollector(config, db=None)
        with mock.patch("src.collectors.requests.get", side_effect=fake_get):
            items = collector._fetch(source)

        self.assertEqual(len(items), 1)
        self.assertTrue(items[0].extra["article_text_fetched"])
        self.assertGreater(len(items[0].body), 1800)
        self.assertIn("Paragraph 24", items[0].body)
        self.assertNotIn("ignore nav", items[0].body)

    def test_rss_backfill_keeps_three_day_old_reports_within_configured_window(self):
        from datetime import timedelta
        from unittest import mock

        from src.collectors import RssCollector

        config = load_config()
        config.settings["poll"]["max_item_age_hours"] = 96
        config.settings["poll"]["fetch_full_articles"] = False
        published = datetime.now(timezone.utc) - timedelta(days=3)
        pub_date = published.strftime("%a, %d %b %Y %H:%M:%S GMT")
        feed = (
            "<?xml version='1.0' encoding='utf-8'?>"
            "<rss version='2.0'><channel><title>Example</title><item>"
            "<title>İranlı yolcular İstanbul Havalimanı'nda yakalandı</title>"
            "<link>https://example.com/iran-airport-arrest</link>"
            f"<pubDate>{pub_date}</pubDate>"
            "<description>İranlı yolcular İstanbul Havalimanı'nda yakalandı.</description>"
            "</item></channel></rss>"
        ).encode("utf-8")
        source = {
            "id": "tr_example", "name": "Example", "url": "https://example.com/rss",
            "lang": "tr", "country": "TR", "tier": 2,
        }

        collector = RssCollector(config, db=None)
        with mock.patch.object(collector, "_read_bounded", return_value=feed):
            items = collector._fetch(source)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].published_at.date(), published.date())
        self.assertIn("collected_at", items[0].extra)

        config.settings["poll"]["max_item_age_hours"] = 12
        recent_only = RssCollector(config, db=None)
        with mock.patch.object(recent_only, "_read_bounded", return_value=feed):
            self.assertEqual(recent_only._fetch(source), [])

    def test_article_fetch_per_poll_limit_is_enforced(self):
        from types import SimpleNamespace

        from src.collectors import RssCollector

        config = load_config()
        config.settings["poll"]["article_fetch_max_per_poll"] = 1
        collector = RssCollector(config, db=None)
        collector.classifier.classify = lambda _item: SimpleNamespace(relevant=True)
        item = RawItem(
            source_id="test", source_name="test", source_url="https://example.com/rss",
            title="short relevant article", body="excerpt", url="https://example.com/story",
        )

        self.assertTrue(collector._should_fetch_article(item, article_fetches=0))
        self.assertFalse(collector._should_fetch_article(item, article_fetches=0))

    def test_article_fetcher_blocks_cross_site_and_private_redirects(self):
        from unittest import mock

        from src.article import ArticleFetcher

        fetcher = ArticleFetcher()
        with mock.patch("src.article.requests.get") as get:
            self.assertIsNone(
                fetcher.fetch("https://other.example/article", "https://example.com/rss")
            )
            self.assertIsNone(
                fetcher.fetch("https://127.0.0.1/admin", "https://example.com/rss")
            )
            get.assert_not_called()

            class RedirectResponse:
                status_code = 302
                headers = {"Location": "http://127.0.0.1/admin"}

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

            get.return_value = RedirectResponse()
            self.assertIsNone(
                fetcher.fetch("https://example.com/article", "https://example.com/rss")
            )
            self.assertEqual(get.call_count, 1)

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
        <div class=\"tgme_widget_message\" data-post=\"testchannel/4\" data-post-date=\"{now - 7 * 86400}\">
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

    def test_long_telegram_message_is_split_without_loss_and_returns_root_id(self):
        import os
        from unittest import mock

        from src.telegram_bot import MAX_LEN, TelegramBot, _split_message

        text = "<b>تیتر</b>\n" + ("متن کامل &amp; جزئیات خبر. " * 500)
        chunks = _split_message(text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= MAX_LEN for chunk in chunks))
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(not chunk.endswith("&amp") for chunk in chunks))

        calls = []

        def fake_call(method, payload, **kwargs):
            calls.append((method, payload))
            return {"message_id": 100 + len(calls)}

        with mock.patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "123456:test-token", "TELEGRAM_CHAT_ID": "123"},
        ):
            bot = TelegramBot(load_config(), db=None)
            bot._call = fake_call
            with mock.patch("src.telegram_bot.time.sleep"):
                root_id = bot.send(
                    text,
                    keyboard=[[{"text": "مفید", "callback_data": "fb:up:1"}]],
                )

        self.assertEqual(root_id, 101)
        self.assertEqual(len(calls), len(chunks))
        self.assertIn("reply_markup", calls[0][1])
        self.assertNotIn("reply_markup", calls[-1][1])
        self.assertEqual(calls[1][1]["reply_parameters"]["message_id"], root_id)

    def test_news_chat_keyboard_discovers_bot_username_with_getme(self):
        import os
        from unittest import mock

        from src.telegram_bot import TelegramBot

        with mock.patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "123456:test-token", "TELEGRAM_BOT_USERNAME": ""},
        ):
            bot = TelegramBot(load_config(), db=None)
            calls = []

            def fake_call(method, payload, **kwargs):
                calls.append(method)
                return {"username": "iran_watch_bot"}

            bot._call = fake_call
            bot.bind_news_chat(lambda event, question, history: "پاسخ", enabled=True)
            keyboard = bot.news_chat_keyboard(42)

        self.assertEqual(calls, ["getMe"])
        self.assertEqual(
            keyboard[0][0]["url"],
            "https://t.me/iran_watch_bot?start=chat_42",
        )

    def test_news_chat_deep_link_keeps_broadcast_chat_and_answers_with_context(self):
        import os
        import tempfile
        from unittest import mock

        from src.db import Database
        from src.telegram_bot import TelegramBot

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "123456:test-token",
                "TELEGRAM_BOT_USERNAME": "iran_watch_bot",
                "TELEGRAM_CHAT_ID": "-100broadcast",
            },
        ):
            db = Database(Path(tmp) / "news-chat.sqlite")
            try:
                event_id, _ = db.create_event({
                    "fingerprint": "news-chat-event",
                    "title_fa": "تغییر مسیر پرواز در تهران",
                    "body_fa": "متن کامل خبر درباره‌ی تغییر مسیر پرواز.",
                    "primary_source": "خبرگزاری آزمایشی",
                    "primary_published_at": "2026-10-25T09:00:00+00:00",
                })
                bot = TelegramBot(load_config(), db)
                seen = []
                sent = []
                bot.bind_news_chat(
                    lambda event, question, history: (
                        seen.append((event, question, history)) or "پاسخ <قابل‌اعتماد> & کامل"
                    ),
                    enabled=True,
                )
                bot.send = lambda text, chat_id=None, **kwargs: sent.append((text, chat_id))

                keyboard = bot.feedback_keyboard(event_id)
                self.assertEqual(
                    keyboard[1][0]["url"],
                    f"https://t.me/iran_watch_bot?start=chat_{event_id}",
                )

                bot._handle_update({"message": {
                    "chat": {"id": 77, "type": "private"},
                    "from": {"id": 77},
                    "text": f"/start chat_{event_id}",
                }})
                self.assertIsNone(db.kv_get("chat_id"))
                self.assertEqual(bot.chat_id(), "-100broadcast")
                self.assertEqual(db.get_news_chat_session("77", "77")["event_id"], event_id)

                bot._handle_update({"message": {
                    "chat": {"id": 77, "type": "private"},
                    "from": {"id": 77},
                    "text": "چرا مسیر عوض شد؟",
                }})
                bot._handle_update({"message": {
                    "chat": {"id": 77, "type": "private"},
                    "from": {"id": 77},
                    "text": "منبع خبر چه بوده؟",
                }})

                self.assertEqual(seen[0][0]["primary_source"], "خبرگزاری آزمایشی")
                self.assertEqual(seen[0][1], "چرا مسیر عوض شد؟")
                self.assertEqual(seen[0][2], [])
                self.assertEqual(seen[1][2][-2]["content"], "چرا مسیر عوض شد؟")
                self.assertIn("&lt;قابل‌اعتماد&gt; &amp; کامل", sent[-1][0])

                bot._handle_update({"message": {
                    "chat": {"id": 77, "type": "private"},
                    "from": {"id": 77},
                    "text": "/stopchat",
                }})
                self.assertIsNone(db.get_news_chat_session("77", "77"))

                bot._bot_username = ""
                bot._bot_username_checked = True
                bot.answer_callback = lambda *args, **kwargs: None
                fallback = bot.news_chat_keyboard(event_id)
                self.assertEqual(fallback[0][0]["callback_data"], f"chat:{event_id}")
                bot._handle_update({"callback_query": {
                    "id": "callback-1",
                    "data": f"chat:{event_id}",
                    "from": {"id": 77},
                    "message": {"chat": {"id": 77, "type": "private"}},
                }})
                self.assertEqual(db.get_news_chat_session("77", "77")["event_id"], event_id)
            finally:
                db.close()

    def test_edit_splits_long_text_and_replaces_old_continuations(self):
        import os
        from unittest import mock

        from src.telegram_bot import TelegramBot, _split_message

        text = "به‌روزرسانی کامل\n" + ("ادامه‌ی متن کامل خبر. " * 500)
        chunks = _split_message(text)
        calls = []

        def fake_call(method, payload, **kwargs):
            calls.append((method, payload))
            return {"message_id": 200 + len(calls)}

        with mock.patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "123456:test-token", "TELEGRAM_CHAT_ID": "123"},
        ):
            bot = TelegramBot(load_config(), db=None)
            bot._call = fake_call
            bot._message_continuations[77] = [88, 89]
            with mock.patch("src.telegram_bot.time.sleep"):
                bot.edit(77, text)

        self.assertEqual(calls[0][0], "editMessageText")
        deleted = [payload["message_id"] for method, payload in calls if method == "deleteMessage"]
        self.assertEqual(deleted, [88, 89])
        sent = [payload["text"] for method, payload in calls if method == "sendMessage"]
        self.assertEqual([calls[0][1]["text"], *sent], chunks)
        self.assertEqual("".join([calls[0][1]["text"], *sent]), text)
        self.assertTrue(all(
            payload["reply_parameters"]["message_id"] == 77
            for method, payload in calls if method == "sendMessage"
        ))

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

    def test_news_chat_uses_article_context_and_recent_conversation(self):
        from src.translator import Translator

        config = load_config()
        config.settings.setdefault("news_chat", {})["max_article_chars"] = 1000
        config.settings["news_chat"]["history_messages"] = 2
        translator = Translator(config, db=None)
        translator.api_key = "test-key"
        captured = {}

        def fake_chat(prompt, system):
            captured["prompt"] = prompt
            captured["system"] = system
            return "پاسخ بر اساس متن خبر"

        translator._chat = fake_chat
        answer = translator.answer_about_event(
            {
                "title_fa": "تغییر مسیر پروازها",
                "body_fa": "جزئیات خبر. " * 200,
                "primary_source": "خبرگزاری آزمایشی",
                "primary_published_at": "2026-10-25T09:00:00+00:00",
            },
            "کدام پروازها تغییر مسیر دادند؟",
            [
                {"role": "user", "content": "سؤال قدیمی"},
                {"role": "assistant", "content": "پاسخ قدیمی"},
                {"role": "user", "content": "سؤال اخیر"},
                {"role": "assistant", "content": "پاسخ اخیر"},
            ],
        )

        self.assertEqual(answer, "پاسخ بر اساس متن خبر")
        self.assertIn("تغییر مسیر پروازها", captured["prompt"])
        self.assertIn("خبرگزاری آزمایشی", captured["prompt"])
        self.assertIn("کدام پروازها تغییر مسیر دادند؟", captured["prompt"])
        self.assertIn("سؤال اخیر", captured["prompt"])
        self.assertNotIn("سؤال قدیمی", captured["prompt"])
        self.assertIn("ادامه‌ی متن به‌دلیل محدودیت زمینه", captured["prompt"])
        self.assertIn("untrusted data", captured["system"])

    def test_news_chat_requires_llm_api_key(self):
        from src.translator import Translator

        translator = Translator(load_config(), db=None)
        translator.api_key = None
        with self.assertRaisesRegex(RuntimeError, "LLM_API_KEY"):
            translator.answer_about_event({"title_fa": "خبر"}, "سؤال")

    def test_persian_passthrough_needs_no_network(self):
        from src.translator import Translator

        translator = Translator(load_config(), db=None)
        result = translator.translate_item("لغو پرواز", "متن فارسی", "fa")
        self.assertEqual(result.engine, "passthrough-fa")

    def test_arabic_article_is_translated_to_persian(self):
        from src.translator import Translator

        translator = Translator(load_config(), db=None)
        translator.api_key = "test-key"
        prompts = []

        def fake_chat(prompt, system):
            prompts.append(prompt)
            if "HEADLINE:" in prompt:
                return "تیتر فارسی خبر"
            return "این متن کامل به فارسی ترجمه شده است."

        translator._chat = fake_chat
        result = translator.translate_item(
            "إيران تعلن تحديثاً جديداً للمطار",
            "أعلنت السلطات أن الرحلات الجوية ستستمر، مع تحديث الخدمات في المطار.",
            "ar",
        )

        self.assertEqual(result.engine, translator.active_model)
        self.assertEqual(result.title_fa, "تیتر فارسی خبر")
        self.assertIn("فارسی ترجمه شده", result.text_fa)
        self.assertTrue(any("Source language: Arabic" in prompt for prompt in prompts))
        self.assertTrue(any("Do not summarize" in prompt for prompt in prompts))

    def test_arabic_text_is_translated_even_if_source_language_is_mislabeled_fa(self):
        from src.translator import Translator

        translator = Translator(load_config(), db=None)
        translator.api_key = "test-key"
        prompts = []
        translator._chat = lambda prompt, system: (
            prompts.append(prompt) or
            ("عنوان فارسی" if "HEADLINE:" in prompt else "این گزارش به فارسی ترجمه شد.")
        )

        result = translator.translate_item(
            "إيران تعلن تحديثاً جديداً للمطار",
            "أعلنت السلطات أن الرحلات الجوية ستستمر في مطار طهران.",
            "fa",
        )

        self.assertNotEqual(result.engine, "passthrough-fa")
        self.assertTrue(prompts)
        self.assertIn("فارسی ترجمه شد", result.text_fa)

    def test_arabic_model_output_is_not_accepted_as_persian(self):
        from src.translator import Translator

        translator = Translator(load_config(), db=None)
        translator.api_key = "test-key"
        translator._chat = lambda prompt, system: (
            "إيران تعلن تحديثاً جديداً للمطار"
            if "HEADLINE:" in prompt
            else "أعلنت السلطات أن الرحلات الجوية ستستمر، مع تحديث الخدمات في المطار."
        )

        result = translator.translate_item(
            "إيران تعلن تحديثاً جديداً للمطار",
            "أعلنت السلطات أن الرحلات الجوية ستستمر، مع تحديث الخدمات في المطار.",
            "ar",
        )

        self.assertEqual(result.engine, "failed")
        self.assertEqual(result.text_fa, "")

    def test_long_article_is_translated_in_complete_chunks(self):
        from src.translator import Translator

        translator = Translator(load_config(), db=None)
        translator.api_key = "test-key"
        body = "\n\n".join(f"Paragraph-{index} full source content." for index in range(1, 180))
        prompts = []

        def fake_chat(prompt, system):
            import re

            prompts.append(prompt)
            if "HEADLINE:" in prompt:
                return "تیتر فارسی"
            fragment = prompt.split("BODY FRAGMENT:\n", 1)[1]
            numbers = " ".join(re.findall(r"\d+", fragment))
            return "پ" * max(120, len(fragment) // 2) + " " + numbers

        translator._chat = fake_chat
        result = translator.translate_item("Aviation report", body, "en")
        body_prompts = [prompt for prompt in prompts if "BODY FRAGMENT:\n" in prompt]
        supplied = "\n".join(
            prompt.split("BODY FRAGMENT:\n", 1)[1] for prompt in body_prompts
        )

        self.assertGreater(len(body_prompts), 1)
        self.assertGreater(len(result.text_fa), 700)
        for index in (1, 20, 90, 179):
            self.assertIn(f"Paragraph-{index} full source content.", supplied)
        self.assertTrue(all("Do not summarize" in prompt for prompt in body_prompts))

    def test_summary_sized_translation_is_rejected(self):
        from src.translator import Translator

        translator = Translator(load_config(), db=None)
        translator.api_key = "test-key"
        translator._chat = lambda prompt, system: (
            "تیتر فارسی" if "HEADLINE:" in prompt else "خلاصه"
        )
        result = translator.translate_item("Long aviation report", "Source sentence. " * 100, "en")
        self.assertEqual(result.engine, "failed")
        self.assertEqual(result.text_fa, "")


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

    def test_event_formatter_keeps_the_complete_body(self):
        config = load_config()
        full_body = "متن کامل خبر و جزئیات آن. " * 100 + "پایان گزارش کامل"
        event = {
            "severity": "important", "category": "airport",
            "title_fa": "خبر فرودگاه", "body_fa": full_body,
            "confidence": "single", "numbers_ok": 1,
            "primary_source": "منبع آزمایشی",
            "primary_url": "https://example.com/full",
            "first_seen": util.iso(util.now_utc()),
            "airlines": "", "countries": "IR",
        }

        formatted = format_event(event, config)

        self.assertIn("پایان گزارش کامل", formatted)
        self.assertGreater(len(formatted), 700)


class PipelineRecoveryTests(unittest.TestCase):
    def test_pipeline_binds_llm_news_chat_to_telegram_bot(self):
        import os
        from unittest import mock

        from src.pipeline import Pipeline

        class ChatBot:
            def bind_news_chat(self, handler, enabled=True):
                self.handler = handler
                self.enabled = enabled

            def news_chat_keyboard(self, event_id, url=""):
                return [[{"text": f"chat:{event_id}"}]]

        with mock.patch.dict(os.environ, {"LLM_API_KEY": "chat-test-key"}):
            bot = ChatBot()
            config = load_config()
            config.settings["notifications"]["include_feedback_buttons"] = False
            pipeline = Pipeline(config, db=None, bot=bot)
            pipeline.translator._chat = lambda prompt, system: "پاسخ آزمایشی"

            self.assertTrue(bot.enabled)
            self.assertEqual(
                bot.handler({"title_fa": "خبر", "body_fa": "متن"}, "سؤال", []),
                "پاسخ آزمایشی",
            )
            self.assertEqual(pipeline._event_keyboard(4), [[{"text": "chat:4"}]])

    def test_foreign_news_is_not_published_when_translation_is_unavailable(self):
        import tempfile

        from src.db import Database
        from src.pipeline import Pipeline

        class FakeBot:
            def __init__(self):
                self.sent = []

            def send(self, text, **kwargs):
                self.sent.append(text)
                return 1

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "translation-required.db")
            try:
                config = load_config()
                bot = FakeBot()
                pipeline = Pipeline(config, db, bot)
                pipeline.translator.api_key = None
                item = RawItem(
                    source_id="ar_news",
                    source_name="Arabic Aviation News",
                    source_url="https://example.com/rss",
                    title="إيران توقف الرحلات الجوية في مطار طهران",
                    body="أعلنت السلطات تعليق الرحلات الجوية مؤقتاً في مطار طهران.",
                    url="https://example.com/news/1",
                    lang="ar",
                    country="AE",
                )
                fingerprint = util.fingerprint(item.source_id, item.url, item.title, item.body[:400])

                with self.assertRaisesRegex(RuntimeError, "متوقف"):
                    pipeline.process(item)

                self.assertEqual(bot.sent, [])
                self.assertFalse(db.item_seen(item.source_id, fingerprint))
            finally:
                db.close()

    def test_clustered_foreign_source_gets_a_full_translated_reply(self):
        import tempfile

        from src.db import Database
        from src.models import Translation
        from src.pipeline import Pipeline

        class FakeBot:
            def __init__(self):
                self.sent = []
                self.edited = []

            def send(self, text, **kwargs):
                self.sent.append(text)
                return 700 + len(self.sent)

            def chat_id(self):
                return "123"

            def feedback_keyboard(self, event_id, url=""):
                return []

            def edit(self, message_id, text, **kwargs):
                self.edited.append((message_id, text))

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "clustered-source.db")
            try:
                bot = FakeBot()
                pipeline = Pipeline(load_config(), db, bot)
                first = RawItem(
                    source_id="ir_first",
                    source_name="منبع فارسی",
                    source_url="https://example.com/rss",
                    title="لغو پروازها در فرودگاه مهرآباد تهران",
                    body="پروازهای مهرآباد موقتاً متوقف شد.",
                    url="https://example.com/first",
                    lang="fa",
                    country="IR",
                )
                self.assertEqual(pipeline.process(first), "published")
                event = db.latest_events(limit=1)[0]

                second = RawItem(
                    source_id="ar_agency",
                    source_name="Arabic Aviation Agency",
                    source_url="https://arabic.example/rss",
                    title="إيران توقف الرحلات في مطار مهرآباد",
                    body="أعلنت السلطات تعليق الرحلات مؤقتاً في مطار مهرآباد.",
                    url="https://arabic.example/news/2",
                    lang="ar",
                    country="AE",
                )
                fingerprint = util.fingerprint(
                    second.source_id, second.url, second.title, second.body[:400]
                )
                self.assertTrue(db.add_item(second, fingerprint))
                pipeline.translator.translate_item = lambda **kwargs: Translation(
                    text_fa="مقام‌های ایران توقف موقت پروازها در مهرآباد را اعلام کردند.",
                    title_fa="توقف موقت پروازها در مهرآباد",
                    engine="mock-translation",
                )
                classification = pipeline.classifier.classify(second)

                result = pipeline._update_event(second, fingerprint, classification, event)

                self.assertEqual(result, "updated")
                self.assertEqual(len(bot.sent), 2)
                self.assertIn("گزارش کامل", bot.sent[1])
                self.assertIn("مقام‌های ایران توقف موقت پروازها", bot.sent[1])
                self.assertNotIn(second.title, bot.sent[1])
            finally:
                db.close()

    def test_late_reprint_promotes_earliest_monitored_source_and_tracks_lag(self):
        import tempfile
        from datetime import timedelta

        from src.db import Database
        from src.models import Translation
        from src.pipeline import Pipeline

        class FakeBot:
            configured = True

            def __init__(self):
                self.sent = []
                self.edited = []

            def send(self, text, **kwargs):
                self.sent.append((text, kwargs))
                return 500 + len(self.sent)

            def chat_id(self):
                return "123"

            def feedback_keyboard(self, event_id, url=""):
                return []

            def edit(self, message_id, text, **kwargs):
                self.edited.append((message_id, text))

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "first-publisher.db")
            try:
                bot = FakeBot()
                pipeline = Pipeline(load_config(), db, bot)
                pipeline.translator.translate_item = lambda **kwargs: Translation(
                    text_fa="ترجمهٔ کامل خبر بازداشت مسافران ایرانی در فرودگاه استانبول.",
                    title_fa="بازداشت مسافران ایرانی در فرودگاه استانبول",
                    engine="test",
                )
                now = util.now_utc()
                first_published = now - timedelta(days=3)
                shared_title = "İranlı iki yolcu İstanbul Havalimanı'nda uyuşturucuyla tutuklandı"
                shared_body = (
                    "İran'dan İstanbul Havalimanı'na gelen iki yolcu şüpheli hareketleri "
                    "nedeniyle durduruldu. Valizlerinde çikolatayla kaplanmış 1 kilo 222 "
                    "gram uyuşturucu bulundu ve iki yolcu tutuklandı."
                )
                first = RawItem(
                    source_id="tr_hurriyet",
                    source_name="Hürriyet",
                    source_url="https://www.hurriyet.com.tr/rss/anasayfa",
                    title=shared_title,
                    body=shared_body,
                    url="https://www.hurriyet.com.tr/story/1",
                    lang="tr",
                    country="TR",
                    published_at=first_published,
                    extra={"collected_at": util.iso(now - timedelta(hours=8))},
                )
                self.assertEqual(pipeline.process(first), "published")
                event_id = int(db.latest_events(limit=1)[0]["id"])

                # خبرگزاری زودتر منتشر کرده، اما فیدش دیرتر از Hürriyet دیده می‌شود.
                dha_published = first_published - timedelta(minutes=15)
                second = RawItem(
                    source_id="tr_dha",
                    source_name="DHA",
                    source_url="https://www.dha.com.tr/son-dakika/",
                    title=shared_title,
                    body=shared_body,
                    url="https://www.dha.com.tr/story/1",
                    lang="tr",
                    country="TR",
                    published_at=dha_published,
                    extra={"collected_at": util.iso(now)},
                )
                pipeline.translator.translate_item = lambda **kwargs: Translation(
                    text_fa="", title_fa="", engine="failed"
                )
                result = pipeline.process(second)

                self.assertEqual(result, "updated")
                self.assertEqual(len(bot.sent), 1)
                self.assertNotIn(shared_body, bot.sent[0][0])
                event = db.get_event(event_id)
                self.assertEqual(event["primary_source"], "DHA")
                self.assertEqual(event["primary_published_at"], util.iso(dha_published))
                self.assertTrue(event["telegram_sent_at"])
                self.assertTrue(bot.edited)
                self.assertIn("نخستین انتشارِ رصدشده: DHA", bot.edited[0][1])
                self.assertIn("فاصله از انتشار تا ارسال", bot.edited[0][1])
                self.assertEqual(len(db.distinct_sources_for_event(event_id)), 2)
            finally:
                db.close()

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
        self.assertIn("from:dhainternet", queries[2])
        self.assertIn("#IranAirspace", queries[3])
        self.assertIn("#iranaviationupdate", queries[4])

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
