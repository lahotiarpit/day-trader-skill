"""Zerodha Kite Connect as the data source (--data kite) and Zerodha order tickets
in trader-broker. Offline: Kite's answers are faked in the shapes its REST API
documents (instruments CSV, historical candles, session token)."""

import contextlib
import datetime
import filecmp
import io
import json
import os
import shutil
import tempfile
import unittest

from _load import ROOT, load

DATA_SKILLS = [("trader-scanner", "scan"), ("trader-entry", "entry"), ("trader-exit", "exit"),
               ("trader-timemachine", "timemachine"), ("trader-alerts", "alerts")]
MODS = {name: load(folder, name) for folder, name in DATA_SKILLS}
import kite  # noqa: E402  (on sys.path now: every data skill puts its folder there)

market = MODS["scan"].market

INSTRUMENTS = (
    "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,"
    "instrument_type,segment,exchange\n"
    "738561,2885,RELIANCE,RELIANCE INDUSTRIES,0,,0,0.1,1,EQ,NSE,NSE\n"
    "519937,2031,M&M,MAHINDRA & MAHINDRA,0,,0,0.05,1,EQ,NSE,NSE\n"
    "256265,1001,NIFTY 50,NIFTY 50,0,,0,0,0,EQ,INDICES,NSE\n"
    "738562,2886,RELIANCE-BE,RELIANCE BE,0,,0,0.1,1,EQ,NSE,NSE\n"
    "9999,99,RELIANCE24NOVFUT,RELIANCE,0,2024-11-28,0,0.1,250,FUT,NFO-FUT,NFO\n"
)


def five_minute_day(day, closes, start_volume=100000):
    """Kite candles for one NSE session from 9:15, one close per bar."""
    rows, t = [], datetime.datetime.combine(day, datetime.time(9, 15))
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        rows.append([t.strftime("%Y-%m-%dT%H:%M:%S+0530"), o, max(o, c) + 0.5, min(o, c) - 0.5, c,
                     start_volume + 1000 * i])
        prev = c
        t += datetime.timedelta(minutes=5)
    return rows


MON, TUE = datetime.date(2026, 11, 16), datetime.date(2026, 11, 17)
# Monday: a quiet session that ends at 1,340. Tuesday: a 9:15 to 9:30 range, then a
# 9:40 bar (closes 9:45) that closes above the range high.
MONDAY = five_minute_day(MON, [1330 + (i % 5) for i in range(75)][:-1] + [1340.0])
TUESDAY = five_minute_day(TUE, [1341.0, 1343.0, 1342.0, 1342.5, 1343.0, 1344.0, 1348.0, 1349.0, 1350.0,
                                1351.0, 1352.0, 1353.0, 1352.0, 1354.0, 1355.0])
DAILY = [["2026-11-%02dT00:00:00+0530" % d, 1300 + d, 1310 + d, 1290 + d, 1305 + d, 5000000 + d]
         for d in (10, 11, 12, 13, 16, 17)]


class FakeKite(object):
    def __init__(self):
        self.calls = []

    def __call__(self, url, data=None, hdrs=None, timeout=20):
        self.calls.append((url, data, dict(hdrs or {})))
        path = url.split("api.kite.trade", 1)[1]
        if path.startswith("/instruments/NSE"):
            return 200, INSTRUMENTS
        if path.startswith("/instruments/historical/738561/5minute"):
            return 200, json.dumps({"status": "success", "data": {"candles": MONDAY + TUESDAY}})
        if path.startswith("/instruments/historical/738561/minute"):
            return 200, json.dumps({"status": "success", "data": {"candles": []}})
        if path.startswith("/instruments/historical/738561/day"):
            return 200, json.dumps({"status": "success", "data": {"candles": DAILY}})
        if path.startswith("/instruments/historical/519937/"):
            return 200, json.dumps({"status": "success", "data": {"candles": []}})
        if path.startswith("/session/token"):
            return 200, json.dumps({"status": "success", "data": {
                "user_id": "AB1234", "access_token": "fresh-token", "login_time": "2026-11-17 08:55:10"}})
        if path.startswith("/user/profile"):
            return 403, json.dumps({"status": "error", "message": "Incorrect `api_key` or `access_token`.",
                                    "error_type": "TokenException"})
        return 404, json.dumps({"status": "error", "message": "route not found"})


class KiteCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = {k: os.environ.get(k) for k in ("KITE_API_KEY", "KITE_ACCESS_TOKEN", "KITE_API_SECRET",
                                                     "TRADER_DATA", "TRADER_MARKET")}
        os.environ.update({"KITE_API_KEY": "testkey", "KITE_ACCESS_TOKEN": "testtoken"})
        for k in ("KITE_API_SECRET", "TRADER_DATA", "TRADER_MARKET"):
            os.environ.pop(k, None)
        self.fake = FakeKite()
        self.orig = (kite._open, kite._sleep, kite.INSTRUMENTS_FILE, kite.SESSION_FILE)
        kite._open, kite._sleep = self.fake, (lambda s: None)
        kite.INSTRUMENTS_FILE = os.path.join(self.tmp, "kite_instruments_%s.csv")
        kite.SESSION_FILE = os.path.join(self.tmp, "kite_session.json")
        kite._TOKENS.clear()

    def tearDown(self):
        kite._open, kite._sleep, kite.INSTRUMENTS_FILE, kite.SESSION_FILE = self.orig
        kite._TOKENS.clear()
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        market.DATA = "yahoo"
        for mod in MODS.values():
            mod.market.apply(vars(mod), "US")
        shutil.rmtree(self.tmp, ignore_errors=True)


class Copies(unittest.TestCase):
    def test_every_data_skill_carries_the_same_kite_py(self):
        first = os.path.join(ROOT, DATA_SKILLS[0][0], "kite.py")
        for folder, _ in DATA_SKILLS[1:]:
            self.assertTrue(filecmp.cmp(first, os.path.join(ROOT, folder, "kite.py"), shallow=False), folder)

    def test_broker_carries_the_same_market_py(self):
        self.assertTrue(filecmp.cmp(os.path.join(ROOT, "trader-scanner", "market.py"),
                                    os.path.join(ROOT, "trader-broker", "market.py"), shallow=False))

    def test_kite_reads_data_and_never_orders(self):
        with open(os.path.join(ROOT, "trader-scanner", "kite.py"), encoding="utf-8") as fh:
            code = fh.read()
        for word in ("/orders", "place_order", "/gtt"):
            self.assertNotIn('"%s' % word, code)


class Choosing(unittest.TestCase):
    def setUp(self):
        self.saved = os.environ.pop("TRADER_DATA", None)

    def tearDown(self):
        os.environ.pop("TRADER_DATA", None)
        if self.saved is not None:
            os.environ["TRADER_DATA"] = self.saved

    def test_flag_env_config_default(self):
        missing = os.path.join(ROOT, "no-such-config.json")
        self.assertEqual(market.pop_data_flag(["orb", "--data", "kite", "--at", "10:00"]),
                         ("kite", ["orb", "--at", "10:00"]))
        self.assertEqual(market.resolve_data(None, "IN", missing), "yahoo")
        self.assertEqual(market.resolve_data("zerodha", "IN", missing), "kite")
        os.environ["TRADER_DATA"] = "kite"
        self.assertEqual(market.resolve_data(None, "IN", missing), "kite")

    def test_kite_needs_india(self):
        missing = os.path.join(ROOT, "no-such-config.json")
        with self.assertRaises(market.MarketError):
            market.resolve_data("kite", "US", missing)
        os.environ["TRADER_DATA"] = "kite"        # a standing setting quietly stays on Yahoo for US runs
        self.assertEqual(market.resolve_data(None, "US", missing), "yahoo")


class Login(KiteCase):
    def test_symbols(self):
        self.assertEqual(kite.split_symbol("RELIANCE.NS"), ("NSE", "RELIANCE"))
        self.assertEqual(kite.split_symbol("M%26M.NS"), ("NSE", "M&M"))
        self.assertEqual(kite.split_symbol("tcs.bo"), ("BSE", "TCS"))
        self.assertEqual(kite.split_symbol("^NSEI"), ("NSE", "NIFTY 50"))
        self.assertEqual(kite.split_symbol("SBIN"), ("NSE", "SBIN"))

    def test_instruments_keep_equities_and_indices_of_one_exchange(self):
        got = kite.parse_instruments(INSTRUMENTS, "NSE")
        self.assertEqual(got["RELIANCE"], 738561)
        self.assertEqual(got["M&M"], 519937)
        self.assertEqual(got["NIFTY 50"], 256265)
        self.assertNotIn("RELIANCE24NOVFUT", got)

    def test_request_token_from_the_redirect(self):
        url = "http://127.0.0.1:5000/?action=login&type=login&status=success&request_token=Ab12Cd34"
        self.assertEqual(kite.request_token_from(url), "Ab12Cd34")
        self.assertEqual(kite.request_token_from("Ab12Cd34"), "Ab12Cd34")
        with self.assertRaises(kite.KiteError):
            kite.request_token_from("http://127.0.0.1/?status=cancelled&request_token=x")
        with self.assertRaises(kite.KiteError):
            kite.request_token_from("not a token!")

    def test_checksum_is_sha256_of_key_token_secret(self):
        import hashlib
        self.assertEqual(kite.checksum("k", "t", "s"), hashlib.sha256(b"kts").hexdigest())

    def test_session_lasts_until_six_am_ist(self):
        self.assertEqual(kite.session_valid_until("2026-11-17 08:55:10"), datetime.datetime(2026, 11, 18, 6, 0))
        self.assertEqual(kite.session_valid_until("2026-11-17 05:30:00"), datetime.datetime(2026, 11, 17, 6, 0))

    def test_session_needs_the_secret_and_saves_privately(self):
        with self.assertRaises(kite.KiteError) as e:
            kite.create_session("Ab12Cd34")
        self.assertIn("KITE_API_SECRET", str(e.exception))
        os.environ["KITE_API_SECRET"] = "testsecret"
        sess = kite.create_session("Ab12Cd34")
        url, data, hdrs = self.fake.calls[-1]
        self.assertTrue(url.endswith("/session/token"))
        self.assertEqual(data["checksum"], kite.checksum("testkey", "Ab12Cd34", "testsecret"))
        self.assertNotIn("testsecret", json.dumps(data))
        self.assertEqual(sess["access_token"], "fresh-token")
        self.assertEqual(oct(os.stat(kite.SESSION_FILE).st_mode & 0o777), "0o600")

    def test_an_expired_session_says_log_in_again(self):
        os.environ.pop("KITE_ACCESS_TOKEN")
        with open(kite.SESSION_FILE, "w") as fh:
            json.dump({"api_key": "testkey", "access_token": "old", "login_time": "2026-11-16 09:00:00"}, fh)
        self.assertEqual(kite.access_token(now=datetime.datetime(2026, 11, 17, 5, 59)), "old")
        with self.assertRaises(kite.KiteError) as e:
            kite.access_token(now=datetime.datetime(2026, 11, 17, 6, 0))
        self.assertIn("kite.py login", str(e.exception))

    def test_certificate_errors_say_how_to_fix_them(self):
        hint = kite.cert_hint("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
        self.assertIn("Install Certificates.command", hint)
        self.assertIn("certifi", hint)
        self.assertIn("Do not turn certificate checks off", hint)
        self.assertEqual(kite.cert_hint("timed out"), "")

    def test_auth_header_and_token_errors(self):
        kite.get("/instruments/NSE", raw=True)
        _, _, hdrs = self.fake.calls[-1]
        self.assertEqual(hdrs["Authorization"], "token testkey:testtoken")
        self.assertEqual(hdrs["X-Kite-Version"], "3")
        with self.assertRaises(kite.KiteError) as e:
            kite.get("/user/profile")
        self.assertIn("Log in again", str(e.exception))


class YahooShape(KiteCase):
    NOW = datetime.datetime(2026, 11, 17, 10, 30)

    def test_chart_answer_has_yahoo_shape(self):
        doc = json.loads(kite.yahoo_get("/v8/finance/chart/RELIANCE.NS?range=1d&interval=5m", now=self.NOW))
        res = doc["chart"]["result"][0]
        self.assertEqual(len(res["timestamp"]), len(TUESDAY))       # 1d = the latest session only
        self.assertEqual(res["timestamp"][0], int(datetime.datetime(2026, 11, 17, 3, 45,
                                                                    tzinfo=datetime.timezone.utc).timestamp()))
        m = res["meta"]
        self.assertEqual((m["symbol"], m["dataGranularity"], m["currency"]), ("RELIANCE.NS", "5m", "INR"))
        self.assertEqual(m["regularMarketPrice"], 1355.0)
        self.assertEqual(m["previousClose"], 1340.0)                 # Monday's last bar
        self.assertEqual(res["indicators"]["quote"][0]["close"][:3], [1341.0, 1343.0, 1342.0])
        url = self.fake.calls[-1][0]
        self.assertIn("/instruments/historical/738561/5minute?", url)

    def test_five_day_range_keeps_both_sessions(self):
        doc = json.loads(kite.yahoo_get("/v8/finance/chart/RELIANCE.NS?range=5d&interval=5m", now=self.NOW))
        self.assertEqual(len(doc["chart"]["result"][0]["timestamp"]), len(MONDAY) + len(TUESDAY))

    def test_unknown_symbol_is_yahoo_not_found(self):
        doc = json.loads(kite.yahoo_get("/v8/finance/chart/NOSUCH.NS?range=1d&interval=5m", now=self.NOW))
        self.assertIsNone(doc["chart"]["result"])
        self.assertIn("no NSE equity called NOSUCH", doc["chart"]["error"]["description"])

    def test_spark_skips_empty_and_unknown(self):
        doc = json.loads(kite.yahoo_get("/v7/finance/spark?symbols=RELIANCE.NS,M%26M.NS,NOSUCH.NS"
                                        "&range=1d&interval=5m", now=self.NOW))
        got = [r["symbol"] for r in doc["spark"]["result"]]
        self.assertEqual(got, ["RELIANCE.NS"])

    def test_a_long_range_is_asked_in_chunks(self):
        doc = json.loads(kite.yahoo_get("/v8/finance/chart/RELIANCE.NS?range=60d&interval=1m", now=self.NOW))
        self.assertIsNone(doc["chart"]["result"])                     # no bars: Yahoo's "no data" shape
        hist = [c for c in self.fake.calls if "/historical/" in c[0]]
        self.assertGreaterEqual(len(hist), 2)                        # minute bars: 60 days a request

    def test_daily_bars_give_the_previous_close(self):
        doc = json.loads(kite.yahoo_get("/v8/finance/chart/RELIANCE.NS?range=5d&interval=1d", now=self.NOW))
        m = doc["chart"]["result"][0]["meta"]
        self.assertEqual(m["regularMarketPrice"], 1322)
        self.assertEqual(m["previousClose"], 1321)


class EndToEnd(KiteCase):
    def run_tool(self, mod, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = mod.main(argv)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def test_entry_reads_kite_bars_and_triggers(self):
        code, out = self.run_tool(MODS["entry"], ["check", "RELIANCE", "--market", "IN", "--data", "kite",
                                                  "--now", "2026-11-17 10:30"])
        self.assertIn("Opening range 9:15-9:30: high \u20b91343.50, low \u20b91340.50 (3 of 3 bars).", out)
        self.assertIn("TRIGGERED 9:45 AM at \u20b91344.00", out)
        self.assertIn("Zerodha Kite", out)
        self.assertNotIn("Yahoo", out)
        self.assertTrue(any("/historical/738561/5minute" in c[0] for c in self.fake.calls))

    def test_yahoo_stays_the_default(self):
        self.assertEqual(market.resolve_data(None, "IN", os.path.join(ROOT, "no-such-config.json")), "yahoo")


TICKET = load("trader-broker", "ticket")
CHECK = load("trader-risk", "check")
ZERODHA = {"broker": "zerodha", "paper": False, "market": "IN"}
BRACKET = {"symbol": "RELIANCE.NS", "side": "buy", "qty": "44", "type": "market", "time_in_force": "day",
           "order_class": "bracket", "take_profit_limit_price": "1351.90", "stop_loss_stop_price": "1340.65",
           "plan": {"entry": 1344.40, "stop": 1340.65}}


class ZerodhaTickets(unittest.TestCase):
    def setUp(self):
        TICKET.market.apply(vars(TICKET), "IN")

    def tearDown(self):
        TICKET.market.apply(vars(TICKET), "US")
        CHECK.market.apply(vars(CHECK), "US")

    def test_kite_place_order_arguments(self):
        k = TICKET.kite_input(BRACKET, ZERODHA)
        self.assertEqual(k, {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE",
                             "transaction_type": "BUY", "quantity": 44, "product": "MIS", "order_type": "MARKET",
                             "validity": "DAY", "tag": "daytraderskill"})
        stop = TICKET.kite_input({"symbol": "TCS.BO", "side": "sell", "qty": 5, "type": "stop",
                                  "stop_price": "3100.05", "time_in_force": "day"}, ZERODHA)
        self.assertEqual((stop["exchange"], stop["tradingsymbol"], stop["order_type"], stop["trigger_price"]),
                         ("BSE", "TCS", "SL-M", 3100.05))
        lim = TICKET.kite_input({"symbol": "M&M", "side": "buy", "qty": 3, "type": "limit", "limit_price": 3390,
                                 "time_in_force": "day"}, dict(ZERODHA, kite_product="cnc"))
        self.assertEqual((lim["tradingsymbol"], lim["order_type"], lim["price"], lim["product"]),
                         ("M&M", "LIMIT", 3390.0, "CNC"))

    def test_zerodha_has_no_paper_account(self):
        why = TICKET.zerodha_problem(BRACKET, {"broker": "zerodha"})
        self.assertIn("no paper account", why)
        self.assertIsNone(TICKET.zerodha_problem(BRACKET, ZERODHA))
        ready, label, _ = TICKET.status_of(BRACKET, None, {"orders": []}, "2026-11-17", {"broker": "zerodha"})
        self.assertFalse(ready)
        self.assertIn("real money", label)

    def test_zerodha_wants_whole_shares_in_india(self):
        self.assertIn("whole shares", TICKET.zerodha_problem(dict(BRACKET, qty="2.5"), ZERODHA))
        self.assertIn("whole shares", TICKET.zerodha_problem({"symbol": "SBIN", "side": "buy", "notional": 5000,
                                                              "type": "market"}, ZERODHA))
        TICKET.market.apply(vars(TICKET), "US")
        self.assertIn("--market IN", TICKET.zerodha_problem(BRACKET, ZERODHA))

    def test_bracket_ticket_says_entry_only(self):
        fp = TICKET.fingerprint(BRACKET)
        risk = {"date": "2026-11-17", "results": [{"fingerprint": fp, "verdict": "PASS", "kind": "entry"}]}
        text, ready = TICKET.render(BRACKET, 1, 1, ZERODHA, risk, {"orders": []}, "2026-11-17")
        self.assertTrue(ready)
        self.assertIn("LIVE ACCOUNT, REAL MONEY (Zerodha has no paper trading), Zerodha", text)
        self.assertIn("this sends the ENTRY ONLY", text)
        self.assertIn("ticket.py protect", text)
        self.assertNotIn("stop_loss_stop_price", text)

    def test_protect_builds_the_stop_from_the_placed_entry(self):
        tmp = tempfile.mkdtemp()
        try:
            cfg, state, orders = (os.path.join(tmp, n) for n in ("config.json", "broker.json", "orders.json"))
            with open(cfg, "w") as fh:
                json.dump(ZERODHA, fh)
            with open(state, "w") as fh:
                json.dump({"orders": [{"at": "2026-11-17T11:16:00+05:30", "date": "2026-11-17", "status": "placed",
                                       "symbol": "RELIANCE.NS", "side": "buy", "qty": "44", "stop": 1340.65,
                                       "kind": "entry"}]}, fh)
            with contextlib.redirect_stdout(io.StringIO()):
                code = TICKET.main(["protect", "--symbol", "reliance.ns", "--config", cfg, "--state", state,
                                    "--orders", orders, "--market", "IN", "--now", "2026-11-17 11:17"])
            self.assertEqual(code, 0)
            with open(orders) as fh:
                o = json.load(fh)["orders"][0]
            self.assertEqual((o["side"], o["qty"], o["type"], o["stop_price"]), ("sell", "44", "stop", "1340.65"))
            self.assertEqual(TICKET.kite_input(o, ZERODHA)["order_type"], "SL-M")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_protect_is_zerodha_only(self):
        with self.assertRaises(TICKET.TicketError):
            TICKET.cmd_protect(type("A", (), {"config": os.path.join(ROOT, "no-such.json"), "symbol": "X"})())

    def test_times_carry_the_ist_offset(self):
        now = datetime.datetime(2026, 11, 17, 5, 46)          # UTC
        self.assertEqual(TICKET.ny_iso(now), "2026-11-17T11:16:00+05:30")

    def test_risk_reads_nse_symbols_in_india(self):
        CHECK.market.apply(vars(CHECK), "IN")
        for sym in ("RELIANCE.NS", "M&M", "BAJAJ-AUTO", "3MINDIA", "GVT&D", "TCS.BO"):
            self.assertTrue(CHECK.IN_TICKER.match(sym), sym)
        self.assertFalse(CHECK.IN_TICKER.match("BTC/USD"))

    def test_risk_window_has_no_early_close_in_india(self):
        CHECK.market.apply(vars(CHECK), "IN")
        reasons, warns = CHECK.window_check(datetime.datetime(2026, 11, 17, 11, 15),
                                            {"no_entries_before": "09:30", "no_entries_after": "14:45"})
        self.assertEqual((reasons, warns), ([], []))


class TimeMachineSymbols(unittest.TestCase):
    def test_index_and_ampersand_symbols_are_accepted(self):
        tm = MODS["timemachine"]
        for sym in ("^NSEI", "M&M", "GVT&D", "3MINDIA", "RELIANCE.NS", "TSLA", "BRK.B"):
            try:
                tm.normalize_plan({"symbol": sym, "entry": 100, "stop": 99, "target": 102})
            except tm.TMError as e:
                self.assertNotIn("needs a ticker symbol", str(e), sym)
        with self.assertRaises(tm.TMError):
            tm.normalize_plan({"symbol": "BTC/USD", "entry": 100, "stop": 99, "target": 102})


if __name__ == "__main__":
    unittest.main()
