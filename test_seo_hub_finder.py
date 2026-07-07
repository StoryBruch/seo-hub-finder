"""Unit tests (mocked network). Run: python -m unittest test_seo_hub_finder -v

Dev-only — not part of requirements.txt; uses stdlib unittest only.
"""
import base64
import io
import json
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import seo_hub_finder as shf

# 1x1 red pixel PNG
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class FakeResponse:
    def __init__(self, payload=None, raw=None):
        self._data = raw if raw is not None else json.dumps(payload).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def gemini_text_response(text):
    return FakeResponse({"candidates": [{"content": {"parts": [{"text": text}]}}]})


def gemini_image_response(image_bytes):
    return FakeResponse({"candidates": [{"content": {"parts": [
        {"text": "here you go"},
        {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(image_bytes).decode("ascii")}},
    ]}}]})


def http_error(code):
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(b""))


def hub_plan_fixture():
    return pd.DataFrame([
        {
            "pattern_id": "pattern_001", "hub_label": "Entkalken Hub",
            "hub_article_title": "Entkalken: Übersicht", "hub_slug": "/entkalken/",
            "query_skeleton": "{slot_1} entkalken", "intent": "how_to",
            "url_template": "/{slot_1}-entkalken/",
            "validated_keywords": "jura e8 entkalken; delonghi magnifica entkalken",
            "total_search_volume": 4300, "article_count": 2,
            "ai_suggested_keywords": "krups evidence entkalken", "ai_suggested_count": 1,
            "article_title_template": "", "recommended_article_structure": "",
            "internal_linking_strategy": "", "duplicate_of": "", "risks": "",
        },
        {
            "pattern_id": "pattern_002", "hub_label": "Kaffeebohnen Hub",
            "hub_article_title": "Kaffeebohnen: Übersicht", "hub_slug": "/kaffeebohnen/",
            "query_skeleton": "beste {slot_1} für {slot_2}", "intent": "commercial_comparison",
            "url_template": "/beste-{slot_1}-fur-{slot_2}/",
            "validated_keywords": "beste kaffeebohnen für vollautomat",
            "total_search_volume": 2400, "article_count": 1,
            "ai_suggested_keywords": "", "ai_suggested_count": 0,
            "article_title_template": "", "recommended_article_structure": "",
            "internal_linking_strategy": "", "duplicate_of": "", "risks": "",
        },
    ])


class ParseNumberTests(unittest.TestCase):
    def test_decimal_k(self):
        self.assertEqual(shf.parse_number("1.5k"), 1500.0)
        self.assertEqual(shf.parse_number("1,5k"), 1500.0)

    def test_grouping(self):
        self.assertEqual(shf.parse_number("1.000"), 1000.0)
        self.assertEqual(shf.parse_number("12,345,678"), 12345678.0)

    def test_ranges_and_plain(self):
        self.assertEqual(shf.parse_number("100 - 1K"), 100.0)
        self.assertEqual(shf.parse_number("3.1"), 3.1)
        self.assertEqual(shf.parse_number(""), 0.0)


class IntentClassifierTests(unittest.TestCase):
    def test_vocab_rules(self):
        self.assertEqual(shf.classify_hub_intent("{slot_1} entkalken"), "how_to")
        self.assertEqual(shf.classify_hub_intent("{slot_1} rezept"), "recipe")
        self.assertEqual(shf.classify_hub_intent("alternative {slot_1}"), "alternatives")
        self.assertEqual(shf.classify_hub_intent("beste {slot_1}"), "commercial_comparison")

    def test_sample_query_fallback(self):
        self.assertEqual(
            shf.classify_hub_intent("{slot_1} für {slot_2}", "beste kaffeebohnen für vollautomat"),
            "commercial_comparison",
        )

    def test_infinitive_heuristic_and_default(self):
        self.assertEqual(shf.classify_hub_intent("{slot_1} programmieren"), "how_to")
        self.assertEqual(shf.classify_hub_intent("{slot_1} mahlwerk"), "informational")


class StaticTemplateTests(unittest.TestCase):
    def test_slots_survive_and_german_capitalization(self):
        for intent in shf.HUB_INTENTS:
            template = shf.static_article_template("{slot_1} entkalken", "Entkalken Hub", intent)
            self.assertIn("{slot_1}", template["h1_template"], intent)
            self.assertTrue(len(template["outline"]) >= 5, intent)
            self.assertTrue(all(template[k] for k in (
                "meta_title_template", "meta_description_template", "intro_template")), intent)
        how_to = shf.static_article_template("{slot_1} entkalken", "x", "how_to")
        self.assertIn("Schritt", how_to["h1_template"])  # capitalization preserved


class SanitizerTests(unittest.TestCase):
    def valid_entry(self):
        return {
            "pattern_id": "pattern_002",
            "h1_template": "Beste {slot_1} für {slot_2}: Test",
            "meta_title_template": "t", "meta_description_template": "d", "intro_template": "i",
            "outline": [{"h2": "A"}, {"h2": "B"}, "C"],
            "faq": ["f1", "f2", "f3"],
        }

    def test_valid_accepted_with_coercion(self):
        result = shf._sanitize_ai_template(self.valid_entry(), "beste {slot_1} für {slot_2}")
        self.assertIsNotNone(result)
        self.assertEqual(result["outline"][2], {"h2": "C", "h3": []})

    def test_missing_slot_rejected(self):
        entry = self.valid_entry()
        entry["h1_template"] = "Beste {slot_1}: Test"  # {slot_2} dropped
        self.assertIsNone(shf._sanitize_ai_template(entry, "beste {slot_1} für {slot_2}"))

    def test_small_faq_kept_as_field_fallback(self):
        entry = self.valid_entry()
        entry["faq"] = ["only one"]
        result = shf._sanitize_ai_template(entry, "beste {slot_1} für {slot_2}")
        self.assertIsNotNone(result)
        self.assertIsNone(result["faq"])


class GeminiClientTests(unittest.TestCase):
    def test_model_fallback_on_404(self):
        calls = []

        def fake_urlopen(request, timeout=None):
            calls.append(request.full_url)
            if len(calls) == 1:
                raise http_error(404)
            return gemini_text_response("hello")

        with patch("seo_hub_finder.urllib.request.urlopen", fake_urlopen):
            text, status = shf._call_gemini("p", "key")
        self.assertEqual(text, "hello")
        self.assertEqual(len(calls), 2)
        self.assertIn(shf.AI_MODEL_FALLBACKS[0], calls[1])
        self.assertIn("ok:", status)

    def test_auth_error_aborts_chain(self):
        calls = []

        def fake_urlopen(request, timeout=None):
            calls.append(1)
            raise http_error(403)

        with patch("seo_hub_finder.urllib.request.urlopen", fake_urlopen):
            text, status = shf._call_gemini("p", "key")
        self.assertIsNone(text)
        self.assertEqual(status, "http_auth")
        self.assertEqual(len(calls), 1)

    def test_key_in_header_not_url(self):
        seen = {}

        def fake_urlopen(request, timeout=None):
            seen["url"] = request.full_url
            seen["key"] = request.get_header("X-goog-api-key")
            return gemini_text_response("x")

        with patch("seo_hub_finder.urllib.request.urlopen", fake_urlopen):
            shf._call_gemini("p", "sekrit")
        self.assertNotIn("sekrit", seen["url"])
        self.assertEqual(seen["key"], "sekrit")


class ArticleTemplateBatchTests(unittest.TestCase):
    def test_single_batched_call_and_merge(self):
        calls = []
        ai_entry = {
            "pattern_id": "pattern_001", "intent": "how_to",
            "h1_template": "{slot_1} entkalken: Die komplette Anleitung",
            "meta_title_template": "mt", "meta_description_template": "md", "intro_template": "in",
            "outline": [{"h2": "Eins"}, {"h2": "Zwei"}, {"h2": "Drei"}],
            "faq": ["a?", "b?", "c?", "d?"],
        }

        def fake_urlopen(request, timeout=None):
            calls.append(request)
            return gemini_text_response(json.dumps([ai_entry]))

        with patch("seo_hub_finder.urllib.request.urlopen", fake_urlopen):
            enriched, status = shf.enrich_hub_plan_with_article_templates(hub_plan_fixture(), api_key="k")
        self.assertEqual(len(calls), 1)  # ONE batched call for all hubs
        self.assertEqual(status, "gemini_partial")  # 1 of 2 hubs answered
        row1 = enriched[enriched["pattern_id"] == "pattern_001"].iloc[0]
        row2 = enriched[enriched["pattern_id"] == "pattern_002"].iloc[0]
        self.assertEqual(row1["template_source"], "gemini")
        self.assertEqual(row1["h1_template"], "{slot_1} entkalken: Die komplette Anleitung")
        self.assertEqual(row2["template_source"], "static")
        self.assertIn("{slot_1}", row2["h1_template"])
        for col in shf.TEMPLATE_COLUMNS:
            self.assertIn(col, enriched.columns)
        json.loads(row1["article_outline_json"])  # valid JSON

    def test_no_key_no_network(self):
        def fail(*a, **k):
            raise AssertionError("network call without key!")

        with patch("seo_hub_finder.urllib.request.urlopen", fail), \
             patch.dict("os.environ", {}, clear=True):
            enriched, status = shf.enrich_hub_plan_with_article_templates(hub_plan_fixture(), api_key=None)
        self.assertEqual(status, "no_api_key")
        self.assertTrue((enriched["template_source"] == "static").all())

    def test_parse_error_falls_back_static(self):
        with patch("seo_hub_finder.urllib.request.urlopen",
                   lambda r, timeout=None: gemini_text_response("Entschuldigung, kein JSON.")):
            enriched, status = shf.enrich_hub_plan_with_article_templates(hub_plan_fixture(), api_key="k")
        self.assertEqual(status, "parse_error")
        self.assertTrue((enriched["template_source"] == "static").all())

    def test_empty_hub_plan(self):
        enriched, status = shf.enrich_hub_plan_with_article_templates(pd.DataFrame(), api_key="k")
        self.assertEqual(status, "no_hubs")
        for col in shf.TEMPLATE_COLUMNS:
            self.assertIn(col, enriched.columns)


class HeroImageTests(unittest.TestCase):
    def enriched_fixture(self):
        with patch("seo_hub_finder.urllib.request.urlopen",
                   lambda r, timeout=None: (_ for _ in ()).throw(http_error(500))):
            enriched, _ = shf.enrich_hub_plan_with_article_templates(
                hub_plan_fixture(), api_key=None, use_ai=False)
        return enriched

    def test_gemini_success(self):
        def fake_urlopen(request, timeout=None):
            return gemini_image_response(TINY_PNG)

        with patch("seo_hub_finder.urllib.request.urlopen", fake_urlopen):
            results = shf.generate_hero_images(
                self.enriched_fixture(), api_key="k",
                gemini_min_interval=0, pollinations_min_interval=0)
        self.assertEqual([r.hero_image_status for r in results], ["ok", "ok"])
        self.assertTrue(all(r.hero_image_provider == "gemini" for r in results))
        self.assertTrue(all(r.image_bytes for r in results))
        self.assertNotIn("hub", results[0].hero_image_file)

    def test_gemini_429_disables_and_falls_back(self):
        calls = {"gemini": 0, "pollinations": 0}
        fake_jpeg = b"\xff\xd8" + b"0" * 20000

        def fake_urlopen(request, timeout=None):
            if "generativelanguage" in request.full_url:
                calls["gemini"] += 1
                raise http_error(429)
            calls["pollinations"] += 1
            return FakeResponse(raw=fake_jpeg)

        with patch("seo_hub_finder.urllib.request.urlopen", fake_urlopen):
            results = shf.generate_hero_images(
                self.enriched_fixture(), api_key="k",
                gemini_min_interval=0, pollinations_min_interval=0)
        self.assertEqual(calls["gemini"], 1)  # disabled after first 429
        self.assertEqual(calls["pollinations"], 2)
        self.assertTrue(all(r.hero_image_provider == "pollinations" for r in results))

    def test_both_fail_keeps_prompt(self):
        def fake_urlopen(request, timeout=None):
            if "generativelanguage" in request.full_url:
                raise http_error(500)
            return FakeResponse(raw=b"<html>error</html>")

        with patch("seo_hub_finder.urllib.request.urlopen", fake_urlopen):
            results = shf.generate_hero_images(
                self.enriched_fixture(), api_key="k",
                gemini_min_interval=0, pollinations_min_interval=0)
        self.assertTrue(all(r.hero_image_status == "failed_all_providers" for r in results))
        self.assertTrue(all(r.hero_image_prompt for r in results))
        self.assertTrue(all("no text" in r.hero_image_prompt for r in results))

    def test_disabled_and_cap(self):
        def fail(*a, **k):
            raise AssertionError("no network when disabled")

        with patch("seo_hub_finder.urllib.request.urlopen", fail):
            results = shf.generate_hero_images(self.enriched_fixture(), api_key="k", enabled=False)
        self.assertTrue(all(r.hero_image_status == "skipped_disabled" for r in results))

        with patch("seo_hub_finder.urllib.request.urlopen",
                   lambda r, timeout=None: gemini_image_response(TINY_PNG)):
            results = shf.generate_hero_images(
                self.enriched_fixture(), api_key="k", max_images=1,
                gemini_min_interval=0, pollinations_min_interval=0)
        self.assertEqual([r.hero_image_status for r in results], ["ok", "skipped_cap"])

    def test_duplicate_hub_skipped(self):
        plan = self.enriched_fixture()
        plan.loc[plan["pattern_id"] == "pattern_002", "duplicate_of"] = "pattern_001"
        with patch("seo_hub_finder.urllib.request.urlopen",
                   lambda r, timeout=None: gemini_image_response(TINY_PNG)):
            results = shf.generate_hero_images(
                plan, api_key="k", gemini_min_interval=0, pollinations_min_interval=0)
        statuses = {r.pattern_id: r.hero_image_status for r in results}
        self.assertEqual(statuses["pattern_002"], "skipped_duplicate_hub")

    def test_postprocess_dimensions(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")
        src = io.BytesIO()
        Image.new("RGB", (1024, 1024), (200, 30, 30)).save(src, format="PNG")
        processed, ext, mime = shf._postprocess_hero_image(src.getvalue(), "image/png")
        self.assertEqual(ext, "jpg")
        img = Image.open(io.BytesIO(processed))
        self.assertEqual(img.size, (shf.HERO_IMAGE_WIDTH, shf.HERO_IMAGE_HEIGHT))


class WriteOutputsTests(unittest.TestCase):
    def test_foreign_files_survive_and_zip_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            (out_dir / "user_data.txt").write_text("keep me", encoding="utf-8")
            hero_dir = out_dir / "hero_images"
            hero_dir.mkdir()
            (hero_dir / "stale.jpg").write_bytes(b"old")

            with patch("seo_hub_finder.urllib.request.urlopen",
                       lambda r, timeout=None: (_ for _ in ()).throw(http_error(500))):
                enriched, _ = shf.enrich_hub_plan_with_article_templates(
                    hub_plan_fixture(), api_key=None, use_ai=False)
            hero = [shf.HeroImageResult(
                pattern_id="pattern_001", hub_label="Entkalken Hub", hero_image_prompt="p",
                hero_image_file="entkalken.jpg", hero_image_provider="gemini",
                hero_image_status="ok", image_bytes=b"\xff\xd8" + b"0" * 100, mime_type="image/jpeg")]
            enriched = shf.attach_hero_image_metadata(enriched, hero)
            empty = pd.DataFrame()
            shf.write_outputs(
                empty, empty, empty, empty, enriched, out_dir,
                hero_images=hero, ai_template_status="no_api_key",
            )
            self.assertEqual((out_dir / "user_data.txt").read_text(encoding="utf-8"), "keep me")
            self.assertFalse((hero_dir / "stale.jpg").exists())
            self.assertTrue((hero_dir / "entkalken.jpg").exists())
            with zipfile.ZipFile(out_dir / "seo_hub_finder_outputs.zip") as zf:
                names = zf.namelist()
            self.assertIn("hero_images/entkalken.jpg", names)
            self.assertNotIn("user_data.txt", names)
            html_report = (out_dir / "seo_hub_finder_report.html").read_text(encoding="utf-8")
            self.assertIn("data:image/jpeg;base64,", html_report)
            self.assertIn("Artikel-Templates pro Hub", html_report)


class TrendsClassificationTests(unittest.TestCase):
    def test_missing_column_is_no_signal_without_retries(self):
        class FakeTrendReq:
            def __init__(self, *a, **k):
                self.calls = 0

            def build_payload(self, *a, **k):
                pass

            def interest_over_time(self):
                # anchor present, candidate silently dropped by Trends
                return pd.DataFrame({"anchor kw": [50, 60]})

        candidates = pd.DataFrame([{"pattern_id": "p1", "candidate_query": "unknown kw"}])
        memberships = pd.DataFrame([{
            "pattern_id": "p1", "query": "anchor kw", "clicks": 10,
            "impressions": 100, "position": 3, "current_url": "",
            "query_skeleton": "", "slot_values": "",
            "pattern_accepted_before_volume": True, "pattern_reject_reason": "",
        }])
        fake_module = type("M", (), {"TrendReq": FakeTrendReq})
        with patch.dict("sys.modules", {"pytrends.request": fake_module, "pytrends": type("P", (), {})}), \
             patch("seo_hub_finder.time.sleep") as fake_sleep:
            result = shf.check_new_keyword_relevance(candidates, memberships)
        self.assertEqual(result.iloc[0]["trends_status"], "no_signal")
        # only the polite 1s inter-request sleep, no retry ladder
        total_slept = sum(c.args[0] for c in fake_sleep.call_args_list)
        self.assertLessEqual(total_slept, 2)

    def test_candidate_equals_anchor_skipped(self):
        candidates = pd.DataFrame([{"pattern_id": "p1", "candidate_query": "anchor kw"}])
        memberships = pd.DataFrame([{
            "pattern_id": "p1", "query": "anchor kw", "clicks": 10,
            "impressions": 100, "position": 3, "current_url": "",
            "query_skeleton": "", "slot_values": "",
            "pattern_accepted_before_volume": True, "pattern_reject_reason": "",
        }])

        class BoomTrendReq:
            def __init__(self, *a, **k):
                pass

            def build_payload(self, *a, **k):
                raise AssertionError("should not be called for anchor-duplicates")

        fake_module = type("M", (), {"TrendReq": BoomTrendReq})
        with patch.dict("sys.modules", {"pytrends.request": fake_module, "pytrends": type("P", (), {})}):
            result = shf.check_new_keyword_relevance(candidates, memberships)
        self.assertEqual(result.iloc[0]["trends_status"], "already_ranking_anchor")


class ArticlePlanTests(unittest.TestCase):
    def test_slot_filling_and_ai_rows(self):
        with patch("seo_hub_finder.urllib.request.urlopen",
                   lambda r, timeout=None: (_ for _ in ()).throw(http_error(500))):
            enriched, _ = shf.enrich_hub_plan_with_article_templates(
                hub_plan_fixture(), api_key=None, use_ai=False)
        opportunities = pd.DataFrame([{
            "pattern_id": "pattern_001", "query": "jura e8 entkalken",
            "slot_values": "jura e8", "current_url": "https://x/jura",
            "search_volume": 1900, "final_status": "confirmed_opportunity",
        }])
        plan = shf.build_article_plan(opportunities, enriched)
        gsc_row = plan[plan["keyword"] == "jura e8 entkalken"].iloc[0]
        self.assertEqual(gsc_row["article_h1"], "Jura e8 entkalken: Schritt-für-Schritt-Anleitung")
        self.assertEqual(gsc_row["article_url"], "/jura-e8-entkalken/")
        self.assertEqual(gsc_row["status"], "already_covered_by_existing_page")
        ai_rows = plan[plan["status"] == "new_article_ai_suggested"]
        self.assertEqual(len(ai_rows), 1)
        self.assertEqual(ai_rows.iloc[0]["keyword"], "krups evidence entkalken")
        self.assertIn("krups evidence", ai_rows.iloc[0]["article_h1"].lower())


class HubLabelTests(unittest.TestCase):
    def test_connector_only_label_uses_member_queries(self):
        label = shf.infer_hub_label(
            "{slot_1} für {slot_2}",
            ["beste kaffeebohnen für vollautomat", "beste kaffeebohnen für espresso"],
        )
        self.assertEqual(label, "Kaffeebohnen Hub")

    def test_public_topic_strips_hub(self):
        self.assertEqual(shf.hub_public_topic("Entkalken Hub"), "Entkalken")
        self.assertEqual(shf.hub_public_topic("Für Pattern Hub"), "Für")


if __name__ == "__main__":
    unittest.main(verbosity=2)
