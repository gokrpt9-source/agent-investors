"""Generator for the dry-run fixture set (``tests/fixtures/dryrun``).

Two stages, both deterministic:

``python tests/fixtures/dryrun/build_fixtures.py data``
    (re)writes ``data/``: seeded synthetic OHLCV (``yf/history``), Yahoo info / fund shapes
    (``yf/info``, ``yf/funds``), 13 synthetic FRED CSVs and SEC EDGAR files (``http/``; the
    trimmed real-shaped KO / JPM / BRK-B files from ``tests/fixtures/edgar`` are copied, the
    other companies get compact synthetic companyfacts / submissions).

``python tests/fixtures/dryrun/build_fixtures.py agents``
    runs the REAL pipeline (``committee.init_run`` + ``run_next``) on the fixture config/data in a
    temporary runs dir and, at every agents step, AUTHORS each work-order output from that run's
    actual data / aggregate / packet files (``_authors.py``), writes it into the run and into
    ``agent_outputs/<order_id>.json`` with the run dir replaced by ``${RUN_DIR}``.

``all`` = data then agents (default). Everything here is SYNTHETIC test data — not market data,
not investment advice.
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

DATA = HERE / "data"
AS_OF = "2026-10-02"
N_BARS = 780
SYNTH = "SYNTHETIC dry-run fixture data (not real market data)"

HOLIDAYS = {
    "2023-11-23", "2023-12-25", "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27",
    "2024-06-19", "2024-07-04", "2024-09-02", "2024-11-28", "2024-12-25",
    "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26", "2025-06-19",
    "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25", "2026-01-01", "2026-01-19", "2026-02-16",
    "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
}

# ------------------------------------------------------------------------------------------------
# Securities. regime segments: (bars, drift per bar, daily vol) applied in order, ending at AS_OF.
# ------------------------------------------------------------------------------------------------

#: ticker -> (seed, end price, avg volume, segments)
PRICES: dict[str, tuple[int, float, float, list[tuple[int, float, float]]]] = {
    # holdings
    "QQQ": (11, 612.40, 42e6, [(400, 0.0007, 0.012), (120, -0.0012, 0.017), (260, 0.0011, 0.010)]),
    "SPMO": (12, 118.30, 2.1e6, [(380, 0.0009, 0.011), (140, -0.0009, 0.016), (260, 0.0012, 0.010)]),
    "BRK-B": (13, 502.65, 3.9e6, [(500, 0.0006, 0.009), (200, 0.0002, 0.010), (80, -0.0004, 0.011)]),
    "KO": (14, 85.65, 14e6, [(450, 0.0004, 0.008), (250, 0.0001, 0.008), (80, 0.0, 0.005)]),
    "PG": (15, 128.40, 7.5e6, [(420, 0.0003, 0.009), (200, -0.0006, 0.010), (160, -0.0011, 0.011)]),
    "JPM": (16, 318.90, 9.8e6, [(380, 0.0008, 0.012), (160, -0.0003, 0.015), (240, 0.0014, 0.011)]),
    # benchmark, sector ETFs, FX
    "SPY": (21, 668.10, 70e6, [(400, 0.0006, 0.009), (120, -0.0010, 0.014), (260, 0.0009, 0.008)]),
    "XLK": (22, 262.50, 8e6, [(400, 0.0008, 0.013), (120, -0.0015, 0.019), (260, 0.0013, 0.011)]),
    "XLV": (23, 141.80, 9e6, [(450, 0.0002, 0.009), (200, -0.0006, 0.010), (130, 0.0007, 0.009)]),
    "XLF": (24, 52.70, 38e6, [(400, 0.0007, 0.010), (140, -0.0004, 0.013), (240, 0.0009, 0.009)]),
    "XLE": (25, 88.40, 15e6, [(400, 0.0001, 0.013), (200, -0.0005, 0.014), (180, 0.0003, 0.012)]),
    "XLI": (26, 149.90, 10e6, [(400, 0.0006, 0.010), (140, -0.0008, 0.013), (240, 0.0010, 0.009)]),
    "XLY": (27, 228.30, 4e6, [(400, 0.0005, 0.012), (140, -0.0012, 0.016), (240, 0.0009, 0.011)]),
    "XLP": (28, 79.60, 11e6, [(450, 0.0003, 0.007), (200, -0.0002, 0.008), (130, -0.0004, 0.007)]),
    "XLU": (29, 84.20, 12e6, [(400, 0.0004, 0.009), (200, 0.0005, 0.010), (180, 0.0006, 0.009)]),
    "XLB": (30, 89.10, 6e6, [(400, 0.0002, 0.010), (200, -0.0004, 0.012), (180, 0.0004, 0.010)]),
    "XLRE": (31, 41.80, 7e6, [(400, -0.0001, 0.011), (200, 0.0002, 0.012), (180, 0.0001, 0.010)]),
    "XLC": (32, 112.60, 5e6, [(400, 0.0008, 0.012), (140, -0.0010, 0.016), (240, 0.0011, 0.010)]),
    "ILS=X": (33, 3.66, 0.0, [(400, 0.0000, 0.004), (200, -0.0002, 0.005), (180, -0.0001, 0.004)]),
    # Phase 3 universe (health_care, XLV top holdings)
    "LLY": (41, 905.20, 3.4e6, [(380, 0.0015, 0.018), (160, -0.0010, 0.020), (240, 0.0016, 0.016)]),
    "JNJ": (42, 171.40, 7.2e6, [(450, 0.0001, 0.008), (180, 0.0004, 0.009), (150, 0.0009, 0.008)]),
    "ABBV": (43, 214.60, 5.8e6, [(400, 0.0007, 0.012), (180, 0.0002, 0.013), (200, 0.0010, 0.011)]),
    "MRK": (44, 86.90, 11e6, [(380, 0.0002, 0.012), (220, -0.0012, 0.014), (180, 0.0010, 0.011)]),
    "TMO": (45, 512.30, 1.6e6, [(400, 0.0001, 0.013), (200, -0.0009, 0.015), (180, 0.0006, 0.012)]),
    "ISRG": (46, 498.70, 1.5e6, [(400, 0.0012, 0.015), (200, -0.0004, 0.017), (180, 0.0005, 0.014)]),
    "ABT": (47, 131.20, 5.5e6, [(420, 0.0004, 0.010), (200, 0.0001, 0.011), (160, 0.0002, 0.010)]),
}

#: trailing returns pinned per ticker: {bars: return %} (63 ~ 3m, 126 ~ 6m, 252 ~ 12m)
ANCHORS: dict[str, dict[int, float]] = {
    "SPY": {63: 3.2, 126: 7.1, 252: 14.2}, "QQQ": {63: 4.1, 126: 9.3, 252: 18.4},
    "SPMO": {63: 6.8, 126: 13.5, 252: 24.9}, "XLK": {63: 4.4, 126: 10.2, 252: 20.3},
    "XLV": {63: 2.1, 126: 3.4, 252: 4.2}, "XLF": {63: 2.2, 126: 6.1, 252: 15.3},
    "XLE": {63: 1.1, 126: -3.2, 252: -6.4}, "XLI": {63: 2.3, 126: 5.2, 252: 12.1},
    "XLY": {63: 2.0, 126: 5.1, 252: 10.2}, "XLP": {63: -1.2, 126: -0.8, 252: 2.1},
    "XLU": {63: 3.1, 126: 6.2, 252: 12.4}, "XLB": {63: 1.0, 126: 2.1, 252: 3.2},
    "XLRE": {63: 1.1, 126: 2.0, 252: 1.3}, "XLC": {63: 3.3, 126: 8.2, 252: 18.1},
    "ILS=X": {63: -0.6, 126: -1.4, 252: -2.9},
    "KO": {63: -0.8, 126: 1.2, 252: 5.3}, "PG": {63: -5.1, 126: -8.2, 252: -12.4},
    "JPM": {63: 4.2, 126: 6.3, 252: 20.4}, "BRK-B": {63: 0.4, 126: 2.2, 252: 8.1},
    "MRK": {63: 6.2, 126: 6.6, 252: -7.9}, "LLY": {63: 6.1, 126: 12.2, 252: 25.3},
    "JNJ": {63: 3.1, 126: 6.2, 252: 12.2}, "ABBV": {63: 3.2, 126: 6.4, 252: 15.1},
    "ABT": {63: 1.2, 126: 3.1, 252: 6.2}, "TMO": {63: 2.2, 126: 4.1, 252: -5.2},
    "ISRG": {63: -2.1, 126: 0.3, 252: 10.4},
}

#: Yahoo info (sector label, shares outstanding, quoteType, long name, extra fields)
INFO: dict[str, dict[str, Any]] = {
    "QQQ": {"quoteType": "ETF", "longName": "Invesco QQQ Trust, Series 1", "netExpenseRatio": 0.18,
            "category": "Large Growth"},
    "SPMO": {"quoteType": "ETF", "longName": "Invesco S&P 500 Momentum ETF", "netExpenseRatio": 0.13,
             "category": "Large Growth"},
    "SPY": {"quoteType": "ETF", "longName": "SPDR S&P 500 ETF Trust", "netExpenseRatio": 0.0945},
    "BRK-B": {"sector": "Financial Services", "industry": "Insurance - Diversified",
              "longName": "Berkshire Hathaway Inc.", "shares": 1408035161, "implied": 2140709794},
    "KO": {"sector": "Consumer Defensive", "industry": "Beverages - Non-Alcoholic",
           "longName": "The Coca-Cola Company", "shares": 4302549243},
    "PG": {"sector": "Consumer Defensive", "industry": "Household & Personal Products",
           "longName": "The Procter & Gamble Company", "shares": 2342000000},
    "JPM": {"sector": "Financial Services", "industry": "Banks - Diversified",
            "longName": "JPMorgan Chase & Co.", "shares": 2749000000},
    "LLY": {"sector": "Healthcare", "industry": "Drug Manufacturers - General",
            "longName": "Eli Lilly and Company", "shares": 897000000},
    "JNJ": {"sector": "Healthcare", "industry": "Drug Manufacturers - General",
            "longName": "Johnson & Johnson", "shares": 2408000000},
    "ABBV": {"sector": "Healthcare", "industry": "Drug Manufacturers - General",
             "longName": "AbbVie Inc.", "shares": 1767000000},
    "MRK": {"sector": "Healthcare", "industry": "Drug Manufacturers - General",
            "longName": "Merck & Co., Inc.", "shares": 2497000000},
    "TMO": {"sector": "Healthcare", "industry": "Diagnostics & Research",
            "longName": "Thermo Fisher Scientific Inc.", "shares": 377000000},
    "ISRG": {"sector": "Healthcare", "industry": "Medical Instruments & Supplies",
             "longName": "Intuitive Surgical, Inc.", "shares": 358000000},
    "ABT": {"sector": "Healthcare", "industry": "Medical Devices",
            "longName": "Abbott Laboratories", "shares": 1741000000},
}

#: info-only names (fund top holdings): ticker -> (sector label, market cap, long name)
INFO_ONLY: dict[str, tuple[str, float, str]] = {
    "AAPL": ("Technology", 3.6e12, "Apple Inc."),
    "MSFT": ("Technology", 3.7e12, "Microsoft Corporation"),
    "NVDA": ("Technology", 4.3e12, "NVIDIA Corporation"),
    "AMZN": ("Consumer Cyclical", 2.4e12, "Amazon.com, Inc."),
    "AVGO": ("Technology", 1.6e12, "Broadcom Inc."),
    "META": ("Communication Services", 1.8e12, "Meta Platforms, Inc."),
    "GOOGL": ("Communication Services", 2.9e12, "Alphabet Inc."),
    "COST": ("Consumer Defensive", 4.2e11, "Costco Wholesale Corporation"),
    "NFLX": ("Communication Services", 5.1e11, "Netflix, Inc."),
    "TSLA": ("Consumer Cyclical", 1.4e12, "Tesla, Inc."),
    "GE": ("Industrials", 3.0e11, "GE Aerospace"),
    "ORCL": ("Technology", 6.4e11, "Oracle Corporation"),
}

FUNDS: dict[str, dict[str, Any]] = {
    "QQQ": {
        "sector_weightings": {"technology": 0.505, "communication_services": 0.158, "consumer_cyclical": 0.132,
                              "consumer_defensive": 0.052, "healthcare": 0.058, "industrials": 0.048,
                              "utilities": 0.014, "basic_materials": 0.013, "financial_services": 0.005,
                              "energy": 0.005, "realestate": 0.010},
        "top_holdings": [("NVDA", 9.4), ("MSFT", 8.6), ("AAPL", 8.1), ("AMZN", 5.4), ("AVGO", 5.0),
                         ("META", 3.9), ("GOOGL", 3.1), ("TSLA", 2.9), ("COST", 2.5), ("NFLX", 2.4)],
        "expense_ratio_pct": 0.18, "expense_ratio_field": "info.netExpenseRatio",
    },
    "SPMO": {
        "sector_weightings": {"technology": 0.312, "financial_services": 0.196, "communication_services": 0.148,
                              "industrials": 0.118, "consumer_cyclical": 0.078, "healthcare": 0.072,
                              "consumer_defensive": 0.042, "energy": 0.018, "utilities": 0.010,
                              "realestate": 0.003, "basic_materials": 0.003},
        "top_holdings": [("META", 7.1), ("NVDA", 6.9), ("AVGO", 6.2), ("JPM", 5.1), ("AMZN", 4.9),
                         ("MSFT", 4.4), ("BRK.B", 3.9), ("COST", 3.1), ("LLY", 2.9), ("GE", 2.6)],
        "expense_ratio_pct": 0.13, "expense_ratio_field": "funds_data.fund_operations.Annual Report Expense Ratio",
    },
    "XLV": {
        "sector_weightings": {"healthcare": 1.0},
        "top_holdings": [("LLY", 12.4), ("JNJ", 8.1), ("ABBV", 7.2), ("ABT", 4.6), ("MRK", 4.4),
                         ("TMO", 4.1), ("ISRG", 3.9)],
        "expense_ratio_pct": 0.08, "expense_ratio_field": "info.netExpenseRatio",
    },
}

#: real CIKs; KO / JPM / BRK-B use the trimmed real-shaped files in tests/fixtures/edgar.
CIKS: dict[str, int] = {"KO": 21344, "JPM": 19617, "BRK-B": 1067983, "PG": 80424, "LLY": 59478,
                        "JNJ": 200406, "ABBV": 1551152, "MRK": 310158, "TMO": 97745, "ISRG": 1035267,
                        "ABT": 1800}
REAL_SHAPED = ("KO", "JPM", "BRK-B")

#: synthetic fundamentals, USD millions: FY2025 revenue, revenue growth, op margin, net margin,
#: D&A % rev, capex % rev, OCF % rev, cash, LT debt, equity, goodwill, intangibles, tax rate, SIC
FUND_PARAMS: dict[str, dict[str, Any]] = {
    "PG": dict(rev=84500, g=0.025, om=0.265, nm=0.205, da=0.034, capex=0.044, ocf=0.240, cash=9800,
               debt=24800, equity=51200, gw=40600, intang=23400, tax=0.205, sic="2840",
               desc="Perfumes, Cosmetics & Other Toilet Preparations", name="PROCTER & GAMBLE Co"),
    "LLY": dict(rev=59400, g=0.32, om=0.43, nm=0.33, da=0.030, capex=0.090, ocf=0.290, cash=4100,
                debt=38900, equity=21800, gw=4300, intang=8600, tax=0.165, sic="2834",
                desc="Pharmaceutical Preparations", name="ELI LILLY & Co"),
    "JNJ": dict(rev=92100, g=0.045, om=0.262, nm=0.205, da=0.078, capex=0.051, ocf=0.262, cash=21300,
                debt=36800, equity=78400, gw=44900, intang=37200, tax=0.170, sic="2834",
                desc="Pharmaceutical Preparations", name="JOHNSON & JOHNSON"),
    "ABBV": dict(rev=61200, g=0.075, om=0.305, nm=0.115, da=0.135, capex=0.022, ocf=0.305, cash=6200,
                 debt=62400, equity=3100, gw=34600, intang=56800, tax=0.140, sic="2834",
                 desc="Pharmaceutical Preparations", name="AbbVie Inc."),
    "MRK": dict(rev=64200, g=0.015, om=0.318, nm=0.262, da=0.062, capex=0.058, ocf=0.270, cash=9400,
                debt=34100, equity=48200, gw=21900, intang=17100, tax=0.155, sic="2834",
                desc="Pharmaceutical Preparations", name="Merck & Co., Inc."),
    "TMO": dict(rev=43900, g=0.035, om=0.185, nm=0.150, da=0.072, capex=0.032, ocf=0.192, cash=4600,
                debt=31200, equity=50100, gw=46100, intang=15900, tax=0.090, sic="3826",
                desc="Laboratory Analytical Instruments", name="THERMO FISHER SCIENTIFIC INC."),
    "ISRG": dict(rev=10000, g=0.19, om=0.285, nm=0.275, da=0.060, capex=0.110, ocf=0.330, cash=4300,
                 debt=0, equity=17500, gw=360, intang=40, tax=0.160, sic="3845",
                 desc="Electromedical & Electrotherapeutic Apparatus", name="INTUITIVE SURGICAL INC"),
    "ABT": dict(rev=44300, g=0.065, om=0.180, nm=0.150, da=0.068, capex=0.050, ocf=0.200, cash=7600,
                debt=12900, equity=48900, gw=23400, intang=6400, tax=0.150, sic="2834",
                desc="Pharmaceutical Preparations", name="ABBOTT LABORATORIES"),
}

FRED: dict[str, tuple[str, float, float, float, int]] = {
    # id: (frequency, start value, end value, noise, seed)
    "DFF": ("d", 5.33, 3.88, 0.004, 101),
    "DGS2": ("d", 4.70, 3.62, 0.03, 102),
    "DGS10": ("d", 4.25, 4.18, 0.03, 103),
    "T10Y2Y": ("d", -0.42, 0.56, 0.02, 104),
    "CPIAUCSL": ("m", 313.2, 329.6, 0.10, 105),
    "CPILFESL": ("m", 318.9, 337.1, 0.08, 106),
    "UNRATE": ("m", 4.1, 4.4, 0.05, 107),
    "PAYEMS": ("m", 158900, 160300, 40, 108),
    "INDPRO": ("m", 102.8, 104.1, 0.25, 109),
    "DTWEXBGS": ("d", 123.5, 119.8, 0.25, 110),
    "DCOILWTICO": ("d", 81.0, 66.4, 0.9, 111),
    "BAMLH0A0HYM2": ("d", 3.20, 3.05, 0.03, 112),
    "VIXCLS": ("d", 15.5, 16.9, 0.9, 113),
}


# ------------------------------------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------------------------------------


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, indent=1, ensure_ascii=False)
        fh.write("\n")


def _read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def trading_days(end: str = AS_OF, n: int = N_BARS) -> pd.DatetimeIndex:
    days = pd.bdate_range(end=end, periods=n + 60)
    days = days[[d.strftime("%Y-%m-%d") not in HOLIDAYS for d in days]]
    return days[-n:]


def make_ohlcv(ticker: str) -> pd.DataFrame:
    seed, end_price, avg_vol, segments = PRICES[ticker]
    rng = np.random.default_rng(seed)
    idx = trading_days()
    n = len(idx)
    drifts, vols = [], []
    for bars, mu, sig in segments:
        drifts += [mu] * bars
        vols += [sig] * bars
    drifts = (drifts + [drifts[-1]] * n)[:n]
    vols = (vols + [vols[-1]] * n)[:n]
    rets = np.array(drifts) + np.array(vols) * rng.standard_normal(n)
    logp = anchor_returns(np.cumsum(rets), ANCHORS.get(ticker, {}))
    close = np.exp(logp - logp[-1]) * end_price
    gap = np.array(vols) * 0.35 * rng.standard_normal(n)
    open_ = np.empty(n)
    open_[0] = close[0] * (1 + gap[0])
    open_[1:] = close[:-1] * (1 + gap[1:])
    hi_ext = np.abs(rng.standard_normal(n)) * np.array(vols) * 0.6
    lo_ext = np.abs(rng.standard_normal(n)) * np.array(vols) * 0.6
    high = np.maximum(open_, close) * (1 + hi_ext)
    low = np.minimum(open_, close) * (1 - lo_ext)
    if avg_vol > 0:
        volume = np.round(avg_vol * np.exp(0.25 * rng.standard_normal(n) - 0.03)).astype(np.int64)
    else:
        volume = np.zeros(n, dtype=np.int64)
    dp = 4 if end_price < 10 else 2
    return pd.DataFrame({"Date": idx.strftime("%Y-%m-%d"), "Open": np.round(open_, dp),
                         "High": np.round(high, dp), "Low": np.round(low, dp),
                         "Close": np.round(close, dp), "Volume": volume})


def anchor_returns(logp: np.ndarray, anchors: dict[int, float]) -> np.ndarray:
    """Pin trailing returns: for each window w (bars) the return close[-1] / close[-1-w] - 1 equals
    ``anchors[w]`` (percent). Linear ramps keep the noise texture; each longer window ramps only over
    the bars before the next shorter window, so the shorter returns are preserved."""
    out = logp.copy()
    n = len(out)
    prev_w = 0
    for w in sorted(anchors):
        target = math.log(1 + anchors[w] / 100.0)
        current = out[-1] - out[n - 1 - w]
        delta = target - current
        ramp = np.zeros(n)
        a, b = n - 1 - w, n - 1 - prev_w          # ramp over (a, b], flat after b
        ramp[a + 1:b + 1] = delta * np.arange(1, b - a + 1) / (b - a)
        ramp[b + 1:] = delta
        out = out + ramp
        prev_w = w
    return out


def fred_csv(series: str) -> str:
    freq, start, end, noise, seed = FRED[series]
    rng = np.random.default_rng(seed)
    if freq == "d":
        idx = pd.bdate_range("2024-07-01", "2026-10-01")
    else:
        idx = pd.date_range("2024-07-01", "2026-08-01", freq="MS")
    n = len(idx)
    base = np.linspace(start, end, n)
    vals = base + noise * np.cumsum(rng.standard_normal(n)) / max(1.0, math.sqrt(n) / 3)
    vals[-1] = end
    dp = 0 if series == "PAYEMS" else (3 if freq == "m" else 2)
    lines = [f"observation_date,{series}"]
    for d, v in zip(idx, vals):
        lines.append(f"{d.strftime('%Y-%m-%d')},{v:.{dp}f}")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------------------------------
# synthetic SEC EDGAR
# ------------------------------------------------------------------------------------------------


def _accn(cik: int, yy: int, n: int) -> str:
    return f"{cik:010d}-{yy:02d}-{n:06d}"


def synth_companyfacts(ticker: str) -> tuple[dict, dict]:
    p = FUND_PARAMS[ticker]
    cik = CIKS[ticker]
    rev25 = float(p["rev"])
    g = float(p["g"])
    years = [2021, 2022, 2023, 2024, 2025]
    rev = {y: rev25 / ((1 + g) ** (2025 - y)) for y in years}
    # 10-K filings (filed mid-Feb of the next year) and the 2026 10-Qs
    k_accn = {y: _accn(cik, (y + 1) % 100, 10 + y - 2021) for y in years}
    k_filed = {y: f"{y + 1}-02-{12 + (y % 3)}" for y in years}
    q1_accn, q1_filed = _accn(cik, 26, 31), "2026-05-01"
    q2_accn, q2_filed = _accn(cik, 26, 32), "2026-08-04"
    # quarterly splits of revenue: 2025 quarters ~ FY/4 with seasonality; 2026 grows at g
    season = [0.245, 0.255, 0.248, 0.252]
    q25 = [rev[2025] * s for s in season]
    q26 = [x * (1 + g) for x in q25[:2]]

    def flows(ratio_key: str, sign: float = 1.0) -> list[dict]:
        ratio = float(p[ratio_key])
        out: list[dict] = []
        for y in years:
            v = round(rev[y] * ratio * 1e6 * sign)
            out.append({"start": f"{y}-01-01", "end": f"{y}-12-31", "val": v, "accn": k_accn[y], "fy": y,
                        "fp": "FY", "form": "10-K", "filed": k_filed[y]})
            if y >= 2024:  # comparatives restated in the next 10-K
                pass
        for (yr, qs, accn, filed) in ((2026, q26, q1_accn, q1_filed),):
            out.append({"start": "2026-01-01", "end": "2026-03-31", "val": round(qs[0] * ratio * 1e6 * sign),
                        "accn": accn, "fy": 2026, "fp": "Q1", "form": "10-Q", "filed": filed})
            out.append({"start": "2025-01-01", "end": "2025-03-31", "val": round(q25[0] * ratio * 1e6 * sign),
                        "accn": accn, "fy": 2026, "fp": "Q1", "form": "10-Q", "filed": filed})
        out.append({"start": "2026-04-01", "end": "2026-06-30", "val": round(q26[1] * ratio * 1e6 * sign),
                    "accn": q2_accn, "fy": 2026, "fp": "Q2", "form": "10-Q", "filed": q2_filed})
        out.append({"start": "2025-04-01", "end": "2025-06-30", "val": round(q25[1] * ratio * 1e6 * sign),
                    "accn": q2_accn, "fy": 2026, "fp": "Q2", "form": "10-Q", "filed": q2_filed})
        out.append({"start": "2026-01-01", "end": "2026-06-30",
                    "val": round((q26[0] + q26[1]) * ratio * 1e6 * sign),
                    "accn": q2_accn, "fy": 2026, "fp": "Q2", "form": "10-Q", "filed": q2_filed})
        out.append({"start": "2025-01-01", "end": "2025-06-30",
                    "val": round((q25[0] + q25[1]) * ratio * 1e6 * sign),
                    "accn": q2_accn, "fy": 2026, "fp": "Q2", "form": "10-Q", "filed": q2_filed})
        return out

    p_tax = float(p["tax"])
    p_pretax = float(p["nm"]) / (1 - p_tax)
    ratios = dict(p)
    ratios.update(one=1.0, pretax=p_pretax, taxexp=p_pretax * p_tax,
                  cogs=1 - float(p["om"]) - 0.22)
    p.update(ratios)

    def instants(key: str) -> list[dict]:
        base = float(p[key]) * 1e6
        return [
            {"end": "2024-12-31", "val": round(base * 0.94), "accn": k_accn[2024], "fy": 2024, "fp": "FY",
             "form": "10-K", "filed": k_filed[2024]},
            {"end": "2025-12-31", "val": round(base), "accn": k_accn[2025], "fy": 2025, "fp": "FY",
             "form": "10-K", "filed": k_filed[2025]},
            {"end": "2026-06-30", "val": round(base * 1.02), "accn": q2_accn, "fy": 2026, "fp": "Q2",
             "form": "10-Q", "filed": q2_filed},
        ]

    shares = float(INFO[ticker]["shares"])
    us_gaap: dict[str, Any] = {
        "Revenues": flows("one"),
        "CostOfRevenue": flows("cogs"),
        "OperatingIncomeLoss": flows("om"),
        "NetIncomeLoss": flows("nm"),
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": flows("pretax"),
        "IncomeTaxExpenseBenefit": flows("taxexp"),
        "NetCashProvidedByUsedInOperatingActivities": flows("ocf"),
        "PaymentsToAcquirePropertyPlantAndEquipment": flows("capex"),
        "DepreciationDepletionAndAmortization": flows("da"),
        "CashAndCashEquivalentsAtCarryingValue": instants("cash"),
        "StockholdersEquity": instants("equity"),
        "LongTermDebt": instants("debt"),
        "Goodwill": instants("gw"),
        "IntangibleAssetsNetExcludingGoodwill": instants("intang"),
    }
    facts = {"cik": cik, "entityName": p["name"], "_comment": SYNTH,
             "facts": {"us-gaap": {k: {"label": k, "units": {"USD": v}} for k, v in us_gaap.items()},
                       "dei": {"EntityCommonStockSharesOutstanding": {"label": "Entity Common Stock, Shares Outstanding",
                                                                      "units": {"shares": [
                           {"end": "2026-04-24", "val": round(shares * 1.004), "accn": q1_accn, "fy": 2026,
                            "fp": "Q1", "form": "10-Q", "filed": q1_filed},
                           {"end": "2026-07-24", "val": round(shares), "accn": q2_accn, "fy": 2026, "fp": "Q2",
                            "form": "10-Q", "filed": q2_filed}]}}}}}
    recent = [
        ("8-K", "2026-09-15", None, f"{ticker.lower()}-8k-20260915.htm", _accn(cik, 26, 41)),
        ("4", "2026-08-20", None, "form4.xml", _accn(cik, 26, 40)),
        ("10-Q", q2_filed, "2026-06-30", f"{ticker.lower()}-20260630.htm", q2_accn),
        ("8-K", "2026-07-28", None, f"{ticker.lower()}-8k-20260728.htm", _accn(cik, 26, 39)),
        ("10-Q", q1_filed, "2026-03-31", f"{ticker.lower()}-20260331.htm", q1_accn),
        ("DEF 14A", "2026-03-20", None, f"{ticker.lower()}-def14a.htm", _accn(cik, 26, 38)),
        ("10-K", k_filed[2025], "2025-12-31", f"{ticker.lower()}-20251231.htm", k_accn[2025]),
        ("10-K", k_filed[2024], "2024-12-31", f"{ticker.lower()}-20241231.htm", k_accn[2024]),
    ]
    subs = {"cik": f"{cik:010d}", "entityType": "operating", "sic": p["sic"], "sicDescription": p["desc"],
            "name": p["name"], "tickers": [ticker], "exchanges": ["NYSE"], "fiscalYearEnd": "1231",
            "_comment": SYNTH,
            "filings": {"recent": {
                "accessionNumber": [r[4] for r in recent], "filingDate": [r[1] for r in recent],
                "reportDate": [r[2] for r in recent], "form": [r[0] for r in recent],
                "primaryDocument": [r[3] for r in recent]}}}
    return facts, subs


# ------------------------------------------------------------------------------------------------
# data stage
# ------------------------------------------------------------------------------------------------


def build_data() -> None:
    if DATA.exists():
        shutil.rmtree(DATA)
    hist = DATA / "yf" / "history"
    hist.mkdir(parents=True)
    closes: dict[str, float] = {}
    for t in PRICES:
        df = make_ohlcv(t)
        closes[t] = float(df["Close"].iloc[-1])
        df.to_csv(hist / f"{t}.csv", index=False, encoding="utf-8", lineterminator="\n")
    # Yahoo info
    for t, meta in INFO.items():
        price = closes[t]
        doc: dict[str, Any] = {"_comment": SYNTH, "symbol": t, "longName": meta["longName"],
                               "shortName": meta["longName"], "quoteType": meta.get("quoteType", "EQUITY"),
                               "currency": "USD", "exchange": "NYQ", "currentPrice": price,
                               "regularMarketPrice": price}
        if doc["quoteType"] == "EQUITY":
            shares = float(meta["shares"])
            doc.update(sector=meta["sector"], industry=meta["industry"], sharesOutstanding=int(shares),
                       impliedSharesOutstanding=int(meta.get("implied", shares)),
                       marketCap=int(round(price * float(meta.get("implied", shares)))))
        else:
            doc.update(netExpenseRatio=meta.get("netExpenseRatio"), category=meta.get("category"))
        _write_json(DATA / "yf" / "info" / f"{t}.json", doc)
    for t, (sector, mcap, name) in INFO_ONLY.items():
        _write_json(DATA / "yf" / "info" / f"{t}.json",
                    {"_comment": SYNTH, "symbol": t, "longName": name, "quoteType": "EQUITY", "currency": "USD",
                     "sector": sector, "marketCap": mcap})
    for t, f in FUNDS.items():
        _write_json(DATA / "yf" / "funds" / f"{t}.json",
                    {"_comment": SYNTH, "sector_weightings": f["sector_weightings"],
                     "top_holdings": [{"symbol": s, "name": f"{s} (synthetic)", "holding_pct": w}
                                      for s, w in f["top_holdings"]],
                     "expense_ratio_pct": f["expense_ratio_pct"], "expense_ratio_field": f["expense_ratio_field"]})
    # HTTP: FRED + SEC
    http = DATA / "http"
    index: dict[str, Any] = {"_comment": SYNTH + " — FRED CSVs and SEC EDGAR JSON served by URL"}
    for sid in FRED:
        rel = f"fred/{sid}.csv"
        (http / "fred").mkdir(parents=True, exist_ok=True)
        with open(http / rel, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(fred_csv(sid))
        index[f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"] = rel
    edgar_src = REPO / "tests" / "fixtures" / "edgar" / "http" / "sec"
    sec = http / "sec"
    sec.mkdir(parents=True, exist_ok=True)
    tickers_doc: dict[str, Any] = {}
    for i, (t, cik) in enumerate(CIKS.items()):
        title = FUND_PARAMS[t]["name"] if t in FUND_PARAMS else {"KO": "COCA COLA CO", "JPM": "JPMORGAN CHASE & CO",
                                                                  "BRK-B": "BERKSHIRE HATHAWAY INC"}[t]
        tickers_doc[str(i)] = {"cik_str": cik, "ticker": t, "title": title}
        cf_rel, sub_rel = f"sec/companyfacts_CIK{cik:010d}.json", f"sec/submissions_CIK{cik:010d}.json"
        if t in REAL_SHAPED:
            shutil.copyfile(edgar_src / f"companyfacts_CIK{cik:010d}.json", http / cf_rel)
            shutil.copyfile(edgar_src / f"submissions_CIK{cik:010d}.json", http / sub_rel)
        else:
            facts, subs = synth_companyfacts(t)
            _write_json(http / cf_rel, facts)
            _write_json(http / sub_rel, subs)
        index[f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"] = cf_rel
        index[f"https://data.sec.gov/submissions/CIK{cik:010d}.json"] = sub_rel
    tickers_doc[str(len(tickers_doc))] = {"cik_str": 1067983, "ticker": "BRK-A", "title": "BERKSHIRE HATHAWAY INC"}
    _write_json(sec / "company_tickers.json", tickers_doc)
    index["https://www.sec.gov/files/company_tickers.json"] = "sec/company_tickers.json"
    _write_json(http / "index.json", index)
    print(f"data: {len(PRICES)} price series, {len(INFO) + len(INFO_ONLY)} info, {len(FUNDS)} funds, "
          f"{len(FRED)} FRED series, {len(CIKS)} SEC companies -> {DATA.as_posix()}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", nargs="?", default="all", choices=("data", "agents", "all"))
    args = ap.parse_args(argv)
    if args.stage in ("data", "all"):
        build_data()
    if args.stage in ("agents", "all"):
        from _authors import build_agent_outputs  # noqa: PLC0415 (sibling module)
        build_agent_outputs(HERE)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    sys.exit(main())
