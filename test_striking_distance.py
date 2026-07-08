"""Unit tests for striking_distance_finder. All network calls are mocked."""
import io
import json
import math
import unittest
import urllib.error
from unittest.mock import patch

import pandas as pd

import striking_distance_finder as sdf


class FakeResponse:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def http_error(code):
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(b""))


class ParseNumberTests(unittest.TestCase):
    def test_german_decimal(self):
        self.assertAlmostEqual(sdf.parse_number("7,3"), 7.3)

    def test_dot_thousands(self):
        self.assertEqual(sdf.parse_number("1.234"), 1234)

    def test_comma_thousands(self):
        self.assertEqual(sdf.parse_number("1,234"), 1234)

    def test_english_mixed(self):
        self.assertAlmostEqual(sdf.parse_number("1,234.5"), 1234.5)

    def test_german_mixed(self):
        self.assertAlmostEqual(sdf.parse_number("1.234,56"), 1234.56)

    def test_percent_stripped(self):
        self.assertAlmostEqual(sdf.parse_number("2.15%"), 2.15)

    def test_millions(self):
        self.assertEqual(sdf.parse_number("1.234.567"), 1234567)

    def test_plain_int(self):
        self.assertEqual(sdf.parse_number("12"), 12)

    def test_empty_is_nan(self):
        self.assertTrue(math.isnan(sdf.parse_number("")))

    def test_dash_is_nan(self):
        self.assertTrue(math.isnan(sdf.parse_number("–")))


class BucketTests(unittest.TestCase):
    def test_fractional_positions_never_fall_through(self):
        # Regression: 8.1 / 5.9 / 8.8 used to return None with integer buckets.
        self.assertEqual(sdf.assign_bucket(8.1), "6–8")
        self.assertEqual(sdf.assign_bucket(5.9), "4–5")
        self.assertEqual(sdf.assign_bucket(8.8), "6–8")

    def test_boundaries(self):
        self.assertEqual(sdf.assign_bucket(1.0), "1")
        self.assertEqual(sdf.assign_bucket(3.0), "3")
        self.assertEqual(sdf.assign_bucket(4.0), "4–5")
        self.assertEqual(sdf.assign_bucket(6.0), "6–8")
        self.assertEqual(sdf.assign_bucket(20.5), "16–20")

    def test_out_of_range(self):
        self.assertIsNone(sdf.assign_bucket(21.0))
        self.assertIsNone(sdf.assign_bucket(float("nan")))
        self.assertEqual(sdf.assign_bucket(0.4), "1")

    def test_every_position_1_to_20_has_a_bucket(self):
        for p in [x / 10 for x in range(10, 210)]:  # 1.0 .. 20.9
            self.assertIsNotNone(sdf.assign_bucket(p), f"gap at {p}")


class CleanGscTests(unittest.TestCase):
    def _df(self, **overrides):
        data = {
            "Query": ["a", "b", "c", ""],
            "Page": ["/a", "/b", "/c", "/d"],
            "Clicks": ["10", "0", "5", "1"],
            "Impressions": ["100", "50", "0", "20"],
            "CTR": ["10%", "0%", "0%", "5%"],
            "Position": ["4,5", "8.1", "12", "3"],
        }
        data.update(overrides)
        return pd.DataFrame(data)

    def test_recomputes_ctr_from_clicks_impressions(self):
        out = sdf.clean_gsc(self._df())
        row = out[out["query"] == "a"].iloc[0]
        self.assertAlmostEqual(row["ctr"], 0.10)

    def test_drops_empty_query_and_zero_impressions(self):
        out = sdf.clean_gsc(self._df())
        self.assertNotIn("", list(out["query"]))
        self.assertNotIn("c", list(out["query"]))  # impressions 0 -> dropped

    def test_german_position_decimal(self):
        out = sdf.clean_gsc(self._df())
        self.assertAlmostEqual(out[out["query"] == "a"].iloc[0]["position"], 4.5)

    def test_german_aliases(self):
        df = pd.DataFrame({
            "Suchanfrage": ["x"], "Seite": ["/x"], "Klicks": ["3"],
            "Impressionen": ["100"], "Position": ["6,0"]})
        out = sdf.clean_gsc(df)
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out.iloc[0]["ctr"], 0.03)

    def test_missing_core_column_raises(self):
        df = pd.DataFrame({"Query": ["x"], "Clicks": ["1"], "Impressions": ["10"]})
        with self.assertRaises(sdf.GscFormatError) as ctx:
            sdf.clean_gsc(df)
        self.assertIn("Position", str(ctx.exception))


class ReadCsvTests(unittest.TestCase):
    def test_semicolon_separated(self):
        text = "Query;Page;Clicks;Impressions;Position\nfoo;/foo;10;200;5,2\n"
        df = sdf.read_gsc_csv(io.BytesIO(text.encode("utf-8")))
        out = sdf.clean_gsc(df)
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out.iloc[0]["position"], 5.2)

    def test_comma_separated(self):
        text = "Query,Page,Clicks,Impressions,Position\nfoo,/foo,10,200,5.2\n"
        df = sdf.read_gsc_csv(io.BytesIO(text.encode("utf-8")))
        out = sdf.clean_gsc(df)
        self.assertEqual(len(out), 1)


class BaselineTests(unittest.TestCase):
    def test_thin_bucket_uses_fallback(self):
        df = pd.DataFrame({
            "query": ["a"], "page": ["/a"], "clicks": [1.0],
            "impressions": [100.0], "position": [7.0], "ctr": [0.01]})
        baseline, fallback = sdf.calculate_baseline(df, min_samples=5)
        self.assertTrue(fallback["6–8"])  # only 1 sample -> fallback
        self.assertEqual(baseline["6–8"], sdf.FALLBACK_CTR["6–8"])

    def test_rich_bucket_uses_own_data(self):
        n = 6
        df = pd.DataFrame({
            "query": [f"q{i}" for i in range(n)],
            "page": [f"/{i}" for i in range(n)],
            "clicks": [3.0] * n, "impressions": [100.0] * n,
            "position": [7.0] * n, "ctr": [0.03] * n})
        baseline, fallback = sdf.calculate_baseline(df, min_samples=5)
        self.assertFalse(fallback["6–8"])
        self.assertAlmostEqual(baseline["6–8"], 0.03)

    def test_brand_excluded_from_baseline(self):
        df = pd.DataFrame({
            "query": ["brandx"] * 6, "page": ["/b"] * 6,
            "clicks": [50.0] * 6, "impressions": [100.0] * 6,
            "position": [7.0] * 6, "ctr": [0.5] * 6})
        baseline, fallback = sdf.calculate_baseline(df, brand_terms=["brandx"],
                                                    min_samples=5)
        # All rows are brand -> excluded -> bucket falls back, not 0.5.
        self.assertTrue(fallback["6–8"])


class StrikingDistanceTests(unittest.TestCase):
    def _df(self):
        rows = []
        # 6 baseline rows at pos 7 with 3% CTR
        for i in range(6):
            rows.append(("base%d" % i, "/base", 3.0, 100.0, 7.0))
        # underperformer: pos 7, big impressions, weak CTR
        rows.append(("weak", "/weak", 10.0, 2000.0, 7.0))
        # out of range (pos 2) and low impressions
        rows.append(("toohigh", "/th", 50.0, 100.0, 2.0))
        rows.append(("lowimpr", "/li", 0.0, 5.0, 8.0))
        df = pd.DataFrame(rows, columns=["query", "page", "clicks",
                                         "impressions", "position"])
        df["ctr"] = df["clicks"] / df["impressions"]
        return df

    def test_filters_and_scores(self):
        cand, baseline, fb = sdf.find_striking_distance(
            self._df(), pos_min=4, pos_max=20, min_impressions=30)
        queries = list(cand["query"])
        self.assertIn("weak", queries)
        self.assertNotIn("toohigh", queries)   # position < pos_min
        self.assertNotIn("lowimpr", queries)   # below min impressions

    def test_weak_is_underperformer_with_upside(self):
        cand, *_ = sdf.find_striking_distance(
            self._df(), pos_min=4, pos_max=20, min_impressions=30)
        weak = cand[cand["query"] == "weak"].iloc[0]
        self.assertTrue(bool(weak["is_underperformer"]))
        self.assertGreater(weak["opportunity_score"], 0)
        self.assertIn("unter deinem Schnitt", weak["reasoning"])

    def test_revenue_column_added(self):
        cand, *_ = sdf.find_striking_distance(
            self._df(), pos_min=4, pos_max=20, min_impressions=30,
            value_per_click=2.0)
        self.assertIn("est_revenue_upside", cand.columns)
        weak = cand[cand["query"] == "weak"].iloc[0]
        self.assertAlmostEqual(weak["est_revenue_upside"],
                               weak["opportunity_score"] * 2.0)

    def test_empty_result_has_columns(self):
        cand, *_ = sdf.find_striking_distance(
            self._df(), pos_min=4, pos_max=20, min_impressions=999999)
        self.assertTrue(cand.empty)
        self.assertIn("reasoning", cand.columns)

    def test_group_by_page(self):
        cand, *_ = sdf.find_striking_distance(
            self._df(), pos_min=4, pos_max=20, min_impressions=30)
        grouped = sdf.group_by_page(cand)
        self.assertIn("n_keywords", grouped.columns)
        self.assertGreaterEqual(grouped["total_upside"].sum(), 0)


class GeminiClientTests(unittest.TestCase):
    def test_no_key(self):
        text, status = sdf._call_gemini("hi", "")
        self.assertIsNone(text)
        self.assertEqual(status, "no_api_key")

    def test_fallback_chain_on_5xx(self):
        good = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
        with patch("striking_distance_finder.urllib.request.urlopen") as m:
            m.side_effect = [http_error(500), FakeResponse(good)]
            text, status = sdf._call_gemini("hi", "key")
        self.assertEqual(status, "ok")
        self.assertEqual(text, "ok")
        self.assertEqual(m.call_count, 2)

    def test_auth_error_aborts(self):
        with patch("striking_distance_finder.urllib.request.urlopen") as m:
            m.side_effect = [http_error(401)]
            text, status = sdf._call_gemini("hi", "key")
        self.assertEqual(status, "http_auth")
        self.assertEqual(m.call_count, 1)  # no fallback attempts

    def test_key_sent_in_header_not_url(self):
        good = {"candidates": [{"content": {"parts": [{"text": "x"}]}}]}
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["header"] = request.get_header("X-goog-api-key")
            captured["url"] = request.full_url
            return FakeResponse(good)

        with patch("striking_distance_finder.urllib.request.urlopen", fake_urlopen):
            sdf._call_gemini("hi", "secret-key")
        self.assertEqual(captured["header"], "secret-key")
        self.assertNotIn("secret-key", captured["url"])


class DetectBrandTermsTests(unittest.TestCase):
    def test_extracts_brand_label_from_domain(self):
        pages = ["https://www.cloudwards.net/best-vpn/",
                 "http://cloudwards.net/reviews", "https://www.cloudwards.net/x"]
        self.assertEqual(sdf.detect_brand_terms(pages), ["cloudwards"])

    def test_multi_part_tld(self):
        self.assertEqual(sdf.detect_brand_terms(["https://www.example.co.uk/a"]),
                         ["example"])

    def test_ignores_rows_without_url(self):
        self.assertEqual(sdf.detect_brand_terms(["(keine URL)", "", None]), [])

    def test_orders_by_frequency(self):
        pages = ["https://alpha.com/a", "https://beta.com/b", "https://beta.com/c"]
        self.assertEqual(sdf.detect_brand_terms(pages)[0], "beta")


class KeywordInTitleTests(unittest.TestCase):
    def test_filler_word_between(self):
        self.assertTrue(sdf.keyword_in_title("iphone test", "iPhone im Test"))

    def test_plural_and_reordered(self):
        self.assertTrue(sdf.keyword_in_title(
            "kaffeemaschine vergleich",
            "Vergleich: die besten Kaffeemaschinen gegenübergestellt"))

    def test_punctuation_and_case(self):
        self.assertTrue(sdf.keyword_in_title("vpn test 2026", "VPN-Test 2026 | Chip"))

    def test_missing_word_is_false(self):
        self.assertFalse(sdf.keyword_in_title("iphone test", "Samsung Galaxy Ratgeber"))

    def test_empty_title_is_false(self):
        self.assertFalse(sdf.keyword_in_title("iphone", ""))
        self.assertFalse(sdf.keyword_in_title("iphone", None))

    def test_umlaut_folding(self):
        self.assertTrue(sdf.keyword_in_title("bücher kaufen", "Guenstig Buecher kaufen"))


class EnforceTitleLengthTests(unittest.TestCase):
    def test_short_is_padded_into_window(self):
        title, ok = sdf.enforce_title_length("iPhone Test")
        self.assertTrue(ok)
        self.assertTrue(sdf.TITLE_MIN <= len(title) <= sdf.TITLE_MAX, len(title))

    def test_long_is_trimmed_into_window(self):
        long = ("Der ultimative riesige und sehr ausführliche Testbericht über "
                "die allerbesten Kaffeevollautomaten des Jahres 2026")
        title, ok = sdf.enforce_title_length(long)
        self.assertTrue(ok)
        self.assertTrue(sdf.TITLE_MIN <= len(title) <= sdf.TITLE_MAX, len(title))

    def test_already_in_window_untouched(self):
        exact = "Kaffeevollautomat Test 2026 - die besten Modelle im" + "x" * 4
        self.assertTrue(sdf.TITLE_MIN <= len(exact) <= sdf.TITLE_MAX)
        title, ok = sdf.enforce_title_length(exact)
        self.assertTrue(ok)
        self.assertEqual(title, exact)

    def test_strips_wrapping_quotes(self):
        title, _ = sdf.enforce_title_length('"iPhone 15 Test"')
        self.assertFalse(title.startswith('"'))


class FetchMetaTitleTests(unittest.TestCase):
    def test_extracts_title(self):
        html_page = (b"<html><head><title>Kaffee &amp; Tee - Test 2026</title>"
                     b"</head><body>x</body></html>")

        class Resp:
            headers = type("H", (), {"get_content_charset": lambda self: "utf-8"})()

            def read(self, n=None):
                return html_page

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with patch("striking_distance_finder.urllib.request.urlopen",
                   return_value=Resp()):
            title, status = sdf.fetch_meta_title("https://example.com")
        self.assertEqual(status, "ok")
        self.assertEqual(title, "Kaffee & Tee - Test 2026")

    def test_no_url(self):
        title, status = sdf.fetch_meta_title("(keine URL)")
        self.assertIsNone(title)
        self.assertEqual(status, "no_url")

    def test_http_error_is_caught(self):
        with patch("striking_distance_finder.urllib.request.urlopen",
                   side_effect=http_error(404)):
            title, status = sdf.fetch_meta_title("https://example.com")
        self.assertIsNone(title)
        self.assertEqual(status, "http_404")


class GeminiMetaTitleTests(unittest.TestCase):
    def test_no_key(self):
        title, status = sdf.gemini_meta_title("iphone test", api_key="")
        self.assertEqual(status, "no_api_key")

    def test_no_keywords(self):
        title, status = sdf.gemini_meta_title("   ", api_key="key")
        self.assertEqual(status, "no_keywords")

    def test_output_is_length_enforced(self):
        payload = {"candidates": [{"content": {"parts": [
            {"text": "iPhone 15 Test"}]}}]}  # too short -> must be padded
        with patch("striking_distance_finder.urllib.request.urlopen",
                   return_value=FakeResponse(payload)):
            title, status = sdf.gemini_meta_title("iphone test", api_key="key")
        self.assertEqual(status, "ok")
        self.assertTrue(sdf.TITLE_MIN <= len(title) <= sdf.TITLE_MAX, len(title))

    def test_accepts_keyword_list(self):
        payload = {"candidates": [{"content": {"parts": [
            {"text": "VPN Test & Streaming Vergleich 2026 - die besten Anbieter"}]}}]}
        with patch("striking_distance_finder.urllib.request.urlopen",
                   return_value=FakeResponse(payload)):
            title, status = sdf.gemini_meta_title(["vpn test", "streaming vpn"],
                                                  api_key="key")
        self.assertIn(status, ("ok", "length_warn"))
        self.assertTrue(sdf.TITLE_MIN <= len(title) <= sdf.TITLE_MAX, len(title))


if __name__ == "__main__":
    unittest.main(verbosity=2)
