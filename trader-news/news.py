#!/usr/bin/env python3
"""News monitor for trader-news. Standard library only.

Watches headlines and SEC filings for the stocks you watch or hold and
prints only what is new since the last check, newest first, with how far each
stock has moved today and a flag on anything that touches your tickers.

    python3 news.py                                  # watchlist + plan + portfolio tickers
    python3 news.py --tickers TSLA NVDA AMD
    python3 news.py --tickers TSLA --all --hours 72  # everything in the last 3 days, seen or not
    python3 news.py --no-sec                         # headlines only
    python3 news.py --no-moves                       # skip the price line per ticker

Your tickers, when --tickers is not given: every hit in trader/watchlist.json
(trader-scanner), the symbol in trader/plan.json (trader-exit) and the
positions in trader/portfolio.json.

Sources, one request each per ticker:
  - Yahoo Finance headline RSS:
    https://feeds.finance.yahoo.com/rss/2.0/headline?s=<TICKER>&region=US&lang=en-US
  - SEC EDGAR filings Atom feed for the company (8-K by default: earnings,
    executive changes, deals, restatements):
    https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=<CIK>&type=8-K&count=10&output=atom
    SEC needs a User-Agent with your name and email (sec_user_agent in
    trader/config.json or SEC_USER_AGENT). Without one the SEC part is skipped
    and the tool says so; headlines still run.

  - The move: Yahoo Finance's chart endpoint (unofficial, can lag), the last
    price against yesterday's close:
    https://query2.finance.yahoo.com/v8/finance/chart/<TICKER>?range=1d&interval=5m

Times print in New York time (ET). State: trader/state/news.json remembers
what you have seen (30 days), so an item shows once. Offline: --offline DIR
reads yahoo-<TICKER>.xml, chart-<TICKER>.json, sec-<FORM>-<CIK>.xml and
company_tickers.json from DIR. The tests use it.

India (NSE): tickers ending .NS, or bare tickers with --market IN (or
"market": "IN" in trader/config.json), also read NSE's corporate announcements,
the filings Indian companies must make (results, board meetings, fund raises,
deals, rating changes, resignations):
  - NSE's announcements RSS (today's filings, every company):
    https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml
  - NSE's per-company announcements (older ones too), when NSE lets a script
    in; when it does not, the tool says so and --nse-file reads the same JSON
    saved from a browser:
    https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=<SYMBOL>
  - NSE's equity list, to match a company name to its symbol:
    https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv
SEC filings are skipped for them. Times print in IST.

Headlines are leads, not facts: open the story before acting, and treat
nothing here as advice.
"""

import argparse
import datetime as _dt
import email.utils
import gzip
import hashlib
import json
import os
import re
import sys
import time
import csv
import http.cookiejar
import io
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

YAHOO = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=US&lang=en-US"
CHART = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=5m"
SEC_FEED = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=%s&type=%s"
            "&dateb=&owner=include&count=%d&output=atom")
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
PLACEHOLDER = "you@example.com"
BROWSER_UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
KEEP_DAYS = 30
MOVE_PCT = 3.0  # a stock this far from yesterday's close is flagged as moving

ITEMS_8K = {
    "1.01": "material agreement", "1.02": "agreement ended", "1.03": "bankruptcy",
    "1.05": "cybersecurity incident", "2.01": "acquisition or sale of assets",
    "2.02": "earnings results", "2.03": "new debt", "2.04": "debt acceleration",
    "2.05": "restructuring costs", "2.06": "impairment", "3.01": "delisting notice",
    "3.02": "unregistered share sale", "3.03": "shareholder rights change",
    "4.01": "auditor change", "4.02": "past financials no longer reliable",
    "5.01": "change in control", "5.02": "director or officer change",
    "5.03": "bylaws or fiscal year change", "5.07": "shareholder vote results",
    "7.01": "Regulation FD disclosure", "8.01": "other events", "9.01": "exhibits",
}
IMPORTANT_ITEMS = {"1.03", "1.05", "2.02", "2.04", "2.06", "3.01", "4.02", "5.01", "5.02"}
KEYWORDS = [
    ("earnings", r"\bearnings\b|\bresults\b|\bEPS\b|\bguidance\b|\bforecast\b"),
    ("analyst", r"\bupgrade[sd]?\b|\bdowngrade[sd]?\b|price target|\boutperform\b|\bunderperform\b"),
    ("legal", r"\blawsuit\b|\bsued\b|\bprobe\b|\binvestigation\b|\bSEC charges\b|\bantitrust\b|\bjudge\b|\bcourt\b"),
    ("deal", r"\bacqui(?:re|res|red|sition)\b|\bmerger\b|\bbuyout\b|\btakeover\b|\bdeal\b"),
    ("capital", r"\bdividend\b|\bbuyback\b|\brepurchase\b|\bstock split\b|\boffering\b"),
    ("people", r"\bCEO\b|\bCFO\b|\bresign|\bsteps down\b|\bappoint"),
    ("risk", r"\brecall\b|\bbankrupt|\bdefault\b|\bdelist|\bhack|\bbreach\b|\boutage\b"),
    ("halt", r"\bhalt(?:s|ed)?\b|\btrading (?:pause|suspen)"),
    ("regulatory", r"\bFDA\b|\bapproval\b|\bapproves\b|\bclinical\b|\bFTC\b|\bDOJ\b"),
    ("macro", r"\bFed\b|\bFOMC\b|\bCPI\b|\binflation\b|\bjobs report\b|\bpayrolls\b|\btariffs?\b|\bfutures\b"),
]


class NewsError(Exception):
    pass


# ---------------------------------------------------------------- NSE (India)

NSE_RSS = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
NSE_LIST = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
NSE_HOME = "https://www.nseindia.com/"
NSE_API = "https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=%s"
NSE_PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
# NSE's servers stall requests that name themselves a script; they answer a browser's string
NSE_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/120 Safari/537.36")
IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
# subjects that can move a price: shown with "!"
NSE_IMPORTANT = re.compile(
    r"financial result|outcome of board meeting|board meeting intimation|fund ?rais|preferential|"
    r"\bqip\b|qualified institution|rights issue|allotment|acquisition|amalgamation|merger|"
    r"scheme of arrangement|credit rating|resignation|cessation|change in (?:management|control|director)|"
    r"key managerial|"
    r"\bdefault\b|insolvency|\bnclt\b|fraud|search|pledge|open offer|delisting|buy ?back|"
    r"bulk deal|block deal|award of order|receipt of order|bagging|litigation|penalty", re.I)
NSE_KEYWORDS = [
    ("earnings", r"financial result|\bresults?\b"),
    ("capital", r"fund ?rais|preferential|\bqip\b|rights issue|allotment|dividend|buy ?back|warrant"),
    ("deal", r"acquisition|amalgamation|merger|scheme of arrangement|open offer|award of order|"
             r"receipt of order|bagging"),
    ("people", r"resignation|cessation|appointment|change in management"),
    ("risk", r"credit rating|\bdefault\b|insolvency|\bnclt\b|fraud|pledge|search|penalty|litigation"),
    ("regulatory", r"sebi|regulation 30|\brbi\b"),
]


def is_indian(t):
    return t.upper().endswith((".NS", ".BO"))


def nse_symbol(t):
    t = t.upper()
    return t[:-3] if is_indian(t) else t


def _norm_name(name):
    n = re.sub(r"[^a-z0-9& ]+", " ", (name or "").lower())
    n = re.sub(r"\b(?:limited|ltd|the|india|pvt|private|co|company|corporation|corp)\b", " ", n)
    return " ".join(n.split())


def parse_nse_list(data):
    """NSE's EQUITY_L.csv -> {SYMBOL: company name}."""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    out = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) >= 2 and row[0].strip() and row[0].strip() != "SYMBOL":
            out[row[0].strip().upper()] = row[1].strip()
    return out


def nse_time(value):
    """NSE's '20-Nov-2026 12:04:00' (IST), or '2026-11-20 12:04:00', -> aware UTC."""
    v = (value or "").strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y %H:%M", "%d%m%Y%H%M%S"):
        try:
            return _dt.datetime.strptime(v, fmt).replace(tzinfo=IST).astimezone(_dt.timezone.utc)
        except ValueError:
            continue
    return parse_time(v)


def _nse_item(ticker, sym, company, subject, text, link, when, key):
    title = "%s: %s" % (company or sym, subject or (text or "")[:120])
    hay = "%s %s" % (subject, text)
    return {"id": "nse:" + key, "source": "NSE filing", "kind": "filing", "form": "NSE announcement",
            "ticker": ticker, "title": title.strip(), "summary": text or "", "items": [],
            "important": bool(NSE_IMPORTANT.search(hay)), "link": link or NSE_PAGE, "time": when}


def parse_nse_rss(data, wanted, names):
    """NSE's announcements RSS -> items for the wanted tickers. A row belongs to a ticker when its
    description starts with 'SYMBOL :', its file is named SYMBOL_..., or its title is the company."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("NSE announcements RSS is not valid XML: %s" % exc)
    by_name = {}
    for t in wanted:
        n = _norm_name(names.get(nse_symbol(t), ""))
        if n:
            by_name[n] = t
    out = []
    for it in root.iter("item"):
        title = _text(it, "title")
        desc = _text(it, "description")
        link = _text(it, "link")
        when = nse_time(_text(it, "pubDate"))
        m = re.match(r"^\s*([A-Z0-9&\-]{1,20})\s+:\s*(.*)$", desc, re.S)
        sym_in_desc = m.group(1) if m else ""
        body = m.group(2) if m else desc
        fname = link.rsplit("/", 1)[-1].upper() if link else ""
        hit = None
        for t in wanted:
            s = nse_symbol(t)
            if sym_in_desc == s or (fname and fname.startswith(s + "_")):
                hit = t
                break
        if hit is None:
            hit = by_name.get(_norm_name(title))
        if hit is None:
            continue
        key = hashlib.sha1(("%s|%s|%s" % (title, desc, _text(it, "pubDate"))).encode("utf-8")).hexdigest()[:16]
        m2 = re.search(r"informed the Exchange (?:about|regarding|that)\s+(.+?)(?:\.\s|$)", body, re.S)
        subject = (m2.group(1) if m2 else re.split(r"(?<=[a-z])\.\s", body.strip(), 1)[0]).strip().rstrip(".")[:160]
        out.append(_nse_item(hit, nse_symbol(hit), title, subject, body.strip(), link, when, key))
    return out


def parse_nse_json(data, ticker):
    """NSE's corporate-announcements JSON (the live API or a file saved from a browser) -> items."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
    except ValueError:
        raise NewsError("NSE announcements for %s are not JSON (NSE may have sent a block page)" % ticker)
    rows = doc.get("data", doc) if isinstance(doc, dict) else doc
    if not isinstance(rows, list):
        raise NewsError("NSE announcements for %s are not the shape this tool knows" % ticker)
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("symbol") or nse_symbol(ticker)).upper()
        if sym != nse_symbol(ticker):
            continue
        subject = r.get("desc") or r.get("subject") or ""
        text = r.get("attchmntText") or r.get("details") or ""
        when = nse_time(r.get("an_dt") or r.get("sort_date") or r.get("dt") or "")
        key = str(r.get("seq_id") or hashlib.sha1(json.dumps(r, sort_keys=True).encode()).hexdigest()[:16])
        out.append(_nse_item(ticker, sym, r.get("sm_name") or sym, subject, text, r.get("attchmntFile"),
                             when, key))
    return out


def nse_company_json(sym, timeout=20):
    """NSE's per-company announcements. NSE wants its own cookies first: one visit to the home
    page, then the API. It often refuses scripts; the caller says so and falls back."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    hdr = {"User-Agent": NSE_UA, "Accept": "application/json, text/plain, */*",
           "Accept-Language": "en-US,en;q=0.9", "Referer": NSE_PAGE}
    try:
        opener.open(urllib.request.Request(NSE_HOME, headers=hdr), timeout=timeout).read()
        with opener.open(urllib.request.Request(NSE_API % urllib.request.quote(sym), headers=hdr),
                         timeout=timeout) as resp:
            data = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
            return data
    except urllib.error.HTTPError as exc:
        raise NewsError("HTTP %s" % exc.code)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise NewsError("could not reach www.nseindia.com: %s" % (getattr(exc, "reason", None) or exc or
                                                                   type(exc).__name__))


def nse_names(fetcher, state, today):
    cache = state.get("nse_names") or {}
    if cache.get("date") == today and cache.get("names"):
        return cache["names"]
    names = parse_nse_list(fetcher.get(NSE_LIST, "nse-EQUITY_L.csv"))
    state["nse_names"] = {"date": today, "names": names}
    return names


def collect_nse(fetcher, india, state, now, nse_files=(), company_api=True, errors=None):
    """NSE announcements for the Indian tickers: today's RSS, then each company's own list."""
    errors = [] if errors is None else errors
    items = []
    try:
        names = nse_names(fetcher, state, now.astimezone(IST).date().isoformat())
    except NewsError as exc:
        errors.append("NSE equity list (company names): %s" % exc)
        names = {}
    try:
        items += parse_nse_rss(fetcher.get(NSE_RSS, "nse-announcements.xml"), india, names)
    except NewsError as exc:
        if not (fetcher.offline and "no file" in str(exc)):
            errors.append("NSE announcements RSS: %s" % exc)
    for path in nse_files or ():
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            errors.append("--nse-file %s: %s" % (path, exc))
            continue
        for t in india:
            try:
                items += parse_nse_json(data, t)
            except NewsError as exc:
                errors.append(str(exc))
    if company_api and not fetcher.offline and not nse_files:
        refused, why = [], ""
        for t in sorted(india):
            sym = nse_symbol(t)
            try:
                items += parse_nse_json(nse_company_json(sym), t)
            except NewsError as exc:
                refused.append(sym)
                why = why or str(exc)
                if "403" in why or "could not reach" in why:      # the same answer for the rest
                    refused += [nse_symbol(x) for x in sorted(india) if nse_symbol(x) not in refused]
                    break
        if refused:
            errors.append("NSE's per-company announcements were not reachable (%s), so only today's NSE RSS was "
                          "read for %s. For older ones: open %s (any symbol) in a browser, save it, and pass "
                          "--nse-file FILE" % (why, ", ".join(refused), NSE_API % refused[0]))
    return items


# ---------------------------------------------------------------- time

def _nth_sunday(year, month, n):
    d = _dt.date(year, month, 1)
    d += _dt.timedelta(days=(6 - d.weekday()) % 7)
    return d + _dt.timedelta(weeks=n - 1)


def eastern_offset(utc):
    """Hours from UTC for New York: -4 in daylight time, -5 otherwise (US rule since 2007)."""
    y = utc.year
    start = _dt.datetime.combine(_nth_sunday(y, 3, 2), _dt.time(7, 0))   # 2:00 EST
    end = _dt.datetime.combine(_nth_sunday(y, 11, 1), _dt.time(6, 0))    # 2:00 EDT
    naive = utc.replace(tzinfo=None)
    return -4 if start <= naive < end else -5


def to_eastern(utc):
    """Aware or naive UTC -> naive New York wall-clock time."""
    if utc.tzinfo is not None:
        utc = utc.astimezone(_dt.timezone.utc)
    return utc.replace(tzinfo=None) + _dt.timedelta(hours=eastern_offset(utc))


# ---------------------------------------------------------------- inputs

def load_json(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def watch_from_files(portfolio="trader/portfolio.json", watchlist="trader/watchlist.json",
                     plan="trader/plan.json"):
    """{ticker: name or None}: the scanner's hits, the planned trade and your holdings."""
    out = {}
    wl = load_json(watchlist) or {}
    for h in wl.get("hits", []):
        sym = h.get("symbol") or h.get("ticker")
        if sym:
            out[sym.upper()] = h.get("name")
    pl = load_json(plan) or {}
    if pl.get("symbol"):
        out.setdefault(pl["symbol"].upper(), pl.get("name"))
    pf = load_json(portfolio) or {}
    for p in pf.get("positions", []):
        sym = p.get("ticker") or p.get("symbol")
        if sym:
            out[sym.upper()] = p.get("name") or out.get(sym.upper())
    return out


def sec_user_agent(cli=None, config="trader/config.json"):
    ua = (cli or os.environ.get("SEC_USER_AGENT") or "").strip()
    if not ua:
        cfg = load_json(config) or {}
        ua = str(cfg.get("sec_user_agent") or "").strip()
    if not ua or "@" not in ua or PLACEHOLDER in ua:
        return None
    return ua


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    def __init__(self, sec_ua=None, offline=None, pause=0.15):
        self.sec_ua = sec_ua
        self.offline = offline
        self.pause = pause
        self._last = 0.0

    def get(self, url, name, sec=False):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise NewsError("offline: no file %s" % path)
            with open(path, "rb") as fh:
                return fh.read()
        if sec and not self.sec_ua:
            raise NewsError("no SEC User-Agent set")
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        ua = self.sec_ua if sec else (NSE_UA if "nseindia.com" in url else BROWSER_UA)
        headers = {"User-Agent": ua, "Accept-Encoding": "gzip"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as exc:
            raise NewsError("HTTP %s for %s" % (exc.code, url))
        except urllib.error.URLError as exc:
            raise NewsError("could not reach %s: %s" % (url, exc.reason))
        except (OSError, TimeoutError) as exc:      # a stalled read: a timeout, a reset
            raise NewsError("%s did not answer: %s" % (url.split("/")[2], exc or type(exc).__name__))


def yahoo_symbol(ticker):
    t = ticker.upper()
    if t.endswith((".NS", ".BO")):
        return t[:-3].replace(".", "-") + t[-3:]      # RELIANCE.NS stays; BRK.B -> BRK-B
    return t.replace(".", "-")


def cik_map(fetcher, state):
    """ticker -> 10-digit CIK, from SEC's company_tickers.json (cached in state)."""
    cached = state.setdefault("ciks", {})
    if cached:
        return cached
    data = json.loads(fetcher.get(TICKERS_URL, "company_tickers.json", sec=True).decode("utf-8"))
    for v in data.values():
        cached.setdefault(v["ticker"].upper().replace("-", "."), str(v["cik_str"]).zfill(10))
    return cached


# ---------------------------------------------------------------- parsing

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _text(el, name):
    for c in el.iter():
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def parse_time(value):
    """RFC 822 (RSS) or ISO 8601 (Atom) -> aware UTC datetime, or None."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        d = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc)


def parse_rss(data, ticker):
    """Yahoo headline RSS -> items."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("RSS for %s is not valid XML: %s" % (ticker, exc))
    out = []
    for it in root.iter("item"):
        title = _text(it, "title")
        link = _text(it, "link")
        guid = _text(it, "guid") or link
        out.append({"id": "rss:" + guid, "source": "Yahoo Finance", "kind": "headline",
                    "ticker": ticker, "title": title, "summary": _text(it, "description"),
                    "link": link, "time": parse_time(_text(it, "pubDate"))})
    return out


def parse_sec_atom(data, ticker):
    """EDGAR company filings Atom -> items."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("SEC feed for %s is not valid XML: %s" % (ticker, exc))
    company = ""
    for el in root.iter():
        if _local(el.tag) == "conformed-name":
            company = (el.text or "").strip()
            break
    out = []
    for entry in root.iter():
        if _local(entry.tag) != "entry":
            continue
        form = _text(entry, "filing-type")
        acc = _text(entry, "accession-number")
        items = re.findall(r"\d+\.\d+", _text(entry, "items-desc"))
        labels = [ITEMS_8K.get(i, "item " + i) for i in items if i != "9.01"]
        link = ""
        for c in entry.iter():
            if _local(c.tag) == "link":
                link = c.get("href", "")
        title = "%s filed a %s" % (company or ticker, form)
        if labels:
            title += ": " + ", ".join(labels)
        out.append({"id": "sec:" + acc, "source": "SEC EDGAR", "kind": "filing", "form": form,
                    "ticker": ticker, "title": title, "summary": _text(entry, "form-name"),
                    "items": items, "important": bool(set(items) & IMPORTANT_ITEMS),
                    "link": link, "time": parse_time(_text(entry, "updated"))})
    return out


def parse_chart(data, ticker):
    """Yahoo chart JSON -> {"ticker", "price", "prev_close", "pct", "time"} or raise."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        meta = doc["chart"]["result"][0]["meta"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise NewsError("chart for %s is not usable" % ticker)
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if not price or not prev:
        raise NewsError("chart for %s has no price" % ticker)
    when = meta.get("regularMarketTime")
    return {"ticker": ticker, "price": round(float(price), 2), "prev_close": round(float(prev), 2),
            "pct": round((float(price) / float(prev) - 1) * 100, 2),
            "time": _dt.datetime.fromtimestamp(when, _dt.timezone.utc) if when else None}


def collect_moves(fetcher, watch, errors=None):
    errors = [] if errors is None else errors
    out = []
    for t in sorted(watch):
        name = "chart-%s.json" % t
        try:
            try:
                data = fetcher.get(CHART % ("query2", yahoo_symbol(t)), name)
            except NewsError as exc:
                if fetcher.offline or not re.search(r"HTTP (429|5\d\d)", str(exc)):
                    raise
                data = fetcher.get(CHART % ("query1", yahoo_symbol(t)), name)
            out.append(parse_chart(data, t))
        except NewsError as exc:
            if not (fetcher.offline and "no file" in str(exc)):
                errors.append("price for %s: %s" % (t, exc))
    return out


def mention_patterns(watch):
    pats = {}
    for t, name in watch.items():
        alts = [r"\(%s\)" % re.escape(t), r"\$%s\b" % re.escape(t),
                r"\b(?:NYSE|NASDAQ|Nasdaq)\s*:\s*%s\b" % re.escape(t)]
        if name and len(name) >= 4:
            alts.append(r"\b%s\b" % re.escape(name).replace(r"\ ", r"[\s-]"))
        pats[t] = re.compile("|".join(alts))
    return pats


def tag(items, watch):
    """Which of your tickers each item touches, and why it matters."""
    pats = mention_patterns(watch)
    for it in items:
        hay = "%s %s" % (it["title"], it.get("summary") or "")
        touches = {it["ticker"]} if it["ticker"] in watch else set()
        for t, rx in pats.items():
            if rx.search(hay):
                touches.add(t)
        it["touches"] = sorted(touches)
        kw = NSE_KEYWORDS if it["id"].startswith("nse:") else KEYWORDS
        it["tags"] = [label for label, rx in kw if re.search(rx, hay, re.I)]
        if it["kind"] == "filing" and "2.02" in it.get("items", []) and "earnings" not in it["tags"]:
            it["tags"].insert(0, "earnings")
    return items


def dedupe(items):
    """One item per story: same id, or same link, or same title the same day."""
    seen, out = {}, []
    for it in items:
        keys = [it["id"], it["link"],
                "%s|%s" % (re.sub(r"\W+", " ", it["title"].lower()).strip(),
                           it["time"].date() if it["time"] else "")]
        hit = next((seen[k] for k in keys if k and k in seen), None)
        if hit is not None:
            for t in it.get("touches", []):
                if t not in hit["touches"]:
                    hit["touches"].append(t)
            hit["touches"].sort()
            continue
        for k in keys:
            if k:
                seen[k] = it
        out.append(it)
    return out


def fingerprint(it):
    return hashlib.sha1(it["id"].encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- the check

def collect(fetcher, watch, state, sec=True, forms=("8-K",), count=10, errors=None):
    errors = [] if errors is None else errors
    items = []
    us = {t: n for t, n in watch.items() if not is_indian(t)}
    for t in sorted(watch):
        try:
            items += parse_rss(fetcher.get(YAHOO % yahoo_symbol(t), "yahoo-%s.xml" % t), t)
        except NewsError as exc:
            errors.append("headlines for %s: %s" % (t, exc))
    if sec and us:
        try:
            ciks = cik_map(fetcher, state)
        except NewsError as exc:
            errors.append("SEC ticker list: %s" % exc)
            ciks = {}
        for t in sorted(us):
            cik = ciks.get(t)
            if not cik:
                if ciks:
                    errors.append("no SEC CIK for %s" % t)
                continue
            for form in forms:
                try:
                    data = fetcher.get(SEC_FEED % (cik, form, count),
                                       "sec-%s-%s.xml" % (form, cik), sec=True)
                    items += parse_sec_atom(data, t)
                except NewsError as exc:
                    errors.append("SEC %s for %s: %s" % (form, t, exc))
    return items


def select(items, state, now, hours=48, show_all=False):
    """Items within the window; only unseen ones unless show_all."""
    seen = state.setdefault("seen", {})
    cutoff = now - _dt.timedelta(hours=hours)
    ahead = now + _dt.timedelta(minutes=10)  # a pinned --now: nothing from after it
    out = []
    for it in items:
        if it["time"] is not None and (it["time"] < cutoff or it["time"] > ahead):
            continue
        fp = fingerprint(it)
        it["new"] = fp not in seen
        if it["new"] or show_all:
            out.append(it)
    out.sort(key=lambda i: (i["time"] or now), reverse=True)
    return out


def remember(items, state, now):
    seen = state.setdefault("seen", {})
    stamp = now.isoformat()
    for it in items:
        seen.setdefault(fingerprint(it), stamp)
    cutoff = (now - _dt.timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["last_check"] = stamp


ZONE = {"name": "ET"}


def local_time(d):
    return d.astimezone(IST).replace(tzinfo=None) if ZONE["name"] == "IST" else to_eastern(d)


def render_moves(moves, move_pct=MOVE_PCT):
    out = []
    for m in moves:
        flag = "MOVING" if abs(m["pct"]) >= move_pct else ""
        when = (" at %s %s" % (local_time(m["time"]).strftime("%H:%M"), ZONE["name"])) if m.get("time") else ""
        out.append("%-6s %s %+.2f%% vs yesterday's close %s%s  %s" % (
            m["ticker"], "{:,.2f}".format(m["price"]), m["pct"], "{:,.2f}".format(m["prev_close"]),
            when, flag))
    return out


def render(items, errors, now, moves=None, move_pct=MOVE_PCT):
    out = render_moves(moves or [], move_pct)
    if out:
        out.append("")
    if not items:
        out.append("Nothing new.")
    for it in items:
        when = (local_time(it["time"]).strftime("%Y-%m-%d %H:%M ") + ZONE["name"]) if it["time"] else "time unknown"
        mark = "!" if it.get("important") else " "
        who = ",".join(it["touches"]) or it["ticker"]
        out.append("%s %s  %-11s %-13s %s" % (mark, when, who, it["source"], it["title"]))
        extra = []
        if it["tags"]:
            extra.append("tags: " + ", ".join(it["tags"]))
        extra.append(it["link"])
        out.append("      " + "  ".join(extra))
    for e in errors:
        out.append("note: %s" % e)
    out.append("Checked %s. Headlines are leads, not facts; open the story before acting. "
               "Not financial advice." % (local_time(now).strftime("%Y-%m-%d %H:%M ") + ZONE["name"]))
    return "\n".join(out) + "\n"


def _json_ready(items):
    out = []
    for it in items:
        d = dict(it)
        d["time"] = it["time"].isoformat() if it["time"] else None
        out.append(d)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tickers", nargs="*", help="default: watchlist + plan + portfolio")
    ap.add_argument("--portfolio", default="trader/portfolio.json")
    ap.add_argument("--watchlist", default="trader/watchlist.json")
    ap.add_argument("--plan", default="trader/plan.json")
    ap.add_argument("--state", default="trader/state/news.json")
    ap.add_argument("--hours", type=float, default=48, help="ignore items older than this")
    ap.add_argument("--all", action="store_true", help="show seen items too")
    ap.add_argument("--forms", default="8-K", help="SEC forms to watch, comma separated")
    ap.add_argument("--no-sec", action="store_true", help="headlines only")
    ap.add_argument("--no-moves", action="store_true", help="skip the price line per ticker")
    ap.add_argument("--move-pct", type=float, default=MOVE_PCT, help="flag a move this big (percent)")
    ap.add_argument("--user-agent", help="SEC User-Agent: \"Your Name you@example.com\"")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--now", help="ISO time, for reproducible runs")
    ap.add_argument("--dry-run", action="store_true", help="do not update the state file")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--market", type=str.upper, choices=["US", "IN"],
                    help="IN: bare tickers are NSE symbols (UGROCAP means UGROCAP.NS); default from config")
    ap.add_argument("--nse-file", action="append", metavar="FILE",
                    help="NSE corporate-announcements JSON saved from a browser (repeatable)")
    ap.add_argument("--no-nse-company", action="store_true",
                    help="skip NSE's per-company page (it often refuses scripts); the RSS still runs")
    args = ap.parse_args(argv)
    market = args.market or os.environ.get("TRADER_MARKET", "").upper() or \
        str((load_json(args.config) or {}).get("market") or "US").upper()

    if args.tickers:
        watch = {(t.upper() + ".NS" if market == "IN" and not is_indian(t) and not t.startswith("^")
                  else t.upper()): None for t in args.tickers}
        known = watch_from_files(args.portfolio, args.watchlist, args.plan)
        watch.update({t: known[t] for t in watch if known.get(t)})
    else:
        watch = watch_from_files(args.portfolio, args.watchlist, args.plan)
    if not watch:
        print("news: no tickers. Pass --tickers, or run /trader-scanner first (it writes "
              "trader/watchlist.json).", file=sys.stderr)
        return 2
    now = parse_time(args.now) if args.now else _dt.datetime.now(_dt.timezone.utc)
    india = sorted(t for t in watch if is_indian(t))
    ZONE["name"] = "IST" if india and len(india) == len(watch) else "ET"
    state = load_json(args.state) or {}
    errors = []
    ua = sec_user_agent(args.user_agent, args.config)
    sec = not args.no_sec
    if sec and not ua and not args.offline and len(india) < len(watch):
        errors.append("SEC filings skipped: set sec_user_agent in trader/config.json or "
                      "SEC_USER_AGENT (\"Your Name you@example.com\") to include them")
        sec = False
    fetcher = Fetcher(ua, offline=args.offline)
    forms = [f.strip().upper() for f in args.forms.split(",") if f.strip()]
    items = collect(fetcher, watch, state, sec=sec, forms=forms, errors=errors)
    if india:
        items += collect_nse(fetcher, india, state, now, args.nse_file, not args.no_nse_company, errors)
    items = dedupe(tag(items, watch))
    shown = select(items, state, now, hours=args.hours, show_all=args.all)
    moves = [] if args.no_moves else collect_moves(fetcher, watch, errors)
    if args.json:
        mv = [dict(m, time=m["time"].isoformat() if m.get("time") else None,
                   moving=abs(m["pct"]) >= args.move_pct) for m in moves]
        print(json.dumps({"checked": now.isoformat(), "moves": mv, "items": _json_ready(shown),
                          "notes": errors}, indent=2))
    else:
        sys.stdout.write(render(shown, errors, now, moves, args.move_pct))
    if not args.dry_run:
        remember(items, state, now)
        folder = os.path.dirname(args.state)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.state, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
            fh.write("\n")
    return 0 if any(i["new"] for i in shown) else 1


if __name__ == "__main__":
    sys.exit(main())
