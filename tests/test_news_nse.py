"""trader-news for India: NSE's corporate announcements. Offline.

Fixtures in tests/fixtures/trader-news/nse/: the DTIL, GABRIEL and NIFTYIETF rows are cut
from NSE's announcements RSS of 2026-11-20 and its equity list; the UGROCAP row and the PAYTM
JSON are MADE UP in the same shapes, to test matching. They are not real announcements."""

import contextlib
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest

from _load import ROOT, load

NEWS = load("trader-news", "news")
NSE = os.path.join(ROOT, "tests", "fixtures", "trader-news", "nse")


def read(name):
    with open(os.path.join(NSE, name), "rb") as fh:
        return fh.read()


NAMES = NEWS.parse_nse_list(read("nse-EQUITY_L.csv"))


class Parsing(unittest.TestCase):
    def test_equity_list(self):
        self.assertEqual(NAMES["UGROCAP"], "Ugro Capital Limited")
        self.assertEqual(NAMES["DTIL"], "Dhunseri Tea & Industries Limited")
        self.assertNotIn("SYMBOL", NAMES)

    def test_ist_times(self):
        t = NEWS.nse_time("20-Nov-2026 11:54:31")
        self.assertEqual(t, datetime.datetime(2026, 11, 20, 6, 24, 31, tzinfo=datetime.timezone.utc))
        self.assertEqual(NEWS.nse_time("2026-11-20 11:54:31"), t)

    def test_symbols_and_yahoo(self):
        self.assertTrue(NEWS.is_indian("ugrocap.ns"))
        self.assertEqual(NEWS.nse_symbol("UGROCAP.NS"), "UGROCAP")
        self.assertEqual(NEWS.yahoo_symbol("UGROCAP.NS"), "UGROCAP.NS")      # was UGROCAP-NS: a 404
        self.assertEqual(NEWS.yahoo_symbol("M&M.NS"), "M&M.NS")
        self.assertEqual(NEWS.yahoo_symbol("BRK.B"), "BRK-B")

    def test_rss_matches_by_name_file_or_symbol(self):
        items = NEWS.parse_nse_rss(read("nse-announcements.xml"),
                                   ["DTIL.NS", "UGROCAP.NS", "NIFTYIETF.NS", "GABRIEL.NS", "PAYTM.NS"], NAMES)
        by = {i["ticker"]: i for i in items}
        self.assertEqual(sorted(by), ["DTIL.NS", "GABRIEL.NS", "NIFTYIETF.NS", "UGROCAP.NS"])
        self.assertEqual(by["DTIL.NS"]["title"],
                         "Dhunseri Tea & Industries Limited: Outcome of Board Meeting held on November 20, 2026")
        self.assertTrue(by["DTIL.NS"]["important"])
        self.assertTrue(by["UGROCAP.NS"]["important"])                    # a preferential issue
        self.assertTrue(by["GABRIEL.NS"]["important"])                    # a director change
        self.assertFalse(by["NIFTYIETF.NS"]["important"])                 # an ETF's NAV
        self.assertTrue(by["UGROCAP.NS"]["link"].endswith("Preferential_Allotment.pdf"))
        self.assertEqual(by["UGROCAP.NS"]["source"], "NSE filing")

    def test_company_json(self):
        items = NEWS.parse_nse_json(read("nse-PAYTM.json"), "PAYTM.NS")
        self.assertEqual([i["title"] for i in items],
                         ["One 97 Communications Limited: Credit Rating",
                          "One 97 Communications Limited: Investor Presentation"])
        self.assertTrue(items[0]["important"])
        self.assertFalse(items[1]["important"])
        self.assertEqual(NEWS.parse_nse_json(read("nse-PAYTM.json"), "UGROCAP.NS"), [])
        with self.assertRaises(NEWS.NewsError):
            NEWS.parse_nse_json(b"<html>Access Denied</html>", "PAYTM.NS")

    def test_tags(self):
        items = NEWS.tag(NEWS.parse_nse_rss(read("nse-announcements.xml"), ["UGROCAP.NS", "DTIL.NS"], NAMES),
                         {"UGROCAP.NS": None, "DTIL.NS": None})
        tags = {i["ticker"]: i["tags"] for i in items}
        self.assertIn("capital", tags["UGROCAP.NS"])


class Command(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        self.off = os.path.join(self.tmp, "off")
        shutil.copytree(NSE, self.off)
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_news(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = NEWS.main(list(argv))
        return code, out.getvalue()

    def test_bare_symbols_with_market_in(self):
        code, out = self.run_news("--tickers", "UGROCAP", "DTIL", "--market", "IN", "--offline", self.off,
                                  "--now", "2026-11-20T06:45:00Z", "--hours", "24", "--no-moves", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("! 2026-11-20 11:54 IST  DTIL.NS", out)
        self.assertIn("UGROCAP.NS", out)
        self.assertIn("Ugro Capital Limited: Fund raising by way of Preferential Issue", out)
        self.assertNotIn("SEC filings skipped", out)                   # no SEC for Indian tickers
        self.assertIn("Checked 2026-11-20 12:15 IST", out)

    def test_saved_company_file(self):
        code, out = self.run_news("--tickers", "PAYTM.NS", "--offline", self.off, "--nse-file",
                                  os.path.join(self.off, "nse-PAYTM.json"), "--now", "2026-11-20T06:45:00Z",
                                  "--hours", "72", "--no-moves", "--dry-run", "--json")
        doc = json.loads(out)
        self.assertEqual([i["title"] for i in doc["items"]], ["One 97 Communications Limited: Credit Rating"])


if __name__ == "__main__":
    unittest.main()
