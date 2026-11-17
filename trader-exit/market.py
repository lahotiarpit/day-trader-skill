"""Market profiles for the clock-reading tools: US (NYSE / Nasdaq, the default) and
IN (India, NSE). Standard library only.

Every skill folder whose tool reads the exchange clock carries an identical copy
of this file (scanner, entry, exit, timemachine, risk, alerts), so each folder
still installs on its own. tests/test_market.py checks the copies match.

Pick the market, first match wins:
    --market IN            on the command line of any of those tools
    TRADER_MARKET=IN       in the environment
    "market": "IN"         in trader/config.json
    US                     otherwise

and where the bars come from: --data kite (TRADER_DATA=kite, "data": "kite")
reads Zerodha Kite Connect through kite.py instead of Yahoo. IN only.

US is whatever the tool already defines: this file never changes US behaviour.
IN swaps in the NSE session (9:15 to 15:30 IST, UTC+5:30, no daylight saving),
the NSE holiday list, India day-trade defaults, Yahoo's .NS tickers and rupee
labels on screen.
"""

import datetime
import json
import os
import re
import sys
import urllib.parse

DEFAULT_CONFIG = "trader/config.json"
CODES = ("US", "IN")

# NSE equity segment trading holidays, weekdays only (weekend holidays cost no
# session). NSE circular NSE/FAOP/71777 (Dec 12 2025), as listed by brokers'
# holiday pages read 2026-11-17, plus the Jan 15 Maharashtra municipal election
# holiday NSE added in Jan 2026. NSE publishes the 2027 list each December: until
# it is added here, every 2027 weekday counts as a trading day (see CALENDAR_YEARS).
NSE_HOLIDAYS = {
    "2026-01-15": "Municipal Corporation Election in Maharashtra",
    "2026-01-26": "Republic Day",
    "2026-03-03": "Holi",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-10": "Diwali Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}

IN = {
    "code": "IN",
    "exchange": "NSE",
    "place": "India",
    "zone": "IST",
    "currency": "₹",           # the rupee sign
    "utc_offset_hours": 5.5,        # IST, no daylight saving
    "open": (9, 15),
    "close": (15, 30),
    # The Time Machine replays to 15:15, not 15:30: Yahoo leaves NSE's 15:20 and
    # 15:25 five-minute bars empty on most days (checked on 60 sessions of SBIN,
    # RELIANCE and INFY to 2026-11-17), and brokers square off intraday from 15:15.
    "replay_close": (15, 15),
    "holidays": NSE_HOLIDAYS,
    "early_close": {},              # NSE has no half days; Muhurat trading is a separate evening session
    "calendar_years": (2026,),
    # Day-trade defaults. Brokers square off intraday (MIS) positions from about
    # 15:15 to 15:20, so be flat before that; the 15-minute range ends at 9:30.
    "defaults": {"no_entries_before": "09:30", "no_entries_after": "14:45", "flat_by": "15:10"},
    "min_price": 50.0,              # rupees
    "min_volume": 100000,           # shares in the last session
    "yahoo_suffix": ".NS",          # NSE on Yahoo; .BO is BSE
}

# Module-level names a tool may define; apply() saves the tool's own values the
# first time (they are the US profile) and swaps in India's for IN.
PATCHABLE = ("NYSE_HOLIDAYS", "NYSE_EARLY_CLOSE", "CALENDAR_YEARS", "OPEN", "CLOSE", "EARLY",
             "OPEN_MIN", "CLOSE_MIN", "EARLY_MIN", "SESSION_BARS", "DEFAULTS", "FLAT_BY",
             "NO_ENTRIES_AFTER", "MIN_PRICE", "MIN_VOLUME", "eastern_offset", "eastern_to_utc",
             "to_yahoo", "yahoo_symbol")
_SAVED = "_MARKET_US_ORIGINALS"


class MarketError(Exception):
    pass


# ---------------------------------------------------------------- choosing

def pop_market_flag(argv):
    """-> (code or None, argv without --market X / --market=X)."""
    out, code, i = [], None, 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--market":
            if i + 1 >= len(argv):
                raise MarketError("--market wants US or IN")
            code = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--market="):
            code = arg.split("=", 1)[1]
        else:
            out.append(arg)
        i += 1
    return code, out


def config_market(path=DEFAULT_CONFIG, key="market"):
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return None
    return cfg.get(key) if isinstance(cfg, dict) else None


# Where the bars come from, first match wins: --data kite, TRADER_DATA=kite,
# "data": "kite" in trader/config.json, else yahoo. Kite (Zerodha) serves NSE and
# BSE only, so it needs the IN market; kite.py in the same folder does the work.
DATA_SOURCES = ("yahoo", "kite")
DATA = "yahoo"


def pop_data_flag(argv):
    """-> (source or None, argv without --data X / --data=X)."""
    out, src, i = [], None, 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--data":
            if i + 1 >= len(argv):
                raise MarketError("--data wants yahoo or kite")
            src = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--data="):
            src = arg.split("=", 1)[1]
        else:
            out.append(arg)
        i += 1
    return src, out


def resolve_data(cli=None, code="US", config_path=DEFAULT_CONFIG):
    src = cli or os.environ.get("TRADER_DATA") or config_market(config_path, "data") or "yahoo"
    src = str(src).strip().lower()
    if src in ("zerodha", "kiteconnect"):
        src = "kite"
    if src not in DATA_SOURCES:
        raise MarketError("unknown data source %r: use yahoo or kite" % src)
    if src == "kite" and code != "IN":
        if not cli:
            return "yahoo"      # a standing kite setting does not break US runs
        raise MarketError("--data kite is Zerodha, which serves NSE and BSE only: add --market IN")
    return src


def resolve(cli=None, config_path=DEFAULT_CONFIG):
    code = cli or os.environ.get("TRADER_MARKET") or config_market(config_path) or "US"
    code = str(code).strip().upper()
    if code in ("INDIA", "NSE"):
        code = "IN"
    if code in ("USA", "NYSE", "NASDAQ"):
        code = "US"
    if code not in CODES:
        raise MarketError("unknown market %r: use US or IN" % code)
    return code


def setup(g, argv=None):
    """Call first thing in a tool's main(). Picks the market, applies it to the
    tool's globals and returns argv without the --market and --data flags."""
    global DATA
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        cli, argv = pop_market_flag(argv)
        dcli, argv = pop_data_flag(argv)
        code = resolve(cli)
        DATA = resolve_data(dcli, code)
    except MarketError as e:
        print("%s: %s" % (os.path.basename(g.get("__file__", "tool")), e), file=sys.stderr)
        raise SystemExit(2)
    apply(g, code)
    return argv


# ---------------------------------------------------------------- India's clock

def ist_offset(utc):
    """Hours from UTC for India: always +5:30."""
    return IN["utc_offset_hours"]


def ist_to_utc(local):
    return local - datetime.timedelta(hours=IN["utc_offset_hours"])


def in_to_yahoo(sym):
    """RELIANCE -> RELIANCE.NS; RELIANCE.NS, TCS.BO and ^NSEI stay as they are.
    Unescaped (M&M.NS): quote it with url_symbol() when it goes into a URL."""
    s = sym.strip().upper()
    if s.startswith("^") or s.endswith((".NS", ".BO")):
        return s
    return s + IN["yahoo_suffix"]


def url_symbol(ysym):
    """A Yahoo symbol made safe for a URL path or query: M&M.NS -> M%26M.NS.
    Plain US tickers (TSLA, BRK-B, ^GSPC) come back unchanged."""
    return urllib.parse.quote(ysym, safe="^.-=")


# ---------------------------------------------------------------- applying

def apply(g, code):
    """Swap a tool's module globals to the market `code`. Safe to call again
    (tests switch back and forth): US restores the tool's own values."""
    if _SAVED not in g:
        g[_SAVED] = {k: g[k] for k in PATCHABLE if k in g}
    orig = g[_SAVED]
    for k, v in orig.items():
        g[k] = dict(v) if isinstance(v, dict) else v
    g["MARKET"] = code
    relabel(code == "IN")
    if code != "IN":
        return
    m = IN
    t_open, t_close = datetime.time(*m["open"]), datetime.time(*m["close"])
    o_min = m["open"][0] * 60 + m["open"][1]
    c_min = m["close"][0] * 60 + m["close"][1]
    r_min = m["replay_close"][0] * 60 + m["replay_close"][1]
    new = {
        "NYSE_HOLIDAYS": dict(m["holidays"]),
        "NYSE_EARLY_CLOSE": dict(m["early_close"]),
        "CALENDAR_YEARS": m["calendar_years"],
        "OPEN": t_open, "CLOSE": t_close, "EARLY": t_close,
        "OPEN_MIN": o_min, "CLOSE_MIN": r_min, "EARLY_MIN": r_min,   # the Time Machine's day
        "SESSION_BARS": (r_min - o_min) // 5,
        "FLAT_BY": m["defaults"]["flat_by"],
        "NO_ENTRIES_AFTER": m["defaults"]["no_entries_after"],
        "MIN_PRICE": m["min_price"], "MIN_VOLUME": m["min_volume"],
        "eastern_offset": ist_offset, "eastern_to_utc": ist_to_utc, "to_yahoo": in_to_yahoo,
        "yahoo_symbol": in_to_yahoo,
    }
    for k, v in new.items():
        if k in orig:
            g[k] = v
    if "DEFAULTS" in orig:
        d = dict(orig["DEFAULTS"])
        for k, v in m["defaults"].items():
            if k in d:
                d[k] = v
        g["DEFAULTS"] = d


# ---------------------------------------------------------------- what the user reads
# The tools print New York, ET, NYSE and $ in many sentences. For IN, one filter on
# stdout and stderr says India, IST, NSE and the rupee sign instead. Files the tools
# write keep their numbers; only the words on screen change.

_RULES = (
    (re.compile(r"New York"), IN["place"]),
    (re.compile(r"\bNYSE\b"), IN["exchange"]),
    (re.compile(r"\bET\b"), IN["zone"]),
    (re.compile(r"\$"), IN["currency"]),
)


_KITE_RULES = (
    (re.compile(r"Yahoo, free, can lag"), "Zerodha Kite"),
    (re.compile(r"Yahoo spark, "), "Zerodha Kite, "),
    (re.compile(r"Yahoo's quick spark feed"), "the quick pass"),
    (re.compile(r"Yahoo"), "Zerodha Kite"),
)


def relabel_text(text):
    for rx, new in _RULES:
        text = rx.sub(new, text)
    if DATA == "kite":
        for rx, new in _KITE_RULES:
            text = rx.sub(new, text)
    return text


class _Relabel(object):
    def __init__(self, stream):
        self._stream = stream

    def write(self, text):
        text = relabel_text(text)
        try:
            return self._stream.write(text)
        except UnicodeEncodeError:  # a console that cannot show the rupee sign
            return self._stream.write(text.replace(IN["currency"], "Rs "))

    def __getattr__(self, name):
        return getattr(self._stream, name)


def relabel(on):
    for name in ("stdout", "stderr"):
        cur = getattr(sys, name)
        wrapped = isinstance(cur, _Relabel)
        if on and not wrapped:
            setattr(sys, name, _Relabel(cur))
        elif not on and wrapped:
            setattr(sys, name, cur._stream)
