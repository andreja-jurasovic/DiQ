import unittest
from unittest import mock

from prospector import revenue


class Stub(revenue.RevenueSource):
    def __init__(self, result, paid=False):
        self.result, self.paid, self.calls = result, paid, 0

    def lookup(self, oib, name, town=None):
        self.calls += 1
        return self.result


class TestRevenueChain(unittest.TestCase):
    def run_with(self, sources, **kw):
        with mock.patch.object(revenue, "SOURCES", sources):
            return revenue.find("11111111111", "A d.o.o.", "Osijek", **kw)

    def test_first_hit_wins(self):
        a, b = Stub({"revenue": 1}), Stub({"revenue": 2})
        self.assertEqual(self.run_with([a, b])["revenue"], 1)
        self.assertEqual(b.calls, 0)

    def test_paid_sources_skipped_when_not_allowed(self):
        paid = Stub({"revenue": 2}, paid=True)
        self.assertIsNone(self.run_with([Stub(None), paid], allow_paid=False))
        self.assertEqual(paid.calls, 0)

    def test_obrt_without_oib_asks_nobody(self):
        s = Stub({"revenue": 1})
        with mock.patch.object(revenue, "SOURCES", [s]):
            self.assertIsNone(revenue.find(None, "Obrt"))
        self.assertEqual(s.calls, 0)

    def test_fina_source_fields(self):
        with mock.patch.object(revenue.fina, "latest", return_value={"revenue": 5.0, "year": 2025, "employees": 3}):
            r = revenue.FinaSource().lookup("11111111111", "A")
        self.assertEqual(r, {"revenue": 5.0, "revenue_year": 2025, "employees": 3, "revenue_source": "FINA", "revenue_url": None})

    def test_web_source_fields(self):
        est = {"revenue_eur": 9.0, "year": 2024, "employees": 2, "source_url": "u"}
        with mock.patch.object(revenue.web_revenue, "lookup", return_value=est):
            r = revenue.WebSearchSource().lookup("11111111111", "A")
        self.assertEqual(r["revenue_source"], "procjena (web)")
        self.assertEqual(r["revenue_url"], "u")

    def test_default_chain_tries_companywall_before_web_estimates(self):
        self.assertEqual(
            [type(s).__name__ for s in revenue.SOURCES],
            ["FinaSource", "ScraperSource", "WebSearchSource"],
        )


if __name__ == "__main__":
    unittest.main()
