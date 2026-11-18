"""Overnight replay (buy today, sell tomorrow): in at one bar's close, out at the next
session's open or a bar's close, every pair of back-to-back sessions. Offline."""

import datetime
import json
import unittest

from _load import load

TM = load("trader-timemachine", "timemachine")
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def chart(n_days=25, sym="^NSEI"):
    """n_days NSE sessions (9:15 to 15:15, 72 bars). Day i drifts from 100*(i+1) to +10 by
    15:15 (an up day), then gaps: even nights +6 at the open, odd nights -2; the next 9:25
    bar closes 2 above that open."""
    ts, o, h, l, c = [], [], [], [], []
    day = datetime.date(2026, 9, 1)
    prev_close = None
    for i in range(n_days):
        while day.weekday() >= 5:
            day += datetime.timedelta(days=1)
        base = prev_close + (6 if (i - 1) % 2 == 0 else -2) if prev_close is not None else 1000.0
        for k in range(72):
            t = datetime.datetime.combine(day, datetime.time(9, 15), IST) + datetime.timedelta(minutes=5 * k)
            op = base if k == 0 else c[-1]
            cl = base + 2 if k in (0, 1, 2) else base + 10 * (k + 1) / 72.0
            ts.append(int(t.timestamp()))
            o.append(op)
            c.append(cl)
            h.append(max(op, cl) + 0.5)
            l.append(min(op, cl) - 0.5)
        prev_close = c[-1]
        day += datetime.timedelta(days=1)
    res = {"meta": {"symbol": sym, "dataGranularity": "5m"}, "timestamp": ts,
           "indicators": {"quote": [{"open": o, "high": h, "low": l, "close": c, "volume": [0] * len(ts)}]}}
    return json.dumps({"chart": {"result": [res], "error": None}})


NOW = datetime.datetime(2026, 12, 31, 12, 0)


class Overnight(unittest.TestCase):
    def setUp(self):
        TM.market.apply(vars(TM), "IN")

    def tearDown(self):
        TM.market.apply(vars(TM), "US")

    def test_every_night_is_replayed(self):
        o = TM.overnight(chart(), "^NSEI", NOW, "15:15", "09:30")
        self.assertEqual(len(o["nights"]), 24)
        n0 = o["nights"][0]
        self.assertAlmostEqual(n0["entry"], 1010.0)
        self.assertAlmostEqual(n0["gap"], 6.0)             # night 0 is "even": +6 at the open
        self.assertAlmostEqual(n0["move"], 8.0)            # the 9:25 bar closes 2 above that open
        buy = o["rules"]["buy"]
        self.assertAlmostEqual(buy["mean"], (12 * 8 + 12 * 0) / 24.0)
        self.assertAlmostEqual(buy["win"], 0.5)
        self.assertAlmostEqual(o["rules"]["sell"]["mean"], -buy["mean"])
        # every day here closes up, so "follow" buys every night and "fade" sells
        self.assertAlmostEqual(o["rules"]["follow"]["mean"], buy["mean"])
        self.assertEqual(o["lot"], 65)

    def test_exit_at_the_open_is_the_gap(self):
        o = TM.overnight(chart(), "^NSEI", NOW, "15:15", "open")
        self.assertTrue(all(abs(n["move"] - n["gap"]) < 1e-9 for n in o["nights"]))
        self.assertAlmostEqual(o["gap_max"], 6.0)

    def test_times_must_be_bar_closes_in_the_session(self):
        with self.assertRaises(TM.TMError):
            TM.overnight(chart(), "^NSEI", NOW, "15:17", "09:30")
        with self.assertRaises(TM.TMError):
            TM.overnight(chart(), "^NSEI", NOW, "15:15", "16:00")

    def test_too_few_nights(self):
        with self.assertRaises(TM.TMError) as e:
            TM.overnight(chart(10), "^NSEI", NOW)
        self.assertIn("only 9 full nights", str(e.exception))

    def test_the_report_says_no_edge_and_the_gap(self):
        o = TM.overnight(chart(), "^BSESN", NOW, "15:15", "09:30")
        text = "\n".join(TM.overnight_report(o))
        self.assertIn("24 nights", text)
        self.assertIn("Opening gaps", text)
        self.assertIn("one lot (20)", text)
        self.assertIn("outside this pack's day-trade rules", text)


if __name__ == "__main__":
    unittest.main()
