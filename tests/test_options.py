"""NSE options: an index signal (breakout -> call, breakdown -> put) turned into one
option, sized in whole lots, checked, and shown as Zerodha tickets. Offline: the
option list rows have the shape of Zerodha's NFO instruments CSV (checked against
its option chain on 2026-11-17: name NIFTY, lot 65, tick 0.05)."""

import contextlib
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest

from _load import load

EXIT = load("trader-exit", "exit")
SIZE = load("trader-budget", "size")
CHECK = load("trader-risk", "check")
TICKET = load("trader-broker", "ticket")
import kite  # noqa: E402  (trader-exit put its folder on sys.path)

HEAD = ("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,"
        "instrument_type,segment,exchange\n")


def nfo_rows():
    rows = []
    tok = 11000000
    for exp, code in (("2026-11-17", "26N17"), ("2026-11-23", "26N23"), ("2026-12-01", "26D01")):
        for k in range(25100, 25501, 50):
            for t in ("CE", "PE"):
                tok += 1
                rows.append("%d,1,NIFTY%s%d%s,NIFTY,0,%s,%d,0.05,65,%s,NFO-OPT,NFO"
                            % (tok, code, k, t, exp, k, t))
    rows.append("99,1,BANKNIFTY26N2557000CE,BANKNIFTY,0,2026-11-23,57000,0.05,30,CE,NFO-OPT,NFO")
    rows.append("98,1,NIFTY26NOVFUT,NIFTY,0,2026-11-24,0,0.1,65,FUT,NFO-FUT,NFO")
    return HEAD + "\n".join(rows) + "\n"


CHAIN = kite.parse_options(nfo_rows(), "NIFTY")
CFG = {"market": "IN", "broker": "zerodha", "paper": False, "kite_product": "NRML", "budget": 19000,
       "max_loss_per_trade_usd": 1000, "max_daily_loss_usd": 3000, "max_order_usd": 15000,
       "allow_options": True, "max_lots": 2}


def ist(y, m, d, hh, mm):
    return datetime.datetime(y, m, d, hh, mm)


class Chain(unittest.TestCase):
    def test_parse_keeps_one_underlyings_options(self):
        self.assertEqual(len(CHAIN), 3 * 9 * 2)
        self.assertTrue(all(r["name"] == "NIFTY" and r["lot_size"] == 65 and r["tick_size"] == 0.05 for r in CHAIN))
        self.assertNotIn("NIFTY26NOVFUT", [r["tradingsymbol"] for r in CHAIN])

    def test_option_root(self):
        self.assertEqual(kite.option_root("^NSEI"), "NIFTY")
        self.assertEqual(kite.option_root("^NSEBANK"), "BANKNIFTY")
        self.assertEqual(kite.option_root("RELIANCE.NS"), "RELIANCE")
        with self.assertRaises(kite.KiteError):
            kite.option_root("^GSPC")

    def test_expiry_same_day_until_1330_then_next(self):
        self.assertEqual(kite.pick_expiry(CHAIN, ist(2026, 11, 17, 10, 0))[0], "2026-11-17")
        self.assertEqual(kite.pick_expiry(CHAIN, ist(2026, 11, 17, 13, 29))[0], "2026-11-17")
        exp, why = kite.pick_expiry(CHAIN, ist(2026, 11, 17, 13, 30))
        self.assertEqual(exp, "2026-11-23")
        self.assertIn("past 13:30", why)
        self.assertEqual(kite.pick_expiry(CHAIN, ist(2026, 11, 18, 9, 30))[0], "2026-11-23")

    def test_atm_strike_and_one_step_away(self):
        self.assertEqual(kite.pick_strike(CHAIN, "2026-11-23", "CE", 25262)["strike"], 25250)
        self.assertEqual(kite.pick_strike(CHAIN, "2026-11-23", "PE", 25290)["strike"], 25300)
        # a tie goes in the money: a call's strike under the index, a put's over it
        self.assertEqual(kite.pick_strike(CHAIN, "2026-11-23", "CE", 25275)["strike"], 25250)
        self.assertEqual(kite.pick_strike(CHAIN, "2026-11-23", "PE", 25275)["strike"], 25300)
        self.assertEqual(kite.pick_strike(CHAIN, "2026-11-23", "CE", 25262, "otm1")["strike"], 25300)
        self.assertEqual(kite.pick_strike(CHAIN, "2026-11-23", "PE", 25262, "otm1")["strike"], 25200)
        self.assertEqual(kite.pick_strike(CHAIN, "2026-11-23", "CE", 25262, "itm1")["strike"], 25200)
        row = kite.pick_strike(CHAIN, "2026-11-23", "PE", 25262)
        self.assertEqual(row["tradingsymbol"], "NIFTY26N2325250PE")


class Workdir(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)
        os.makedirs("trader")
        with open("trader/config.json", "w") as fh:
            json.dump(CFG, fh)
        for mod in (EXIT, TICKET, CHECK):
            mod.market.apply(vars(mod), "IN")

    def tearDown(self):
        for mod in (EXIT, TICKET, CHECK):
            mod.market.apply(vars(mod), "US")
        os.chdir(self.old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_tool(self, mod, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = mod.main(argv)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def index_plan(self, side="sell"):
        argv = ["plan", "--symbol", "^NSEI", "--market", "IN", "--date", "2026-11-18", "--time", "10:00 AM",
                "--now", "2026-11-18 10:00", "--write"]
        if side == "sell":
            argv += ["--entry", "25260", "--or-high", "25357.85", "--or-low", "25269.20", "--side", "sell"]
        else:
            argv += ["--entry", "25360", "--or-high", "25357.85", "--or-low", "25269.20"]
        code, out, err = self.run_tool(EXIT, argv)
        self.assertEqual(code, 0, err)

    def option(self, *extra, contract="NIFTY26N2325250PE", strike="25250"):
        return self.run_tool(EXIT, ["option", "--market", "IN", "--now", "2026-11-18 10:05", "--contract", contract,
                                    "--expiry", "2026-11-23", "--strike-price", strike, "--lot-size", "65",
                                    "--premium", "112.35"] + list(extra))


class OptionPlan(Workdir):
    def test_breakdown_becomes_a_put_with_tick_rounded_levels(self):
        self.index_plan("sell")
        code, out, err = self.option("--stop-pct", "13", "--write")
        self.assertEqual(code, 0, err)
        with open("trader/option_plan.json") as fh:
            o = json.load(fh)
        self.assertEqual((o["option_type"], o["direction"], o["side"]), ("PE", "down", "buy"))
        self.assertEqual((o["entry"], o["stop"], o["target"]), (112.35, 97.7, 141.65))
        self.assertEqual((o["risk_per_lot"], o["premium_per_lot"]), (952.25, 7302.75))
        self.assertEqual(o["index"]["stop"], 25313.53)
        self.assertFalse(o["expiry_day"])
        self.assertIn("a put: NIFTY broke down", out)

    def test_the_wrong_kind_of_option_is_refused(self):
        self.index_plan("sell")
        code, _, err = self.option(contract="NIFTY26N2325250CE")
        self.assertEqual(code, 1)
        self.assertIn("put (PE)", err)

    def test_breakout_picks_a_call_from_the_option_list(self):
        self.index_plan("buy")
        kite._CHAINS[("NFO", "NIFTY")] = CHAIN
        orig = kite.ltp
        kite.ltp = lambda instruments: {i: 98.4 for i in instruments}
        try:
            code, out, err = self.run_tool(EXIT, ["option", "--market", "IN", "--now", "2026-11-18 10:05", "--json"])
        finally:
            kite.ltp = orig
            kite._CHAINS.clear()
        self.assertEqual(code, 0, err)
        o = json.loads(out)
        self.assertEqual((o["symbol"], o["option_type"], o["strike"], o["expiry"]),
                         ("NIFTY26N2325350CE", "CE", 25350.0, "2026-11-23"))
        self.assertEqual((o["entry"], o["stop"]), (98.4, 68.85))

    def test_expiry_day_flat_by_three(self):
        self.index_plan("sell")
        code, out, err = self.run_tool(EXIT, ["option", "--market", "IN", "--now", "2026-11-18 10:05",
                                              "--contract", "NIFTY26N1825250PE", "--expiry", "2026-11-18",
                                              "--strike-price", "25250", "--lot-size", "65", "--premium", "40",
                                              "--json"])
        o = json.loads(out)
        self.assertTrue(o["expiry_day"])
        self.assertEqual(o["flat_by"], "15:00")
        self.assertTrue(any("Expires TODAY" in w for w in o["warnings"]))

    def test_options_are_nse_only(self):
        self.index_plan("sell")
        with open("trader/config.json", "w") as fh:
            json.dump(dict(CFG, market="US"), fh)
        code, _, err = self.run_tool(EXIT, ["option", "--now", "2026-11-18 10:05", "--premium", "1"])
        self.assertIn("--market IN", err)


class Sizing(Workdir):
    def plan(self, stop_pct="13"):
        self.index_plan("sell")
        self.assertEqual(self.option("--stop-pct", stop_pct, "--write")[0], 0)
        with open("trader/option_plan.json") as fh:
            return json.load(fh)

    def test_one_lot_fits_a_1000_max_loss_at_13_pct(self):
        s = SIZE.size_option(self.plan("13"), CFG, today="2026-11-18")
        self.assertEqual((s["lots"], s["qty"], s["max_loss"], s["bound_by"]), (1, 65, 952.25, "max loss"))
        o = SIZE.option_order(s)
        self.assertEqual((o["exchange"], o["side"], o["qty"], o["type"], o["limit_price"]),
                         ("NFO", "buy", "65", "limit", "112.35"))
        self.assertEqual(o["option"]["lots"], 1)

    def test_a_30_pct_stop_is_refused_with_the_fix(self):
        s = SIZE.size_option(self.plan("30"), CFG, today="2026-11-18")
        self.assertEqual(s["lots"], 0)
        self.assertIn("more than your $1,000 max loss", s["refusal"])
        self.assertIn("--stop-pct 13", s["refusal"])
        self.assertIsNone(SIZE.option_order(s))

    def test_max_lots_caps_it(self):
        s = SIZE.size_option(self.plan("13"), dict(CFG, max_loss_per_trade_usd=5000, max_lots=1), today="2026-11-18")
        self.assertEqual((s["lots"], s["bound_by"]), (1, "max lots"))

    def test_options_off_by_default_and_buy_only(self):
        op = self.plan("13")
        with self.assertRaises(SIZE.SizeError):
            SIZE.size_option(op, dict(CFG, allow_options=False), today="2026-11-18")
        with self.assertRaises(SIZE.SizeError):
            SIZE.size_option(dict(op, side="sell"), CFG, today="2026-11-18")


ENTRY = {"symbol": "NIFTY26N2325250PE", "exchange": "NFO", "side": "buy", "qty": "65", "type": "limit",
         "limit_price": "112.35", "time_in_force": "day",
         "option": {"underlying": "^NSEI", "type": "PE", "strike": 25250, "expiry": "2026-11-23", "lot_size": 65,
                    "lots": 1, "tick_size": 0.05, "flat_by": "15:10"},
         "plan": {"kind": "option", "entry": 112.35, "stop": 97.7, "target": 141.65, "max_loss": 952.25}}
UTC_1005 = datetime.datetime(2026, 11, 18, 4, 35)        # 10:05 IST


class Risk(unittest.TestCase):
    def setUp(self):
        CHECK.market.apply(vars(CHECK), "IN")

    def tearDown(self):
        CHECK.market.apply(vars(CHECK), "US")

    def verdict(self, order, cfg=CFG, now=UTC_1005, portfolio=None):
        r = CHECK.check([order], cfg, portfolio=portfolio or {"cash": 19000, "positions": []}, now=now)
        return r["results"][0]

    def test_a_sized_put_passes(self):
        r = self.verdict(ENTRY)
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertEqual(r["max_loss"], 952.25)

    def test_options_off_blocks(self):
        r = self.verdict(ENTRY, dict(CFG, allow_options=False))
        self.assertIn("options are off", " ".join(r["reasons"]))

    def test_never_sells_an_option_it_does_not_hold(self):
        r = self.verdict(dict(ENTRY, side="sell"))
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("only buys options", " ".join(r["reasons"]))

    def test_lots_whole_and_capped(self):
        r = self.verdict(dict(ENTRY, qty="100"))
        self.assertIn("not a whole number of lots", " ".join(r["reasons"]))
        r = self.verdict(dict(ENTRY, qty="195"), dict(CFG, max_loss_per_trade_usd=99999, max_order_usd=99999,
                                                      max_daily_loss_usd=99999, budget=99999))
        self.assertIn("over your max_lots 2", " ".join(r["reasons"]))

    def test_expiring_option_after_1330_is_blocked(self):
        late = dict(ENTRY, option=dict(ENTRY["option"], expiry="2026-11-18"))
        self.assertEqual(self.verdict(late)["verdict"], "PASS")
        r = self.verdict(late, now=datetime.datetime(2026, 11, 18, 8, 5))      # 13:35 IST
        self.assertIn("expires today and it is past 13:30", " ".join(r["reasons"]))

    def test_selling_what_you_hold_is_an_exit(self):
        stop = {"symbol": "NIFTY26N2325250PE", "exchange": "NFO", "side": "sell", "qty": "65", "type": "stop_limit",
                "stop_price": "97.70", "limit_price": "92.80", "time_in_force": "day", "option": ENTRY["option"]}
        pf = {"cash": 11697.25, "positions": [{"ticker": "NIFTY26N2325250PE", "shares": 65, "price": 112.35}]}
        r = self.verdict(stop, portfolio=pf)
        self.assertEqual((r["verdict"], r["kind"]), ("PASS", "exit"))


class Tickets(Workdir):
    def test_option_entry_ticket(self):
        k = TICKET.kite_input(ENTRY, CFG)
        self.assertEqual(k, {"mode": "place", "variety": "regular", "exchange": "NFO",
                             "tradingsymbol": "NIFTY26N2325250PE", "transaction_type": "BUY", "quantity": 65,
                             "product": "NRML", "order_type": "LIMIT", "validity": "DAY", "price": 112.35,
                             "tag": "daytraderskill"})
        self.assertIsNone(TICKET.zerodha_problem(ENTRY, CFG))
        self.assertIn("1 lot", TICKET.plain(ENTRY))

    def test_no_sl_m_on_options_and_whole_lots(self):
        self.assertIn("SL-M", TICKET.zerodha_problem(dict(ENTRY, type="stop", stop_price="90"), CFG))
        self.assertIn("whole number of lots", TICKET.zerodha_problem(dict(ENTRY, qty="70"), CFG))

    def test_protect_makes_a_stop_limit_for_an_option(self):
        with open("trader/state.json", "w") as fh:
            json.dump({"orders": [{"at": "2026-11-18T10:06:00+05:30", "date": "2026-11-18", "status": "placed",
                                   "symbol": "NIFTY26N2325250PE", "side": "buy", "qty": "65", "stop": 97.7,
                                   "kind": "entry", "exchange": "NFO", "option": ENTRY["option"]}]}, fh)
        code, out, err = self.run_tool(TICKET, ["protect", "--symbol", "NIFTY26N2325250PE", "--state",
                                                "trader/state.json", "--market", "IN", "--now", "2026-11-18 10:07"])
        self.assertEqual(code, 0, err)
        with open("trader/orders.json") as fh:
            o = json.load(fh)["orders"][0]
        self.assertEqual((o["type"], o["stop_price"], o["limit_price"], o["exchange"]),
                         ("stop_limit", "97.7", "92.80", "NFO"))
        k = TICKET.kite_input(o, CFG)
        self.assertEqual((k["order_type"], k["trigger_price"], k["price"]), ("SL", 97.7, 92.8))
        self.assertNotIn("market_protection", k)


if __name__ == "__main__":
    unittest.main()
