#!/usr/bin/env python3
"""Zerodha Kite Connect as a data source for the clock tools. Standard library only.

Every skill folder that reads charts carries an identical copy of this file
(scanner, entry, exit, timemachine, alerts), so each folder still installs on its
own. tests/test_kite.py checks the copies match.

The tools were written against Yahoo's chart JSON. With --data kite (or
TRADER_DATA=kite, or "data": "kite" in trader/config.json) they ask this file
instead, and it answers the same Yahoo path (/v8/finance/chart/... or
/v7/finance/spark?...) with the same JSON shape, built from Kite's historical
candles. So every rule, every check and every number downstream stays the same:
only where the bars come from changes. Kite serves NSE and BSE, so it needs
--market IN.

It reads data only. It never places, changes or cancels an order: orders go
through trader-broker and the Kite MCP server, with your yes and Claude's
Allow once card.

What you need (all yours, never in the chat):
  - a Kite Connect app at developers.kite.trade with the paid Connect plan
    (the free Personal plan has no market data)
  - KITE_API_KEY and KITE_API_SECRET in your environment (your shell profile or
    Claude Code's settings.json "env"), on your own computer
  - one browser login a day: Zerodha ends every API session at 6:00 AM IST

    python3 kite.py login                 # prints the Zerodha login link
    python3 kite.py session "<the URL Zerodha sent you back to, or its request_token>"
    python3 kite.py status                # who is logged in, and until when
    python3 kite.py check RELIANCE        # today's 5-minute bars, a quick test

`session` writes trader/kite_session.json (your access token, readable by you
only). KITE_ACCESS_TOKEN in the environment works too and wins over the file.
"""

import csv
import datetime
import hashlib
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.kite.trade"
LOGIN_URL = "https://kite.zerodha.com/connect/login?v=3&api_key=%s"
SESSION_FILE = "trader/kite_session.json"
CONFIG_FILE = "trader/config.json"
INSTRUMENTS_FILE = "trader/kite_instruments_%s.csv"
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
SESSION_ENDS = datetime.time(6, 0)      # Zerodha flushes every access token at 6:00 AM IST
PAUSE = 0.35                            # historical candles: 3 requests a second at most

# Yahoo interval -> Kite interval, and the most days Kite gives in one request
INTERVALS = {"1m": "minute", "3m": "3minute", "5m": "5minute", "15m": "15minute",
             "30m": "30minute", "60m": "60minute", "1h": "60minute", "1d": "day"}
MAX_DAYS = {"minute": 60, "3minute": 100, "5minute": 100, "15minute": 200, "30minute": 200,
            "60minute": 400, "day": 2000}
# Yahoo range -> sessions (s) or calendar days (d)
RANGES = {"1d": ("s", 1), "5d": ("s", 5), "1mo": ("d", 31), "60d": ("d", 60), "3mo": ("d", 92),
          "6mo": ("d", 183), "1y": ("d", 366), "2y": ("d", 731), "5y": ("d", 1827)}
# Yahoo index tickers -> Kite trading symbols
INDICES = {"^NSEI": ("NSE", "NIFTY 50"), "^NSEBANK": ("NSE", "NIFTY BANK"),
           "^CNXIT": ("NSE", "NIFTY IT"), "^BSESN": ("BSE", "SENSEX")}


class KiteError(Exception):
    pass


class NoData(KiteError):
    """An unknown symbol or an empty answer: the Yahoo shape says 'no data', not an error."""


# ---------------------------------------------------------------- credentials

def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def now_ist():
    return datetime.datetime.now(IST).replace(tzinfo=None)


def session_valid_until(login_time):
    """The 6:00 AM IST after a login (Kite's login_time is IST, 'YYYY-MM-DD HH:MM:SS')."""
    t = datetime.datetime.strptime(str(login_time)[:19], "%Y-%m-%d %H:%M:%S")
    end = datetime.datetime.combine(t.date(), SESSION_ENDS)
    return end if t < end else end + datetime.timedelta(days=1)


def api_key(env=None):
    env = os.environ if env is None else env
    key = env.get("KITE_API_KEY")
    if key:
        return key.strip()
    sess = _read_json(SESSION_FILE) or {}
    if sess.get("api_key"):
        return sess["api_key"]
    raise KiteError("no Kite API key: set KITE_API_KEY in your environment (your Kite Connect app's key "
                    "from developers.kite.trade). Never paste it in the chat.")


def access_token(env=None, now=None):
    env = os.environ if env is None else env
    tok = env.get("KITE_ACCESS_TOKEN")
    if tok:
        return tok.strip()
    sess = _read_json(SESSION_FILE)
    if not sess or not sess.get("access_token"):
        raise KiteError("not logged in to Zerodha today. Run: python3 kite.py login, log in in your "
                        "browser, then python3 kite.py session \"<the URL it sends you back to>\"")
    if sess.get("login_time"):
        until = session_valid_until(sess["login_time"])
        if (now or now_ist()) >= until:
            raise KiteError("your Zerodha session ended at %s IST (Zerodha ends every session at 6:00 AM). "
                            "Log in again: python3 kite.py login" % until.strftime("%-d %b %H:%M"))
    return sess["access_token"]


def headers():
    return {"X-Kite-Version": "3", "Authorization": "token %s:%s" % (api_key(), access_token()),
            "User-Agent": "day-trader-skill"}


# ---------------------------------------------------------------- HTTP

def _open(url, data=None, hdrs=None, timeout=20):
    """(status, body text). Tests swap this out."""
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=hdrs or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "replace")
        finally:
            e.close()
    except urllib.error.URLError as e:
        raise KiteError("could not reach Zerodha (%s): %s%s" % (url.split("/")[2], e.reason, cert_hint(e.reason)))


def cert_hint(reason):
    """Python from python.org on a Mac ships without the certificates it needs to
    check https sites. Say how to fix that; never turn the check off."""
    if "CERTIFICATE_VERIFY_FAILED" not in str(reason):
        return ""
    ver = "%d.%d" % sys.version_info[:2]
    return ("\nThis Python cannot check https certificates yet (common with Python from python.org on a Mac). "
            "Fix it once, then run this again:\n"
            "  open \"/Applications/Python %s/Install Certificates.command\"\n"
            "If that file does not exist: python3 -m pip install --upgrade certifi, then "
            "export SSL_CERT_FILE=\"$(python3 -m certifi)\" (add that line to ~/.zshrc to keep it).\n"
            "Do not turn certificate checks off: they are what stop a fake Zerodha from taking your login." % ver)


def _sleep(s):
    time.sleep(s)


def _explain(status, text):
    try:
        doc = json.loads(text)
    except ValueError:
        doc = {}
    msg = doc.get("message") or text[:200].strip() or "no message"
    kind = doc.get("error_type") or ""
    if kind == "TokenException" or status == 403:
        return ("Zerodha refused the session (%s). Log in again: python3 kite.py login" % msg)
    if status == 429:
        return "Zerodha is rate limiting (HTTP 429). Wait a few seconds and run it again."
    return "Zerodha answered HTTP %d: %s" % (status, msg)


def get(path, params=None, raw=False):
    url = API + path + (("?" + urllib.parse.urlencode(params, doseq=True)) if params else "")
    for attempt in (0, 1):
        status, text = _open(url, hdrs=headers())
        if status == 429 and attempt == 0:
            _sleep(1.0)
            continue
        break
    if status != 200:
        raise KiteError(_explain(status, text))
    if raw:
        return text
    try:
        doc = json.loads(text)
    except ValueError:
        raise KiteError("Zerodha sent something that is not JSON for %s" % path)
    if doc.get("status") != "success":
        raise KiteError(_explain(status, text))
    return doc.get("data")


# ---------------------------------------------------------------- symbols

def split_symbol(ysym):
    """Yahoo symbol -> (exchange, Kite trading symbol). RELIANCE.NS -> (NSE, RELIANCE),
    TCS.BO -> (BSE, TCS), ^NSEI -> (NSE, NIFTY 50), M&M -> (NSE, M&M)."""
    s = urllib.parse.unquote(str(ysym)).strip().upper()
    if s in INDICES:
        return INDICES[s]
    if s.endswith(".NS"):
        return "NSE", s[:-3]
    if s.endswith(".BO"):
        return "BSE", s[:-3]
    if s.startswith("^"):
        raise KiteError("%s is not an index this tool maps to Kite (it knows %s)" % (s, ", ".join(sorted(INDICES))))
    return "NSE", s


def parse_instruments(text, exchange):
    """Kite's instruments CSV -> {tradingsymbol: instrument_token}, equities and indices only."""
    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("exchange") != exchange:
            continue
        seg = row.get("segment") or ""
        if row.get("instrument_type") == "EQ" or seg == "INDICES":
            try:
                out[row["tradingsymbol"].upper()] = int(row["instrument_token"])
            except (KeyError, ValueError):
                continue
    return out


_TOKENS = {}


def instrument_token(exchange, tsym, today=None):
    """The instrument token Kite's candle API wants. The instruments list is
    downloaded once a day per exchange and kept in trader/."""
    if exchange not in _TOKENS:
        path = INSTRUMENTS_FILE % exchange
        day = (today or now_ist().date()).isoformat()
        text = None
        try:
            mtime = datetime.datetime.fromtimestamp(os.path.getmtime(path), IST).date().isoformat()
            if mtime == day:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
        except OSError:
            pass
        if text is None:
            text = get("/instruments/%s" % exchange, raw=True)
            try:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text)
            except OSError:
                pass
        _TOKENS[exchange] = parse_instruments(text, exchange)
    tok = _TOKENS[exchange].get(tsym.upper())
    if tok is None:
        raise NoData("Zerodha has no %s equity called %s (check the NSE symbol)" % (exchange, tsym))
    return tok


# ---------------------------------------------------------------- candles

def parse_ts(s):
    """'2026-11-17T09:15:00+0530' -> Unix seconds."""
    return int(datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z").timestamp())


def candles(token, interval, start, end):
    """Kite historical candles between two IST datetimes, in chunks Kite accepts."""
    span = datetime.timedelta(days=MAX_DAYS[interval])
    out, a, first = [], start, True
    while a <= end:
        b = min(end, a + span)
        if not first:
            _sleep(PAUSE)
        first = False
        data = get("/instruments/historical/%d/%s" % (token, interval),
                   {"from": a.strftime("%Y-%m-%d %H:%M:%S"), "to": b.strftime("%Y-%m-%d %H:%M:%S")})
        out.extend((data or {}).get("candles") or [])
        a = b + datetime.timedelta(seconds=1)
    seen, rows = set(), []
    for c in out:
        if c[0] in seen:
            continue
        seen.add(c[0])
        rows.append(c)
    return rows


def to_yahoo_result(ysym, interval_y, rng, rows, now=None):
    """Kite candle rows -> one Yahoo v8 chart result, keeping only `rng`.
    rows: [[time, open, high, low, close, volume], ...] oldest first."""
    kind, n = RANGES[rng]
    dates = sorted({r[0][:10] for r in rows})
    if kind == "s":
        keep = set(dates[-n:])
    else:
        cut = ((now or now_ist()).date() - datetime.timedelta(days=n)).isoformat()
        keep = {d for d in dates if d > cut}
    before = [r for r in rows if r[0][:10] not in keep and r[0][:10] < min(keep or {"9999"})]
    rows = [r for r in rows if r[0][:10] in keep]
    q = {"open": [], "high": [], "low": [], "close": [], "volume": []}
    ts = []
    for r in rows:
        ts.append(parse_ts(r[0]))
        for i, k in enumerate(("open", "high", "low", "close", "volume"), 1):
            q[k].append(r[i])
    meta = {"symbol": urllib.parse.unquote(ysym).upper(), "currency": "INR", "dataGranularity": interval_y,
            "range": rng, "source": "kite"}
    if rows:
        last_day = rows[-1][0][:10]
        today_rows = [r for r in rows if r[0][:10] == last_day]
        meta.update({"regularMarketPrice": rows[-1][4], "regularMarketTime": ts[-1],
                     "regularMarketDayHigh": max(r[2] for r in today_rows),
                     "regularMarketDayLow": min(r[3] for r in today_rows),
                     "regularMarketVolume": sum(r[5] or 0 for r in today_rows)})
        prior = [r for r in rows if r[0][:10] < last_day] or before
        if prior:
            meta["previousClose"] = prior[-1][4]
        if before:
            meta["chartPreviousClose"] = before[-1][4]
    return {"meta": meta, "timestamp": ts, "indicators": {"quote": [q]}}


def chart_result(ysym, rng, interval_y, now=None):
    if interval_y not in INTERVALS:
        raise KiteError("Kite has no %s bars (it has %s)" % (interval_y, ", ".join(sorted(INTERVALS))))
    if rng not in RANGES:
        raise KiteError("this tool cannot ask Kite for range %s" % rng)
    interval = INTERVALS[interval_y]
    exchange, tsym = split_symbol(ysym)
    token = instrument_token(exchange, tsym)
    end = now or now_ist()
    kind, n = RANGES[rng]
    days = n + 10 if kind == "d" else n * 2 + 10    # room for weekends, holidays and the previous close
    days = min(days, 2000 if interval == "day" else 400)
    start = datetime.datetime.combine(end.date() - datetime.timedelta(days=days), datetime.time(0, 0))
    rows = candles(token, interval, start, end)
    if not rows:
        raise NoData("Zerodha returned no %s bars for %s" % (interval_y, tsym))
    return to_yahoo_result(ysym, interval_y, rng, rows, now=end)


def yahoo_get(path, now=None):
    """Answer a Yahoo chart or spark path with Kite data, as Yahoo's JSON text."""
    u = urllib.parse.urlsplit(path)
    qs = urllib.parse.parse_qs(u.query)
    rng = (qs.get("range") or ["1d"])[0]
    interval = (qs.get("interval") or ["1d"])[0]
    if u.path.startswith("/v8/finance/chart/"):
        ysym = u.path.rsplit("/", 1)[1]
        try:
            res = chart_result(ysym, rng, interval, now)
        except NoData as e:
            return json.dumps({"chart": {"result": None, "error": {"code": "Not Found", "description": str(e)}}})
        return json.dumps({"chart": {"result": [res], "error": None}})
    if u.path.startswith("/v7/finance/spark"):
        syms = [s for s in (qs.get("symbols") or [""])[0].split(",") if s]
        result = []
        for i, ysym in enumerate(syms):
            if i:
                _sleep(PAUSE)
            try:
                res = chart_result(ysym, rng, interval, now)
            except NoData:
                continue
            result.append({"symbol": urllib.parse.unquote(ysym).upper(), "response": [res]})
        return json.dumps({"spark": {"result": result, "error": None}})
    raise KiteError("this tool does not know how to answer %s from Kite" % u.path)


# ---------------------------------------------------------------- options (NFO): read only

# Yahoo index ticker -> the name Zerodha's NFO list gives its options
UNDERLYINGS = {"^NSEI": "NIFTY", "^NSEBANK": "BANKNIFTY", "^CNXFIN": "FINNIFTY", "^NSEMDCP50": "MIDCPNIFTY"}
EXPIRY_SWITCH = "13:30"     # on expiry day, after this time use the next expiry


def option_root(ysym):
    """^NSEI -> NIFTY; RELIANCE.NS or RELIANCE -> RELIANCE (stock options)."""
    s = urllib.parse.unquote(str(ysym)).strip().upper()
    if s in UNDERLYINGS:
        return UNDERLYINGS[s]
    if s.startswith("^"):
        raise KiteError("no options mapped for the index %s (known: %s)" % (s, ", ".join(sorted(UNDERLYINGS))))
    return split_symbol(s)[1]


def parse_options(text, root, exchange="NFO"):
    """Zerodha's NFO instruments CSV -> option rows of one underlying."""
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("exchange") != exchange or (row.get("name") or "").upper() != root:
            continue
        if row.get("instrument_type") not in ("CE", "PE"):
            continue
        try:
            out.append({"tradingsymbol": row["tradingsymbol"], "exchange": exchange, "name": root,
                        "expiry": row["expiry"], "strike": float(row["strike"]), "type": row["instrument_type"],
                        "lot_size": int(float(row["lot_size"])), "tick_size": float(row["tick_size"]),
                        "instrument_token": int(row["instrument_token"])})
        except (KeyError, ValueError):
            continue
    return out


_CHAINS = {}


def option_chain(root, exchange="NFO", today=None):
    """Every live option of `root`, from the day's instruments list (cached in trader/)."""
    key = (exchange, root)
    if key not in _CHAINS:
        path = INSTRUMENTS_FILE % exchange
        day = (today or now_ist().date()).isoformat()
        text = None
        try:
            if datetime.datetime.fromtimestamp(os.path.getmtime(path), IST).date().isoformat() == day:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
        except OSError:
            pass
        if text is None:
            text = get("/instruments/%s" % exchange, raw=True)
            try:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text)
            except OSError:
                pass
        _CHAINS[key] = parse_options(text, root, exchange)
    if not _CHAINS[key]:
        raise NoData("Zerodha lists no %s options for %s" % (exchange, root))
    return _CHAINS[key]


def pick_expiry(chain, now, switch=EXPIRY_SWITCH):
    """The nearest expiry; on expiry day itself, the next one once it is `switch` (13:30) or later."""
    day = now.date().isoformat()
    cut = datetime.datetime.strptime(switch, "%H:%M").time()
    dates = sorted({r["expiry"] for r in chain if r["expiry"] >= day})
    if not dates:
        raise NoData("no %s option expiry on or after %s" % (chain[0]["name"] if chain else "?", day))
    if dates[0] == day and now.time() >= cut:
        if len(dates) < 2:
            raise NoData("it is past %s on expiry day and Zerodha lists no later expiry yet" % switch)
        return dates[1], "today's expiry is past %s, so the next one" % switch
    return dates[0], ("expires today" if dates[0] == day else "the nearest expiry")


def pick_strike(chain, expiry, kind, spot, moneyness="atm"):
    """ATM: the strike nearest the index (a tie goes in the money). itm1/otm1: one strike away."""
    rows = sorted((r for r in chain if r["expiry"] == expiry and r["type"] == kind), key=lambda r: r["strike"])
    if not rows:
        raise NoData("no %s %s options for %s" % (chain[0]["name"] if chain else "?", kind, expiry))
    strikes = [r["strike"] for r in rows]

    def tie_key(i):
        k = strikes[i]
        itm = k <= spot if kind == "CE" else k >= spot
        return (abs(k - spot), 0 if itm else 1)
    i = min(range(len(strikes)), key=tie_key)
    shift = {"atm": 0, "itm1": -1 if kind == "CE" else 1, "otm1": 1 if kind == "CE" else -1}.get(moneyness)
    if shift is None:
        raise KiteError("strike choice is atm, itm1 or otm1, not %r" % moneyness)
    i = max(0, min(len(rows) - 1, i + shift))
    return rows[i]


def ltp(instruments):
    """{"NFO:NIFTY26N2425400CE": last price, ...} from Kite's LTP quote."""
    data = get("/quote/ltp", {"i": list(instruments)}) or {}
    return {k: (v or {}).get("last_price") for k, v in data.items()}


# ---------------------------------------------------------------- login

def request_token_from(text):
    """Zerodha's redirect URL (or the bare token) -> request_token."""
    t = (text or "").strip()
    if "request_token=" in t:
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(t).query)
        if (qs.get("status") or ["success"])[0] != "success":
            raise KiteError("Zerodha's redirect says the login did not succeed (status %s)" % qs.get("status"))
        t = (qs.get("request_token") or [""])[0]
    if not t or not t.isalnum():
        raise KiteError("that is not a request_token: paste the whole URL Zerodha sent you back to")
    return t


def checksum(key, request_token, secret):
    return hashlib.sha256((key + request_token + secret).encode("utf-8")).hexdigest()


def create_session(request_token, env=None):
    env = os.environ if env is None else env
    key = api_key(env)
    secret = (env.get("KITE_API_SECRET") or "").strip()
    if not secret:
        raise KiteError("KITE_API_SECRET is not set. Put your Kite Connect app's secret in your environment "
                        "on your own computer (never in the chat), then run this again.")
    status, text = _open(API + "/session/token",
                         data={"api_key": key, "request_token": request_token,
                               "checksum": checksum(key, request_token, secret)},
                         hdrs={"X-Kite-Version": "3", "User-Agent": "day-trader-skill"})
    if status != 200:
        raise KiteError(_explain(status, text))
    data = (json.loads(text) or {}).get("data") or {}
    if not data.get("access_token"):
        raise KiteError("Zerodha did not return an access token")
    sess = {"api_key": key, "access_token": data["access_token"], "user_id": data.get("user_id"),
            "login_time": data.get("login_time") or now_ist().strftime("%Y-%m-%d %H:%M:%S")}
    os.makedirs(os.path.dirname(SESSION_FILE) or ".", exist_ok=True)
    fd = os.open(SESSION_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(sess, fh, indent=2)
    return sess


# ---------------------------------------------------------------- command line

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "help"
    try:
        if cmd == "login":
            link = LOGIN_URL % api_key()
            print("Open this link in your browser and log in to Zerodha:")
            print("  " + link)
            print("Zerodha then sends you to your app's redirect URL. Copy that whole URL from the address bar")
            print("(it holds a one-time request_token) and run:")
            print("  python3 kite.py session \"<that URL>\"")
            return 0
        if cmd == "session":
            if len(argv) < 2:
                raise KiteError("say what Zerodha sent you back to: kite.py session \"<URL or request_token>\"")
            sess = create_session(request_token_from(argv[1]))
            until = session_valid_until(sess["login_time"])
            print("Logged in to Zerodha as %s. The session lasts until %s IST. Saved to %s (only you can read it)."
                  % (sess.get("user_id") or "you", until.strftime("%a %-d %b %H:%M"), SESSION_FILE))
            return 0
        if cmd == "status":
            prof = get("/user/profile") or {}
            sess = _read_json(SESSION_FILE) or {}
            until = (" until %s IST" % session_valid_until(sess["login_time"]).strftime("%a %-d %b %H:%M")
                     if sess.get("login_time") and not os.environ.get("KITE_ACCESS_TOKEN") else "")
            print("Logged in to Zerodha as %s%s. Exchanges: %s." % (
                prof.get("user_id") or "?", until, ", ".join(prof.get("exchanges") or []) or "?"))
            return 0
        if cmd == "check":
            sym = argv[1] if len(argv) > 1 else "RELIANCE"
            res = chart_result(sym, "1d", "5m")
            n = len(res["timestamp"])
            when = datetime.datetime.fromtimestamp(res["timestamp"][-1], IST).strftime("%a %-d %b %H:%M") if n else "-"
            print("Kite: %d five-minute bars for %s, last one starts %s IST, close %s." % (
                n, split_symbol(sym)[1], when, res["meta"].get("regularMarketPrice")))
            return 0
        print(__doc__.strip().split("\n\n")[-2])
        return 1 if cmd != "help" else 0
    except KiteError as e:
        print("kite.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
