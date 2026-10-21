"""Tests for the generic parts of revenue_sources.py: the source chain, obrti handling,
DiskCache, to_eur, FinaCsvSource and WebSearchSource. DirectoryPageSource and
parse_financials are not covered here (scraper code)."""
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import revenue_sources as rs


class StubSource(rs.RevenueSource):
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def lookup(self, oib, name, town=None):
        self.calls += 1
        return self.result


class CountingClient:
    """Stands in for the web-search client; counts paid calls."""

    def __init__(self, response):
        self.response = response
        self.calls = 0

    def search_and_extract(self, query):
        self.calls += 1
        return self.response


class TempCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cache.json"
        self.cache = rs.DiskCache(str(self.path))

    def tearDown(self):
        self.tmp.cleanup()


class TestPipeline(TempCacheTest):
    def pipeline_with(self, *sources):
        p = rs.RevenuePipeline(self.cache)
        p.sources = list(sources)
        return p

    def test_first_hit_wins_and_later_sources_are_skipped(self):
        first, second = StubSource({"revenue": 1}), StubSource({"revenue": 2})
        result = self.pipeline_with(first, second).find_revenue(rs.Company("A d.o.o.", "11111111111", "Osijek"))
        self.assertEqual(result["revenue"], 1)
        self.assertEqual(second.calls, 0)

    def test_falls_through_on_none(self):
        result = self.pipeline_with(StubSource(None), StubSource({"revenue": 2})).find_revenue(
            rs.Company("A d.o.o.", "11111111111"))
        self.assertEqual(result["revenue"], 2)

    def test_obrt_without_oib_calls_no_source(self):
        src = StubSource({"revenue": 1})
        self.assertIsNone(self.pipeline_with(src).find_revenue(rs.Company("Obrt", None)))
        self.assertIsNone(self.pipeline_with(src).find_revenue(rs.Company("Obrt", "")))
        self.assertEqual(src.calls, 0)

    def test_town_is_optional(self):
        self.assertIsNone(rs.Company("A").town)


class TestKeysMatchAgent(TempCacheTest):
    AGENT_KEYS = {"revenue", "revenue_year", "employees", "revenue_source", "revenue_url"}

    def test_fina_source_keys(self):
        src = rs.FinaCsvSource({"41749168764": {"revenue": 2_890_000, "year": 2025, "employees": 22}})
        result = src.lookup("41749168764", "FENESTRA d.o.o.")
        self.assertEqual(set(result), self.AGENT_KEYS)
        self.assertEqual(result["revenue_year"], 2025)
        self.assertIsNone(src.lookup("00000000000", "x"))

    def test_web_source_keys(self):
        client = CountingClient({"oib_matches": True, "revenue": 1, "year": 2024, "employees": 3, "source_url": "u"})
        result = rs.WebSearchSource(self.cache, client).lookup("11111111111", "A")
        self.assertEqual(set(result), self.AGENT_KEYS)
        self.assertEqual(result["revenue_source"], "procjena (web)")

    def test_web_source_rejects_result_for_other_company(self):
        client = CountingClient({"oib_matches": False, "revenue": 1})
        self.assertIsNone(rs.WebSearchSource(self.cache, client).lookup("11111111111", "A"))


class TestDiskCache(TempCacheTest):
    def test_round_trip_survives_restart(self):
        self.cache.put("k", {"v": 1})
        self.assertEqual(rs.DiskCache(str(self.path)).get("k"), {"v": 1})

    def test_expiry(self):
        self.cache.put("k", {"v": 1})
        old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        self.cache._store["k"]["cached_at"] = old
        self.assertIs(self.cache.get("k", max_age_days=180), rs._NOT_SET)
        self.assertFalse(self.cache.has("k"))
        self.assertNotIn("k", json.loads(self.path.read_text()))

    def test_corrupt_file_starts_empty(self):
        self.path.write_text("{not json")
        self.assertIs(rs.DiskCache(str(self.path)).get("k"), rs._NOT_SET)

    def test_cached_miss_is_distinguishable_from_not_cached(self):
        self.cache.put("k", None)
        self.assertTrue(self.cache.has("k"))
        self.assertIsNone(self.cache.get("k"))
        self.assertFalse(self.cache.has("never"))
        self.assertIs(self.cache.get("never"), rs._NOT_SET)

    def test_cached_miss_survives_restart(self):
        self.cache.put("k", None)
        reloaded = rs.DiskCache(str(self.path))
        self.assertTrue(reloaded.has("k"))
        self.assertIsNone(reloaded.get("k"))

    def test_concurrent_writes_from_threads(self):
        errors = []

        def write(i):
            try:
                for j in range(50):
                    self.cache.put(f"k{i}_{j}", {"v": j})
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=write, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        on_disk = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.assertEqual((errors, len(on_disk)), ([], 300))


class TestWebSearchCost(TempCacheTest):
    def test_hit_is_not_paid_twice(self):
        client = CountingClient({"oib_matches": True, "revenue": 1, "year": 2024, "employees": 3, "source_url": "u"})
        src = rs.WebSearchSource(self.cache, client)
        src.lookup("11111111111", "A")
        src.lookup("11111111111", "A")
        self.assertEqual(client.calls, 1)

    def test_miss_is_not_paid_twice(self):
        client = CountingClient(None)
        src = rs.WebSearchSource(self.cache, client)
        src.lookup("11111111111", "A")
        src.lookup("11111111111", "A")
        self.assertEqual(client.calls, 1)

    def test_run_without_client_caches_nothing(self):
        self.assertIsNone(rs.WebSearchSource(self.cache, None).lookup("11111111111", "A"))
        self.assertFalse(self.cache.has("web_revenue_11111111111"))

    def test_expired_miss_is_searched_again(self):
        client = CountingClient(None)
        src = rs.WebSearchSource(self.cache, client)
        src.lookup("11111111111", "A")
        self.cache._store["web_revenue_11111111111"]["cached_at"] = (
            datetime.now(timezone.utc) - timedelta(days=181)).isoformat()
        src.lookup("11111111111", "A")
        self.assertEqual(client.calls, 2)

    def test_pipeline_stops_on_cached_web_miss_without_new_search(self):
        client = CountingClient(None)
        pipe = rs.RevenuePipeline(self.cache, web_api_client=client)
        pipe.sources = [s for s in pipe.sources if not isinstance(s, rs.DirectoryPageSource)]
        company = rs.Company("A d.o.o.", "11111111111")
        self.assertIsNone(pipe.find_revenue(company))
        self.assertIsNone(pipe.find_revenue(company))
        self.assertEqual(client.calls, 1)

    def test_run_without_client_does_not_block_later_runs(self):
        rs.WebSearchSource(self.cache, None).lookup("11111111111", "A")
        client = CountingClient({"oib_matches": True, "revenue": 1, "year": 2024, "employees": 3, "source_url": "u"})
        self.assertIsNotNone(rs.WebSearchSource(self.cache, client).lookup("11111111111", "A"))


class TestToEur(unittest.TestCase):
    def test_croatian_formats(self):
        self.assertAlmostEqual(rs.to_eur("2,89", "mil."), 2_890_000)
        self.assertAlmostEqual(rs.to_eur("455.630"), 455_630)
        self.assertAlmostEqual(rs.to_eur("2.890.000,00"), 2_890_000)
        self.assertAlmostEqual(rs.to_eur("1,2", "mlrd."), 1_200_000_000)
        self.assertAlmostEqual(rs.to_eur("850", "tis."), 850_000)
        self.assertAlmostEqual(rs.to_eur("850", "EUR"), 850)

    def test_unit_without_dot_now_works(self):
        self.assertAlmostEqual(rs.to_eur("2,89", "mil"), 2_890_000)
        self.assertAlmostEqual(rs.to_eur("2,89", " MIL. "), 2_890_000)

    def test_unknown_unit_raises(self):
        with self.assertRaises(ValueError):
            rs.to_eur("2,89", "kn")

    def test_bad_number_raises(self):
        with self.assertRaises(ValueError):
            rs.to_eur("n/a")


if __name__ == "__main__":
    unittest.main(verbosity=2)
