"""Daily OHLCV for tickers + FX -> ``phase1/prices.json`` (schema ``prices/v1``, DESIGN §4.1).

Provider order per ticker (never bypasses a bot wall):

1. disk cache ``data/cache/prices/prices_<T>.csv`` younger than ``cache_ttl_hours.prices``;
2. yfinance (``common.yf_history``);
3. Stooq CSV (``common.STOOQ_CSV_URL``) when ``prices.stooq_fallback`` — an HTML / JavaScript
   bot-check page, "No data" or the daily-limit notice raises ``common.ProviderBlocked`` and is
   simply recorded (it is NEVER solved or worked around);
4. the expired cache entry (``provider: cache_stale``, ``stale: true``, flag ``stale_cache`` high);
5. otherwise the ticker lands in ``failed{}``.

The FX rate (``committee.yaml: fx_ticker``, Stooq symbol ``fx_stooq_symbol``) is reported as
``fx.usdils``. Every number is a Fact (kind ``price``, ``as_of`` = last bar date, source
``D:PX:<T>``). All network goes through the ``common`` seam (fixtures / offline aware).

API::

    run(tickers, out=None, *, as_of=None, period=None, cache=None, include_fx=True) -> dict
    load_series(ticker) -> DataFrame | None      # cached OHLCV (stale allowed), never network
    fetch_series(ticker, *, period=None, cache=None, cfg=None) -> dict   # one ticker, raw frame

CLI: ``python scripts/fetch_prices.py --tickers KO,SPY --out runs/<id>/phase1/prices.json``.
"""
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import common  # noqa: E402

import argparse  # noqa: E402
import io  # noqa: E402
import math  # noqa: E402
import re  # noqa: E402
from datetime import date  # noqa: E402
from typing import Any, Iterable  # noqa: E402

import pandas as pd  # noqa: E402

CACHE_KIND = "prices"
PROVIDER_YF = "yfinance"
PROVIDER_STOOQ = "stooq"
PROVIDER_STALE = "cache_stale"
SCHEMA_NAME = "prices"

# Fallbacks used only when committee.yaml lacks a key (the shipped config defines all of them).
DEFAULT_PERIOD = "3y"
DEFAULT_TTL_HOURS = 24.0
DEFAULT_ADV_WINDOW = 63
DEFAULT_RETURN_WINDOWS = {"1m": 21, "3m": 63, "6m": 126, "12m": 252}
DEFAULT_PRICE_MAX_TRADING_DAYS = 3

#: Text that marks a Stooq response as a bot check / challenge page instead of CSV data.
BOT_WALL_MARKERS = (
    "<html", "<!doctype", "<head", "<body", "<script", "<meta", "javascript",
    "verify your browser", "verifying your browser", "checking your browser",
    "proof of work", "proof-of-work", "captcha", "are you a robot", "challenge",
)
#: Plain-text Stooq refusals (treated as ProviderBlocked per DESIGN §4.1).
STOOQ_REFUSAL_MARKERS = ("no data", "exceeded the daily hits limit")


#: Quote currencies the pipeline can convert to USD. Value = how many ILS one quoted unit is worth.
#: Yahoo quotes Tel Aviv stocks in agorot ("ILA", 1/100 ILS). Any other non-USD currency is
#: refused rather than silently priced as dollars.
ILS_PER_QUOTE_UNIT = {"ILS": 1.0, "ILA": 0.01}
#: Exchange suffix -> quote currency, used only when Yahoo reports no currency.
SUFFIX_CURRENCY = {".TA": "ILA"}


def _detect_currency(t: str, *, network: bool = True) -> str | None:
    """Quote currency code of ticker ``t`` ("USD" for suffix-less US tickers, indices and FX pairs).
    Only tickers with an exchange suffix (a dot, e.g. ``LUMI.TA``) can quote in another currency; for
    those Yahoo's ``info['currency']`` is used (``network=False`` skips it), then the suffix table.
    None = unknown (the caller must not assume USD)."""
    if "." not in t:
        return "USD"
    if network:
        try:
            cur = common.yf_info(t).get("currency")
        except Exception:  # noqa: BLE001 - info is optional; fall back to the suffix table
            cur = None
        if isinstance(cur, str) and cur.strip():
            return cur.strip()
    for suffix, code in SUFFIX_CURRENCY.items():
        if t.upper().endswith(suffix):
            return code
    return None


def _currency_from_meta(df: pd.DataFrame, t: str, *, network: bool) -> str | None:
    meta = df.attrs.get("meta") or {}
    cur = meta.get("currency") if isinstance(meta, dict) else None
    if isinstance(cur, str) and cur.strip():
        return cur.strip()
    return _detect_currency(t, network=network)


class PriceFetchError(common.DataUnavailable):
    """No provider (cache, yfinance, Stooq, stale cache) could serve a ticker.
    ``errors`` lists the per-provider reasons, ``flags`` the envelope flags raised on the way."""

    def __init__(self, message: str, *, errors: list[str] | None = None, flags: list[dict] | None = None):
        super().__init__(message)
        self.errors = list(errors or [])
        self.flags = list(flags or [])


# =====================================================================================
# Config helpers
# =====================================================================================


def _cfg(cfg: dict | None = None) -> dict:
    if cfg is not None:
        return cfg
    try:
        return common.committee_cfg()
    except common.ConfigError:
        return {}


def _prices_cfg(cfg: dict) -> dict:
    return dict(cfg.get("prices") or {})


def _ttl_hours(cfg: dict) -> float:
    v = common.safe_float((cfg.get("cache_ttl_hours") or {}).get("prices"))
    return DEFAULT_TTL_HOURS if v is None else v


def _period(cfg: dict, period: str | None) -> str:
    return str(period or _prices_cfg(cfg).get("period") or DEFAULT_PERIOD)


def _fx_ticker(cfg: dict) -> str:
    return common.norm_ticker(cfg.get("fx_ticker") or "ILS=X")


def cache_key(ticker: str) -> str:
    """Cache key of a ticker's series: ``prices_<T>`` (``data/cache/prices/prices_<T>.csv``)."""
    return f"prices_{common.norm_ticker(ticker)}"


def _cache(cache: Any = None) -> common.DiskCache:
    if isinstance(cache, common.DiskCache):
        return cache
    return common.DiskCache(cache) if cache else common.DiskCache()


# =====================================================================================
# Stooq
# =====================================================================================


def stooq_symbol(ticker: str, cfg: dict | None = None) -> str | None:
    """Stooq symbol for a ticker: the configured FX symbol for ``fx_ticker``; ``<t>.us`` for
    plain US tickers (``BRK-B`` -> ``brk-b.us``); None when Stooq has no mapping we trust
    (indices ``^X``, other ``=X`` FX pairs, exchange-suffixed tickers like ``TEVA.TA``)."""
    c = _cfg(cfg)
    t = common.norm_ticker(ticker)
    if t == _fx_ticker(c):
        sym = c.get("fx_stooq_symbol")
        return str(sym).lower() if sym else None
    if t.startswith("^") or "=" in t or "." in t:
        return None
    return f"{t.lower()}.us"


def _looks_like_csv_header(line: str) -> bool:
    low = line.strip().lower().lstrip("﻿")
    return low.startswith("date,") and "close" in low


def is_bot_wall(text: str) -> bool:
    """True when a Stooq response is an HTML / JavaScript bot-check (or other challenge) page
    rather than CSV. Detection only — such pages are never solved or bypassed."""
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped:
        return False
    first = stripped.splitlines()[0]
    if _looks_like_csv_header(first):
        return False
    low = stripped[:20000].lower()
    return stripped.startswith("<") or any(m in low for m in BOT_WALL_MARKERS)


def parse_stooq_csv(text: str, *, symbol: str = "", url: str | None = None) -> pd.DataFrame:
    """Parse a Stooq daily CSV (``Date,Open,High,Low,Close,Volume``) into a clean OHLCV frame.

    Bot-check pages, "No data" and the daily-limit notice -> ``ProviderBlocked``; any other
    non-CSV / empty body -> ``DataUnavailable``."""
    body = (text or "").strip().lstrip("﻿")
    if not body:
        raise common.DataUnavailable(f"empty Stooq response for {symbol}", url=url)
    first = body.splitlines()[0]
    if not _looks_like_csv_header(first):
        if is_bot_wall(body):
            raise common.ProviderBlocked(
                f"Stooq returned a bot-check page instead of CSV for {symbol} (not bypassed)", url=url)
        low = body[:2000].lower()
        if any(m in low for m in STOOQ_REFUSAL_MARKERS):
            raise common.ProviderBlocked(f"Stooq refused {symbol}: {first[:80]!r}", url=url)
        raise common.DataUnavailable(f"unexpected Stooq response for {symbol}: {first[:80]!r}", url=url)
    try:
        raw = pd.read_csv(io.StringIO(body))
    except (ValueError, pd.errors.ParserError) as exc:
        raise common.DataUnavailable(f"unparseable Stooq CSV for {symbol}: {exc}", url=url) from exc
    return common.clean_ohlcv(raw, ticker=symbol)


def trim_period(df: pd.DataFrame, period: str | None) -> pd.DataFrame:
    """Keep the bars inside a yfinance-style ``period`` (``3y``, ``18mo``, ``6wk``, ``90d``,
    ``ytd``, ``max``) counted back from the last bar. Unknown periods leave ``df`` unchanged."""
    if df is None or df.empty or not period:
        return df
    p = str(period).strip().lower()
    if p == "max":
        return df
    last = pd.Timestamp(df.index[-1])
    if p == "ytd":
        return df[df.index >= pd.Timestamp(year=last.year, month=1, day=1)]
    m = re.fullmatch(r"(\d+)\s*(d|wk|mo|y)", p)
    if not m:
        return df
    n, unit = int(m.group(1)), m.group(2)
    offset = {"d": pd.DateOffset(days=n), "wk": pd.DateOffset(weeks=n),
              "mo": pd.DateOffset(months=n), "y": pd.DateOffset(years=n)}[unit]
    return df[df.index >= last - offset]


def fetch_stooq(symbol: str, *, period: str | None = None) -> tuple[pd.DataFrame, str]:
    """Download + parse a Stooq daily CSV through the common seam. Returns (frame, url).
    Raises ``ProviderBlocked`` on a bot wall / refusal and ``DataUnavailable`` otherwise."""
    url = common.stooq_csv_url(symbol)
    text = common.http_get_text(url)
    df = parse_stooq_csv(text, symbol=symbol, url=url)
    df = trim_period(df, period)
    if df.empty:
        raise common.DataUnavailable(f"Stooq series for {symbol} is empty after trimming to {period}", url=url)
    return df, url


# =====================================================================================
# One ticker
# =====================================================================================


def _reason(exc: BaseException) -> str:
    msg = str(exc).strip() or type(exc).__name__
    return msg if isinstance(exc, common.AginvError) else f"{type(exc).__name__}: {msg}"


def _result(ticker: str, df: pd.DataFrame, provider: str, *, stale: bool, source_url: str | None,
            origin: str, cache: common.DiskCache, fetched_at: Any = None, age_hours: Any = None,
            flags: list[dict] | None = None, errors: list[str] | None = None,
            currency: str | None = "USD") -> dict:
    return {
        "ticker": ticker,
        "currency": currency,
        "frame": df,
        "provider": provider,
        "origin_provider": origin,
        "stale": bool(stale),
        "source_url": source_url,
        "fetched_at": fetched_at,
        "age_hours": age_hours,
        "series_path": cache.path_for(CACHE_KIND, cache_key(ticker), "csv").as_posix(),
        "flags": list(flags or []),
        "errors": list(errors or []),
    }


def _store(cache: common.DiskCache, ticker: str, df: pd.DataFrame, provider: str, url: str,
           period: str, currency: str | None = "USD") -> str | None:
    cache.put_frame(CACHE_KIND, cache_key(ticker), df, url,
                    meta={"provider": provider, "ticker": ticker, "period": period, "currency": currency})
    entry = cache.get_entry(CACHE_KIND, cache_key(ticker))
    return entry.get("fetched_at") if entry else None


def _fetch_native(ticker: str, *, period: str | None = None, cache: Any = None, cfg: dict | None = None,
                  use_cache: bool = True) -> dict:
    """Provider chain for one ticker in its NATIVE quote units (``res["currency"]``); see
    ``fetch_series`` for the converted view every consumer uses."""
    c = _cfg(cfg)
    t = common.norm_ticker(ticker)
    store = _cache(cache)
    per = _period(c, period)
    key = cache_key(t)
    errors: list[str] = []
    flags: list[dict] = []

    # 1. fresh cache
    if use_cache:
        df = store.get_frame(CACHE_KIND, key, _ttl_hours(c))
        if df is not None and not df.empty:
            meta = df.attrs.get("meta") or {}
            origin = meta.get("provider") if meta.get("provider") in (PROVIDER_YF, PROVIDER_STOOQ) else PROVIDER_YF
            url = df.attrs.get("source_url") or common.yf_history_url(t)
            return _result(t, _ohlcv(df, t), origin, stale=False, source_url=url, origin=origin, cache=store,
                           fetched_at=df.attrs.get("fetched_at"), age_hours=df.attrs.get("age_hours"),
                           currency=_currency_from_meta(df, t, network=True))

    # 2. yfinance
    try:
        df = common.yf_history(t, period=per)
        url = common.yf_history_url(t)
        cur = _detect_currency(t)
        fetched = _store(store, t, df, PROVIDER_YF, url, per, cur)
        return _result(t, df, PROVIDER_YF, stale=False, source_url=url, origin=PROVIDER_YF, cache=store,
                       fetched_at=fetched, age_hours=0.0, currency=cur)
    except Exception as exc:  # noqa: BLE001 - provider boundary: fall through to the next provider
        errors.append(f"yfinance: {_reason(exc)}")

    # 3. Stooq (never bypass a bot wall)
    if bool(_prices_cfg(c).get("stooq_fallback", True)):
        sym = stooq_symbol(t, c)
        if sym is None:
            errors.append("stooq: no Stooq symbol mapping for this ticker")
        else:
            try:
                df, url = fetch_stooq(sym, period=per)
                fetched = _store(store, t, df, PROVIDER_STOOQ, url, per)
                flags.append(common.flag("provider_fallback", "low",
                                         f"{t}: yfinance unavailable ({errors[-1]}); served by Stooq ({sym})"))
                return _result(t, df, PROVIDER_STOOQ, stale=False, source_url=url, origin=PROVIDER_STOOQ,
                               cache=store, fetched_at=fetched, age_hours=0.0, flags=flags, errors=errors)
            except common.ProviderBlocked as exc:
                errors.append(f"stooq blocked: {_reason(exc)}")
                flags.append(common.flag("provider_blocked", "medium",
                                         f"{t}: Stooq refused service ({_reason(exc)}); not bypassed"))
            except Exception as exc:  # noqa: BLE001 - provider boundary
                errors.append(f"stooq: {_reason(exc)}")
    else:
        errors.append("stooq: fallback disabled (prices.stooq_fallback=false)")

    # 4. stale cache
    if use_cache:
        df = store.get_frame_stale(CACHE_KIND, key)
        if df is not None and not df.empty:
            meta = df.attrs.get("meta") or {}
            origin = meta.get("provider") if meta.get("provider") in (PROVIDER_YF, PROVIDER_STOOQ) else PROVIDER_YF
            age = common.safe_float(df.attrs.get("age_hours"))
            age_txt = f"{age:.1f} h" if age is not None else "unknown age"
            flags.append(common.flag(
                "stale_cache", "high",
                f"{t}: served from stale cache fetched {df.attrs.get('fetched_at')} ({age_txt} old) after: "
                + "; ".join(errors)))
            url = df.attrs.get("source_url") or common.yf_history_url(t)
            return _result(t, _ohlcv(df, t), PROVIDER_STALE, stale=True, source_url=url, origin=origin,
                           cache=store, fetched_at=df.attrs.get("fetched_at"), age_hours=age,
                           flags=flags, errors=errors, currency=_currency_from_meta(df, t, network=False))

    raise PriceFetchError(f"{t}: no price data from any provider ({'; '.join(errors)})",
                          errors=errors, flags=flags)


def _usd_factor(currency: str | None, ticker: str, fx_close: float | None, *, flags: list[dict]) -> float:
    """USD per quoted unit for ``currency`` given the USD/ILS rate. Raises ``PriceFetchError`` for an
    unknown or unsupported currency, or when the rate is unavailable: a wrong-unit price is worse
    than no price."""
    def refuse(msg: str) -> PriceFetchError:
        return PriceFetchError(msg, errors=[msg], flags=flags)

    if currency is None:
        raise refuse(f"{ticker}: quote currency unknown (exchange-suffixed ticker without Yahoo currency "
                     "info); refusing to assume USD")
    mult = ILS_PER_QUOTE_UNIT.get(currency.strip().upper())
    if mult is None:
        raise refuse(f"{ticker}: quote currency {currency!r} is not supported (USD, ILS and ILA only); "
                     "refusing to price it as USD")
    if fx_close is None or not math.isfinite(fx_close) or fx_close <= 0:
        raise refuse(f"{ticker}: quoted in {currency} but no USD/ILS rate is available to convert it")
    return float(f"{mult / fx_close:.10g}")  # 10 significant digits: no long float tails in prices.json


def _convert_frame(df: pd.DataFrame, factor: float) -> pd.DataFrame:
    out = df.copy()
    out.attrs.update(dict(df.attrs))
    for col in ("Open", "High", "Low", "Close"):
        if col in out.columns:
            out[col] = out[col].astype("float64") * factor
    return out


def _conversion_flag(t: str, currency: str, fx_close: float, fx_date: str) -> dict:
    major = "agorot (1/100 ILS)" if currency.upper() == "ILA" else "ILS"
    return common.flag(
        "converted_to_usd", "low",
        f"{t}: quoted in {currency} ({major}); converted to USD at the spot USD/ILS rate {fx_close:.4f} "
        f"({fx_date}) applied to the whole history. Percentage returns, RSI and trend are those of the local "
        "price; USD levels exclude historical FX moves. The native ILS price is in the prices.json entry.")


def fetch_series(ticker: str, *, period: str | None = None, cache: Any = None, cfg: dict | None = None,
                 use_cache: bool = True) -> dict:
    """Fetch one ticker's daily OHLCV with the provider chain (module docstring), expressed in USD.

    Returns ``{"ticker","frame","provider","origin_provider","stale","source_url","fetched_at",
    "age_hours","series_path","flags","errors","currency","native_currency","fx_usd_per_native",
    "usdils","fx_as_of"}``; ``provider`` is ``yfinance|stooq|cache_stale`` (a fresh cache hit reports
    the provider that originally served the data). A series quoted in ILS or agorot (ILA, Tel Aviv)
    is converted to USD per share (flag ``converted_to_usd``); the cache keeps the native quote.
    Raises ``PriceFetchError`` (a ``DataUnavailable``) when nothing could serve the ticker or its
    currency cannot be converted."""
    c = _cfg(cfg)
    res = _fetch_native(ticker, period=period, cache=cache, cfg=c, use_cache=use_cache)
    cur = res.get("currency")
    t = res["ticker"]
    res.update(native_currency=cur, fx_usd_per_native=None, usdils=None, fx_as_of=None)
    if (cur is not None and cur.strip().upper() == "USD") or t == _fx_ticker(c):
        return res
    fx_close: float | None = None
    fx_date: str | None = None
    if cur is not None and cur.strip().upper() in ILS_PER_QUOTE_UNIT:
        try:
            fx = _fetch_native(_fx_ticker(c), period=period, cache=cache, cfg=c, use_cache=use_cache)
            fx_close = common.round_price(fx["frame"]["Close"].iloc[-1])  # float32 noise off the rate
            fx_date = common.date_str(fx["frame"].index[-1])
        except (PriceFetchError, KeyError, IndexError, ValueError):
            fx_close = None
    factor = _usd_factor(cur, t, fx_close, flags=res["flags"])
    res["frame"] = _convert_frame(res["frame"], factor)
    res.update(fx_usd_per_native=factor, usdils=fx_close, fx_as_of=fx_date)
    res["flags"] = list(res["flags"]) + [_conversion_flag(t, str(cur), float(fx_close), str(fx_date))]
    return res


def _ohlcv(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Cached frames are already clean; re-run ``clean_ohlcv`` defensively (keeps attrs)."""
    attrs = dict(df.attrs)
    try:
        out = common.clean_ohlcv(df, ticker=ticker)
    except common.DataUnavailable:
        return df
    out.attrs.update(attrs)
    return out


def load_series(ticker: str, *, cache: Any = None) -> pd.DataFrame | None:
    """Cached OHLCV for ``ticker`` regardless of age (``df.attrs['stale']`` tells whether it is
    older than ``cache_ttl_hours.prices``). Never touches the network; None when not cached."""
    try:
        t = common.norm_ticker(ticker)
    except ValueError:
        return None
    store = _cache(cache)
    df = store.get_frame_stale(CACHE_KIND, cache_key(t))
    if df is None or df.empty:
        return None
    age = common.safe_float(df.attrs.get("age_hours"))
    c = _cfg()
    df.attrs["stale"] = age is None or age > _ttl_hours(c)
    cur = _currency_from_meta(df, t, network=False)
    if (cur is not None and cur.strip().upper() == "USD") or t == _fx_ticker(c):
        return df
    # Non-USD quote (e.g. Tel Aviv, agorot): convert with the cached USD/ILS rate; without a usable
    # rate or with an unsupported currency return None rather than a wrong-unit price.
    fx_df = store.get_frame_stale(CACHE_KIND, cache_key(_fx_ticker(c)))
    fx_close = common.round_price(fx_df["Close"].iloc[-1]) if fx_df is not None and not fx_df.empty else None
    try:
        factor = _usd_factor(cur, t, fx_close, flags=[])
    except PriceFetchError:
        return None
    out = _convert_frame(df, factor)
    out.attrs.update(native_currency=cur, fx_usd_per_native=factor)
    return out


def price_source(ticker: str, res: dict, *, accessed: Any = None) -> dict:
    """Dataset Source ``D:PX:<T>`` for a ``fetch_series`` result (Stooq URL when Stooq served it;
    ``path`` = the cached CSV; ``accessed`` = fetch date for a stale cache, else today)."""
    t = common.norm_ticker(ticker)
    origin = res.get("origin_provider") or PROVIDER_YF
    url = res.get("source_url") or None
    if accessed is None:
        accessed = common.date_str(res.get("fetched_at")) if res.get("stale") else common.today()
    title = None
    if res.get("stale"):
        title = f"{'Stooq' if origin == PROVIDER_STOOQ else 'Yahoo Finance'} daily price history — {t} (stale cache)"
    if origin == PROVIDER_STOOQ:
        if not url:
            sym = stooq_symbol(t)
            url = common.stooq_csv_url(sym) if sym else common.yf_history_url(t)
        return common.dataset_source(f"D:PX:{t}", provider="stooq", url=url,
                                     path=res.get("series_path"), accessed=accessed, title=title)
    if url and not url.startswith("http"):
        url = None
    return common.dataset_source(f"D:PX:{t}", url=url, path=res.get("series_path"), accessed=accessed, title=title)


# =====================================================================================
# Summary of one series -> prices.json entry
# =====================================================================================


def _round(x: Any, dp: int = 6) -> float | None:
    f = common.safe_float(x)
    return None if f is None else round(f, dp)


def _px(x: Any) -> float | None:
    """A price level for a Fact: float32 noise removed (``769.6400146484375`` -> ``769.64``)."""
    return common.round_price(x)


def truncate_to(df: pd.DataFrame, as_of: Any) -> pd.DataFrame:
    """Rows dated on or before ``as_of`` (no look-ahead past the run date)."""
    d = common.parse_date(as_of)
    if d is None or df is None or df.empty:
        return df
    return df[df.index <= pd.Timestamp(d)]


def summarize(ticker: str, df: pd.DataFrame, *, cfg: dict | None = None, is_fx: bool = False) -> dict:
    """prices.json ticker entry fields computed from a clean OHLCV frame (already truncated to
    the run date): ``last_close, last_date, first_date, rows, adv_usd_63d, returns_pct``."""
    c = _cfg(cfg)
    pc = _prices_cfg(c)
    t = common.norm_ticker(ticker)
    sid = f"D:PX:{t}"
    close = df["Close"].astype("float64")
    last_date = common.date_str(df.index[-1])
    first_date = common.date_str(df.index[0])
    n = int(len(df))
    unit = "ILS/USD" if is_fx else "USD/share"
    last_close = common.fact(_px(close.iloc[-1]), unit, sid, last_date, "price")

    window = int(pc.get("adv_window_days") or DEFAULT_ADV_WINDOW)
    if is_fx:
        adv = common.na_fact("USD", sid, last_date, "price", "not_applicable", note="FX series has no volume")
    elif n < window:
        adv = common.na_fact("USD", sid, last_date, "price", "insufficient_history",
                             note=f"{n} bars < {window}-day window")
    else:
        tail = df.tail(window)
        dollar = (tail["Close"] * tail["Volume"]).astype("float64")
        mean = float(dollar.mean())
        if not math.isfinite(mean) or float(tail["Volume"].sum()) <= 0:
            adv = common.na_fact("USD", sid, last_date, "price", "missing", note="no volume reported")
        else:
            adv = common.fact(round(mean, 2), "USD", sid, last_date, "price",
                              derivation=f"mean(Close x Volume) over last {window} bars")

    windows = dict(pc.get("return_windows_days") or DEFAULT_RETURN_WINDOWS)
    for k, v in DEFAULT_RETURN_WINDOWS.items():
        windows.setdefault(k, v)
    returns: dict[str, dict] = {}
    for label, bars in windows.items():
        bars = int(bars)
        if n <= bars:
            returns[str(label)] = common.na_fact("pct", sid, last_date, "price", "insufficient_history",
                                                 note=f"{n} bars <= {bars}")
            continue
        base = float(close.iloc[-1 - bars])
        last = float(close.iloc[-1])
        value = (last / base - 1.0) * 100.0 if base > 0 else None
        returns[str(label)] = common.fact(_round(value, 4), "pct", sid, last_date, "price",
                                          na_reason=None if value is not None else "not_meaningful",
                                          derivation=f"Close {common.date_str(df.index[-1 - bars])} -> {last_date} ({bars} bars)")
    return {"last_close": last_close, "last_date": last_date, "first_date": first_date, "rows": n,
            "adv_usd_63d": adv, "returns_pct": returns}


def _lag_flag(ticker: str, last_date: str, as_of: date, cfg: dict) -> dict | None:
    limit = (cfg.get("evidence") or {}).get("price_max_trading_days", DEFAULT_PRICE_MAX_TRADING_DAYS)
    try:
        lag = common.trading_days_between(last_date, as_of)
    except (ValueError, common.ConfigError):
        return None
    if lag > int(limit):
        return common.flag("price_lag", "medium",
                           f"{ticker}: last bar {last_date} is {lag} trading days before {as_of.isoformat()}")
    return None


# =====================================================================================
# run / CLI
# =====================================================================================


def _normalize_list(tickers: Iterable[str] | str | None) -> tuple[list[str], list[str]]:
    if tickers is None:
        return [], []
    if isinstance(tickers, str):
        tickers = [tickers]
    out: list[str] = []
    bad: list[str] = []
    for raw in tickers:
        for part in str(raw).split(","):
            part = part.strip()
            if not part:
                continue
            try:
                t = common.norm_ticker(part)
            except ValueError:
                bad.append(part)
                continue
            if t not in out:
                out.append(t)
    return out, bad


def run(tickers: Iterable[str] | str | None, out: str | Path | None = None, *, as_of: Any = None,
        period: str | None = None, cache: Any = None, include_fx: bool = True) -> dict:
    """Fetch every ticker (+ the FX series) and build the ``prices/v1`` payload; write it to
    ``out`` when given. Never raises for a provider failure — failures go to ``failed{}``."""
    cfg = _cfg(None)
    as_of_d = common.parse_date(as_of) if as_of else common.today()
    store = _cache(cache)
    names, bad = _normalize_list(tickers)
    fx_t = _fx_ticker(cfg)

    entries: dict[str, dict] = {}
    failed: dict[str, str] = {}
    sources: list[dict] = []
    flags: list[dict] = []
    missing: list[str] = []
    for b in bad:
        flags.append(common.flag("invalid_ticker", "medium", f"ignored invalid ticker {b!r}"))

    unrequested_errors: dict[str, str] = {}

    def process(t: str, requested: bool = True) -> dict | None:
        # A series fetched only as a by-product (the FX rate when ILS=X was not requested) gets
        # no high-severity fetch_failed flag: the caller reports it once as fx_unavailable.
        try:
            res = fetch_series(t, period=period, cache=store, cfg=cfg)
        except PriceFetchError as exc:
            flags.extend(exc.flags)
            if requested:
                flags.append(common.flag("fetch_failed", "high", str(exc)))
                failed[t] = "; ".join(exc.errors) or str(exc)
                missing.append(f"price history for {t} (all providers failed)")
            else:
                unrequested_errors[t] = "; ".join(exc.errors) or str(exc)
            return None
        flags.extend(res["flags"])
        df = truncate_to(res["frame"], as_of_d)
        if df is None or df.empty:
            reason = f"no bars on or before {as_of_d.isoformat()}"
            if requested:
                flags.append(common.flag("fetch_failed", "high", f"{t}: {reason}"))
                failed[t] = reason
            else:
                unrequested_errors[t] = reason
                missing.append(f"price history for {t} up to {as_of_d.isoformat()}")
            return None
        entry = summarize(t, df, cfg=cfg, is_fx=(t == fx_t))
        if res.get("fx_usd_per_native"):
            usd_close = float(df["Close"].iloc[-1])
            native_major = usd_close * float(res["usdils"])  # ILS per share (agorot / 100)
            entry["native"] = {
                "quote_currency": res["native_currency"],
                "currency": "ILS",
                "last_close": common.fact(_px(native_major), "ILS/share", f"D:PX:{t}", entry["last_date"],
                                          "price",
                                          derivation=f"quoted in {res['native_currency']}; /100 if agorot"),
                "usdils": _px(res["usdils"]),
                "usdils_as_of": res["fx_as_of"],
                "fx_usd_per_native": float(res["fx_usd_per_native"]),
            }
        entry.update({
            "provider": res["provider"],
            "stale": bool(res["stale"]),
            "series_path": res["series_path"],
            "source_id": f"D:PX:{t}",
            "source_url": res["source_url"],
            "fetched_at": common.to_jsonable(res["fetched_at"]),
        })
        lag = _lag_flag(t, entry["last_date"], as_of_d, cfg)
        if lag:
            flags.append(lag)
        sources.append(price_source(t, res))
        return entry

    for t in names:
        entry = process(t)
        if entry is not None:
            entries[t] = entry

    usdils: dict | None = None
    if include_fx:
        fx_entry = entries.get(fx_t)
        if fx_entry is None and fx_t not in failed:
            # an FX series fetched only for fx.usdils is not a requested ticker: it is reported
            # under fx (and sources), not under tickers / failed
            fx_entry = process(fx_t, requested=False)
        if fx_entry is not None:
            usdils = dict(fx_entry["last_close"])
            usdils["note"] = f"USD/ILS from {fx_t} ({fx_entry['provider']})"
        else:
            missing.append(f"USD/ILS FX rate ({fx_t})")
            why = unrequested_errors.get(fx_t) or failed.get(fx_t)
            flags.append(common.flag("fx_unavailable", "medium",
                                     f"no FX series for {fx_t}; fx.usdils is null" + (f" ({why})" if why else "")))

    payload = common.envelope(SCHEMA_NAME, as_of_d, sources=sources, missing_data=missing, flags=flags,
                              tickers=entries, failed=failed, fx={"usdils": usdils})
    if out is not None:
        common.write_json(out, payload)
    return payload


def _summary(payload: dict, out: Path | None) -> dict:
    usd = payload.get("fx", {}).get("usdils")
    return {
        "out": out.as_posix() if out else None,
        "as_of": payload.get("as_of"),
        "tickers": {t: {"provider": e["provider"], "last_date": e["last_date"],
                        "last_close": e["last_close"]["value"], "stale": e["stale"], "rows": e["rows"]}
                    for t, e in payload.get("tickers", {}).items()},
        "failed": payload.get("failed", {}),
        "fx_usdils": usd.get("value") if isinstance(usd, dict) else None,
        "flags": [f"{f['code']}:{f['severity']}" for f in payload.get("flags", [])],
    }


def main(argv: list[str] | None = None) -> int:
    common.setup_stdio()
    parser = argparse.ArgumentParser(description="Fetch ~3y daily OHLCV (yfinance -> Stooq -> stale cache) "
                                                 "and write prices/v1 JSON.")
    parser.add_argument("--tickers", action="append", default=[], help="comma-separated tickers (repeatable)")
    parser.add_argument("--ticker", action="append", default=[], help="a single ticker (repeatable)")
    parser.add_argument("--out", help="output JSON path (prints the payload when omitted)")
    parser.add_argument("--period", help="history period (default committee.yaml prices.period)")
    parser.add_argument("--no-fx", action="store_true", help="skip the USD/ILS FX series")
    common.add_common_args(parser)
    args = parser.parse_args(argv)
    try:
        common.apply_common_args(args)
        names, _bad = _normalize_list(list(args.tickers) + list(args.ticker))
        if not names and args.no_fx:
            parser.error("give --tickers or --ticker")
        out = Path(args.out) if args.out else None
        payload = run(names, out, as_of=args.as_of, period=args.period, include_fx=not args.no_fx)
    except common.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if out is None:
        print(common.dumps_json(payload))
    else:
        print(common.dumps_json(_summary(payload, out)))
    if names and not payload["tickers"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
