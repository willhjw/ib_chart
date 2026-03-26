from __future__ import annotations

import os
import sys
import json
import asyncio
import math
import threading
import time
import queue
import csv
from urllib.parse import quote_plus
from urllib.request import Request, urlopen
from collections import defaultdict
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, Response, jsonify, request, stream_with_context
from flask_cors import CORS
from ib_insync import IB, Stock

BASE_DIR = Path(__file__).parent
ET = ZoneInfo("America/New_York")

IB_HOST = os.getenv("IB_HOST", "127.0.0.1")
IB_PORT = int(os.getenv("IB_PORT", "4001"))
IB_CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "1001"))
LOCAL_DAILY_DIR = Path(os.getenv("IB_CHART_LOCAL_DAILY_DIR", r"D:\US_stocks_daily_data\listed stocks from 2000_cleaned"))

app = Flask(__name__)
CORS(app)

_ib_queue: queue.Queue = queue.Queue()
_ib_event_hooked = False
_last_ib_error = ""

_ticker_by_symbol: dict = {}
_bars_1m: dict = defaultdict(dict)
_bars_5m: dict = defaultdict(dict)
_daily_cache: dict = {}
_DAILY_CACHE_TTL = 300.0
_intraday_seeded: dict = {}
_quote_desc_cache: dict[str, str] = {}

# EPS/Revenue (quarter history) cache for UI responsiveness.
# Keyed by: f"{symbol}|{quarters}"
_eps_rev_cache = {}
_eps_rev_cache_ttl_sec = 15 * 60  # 15 minutes


def _normalize_ticker(ticker: str) -> str:
    t = (ticker or "").upper().strip()
    if t.endswith(".HK"):
        t = t[:-3].strip()
    if t.isdigit():
        t = str(int(t))
    return t


def _market_for_ticker(ticker: str) -> tuple[str, str]:
    if ticker.isdigit():
        return "SEHK", "HKD"
    return "SMART", "USD"


def _safe_float(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _safe_int(v):
    try:
        return int(v)
    except Exception:
        return 0


def _is_placeholder_description(desc: str, sym: str) -> bool:
    d = (desc or "").strip()
    s = (sym or "").strip().upper()
    if not d:
        return True
    du = d.upper()
    if du == s:
        return True
    # Common useless placeholders from some quote sources.
    if du in {"N/A", "NA", "-", "--", "UNKNOWN"}:
        return True
    return False


def _fetch_company_name_http(sym: str) -> str:
    """Fallback resolver from Yahoo search endpoint (no auth)."""
    try:
        url = f"https://query1.finance.yahoo.com/v1/finance/search?q={quote_plus(sym)}&quotesCount=10&newsCount=0"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
        quotes = payload.get("quotes") or []
        best = ""
        for q in quotes:
            if str(q.get("symbol", "")).upper() != sym:
                continue
            qt = str(q.get("quoteType", "")).upper()
            if qt and qt != "EQUITY":
                continue
            name = str(q.get("longname") or q.get("shortname") or "").strip()
            if not name:
                continue
            if _is_placeholder_description(name, sym):
                continue
            # Prefer names containing a space (more likely real company name).
            if " " in name:
                return name
            if not best:
                best = name
        return best
    except Exception:
        return ""


def _bucket_ms(dt: datetime, minutes: int) -> int:
    dt = dt.astimezone(ET).replace(second=0, microsecond=0)
    minute = dt.minute - (dt.minute % minutes)
    dt = dt.replace(minute=minute)
    return int(dt.timestamp() * 1000)


def _touch_bar(container: dict, ts_ms: int, price: float, size: int):
    b = container.get(ts_ms)
    if b is None:
        container[ts_ms] = {"open": price, "high": price, "low": price,
                            "close": price, "volume": max(size, 0)}
        return
    b["high"] = max(b["high"], price)
    b["low"] = min(b["low"], price)
    b["close"] = price
    b["volume"] += max(size, 0)


def _on_pending_tickers(tickers):
    now_et = datetime.now(ET)
    for t in tickers:
        sym = getattr(getattr(t, "contract", None), "symbol", None)
        if not sym:
            continue
        sym = sym.upper()
        price = _safe_float(getattr(t, "last", None))
        if price is None:
            price = _safe_float(getattr(t, "marketPrice", lambda: None)())
        if price is None:
            continue
        size = _safe_int(getattr(t, "lastSize", 0))
        _touch_bar(_bars_1m[sym], _bucket_ms(now_et, 1), price, size)
        _touch_bar(_bars_5m[sym], _bucket_ms(now_et, 5), price, size)


def _is_us_trading_day(d: date) -> bool:
    """Simple weekday check — good enough to skip Sat/Sun."""
    return d.weekday() < 5


def _ib_worker():
    global _ib_event_hooked, _last_ib_error

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    ib = IB()

    def _ensure_connected():
        global _last_ib_error, _ib_event_hooked
        if ib.isConnected():
            return
        last_err = None
        for _ in range(2):
            try:
                ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID,
                           timeout=4, readonly=True)
                break
            except Exception as e:
                last_err = e
                try:
                    ib.disconnect()
                except Exception:
                    pass
                time.sleep(0.2)
        if not ib.isConnected():
            _last_ib_error = f"{type(last_err).__name__}: {last_err}" if last_err else "connect failed"
            raise ConnectionError(f"IB connect failed: {_last_ib_error}")
        if not _ib_event_hooked:
            ib.pendingTickersEvent += _on_pending_tickers
            _ib_event_hooked = True
        _last_ib_error = ""

    def _do_subscribe(sym):
        _ensure_connected()
        if sym in _ticker_by_symbol:
            _seed_intraday_from_historical(sym, "1m")
            _seed_intraday_from_historical(sym, "5m")
            return
        exch, cur = _market_for_ticker(sym)
        contract = Stock(sym, exch, cur)
        try:
            q = ib.qualifyContracts(contract)
            if q:
                contract = q[0]
        except Exception:
            pass
        ticker = ib.reqMktData(contract, genericTickList="",
                               snapshot=False, regulatorySnapshot=False)
        _ticker_by_symbol[sym] = ticker
        _seed_intraday_from_historical(sym, "1m", contract=contract)
        _seed_intraday_from_historical(sym, "5m", contract=contract)

    def _seed_intraday_from_historical(sym, interval: str, contract=None):
        # Backfill today's bars so entering mid-session still shows complete 1m/5m series.
        today = datetime.now(ET).strftime("%Y-%m-%d")
        key = (sym, interval, today)
        if _intraday_seeded.get(key):
            return

        exch, cur = _market_for_ticker(sym)
        c = contract or Stock(sym, exch, cur)
        if contract is None:
            try:
                q = ib.qualifyContracts(c)
                if q:
                    c = q[0]
            except Exception:
                pass

        bar_size = "1 min" if interval == "1m" else "5 mins"
        minutes = 1 if interval == "1m" else 5
        target = _bars_1m[sym] if interval == "1m" else _bars_5m[sym]

        bars = []
        # Prefer extended-hours historical bars first (pre/after market),
        # then fallback to RTH-only if needed.
        for what, rth in (
            ("TRADES", False),
            ("MIDPOINT", False),
            ("TRADES", True),
            ("MIDPOINT", True),
        ):
            try:
                bars = ib.reqHistoricalData(
                    c,
                    endDateTime="",
                    durationStr="5 D",
                    barSizeSetting=bar_size,
                    whatToShow=what,
                    useRTH=rth,
                    formatDate=1,
                    timeout=8,
                )
            except Exception:
                bars = []
            if bars:
                break

        now_day = datetime.now(ET).date()
        parsed = []
        for b in bars or []:
            dt = getattr(b, "date", None)
            if hasattr(dt, "astimezone"):
                dt_et = dt.astimezone(ET)
            elif hasattr(dt, "replace"):
                # naive datetime/date
                try:
                    dt_et = dt.replace(tzinfo=ET)
                except Exception:
                    continue
            else:
                continue
            o = _safe_float(getattr(b, "open", None))
            h = _safe_float(getattr(b, "high", None))
            l = _safe_float(getattr(b, "low", None))
            c0 = _safe_float(getattr(b, "close", None))
            if None in (o, h, l, c0):
                continue
            parsed.append((dt_et, {
                "open": o,
                "high": h,
                "low": l,
                "close": c0,
                "volume": int(getattr(b, "volume", 0) or 0),
            }))

        if not parsed:
            # Do not lock out retries when IB temporarily returns empty.
            return

        # Prefer today's bars; if unavailable (e.g. late night/off-session), fallback to last session.
        days = sorted({dt.date() for dt, _ in parsed})
        seed_day = now_day if now_day in days else days[-1]
        for dt_et, row in parsed:
            if dt_et.date() != seed_day:
                continue
            ts_ms = _bucket_ms(dt_et, minutes)
            if ts_ms in target:
                continue
            target[ts_ms] = row

        _intraday_seeded[key] = True

    def _get_live_price(sym):
        """Return (last_price, prev_close) from ticker, or (None, None)."""
        t = _ticker_by_symbol.get(sym)
        if t is None:
            return None, None
        last = _safe_float(getattr(t, "last", None))
        if last is None:
            last = _safe_float(getattr(t, "marketPrice", lambda: None)())
        prev_close = _safe_float(getattr(t, "close", None))
        return last, prev_close

    def _do_historical(sym, period_str):
        cache_key = (sym, period_str)
        now_ts = time.time()
        cached = _daily_cache.get(cache_key)
        if cached and (now_ts - cached["ts"] <= _DAILY_CACHE_TTL):
            return cached["bars"]

        _ensure_connected()

        exch, cur = _market_for_ticker(sym)
        contract = Stock(sym, exch, cur)
        duration = {"1y": "1 Y", "2y": "2 Y", "3y": "3 Y",
                    "5y": "5 Y", "10y": "10 Y"}.get(period_str, "1 Y")

        try:
            q = ib.qualifyContracts(contract)
            if q:
                contract = q[0]
        except Exception:
            pass

        bars = []
        for what, rth in (
            ("TRADES", True), ("ADJUSTED_LAST", True), ("MIDPOINT", True),
            ("TRADES", False), ("ADJUSTED_LAST", False), ("MIDPOINT", False),
        ):
            try:
                bars = ib.reqHistoricalData(
                    contract, endDateTime="", durationStr=duration,
                    barSizeSetting="1 day", whatToShow=what,
                    useRTH=rth, formatDate=1, timeout=60,
                )
            except Exception:
                bars = []
            if bars:
                break

        out = []
        for b in bars:
            d = b.date
            ds = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]
            out.append({"time": ds, "open": float(b.open), "high": float(b.high),
                        "low": float(b.low), "close": float(b.close),
                        "volume": int(b.volume or 0)})
        dedup = {x["time"]: x for x in out}
        result = [dedup[k] for k in sorted(dedup)]

        # ── Inject today's bar from live quote if missing ──────────────────
        today_et = datetime.now(ET).date()
        today_str = today_et.strftime("%Y-%m-%d")
        if _is_us_trading_day(today_et) and today_str not in dedup:
            live_price, _ = _get_live_price(sym)
            if live_price is not None and math.isfinite(live_price):
                # Use prev close from last historical bar as open if we have it
                prev_close = result[-1]["close"] if result else live_price
                today_bar = {
                    "time": today_str,
                    "open": prev_close,
                    "high": live_price,
                    "low": live_price,
                    "close": live_price,
                    "volume": 0,
                }
                result.append(today_bar)
        # ──────────────────────────────────────────────────────────────────

        _daily_cache[cache_key] = {"ts": now_ts, "bars": result}
        return result

    def _do_quote(sym):
        _do_subscribe(sym)
        t = _ticker_by_symbol.get(sym)
        last = _safe_float(getattr(t, "last", None))
        close = _safe_float(getattr(t, "close", None))
        if last is None:
            last = _safe_float(getattr(t, "marketPrice", lambda: None)())
        if close is None:
            close = last

        # Resolve company name from IB once, then cache.
        desc = _quote_desc_cache.get(sym) or ""
        if _is_placeholder_description(desc, sym):
            exch, cur = _market_for_ticker(sym)
            contract = Stock(sym, exch, cur)
            try:
                q = ib.qualifyContracts(contract)
                if q:
                    contract = q[0]
            except Exception:
                pass

            # 1) Try reqContractDetails long/company name.
            try:
                cds = ib.reqContractDetails(contract)
                if cds:
                    # Prefer the first non-placeholder long/company name.
                    for cd0 in cds:
                        for attr in ("longName", "companyName"):
                            v = str(getattr(cd0, attr, "") or "").strip()
                            if not _is_placeholder_description(v, sym):
                                desc = v
                                break
                        if not _is_placeholder_description(desc, sym):
                            break
            except Exception:
                pass

            # 2) Fallback: reqMatchingSymbols description for exact stock symbol.
            if _is_placeholder_description(desc, sym):
                try:
                    ms = ib.reqMatchingSymbols(sym)
                    fallback = ""
                    for m in ms or []:
                        cd = getattr(m, "contract", None)
                        if not cd:
                            continue
                        if str(getattr(cd, "symbol", "") or "").upper() != sym:
                            continue
                        if str(getattr(cd, "secType", "") or "").upper() != "STK":
                            continue
                        v = str(getattr(m, "description", "") or "").strip()
                        if _is_placeholder_description(v, sym):
                            continue
                        if " " in v:
                            desc = v
                            break
                        if not fallback:
                            fallback = v
                    if _is_placeholder_description(desc, sym):
                        desc = fallback
                except Exception:
                    pass

            # 3) HTTP fallback (Yahoo search endpoint).
            if _is_placeholder_description(desc, sym):
                desc = _fetch_company_name_http(sym)

            if not _is_placeholder_description(desc, sym):
                _quote_desc_cache[sym] = desc

        if _is_placeholder_description(desc, sym):
            desc = sym

        return {"symbol": sym, "description": desc,
                "lastPrice": last, "regularMarketClosePrice": close,
                "extendedLastPrice": last}

    def _do_status():
        try:
            _ensure_connected()
            return {"valid": True, "remaining_seconds": 999999}
        except Exception as e:
            return {"valid": False, "remaining_seconds": 0, "error": str(e)}

    # --- main worker loop ---
    while True:
        try:
            job = _ib_queue.get(timeout=0.1)
        except queue.Empty:
            if ib.isConnected():
                try:
                    ib.sleep(0)
                except Exception:
                    pass
            continue

        result_holder, event, fn_name, args = job
        try:
            if fn_name == "historical":
                result_holder["result"] = _do_historical(*args)
            elif fn_name == "subscribe":
                _do_subscribe(*args)
                result_holder["result"] = True
            elif fn_name == "quote":
                result_holder["result"] = _do_quote(*args)
            elif fn_name == "status":
                result_holder["result"] = _do_status()
        except Exception as e:
            result_holder["error"] = str(e)
        finally:
            event.set()


def _call_ib(fn_name: str, *args, timeout: float = 20.0):
    result_holder: dict = {}
    event = threading.Event()
    _ib_queue.put((result_holder, event, fn_name, args))
    if not event.wait(timeout=timeout):
        raise TimeoutError(f"IB call '{fn_name}' timed out after {timeout}s")
    if "error" in result_holder:
        raise RuntimeError(result_holder["error"])
    return result_holder.get("result")


_worker_thread = threading.Thread(target=_ib_worker, daemon=True, name="ib-worker")
_worker_thread.start()


def _intraday_bars(symbol: str, interval: str) -> list[dict]:
    sym = symbol.upper()
    src = _bars_1m[sym] if interval == "1m" else _bars_5m[sym]
    now_et = datetime.now(ET).date()
    if not src:
        return []

    # Prefer today + previous session; if off-session, return latest two sessions.
    available_days = sorted({datetime.fromtimestamp(ts_ms / 1000, tz=ET).date() for ts_ms in src.keys()})
    if not available_days:
        return []
    if now_et in available_days:
        idx = available_days.index(now_et)
        start_idx = max(0, idx - 1)
        selected_days = set(available_days[start_idx:idx + 1])
    else:
        selected_days = set(available_days[-2:])

    out = []
    for ts_ms in sorted(src.keys()):
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=ET)
        if dt.date() not in selected_days:
            continue
        # Keep US equity intraday window only: premarket + regular + after-hours.
        # Exclude overnight prints (roughly "night session") outside 04:00-20:00 ET.
        hhmm = dt.hour * 100 + dt.minute
        if hhmm < 400 or hhmm >= 2000:
            continue
        b = src[ts_ms]
        out.append({"time": int(ts_ms // 1000),
                    "open": b["open"], "high": b["high"],
                    "low": b["low"], "close": b["close"],
                    "volume": int(b["volume"])})
    return out


def _stream_payload(symbol: str) -> str:
    sym = symbol.upper()
    return json.dumps({"symbol": sym,
                       "bars_1m": _intraday_bars(sym, "1m"),
                       "bars_5m": _intraday_bars(sym, "5m"),
                       "ts": int(time.time() * 1000)},
                      ensure_ascii=True)


def _daily_bars_from_local_csv(symbol: str, period_str: str) -> list[dict]:
    days_map = {"1y": 365, "2y": 730, "3y": 1095, "5y": 1825, "10y": 3650}
    days_back = days_map.get(period_str, 365)
    sym = _normalize_ticker(symbol).upper()
    path = LOCAL_DAILY_DIR / f"{sym}.csv"
    if not path.exists():
        return []

    out = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw_t = (row.get("DateTime") or "").strip()
            t = None
            for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d", "%m/%d/%y"):
                try:
                    t = datetime.strptime(raw_t[:10], fmt).strftime("%Y-%m-%d")
                    break
                except Exception:
                    pass
            if t is None:
                # last-resort parse
                try:
                    t = datetime.fromisoformat(raw_t.replace("/", "-")[:10]).strftime("%Y-%m-%d")
                except Exception:
                    t = None
            o = _safe_float(row.get("Open"))
            h = _safe_float(row.get("High"))
            l = _safe_float(row.get("Low"))
            c = _safe_float(row.get("Close"))
            if not t or None in (o, h, l, c):
                continue
            out.append(
                {
                    "time": t,
                    "open": float(o),
                    "high": float(h),
                    "low": float(l),
                    "close": float(c),
                    "volume": int(_safe_int(row.get("Volume"))),
                }
            )
    if not out:
        return []
    out = out[-(days_back + 7):]
    dedup = {x["time"]: x for x in out}
    return [dedup[k] for k in sorted(dedup.keys())]


@app.route("/api/pricehistory")
def pricehistory():
    symbol = request.args.get("symbol", "").strip().upper()
    period_str = request.args.get("period_str", "1y")
    hist_source = request.args.get("hist_source", "local").strip().lower()
    meta = request.args.get("meta", "0").strip().lower()
    meta_enabled = meta in {"1", "true", "yes", "y"}
    if not symbol:
        return jsonify({"error": "symbol is required"}), 400
    if hist_source == "ib":
        try:
            # Historical requests may need multiple fallbacks in _do_historical
            # (TRADES/ADJUSTED_LAST/MIDPOINT with RTH and non-RTH), so allow
            # a longer timeout than quote/intraday calls.
            bars = _call_ib("historical", _normalize_ticker(symbol), period_str, timeout=60.0)
        except Exception as e:
            return jsonify({"error": f"IB historical fetch failed: {e}"}), 502
        if not bars:
            return jsonify({"error": f"No IB historical data for {symbol}"}), 404
        if meta_enabled:
            return jsonify({"source": "ib", "bars": bars})
        resp = jsonify(bars)
        resp.headers["X-History-Source"] = "ib"
        return resp
    try:
        bars = _daily_bars_from_local_csv(symbol, period_str)
    except Exception as e:
        return jsonify({"error": f"local daily csv read failed: {e}"}), 500
    if not bars:
        return jsonify({"error": f"No local daily data for {symbol} in {LOCAL_DAILY_DIR}"}), 404
    if meta_enabled:
        return jsonify({"source": "local", "bars": bars})
    resp = jsonify(bars)
    resp.headers["X-History-Source"] = "local"
    return resp


@app.route("/api/intraday")
def intraday():
    symbol = request.args.get("symbol", "").strip().upper()
    interval = request.args.get("interval", "1m")
    if interval not in {"1m", "5m"}:
        return jsonify({"error": "interval must be 1m or 5m"}), 400
    if not symbol:
        return jsonify({"error": "symbol is required"}), 400
    try:
        _call_ib("subscribe", _normalize_ticker(symbol), timeout=15.0)
        bars = _intraday_bars(symbol, interval)
    except Exception as e:
        return jsonify({"error": f"IB realtime subscribe failed: {e}"}), 502
    return jsonify({"symbol": symbol, "interval": interval, "bars": bars})


@app.route("/api/intraday/stream")
def intraday_stream():
    symbol = request.args.get("symbol", "").strip().upper()
    if not symbol:
        return jsonify({"error": "symbol is required"}), 400
    try:
        _call_ib("subscribe", _normalize_ticker(symbol), timeout=15.0)
    except Exception as e:
        return jsonify({"error": f"IB realtime subscribe failed: {e}"}), 502

    @stream_with_context
    def event_stream():
        while True:
            try:
                yield f"event: intraday\ndata: {_stream_payload(symbol)}\n\n"
            except Exception:
                yield "event: intraday\ndata: {}\n\n"
            time.sleep(1.0)

    return Response(event_stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "Connection": "keep-alive",
                             "X-Accel-Buffering": "no"})


@app.route("/api/quote")
def quote():
    symbol = request.args.get("symbol", "").strip().upper()
    if not symbol:
        return jsonify({"error": "symbol is required"}), 400
    try:
        data = _call_ib("quote", _normalize_ticker(symbol), timeout=15.0)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/token_status")
def token_status():
    try:
        data = _call_ib("status", timeout=5.0)
        return jsonify(data)
    except Exception:
        return jsonify({"valid": False, "remaining_seconds": 0,
                        "error": _last_ib_error or "worker timeout"})

@app.route("/api/eps-revenue")
def api_eps_revenue():
    """
    EPS/Revenue data endpoint for ib_chart.html UI.

    Query params:
      - symbol (required)
      - quarters (optional, default 5) -> last N quarters + estimate rows
    """
    symbol = (request.args.get("symbol") or "").upper().strip()
    if not symbol:
        return jsonify({"error": "symbol is required"}), 400

    quarters_raw = (request.args.get("quarters") or "5").strip()
    try:
        quarters = int(quarters_raw)
    except Exception:
        quarters = 5

    # Keep bounded; finvizfinance + yfinance calls can be slow.
    quarters = max(1, min(10, quarters))

    cache_key = f"{symbol}|{quarters}"
    now = time.time()
    cached = _eps_rev_cache.get(cache_key)
    if cached and isinstance(cached, dict):
        ts = cached.get("ts")
        data = cached.get("data")
        if isinstance(ts, (int, float)) and data is not None:
            if (now - float(ts)) < _eps_rev_cache_ttl_sec:
                return jsonify(data)

    # Lazy import: heavier deps.
    try:
        import eps_revenue_viewer as _eps_mod  # local file in this repo
    except Exception as e:
        return jsonify({"error": f"eps_revenue_viewer import failed: {e}"}), 500

    try:
        rows = _eps_mod.fetch_recent_eps_revenue(symbol, quarters=quarters)
        meta = getattr(_eps_mod, "LAST_SELECT_INFO", {}) or {}

        row_dicts = []
        for r in rows or []:
            row_dicts.append(
                {
                    "quarter_end": getattr(r, "quarter_end", None),
                    "eps": getattr(r, "eps", None),
                    "eps_yoy": getattr(r, "eps_yoy", None),
                    "revenue": getattr(r, "revenue", None),
                    "revenue_yoy": getattr(r, "revenue_yoy", None),
                }
            )

        data = {"symbol": symbol, "quarters": quarters, "rows": row_dicts, "meta": meta}
        _eps_rev_cache[cache_key] = {"ts": now, "data": data}
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/")
def index():
    # Default landing page: single chart.
    for name in ("ib_chart.html", "ib_multichart.html"):
        html = BASE_DIR / name
        if html.exists():
            return html.read_text(encoding="utf-8"), 200, {
                "Content-Type": "text/html; charset=utf-8",
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            }
    return "<h2>IB backend is running.</h2>", 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/ib_chart.html")
@app.route("/schwab_chart.html")
def serve_single_chart():
    html = BASE_DIR / "ib_chart.html"
    if not html.exists():
        return "<h2>ib_chart.html not found.</h2>", 404, {"Content-Type": "text/html; charset=utf-8"}
    return html.read_text(encoding="utf-8"), 200, {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }


@app.route("/ib_multichart.html")
@app.route("/schwab_multichart.html")
def serve_multi_chart():
    html = BASE_DIR / "ib_multichart.html"
    if not html.exists():
        return "<h2>ib_multichart.html not found.</h2>", 404, {"Content-Type": "text/html; charset=utf-8"}
    return html.read_text(encoding="utf-8"), 200, {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }


if __name__ == "__main__":
    print("=" * 60)
    print("  IB backend  —  single worker thread architecture")
    print(f"  IB host: {IB_HOST}:{IB_PORT},  clientId={IB_CLIENT_ID}")
    print("  http://127.0.0.1:5001")
    print("=" * 60)
    app.run(host="127.0.0.1", port=5001, debug=False, threaded=True)