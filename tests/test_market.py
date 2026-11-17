"""The market profiles: US stays as it was, IN (NSE) swaps the clock, calendar,
defaults, tickers and labels. Offline."""

import datetime
import filecmp
import io
import os
import unittest

from _load import ROOT, load

CLOCK_SKILLS = [("trader-scanner", "scan"), ("trader-entry", "entry"), ("trader-exit", "exit"),
                ("trader-timemachine", "timemachine"), ("trader-risk", "check"), ("trader-alerts", "alerts")]
MODS = {name: load(folder, name) for folder, name in CLOCK_SKILLS}
market = MODS["scan"].market


def use(mod, code):
    mod.market.apply(vars(mod), code)


class Copies(unittest.TestCase):
    def test_every_clock_skill_carries_the_same_market_py(self):
        first = os.path.join(ROOT, CLOCK_SKILLS[0][0], "market.py")
        for folder, _ in CLOCK_SKILLS[1:]:
            self.assertTrue(filecmp.cmp(first, os.path.join(ROOT, folder, "market.py"), shallow=False), folder)


class Choosing(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.pop("TRADER_MARKET", None)

    def tearDown(self):
        os.environ.pop("TRADER_MARKET", None)
        if self._env is not None:
            os.environ["TRADER_MARKET"] = self._env

    def test_flag_is_taken_out_of_argv(self):
        self.assertEqual(market.pop_market_flag(["orb", "--market", "IN", "--at", "10:00"]),
                         ("IN", ["orb", "--at", "10:00"]))
        self.assertEqual(market.pop_market_flag(["daily", "--market=in"]), ("in", ["daily"]))
        self.assertEqual(market.pop_market_flag(["daily"]), (None, ["daily"]))

    def test_flag_then_env_then_config_then_us(self):
        missing = os.path.join(ROOT, "no-such-config.json")
        self.assertEqual(market.resolve(None, missing), "US")
        os.environ["TRADER_MARKET"] = "india"
        self.assertEqual(market.resolve(None, missing), "IN")
        self.assertEqual(market.resolve("US", missing), "US")
        with self.assertRaises(market.MarketError):
            market.resolve("JP", missing)


class India(unittest.TestCase):
    def tearDown(self):
        for mod in MODS.values():
            use(mod, "US")

    def test_ist_is_utc_plus_five_thirty_all_year(self):
        scan = MODS["scan"]
        use(scan, "IN")
        self.assertEqual(scan.parse_now("2026-11-17 09:15"), datetime.datetime(2026, 11, 17, 3, 45))
        self.assertEqual(scan.parse_now("2026-06-17 09:15"), datetime.datetime(2026, 6, 17, 3, 45))
        utc = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=1789000000)
        self.assertEqual(scan.ts_to_eastern(1789000000), utc + datetime.timedelta(hours=5, minutes=30))

    def test_nse_session_and_holidays(self):
        for name in ("scan", "entry", "alerts", "check"):
            mod = MODS[name]
            use(mod, "IN")
            self.assertEqual(mod.OPEN, datetime.time(9, 15), name)
            self.assertEqual(mod.CLOSE, datetime.time(15, 30), name)
            ok, why = mod.trading_day(datetime.date(2026, 11, 24))
            self.assertFalse(ok, name)
            self.assertIn("Guru Nanak", why)
            self.assertTrue(mod.trading_day(datetime.date(2026, 11, 26))[0], name)  # US Thanksgiving
            self.assertTrue(mod.trading_day(datetime.date(2026, 11, 17))[0], name)

    def test_back_to_us_restores_everything(self):
        scan = MODS["scan"]
        use(scan, "IN")
        use(scan, "US")
        self.assertEqual(scan.OPEN, datetime.time(9, 30))
        self.assertFalse(scan.trading_day(datetime.date(2026, 11, 26))[0])
        self.assertEqual(scan.to_yahoo("BRK.B"), "BRK-B")
        self.assertEqual(scan.parse_now("2026-11-17 09:30"), datetime.datetime(2026, 11, 17, 14, 30))
        self.assertNotIsInstance(__import__("sys").stdout, market._Relabel)

    def test_time_machine_replays_to_15_15(self):
        tm = MODS["timemachine"]
        use(tm, "IN")
        self.assertEqual((tm.SESSION_BARS, tm.OPEN_MIN, tm.CLOSE_MIN), (72, 555, 915))
        use(tm, "US")
        self.assertEqual(tm.SESSION_BARS, 78)

    def test_day_trade_defaults(self):
        use(MODS["entry"], "IN")
        self.assertEqual(MODS["entry"].DEFAULTS["no_entries_after"], "14:45")
        use(MODS["check"], "IN")
        self.assertEqual(MODS["check"].DEFAULTS["flat_by"], "15:10")
        use(MODS["exit"], "IN")
        self.assertEqual(MODS["exit"].DEFAULTS["flat_by"], "15:10")
        use(MODS["alerts"], "IN")
        self.assertEqual(MODS["alerts"].FLAT_BY, "15:10")
        use(MODS["scan"], "IN")
        self.assertEqual((MODS["scan"].MIN_PRICE, MODS["scan"].MIN_VOLUME), (50.0, 100000))

    def test_tickers(self):
        scan, entry = MODS["scan"], MODS["entry"]
        use(scan, "IN")
        use(entry, "IN")
        self.assertEqual(scan.to_yahoo("reliance"), "RELIANCE.NS")
        self.assertEqual(scan.to_yahoo("TCS.BO"), "TCS.BO")
        self.assertEqual(scan.to_yahoo("^NSEI"), "^NSEI")
        self.assertIn("M%26M.NS", scan.spark_path(["M&M", "INFY"], "1d", "5m"))
        self.assertIn("/M%26M.NS?", entry.chart_path("M&M", "1d", "5m"))
        use(scan, "US")
        self.assertIn("BRK-B,TSLA", scan.spark_path(["BRK.B", "TSLA"], "1d", "5m"))

    def test_orb_window_uses_the_nse_open(self):
        scan = MODS["scan"]
        use(scan, "IN")
        try:
            with self.assertRaises(scan.ScanError) as cm:
                scan.orb_window(scan.parse_now("2026-11-17 09:32"), None, 15, None)
            self.assertIn("runs 9:15 to 9:30 AM", str(cm.exception))
            day, cutoff = scan.orb_window(scan.parse_now("2026-11-17 10:00"), None, 15, None)
            self.assertEqual(cutoff, datetime.datetime(2026, 11, 17, 10, 0))
        finally:
            use(scan, "US")

    def test_labels_on_screen(self):
        self.assertEqual(market.relabel_text("10:15 AM ET, the NYSE (New York): $364.18"),
                         "10:15 AM IST, the NSE (India): ₹364.18")
        self.assertEqual(market.relabel_text("GET SET"), "GET SET")
        buf = io.StringIO()
        out = market._Relabel(buf)
        out.write("$5 ET")
        self.assertEqual(buf.getvalue(), "₹5 IST")

    def test_nse_equity_list_keeps_eq_only(self):
        text = ("SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE\n"
                "RELIANCE,Reliance Industries Limited,EQ,29-NOV-1995,10\n"
                "M&M,Mahindra & Mahindra Limited,EQ,29-NOV-1995,5\n"
                "XYZ,Some Trade For Trade Ltd,BE,01-JAN-2020,10\n"
                "RELIANCE,Reliance Industries Limited,EQ,29-NOV-1995,10\n")
        kept, counts = MODS["scan"].parse_nse_csv(text)
        self.assertEqual(kept, [("RELIANCE", "Reliance Industries Limited"), ("M&M", "Mahindra & Mahindra Limited")])
        self.assertEqual(counts["series BE"], 1)

    def test_nifty_constituent_list(self):
        text = ("Company Name,Industry,Symbol,Series,ISIN Code\n"
                "HDFC Bank Ltd.,Financial Services,HDFCBANK,EQ,INE040A01034\n")
        kept, _ = MODS["scan"].parse_nse_csv(text)
        self.assertEqual(kept, [("HDFCBANK", "HDFC Bank Ltd.")])

    def test_stooq_is_us_only(self):
        alerts = MODS["alerts"]
        use(alerts, "IN")
        with self.assertRaises(alerts.AlertError):
            alerts.stooq_url("RELIANCE")


if __name__ == "__main__":
    unittest.main()
