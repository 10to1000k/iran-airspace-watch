"""تست‌های سقف بدنه، متن کامل مقاله و پایش هشتگ — بدون شبکه."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from src import util
from src.classify import Classifier
from src.config import load_config
from src.models import RawItem


# ── استخراج و دریافت متن کامل مقاله ──────────────────────────


class _FakeResponse:
    def __init__(self, html: str, content_type: str = "text/html; charset=utf-8"):
        self._html = html
        self.headers = {"Content-Type": content_type}
        self.encoding = "utf-8"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=32768):
        data = self._html.encode("utf-8")
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]


class ArticleTextTests(unittest.TestCase):
    def test_extract_skips_chrome_and_keeps_paragraphs(self):
        from src.article import extract_article_text

        html = (
            "<html><head><style>body{color:red}</style></head><body>"
            "<nav>main menu items</nav>"
            "<article><h1>Iran airspace closed</h1>"
            "<p>Flights were suspended at IKA. Runway 29L is closed.</p>"
            "<script>tracking('pixel');</script>"
            "<p>NOTAM A1234/26 was issued for Tehran FIR.</p>"
            "</article><footer>copyright notice</footer></body></html>"
        )
        text = extract_article_text(html, max_chars=5000)
        self.assertIn("Iran airspace closed", text)
        self.assertIn("Runway 29L is closed", text)
        self.assertIn("NOTAM A1234/26", text)
        self.assertNotIn("tracking", text)
        self.assertNotIn("copyright", text)
        self.assertNotIn("main menu", text)

    def test_extract_caps_length_at_word_boundary(self):
        from src.article import extract_article_text

        html = "<p>" + ("word " * 3000) + "</p>"
        text = extract_article_text(html, max_chars=100)
        self.assertLessEqual(len(text), 101)
        self.assertTrue(text)

    def test_broken_html_does_not_raise(self):
        from src.article import extract_article_text

        text = extract_article_text("<p>unclosed paragraph <b>bold", max_chars=100)
        self.assertIn("unclosed", text)

    def test_fetch_parses_html_response(self):
        from src.article import fetch_article_text

        html = "<html><body><p>Iran airspace NOTAM issued today.</p></body></html>"
        with patch(
            "src.article.requests.get", return_value=_FakeResponse(html)
        ) as get:
            text = fetch_article_text("https://example.com/news/1", max_chars=2000)
        get.assert_called_once()
        self.assertIn("Iran airspace NOTAM issued today.", text)

    def test_fetch_caps_length(self):
        from src.article import fetch_article_text

        html = "<p>" + ("کلمه " * 2000) + "</p>"
        with patch("src.article.requests.get", return_value=_FakeResponse(html)):
            text = fetch_article_text("https://example.com/news/1", max_chars=200)
        self.assertLessEqual(len(text), 201)
        self.assertTrue(text)

    def test_fetch_skips_non_html_content(self):
        from src.article import fetch_article_text

        with patch(
            "src.article.requests.get",
            return_value=_FakeResponse("binary", content_type="image/jpeg"),
        ):
            self.assertEqual(fetch_article_text("https://example.com/x"), "")

    def test_fetch_returns_empty_on_network_error(self):
        from src.article import fetch_article_text

        with patch(
            "src.article.requests.get",
            side_effect=requests.ConnectionError("down"),
        ):
            self.assertEqual(fetch_article_text("https://example.com/x"), "")

    def test_fetch_ignores_non_http_urls(self):
        from src.article import fetch_article_text

        self.assertEqual(fetch_article_text(""), "")
        self.assertEqual(fetch_article_text("ftp://example.com/x"), "")


# ── پایپ‌لاین: جایگزینی خلاصه‌ی بریده با متن کامل ────────────


class FullArticleEnrichmentTests(unittest.TestCase):
    def _item(self, **overrides) -> RawItem:
        fields = dict(
            source_id="rss_test",
            source_name="Test Feed",
            source_url="https://example.com/rss",
            title="Airspace update",
            body="Short summary.",
            url="https://example.com/news/1",
            lang="en",
            country="INTL",
            kind="rss",
        )
        fields.update(overrides)
        return RawItem(**fields)

    def _pipeline(self, tmp: str, config=None):
        from src.db import Database
        from src.pipeline import Pipeline

        db = Database(Path(tmp) / "t.db")
        return Pipeline(config or load_config(), db, Mock()), db

    def test_truncated_rss_body_is_replaced_with_full_text(self):
        from src.db import Database
        from src.pipeline import Pipeline

        config = load_config()
        full_text = "Full article body. " * 100
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "t.db")
            try:
                pipeline = Pipeline(config, db, Mock())
                item = self._item()
                with patch(
                    "src.pipeline.fetch_article_text", return_value=full_text
                ) as fetch:
                    pipeline._enrich_full_text(item)
                fetch.assert_called_once()
                self.assertEqual(item.body, full_text)
            finally:
                db.close()

    def test_complete_summary_skips_fetch(self):
        from src.pipeline import Pipeline

        with tempfile.TemporaryDirectory() as tmp:
            pipeline, db = self._pipeline(tmp)
            try:
                item = self._item(body="x" * 500)
                with patch("src.pipeline.fetch_article_text") as fetch:
                    pipeline._enrich_full_text(item)
                fetch.assert_not_called()
                self.assertEqual(item.body, "x" * 500)
            finally:
                db.close()

    def test_ellipsis_summary_triggers_fetch(self):
        from src.pipeline import Pipeline

        with tempfile.TemporaryDirectory() as tmp:
            pipeline, db = self._pipeline(tmp)
            try:
                item = self._item(body="y" * 500 + "…")
                with patch(
                    "src.pipeline.fetch_article_text", return_value="FULL " * 150
                ) as fetch:
                    pipeline._enrich_full_text(item)
                fetch.assert_called_once()
                self.assertTrue(item.body.startswith("FULL"))
            finally:
                db.close()

    def test_shorter_full_text_does_not_downgrade_body(self):
        from src.pipeline import Pipeline

        with tempfile.TemporaryDirectory() as tmp:
            pipeline, db = self._pipeline(tmp)
            try:
                item = self._item(body="ی" * 500 + "…")
                with patch("src.pipeline.fetch_article_text", return_value="tiny"):
                    pipeline._enrich_full_text(item)
                self.assertTrue(item.body.startswith("ی"))
            finally:
                db.close()

    def test_non_rss_items_are_not_enriched(self):
        from src.pipeline import Pipeline

        with tempfile.TemporaryDirectory() as tmp:
            pipeline, db = self._pipeline(tmp)
            try:
                item = self._item(kind="telegram", url="https://t.me/x/1")
                with patch("src.pipeline.fetch_article_text") as fetch:
                    pipeline._enrich_full_text(item)
                fetch.assert_not_called()
            finally:
                db.close()

    def test_disabled_flag_skips_fetch(self):
        from src.pipeline import Pipeline

        config = load_config()
        config.settings.setdefault("poll", {})["fetch_full_article"] = False
        with tempfile.TemporaryDirectory() as tmp:
            pipeline, db = self._pipeline(tmp, config)
            try:
                with patch("src.pipeline.fetch_article_text") as fetch:
                    pipeline._enrich_full_text(self._item())
                fetch.assert_not_called()
            finally:
                db.close()

    def test_fingerprint_stays_stable_when_body_grows(self):
        """اثر انگشت قبل از غنی‌سازی محاسبه می‌شود؛ آیتم تکراری «seen» می‌ماند."""
        from src.db import Database
        from src.pipeline import Pipeline

        config = load_config()
        full_text = "Unrelated full text about weather in Paris. " * 50
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "t.db")
            try:
                pipeline = Pipeline(config, db, Mock())
                with patch(
                    "src.pipeline.fetch_article_text", return_value=full_text
                ):
                    first = pipeline.process(self._item())
                with patch(
                    "src.pipeline.fetch_article_text", return_value=full_text
                ):
                    second = pipeline.process(self._item())
                # متن ساختگی از فیلتر ربط رد می‌شود ولی ثبت/دِداپ درست کار می‌کند
                self.assertEqual(first, "irrelevant")
                self.assertEqual(second, "seen")
            finally:
                db.close()


# ── سقف بدنه برای هر کالکتور ─────────────────────────────────


class BodyCapTests(unittest.TestCase):
    def test_body_cap_settings_exist(self):
        config = load_config()
        poll = config.settings.get("poll", {})
        for key in (
            "max_feed_body_chars",
            "max_telegram_body_chars",
            "max_x_body_chars",
            "max_instagram_body_chars",
            "fetch_full_article",
            "article_max_chars",
        ):
            self.assertIn(key, poll)
        self.assertIn("max_translate_chars", config.settings.get("app", {}))

    def test_rss_body_uses_configured_cap(self):
        from src.collectors import RssCollector, _entry_body

        config = load_config()
        collector = RssCollector(config, db=None)
        self.assertEqual(
            collector.max_body_chars, int(config.get("poll.max_feed_body_chars", 1200))
        )
        entry = {"summary": "word " * 500}
        body = _entry_body(entry, collector.max_body_chars)
        self.assertLessEqual(len(body), collector.max_body_chars + 1)
        self.assertTrue(body.endswith("…"))

    def test_rsshub_tag_feed_carries_tag_to_classifier_and_telegram(self):
        from src.collectors import RssCollector

        config = load_config()
        collector = RssCollector(config, db=None)
        xml = b"""<?xml version='1.0'?><rss version='2.0'><channel>
        <item><title>Airspace update</title><link>https://x.com/post/1</link>
        <description>Short post.</description></item></channel></rss>"""
        source = {
            "id": "social_x_tag_iran_airspace",
            "name": "X #IranAirspace",
            "url": "http://social-feed-hub:1200/twitter/keyword/%23IranAirspace",
            "lang": "en",
            "country": "INTL",
            "tier": 1,
            "watched_hashtags": ["IranAirspace"],
            "airport_hashtag_feed": True,
        }
        with patch.object(collector, "_read_bounded", return_value=xml):
            items = collector._fetch(source)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].extra["watched_hashtags"], ["IranAirspace"])
        self.assertIn("#IranAirspace", items[0].extra["hashtags"])
        self.assertTrue(items[0].extra["airport_hashtag_feed"])

    def test_telegram_collector_reads_cap_from_config(self):
        from src.collectors import TelegramChannelCollector

        config = load_config()
        telegram = TelegramChannelCollector(config)
        self.assertEqual(
            telegram.max_body_chars,
            int(config.get("poll.max_telegram_body_chars", 1200)),
        )

    def test_x_collector_reads_cap_from_config(self):
        from src.collectors import XCollector

        config = load_config()
        x = XCollector(config, airline_index=None)
        self.assertEqual(x.max_body_chars, int(config.get("poll.max_x_body_chars", 1000)))


# ── پایش هشتگ ────────────────────────────────────────────────


class HashtagMonitoringTests(unittest.TestCase):
    def test_watchlist_settings_exist(self):
        config = load_config()
        hashtags = config.settings.get("hashtags", {})
        self.assertTrue(hashtags.get("enabled"))
        self.assertTrue(hashtags.get("watchlist"))

    def test_load_watchlist_respects_master_switch(self):
        from src.collectors import load_watchlist

        config = load_config()
        tags = load_watchlist(config)
        self.assertIn("TehranFIR", tags)
        config.settings["hashtags"]["enabled"] = False
        self.assertEqual(load_watchlist(config), [])

    def test_watched_hashtags_match_case_insensitively(self):
        from src.collectors import watched_hashtags

        hits = watched_hashtags("Breaking: #tehranfir closed now", ["TehranFIR", "OIIX"])
        self.assertEqual(hits, ["TehranFIR"])
        self.assertEqual(watched_hashtags("no tags here", ["TehranFIR"]), [])

    def test_x_queries_include_hashtag_clause(self):
        from src.collectors import XCollector

        config = load_config()
        queries = XCollector(config, airline_index=None).queries()
        hashtag_queries = [q for q in queries if "#TehranFIR" in q]
        self.assertEqual(len(hashtag_queries), 1)
        self.assertIn("#MahanAir", hashtag_queries[0])
        self.assertIn("-filter:replies", hashtag_queries[0])

    def test_rsshub_hashtag_route_metadata_is_a_topic_signal(self):
        classifier = Classifier(load_config())
        item = RawItem(
            source_id="social_ig_tag_iran_airspace",
            source_name="Instagram #IranAirspace",
            source_url="http://social-feed-hub:1200/instagram/tags/IranAirspace",
            title="Regional aviation update",
            body="A short post without its original hashtag text.",
            url="https://www.instagram.com/p/example/",
            lang="en",
            country="INTL",
            kind="rss",
            extra={"watched_hashtags": ["IranAirspace"]},
        )
        result = classifier.classify(item)
        self.assertTrue(result.relevant)
        self.assertTrue(result.publishable)
        self.assertEqual(result.category, "airspace")

    def test_social_hashtag_feeds_are_enabled_with_publish_all_metadata(self):
        config = load_config()
        sources = config.sources.get("sources", [])
        hashtag_feeds = [s for s in sources if s["id"].startswith("social_") and "_tag_" in s["id"]]
        self.assertEqual(len(hashtag_feeds), 50)
        self.assertTrue(all(s.get("enabled") for s in hashtag_feeds))
        self.assertTrue(all(s.get("airport_hashtag_feed") for s in hashtag_feeds))
        self.assertLessEqual(len(config.get("hashtags.watchlist", [])), 30)

    def test_airport_hashtag_feed_bypasses_keyword_thresholds_and_vetoes(self):
        classifier = Classifier(load_config())
        item = RawItem(
            source_id="social_ig_tag_mashhad_airport",
            source_name="Instagram #MashhadAirport",
            source_url="http://social-feed-hub:1200/instagram/tags/MashhadAirport",
            title="Local council announces a new museum",
            body="A short post with no aviation keywords.",
            url="https://www.instagram.com/p/example/",
            lang="en",
            country="INTL",
            kind="rss",
            extra={"watched_hashtags": ["MashhadAirport"], "airport_hashtag_feed": True},
        )
        result = classifier.classify(item)
        self.assertTrue(result.relevant)
        self.assertTrue(result.publishable)
        self.assertEqual(result.category, "airport")
        self.assertIn("IR", result.countries)

    def test_single_aviation_keyword_is_enough_when_iran_is_present(self):
        classifier = Classifier(load_config())
        item = RawItem(
            source_id="rss_test",
            source_name="Test Feed",
            source_url="",
            title="Iran flight update",
            body="",
            url="",
            lang="en",
            country="INTL",
            kind="rss",
        )
        result = classifier.classify(item)
        self.assertTrue(result.relevant)
        self.assertTrue(result.publishable)

    def test_terse_tehran_fir_post_passes_relevance_via_signal(self):
        classifier = Classifier(load_config())
        item = RawItem(
            source_id="x_test",
            source_name="@test",
            source_url="",
            title="#TehranFIR",
            body="",
            url="",
            lang="en",
            kind="x",
            extra={"watched_hashtags": ["TehranFIR"]},
        )
        result = classifier.classify(item)
        self.assertTrue(result.relevant)
        self.assertEqual(result.category, "airspace")

    def test_watched_airline_hashtag_is_matched_as_airline(self):
        classifier = Classifier(load_config())
        item = RawItem(
            source_id="ig_test",
            source_name="Instagram #MahanAir",
            source_url="",
            title="#MahanAir update",
            body="airspace over Tehran reopened this morning",
            url="",
            lang="en",
            kind="instagram",
            extra={"watched_hashtags": ["MahanAir"]},
        )
        result = classifier.classify(item)
        self.assertTrue(result.relevant)
        self.assertIn("Mahan Air", result.airlines)

    def test_persian_underscored_hashtag_is_expanded(self):
        classifier = Classifier(load_config())
        item = RawItem(
            source_id="tg_test",
            source_name="Telegram",
            source_url="",
            title="ایران: لغو پروازها",
            body="#حریم_هوایی",
            url="",
            lang="fa",
            kind="telegram",
            extra={"watched_hashtags": ["حریم_هوایی"]},
        )
        result = classifier.classify(item)
        self.assertTrue(result.relevant)

    def test_items_without_watched_hashtags_keep_old_behavior(self):
        classifier = Classifier(load_config())
        item = RawItem(
            source_id="x_test",
            source_name="@test",
            source_url="",
            title="Weather forecast for Brazil",
            body="rain expected",
            url="",
            lang="en",
            kind="x",
        )
        self.assertFalse(classifier.classify(item).relevant)


class InstagramCollectorTests(unittest.TestCase):
    def test_disabled_by_default(self):
        from src.collectors import InstagramHashtagCollector, build_jobs
        from src.airlines import AirlineIndex

        config = load_config()
        self.assertFalse(InstagramHashtagCollector(config).enabled)
        jobs = build_jobs(
            config, db=None, airline_index=AirlineIndex(config.airlines)
        )
        self.assertNotIn("instagram", [job.name for job in jobs])
        self.assertNotIn("x", [job.name for job in jobs])

    def test_job_registered_when_env_flag_set(self):
        from src.airlines import AirlineIndex
        from src.collectors import build_jobs

        config = load_config()
        with patch.dict("os.environ", {"ENABLE_INSTAGRAM": "true"}):
            jobs = build_jobs(
                config, db=None, airline_index=AirlineIndex(config.airlines)
            )
        self.assertIn("instagram", [job.name for job in jobs])

    def test_poll_builds_raw_items_from_posts(self):
        from datetime import timedelta

        from src.collectors import InstagramHashtagCollector

        config = load_config()
        collector = InstagramHashtagCollector(config)
        collector.enabled = True
        fresh = util.now_utc()
        fresh_post = SimpleNamespace(
            caption="#TehranFIR ground stop at IKA — updates soon #ایران",
            date_utc=fresh,
            shortcode="C12345",
        )
        stale_post = SimpleNamespace(
            caption="old post #TehranFIR",
            date_utc=fresh - timedelta(hours=48),
            shortcode="C00000",
        )
        first_tag = collector.watched[0]
        with patch.object(collector, "_make_loader", return_value=object()):
            with patch.object(
                collector,
                "_tag_posts",
                side_effect=lambda loader, tag: [fresh_post, stale_post]
                if tag == first_tag
                else [],
            ):
                with patch("src.collectors.time.sleep"):
                    items = collector.poll()
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.kind, "instagram")
        self.assertEqual(item.source_id, "instagram")
        self.assertEqual(item.url, "https://www.instagram.com/p/C12345/")
        self.assertEqual(item.extra["watched_hashtags"], ["TehranFIR"])
        self.assertIn("#TehranFIR", item.extra["hashtags"])

    def test_poll_returns_empty_when_instaloader_missing(self):
        from src.collectors import InstagramHashtagCollector

        collector = InstagramHashtagCollector(load_config())
        collector.enabled = True
        with patch.object(
            collector, "_make_loader", side_effect=ImportError("no instaloader")
        ):
            self.assertEqual(collector.poll(), [])


# ── سقف ورودی مترجم ──────────────────────────────────────────


class TranslatorCapTests(unittest.TestCase):
    def test_long_body_is_capped_before_llm(self):
        from src.translator import Translator

        translator = Translator(load_config(), db=None)
        translator.api_key = "test-key"
        body = "word " * 2000  # ۱۰هزار کاراکتر
        with patch.object(translator, "_chat", return_value="تیتر\n---\nمتن") as chat:
            translator.translate_item("Title", body, "en")
        instruction = chat.call_args[0][0]
        self.assertIn("BODY:\n", instruction)
        body_part = instruction.split("BODY:\n", 1)[1]
        self.assertLessEqual(len(body_part), translator.max_translate_chars + 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
