"""alerts.py: end-of-day warning-sign rules and the US market clock. Offline, pure functions."""
from __future__ import annotations

import json
import shutil
import threading
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from scripts import alerts, common, dashboard

PROFILE = {"max_position_pct": 10, "max_fund_position_pct": 40, "max_sector_pct": 35, "target_cash_pct": 5}
CLOSED = {"open": False, "last_session": date(2026, 10, 26), "closed_at_utc": "x", "et_time": "x"}
OPEN = {**CLOSED, "open": True}


def row(t, value, weight, *, shares=10, kind="stock", day=0.0, pl=5.0, **kw):
    return {"ticker": t, "asset_type": kind, "value_usd": value, "weight_pct": weight, "shares": shares, "day_pct": day,
            "pl_pct": pl, "proxy": False, "error": None, **kw}


def snap(rows, cash_w=6.0, total=10_000.0, fx_day=0.0, as_of="2026-10-26"):
    return {"rows": rows, "totals": {"value_usd": total}, "cash": {"weight_pct": cash_w},
            "fx": {"day_pct": fx_day}, "as_of_config": as_of}


def rules(al):
    return {(a["rule"], a["ticker"]) for a in al}


def ev(s, **k):
    return alerts.evaluate(s, PROFILE, k.pop("cfg", None), market=k.pop("market", CLOSED), today=date(2026, 10, 27), **k)


# --------------------------------------------------------------------------- market clock
def test_dst_offset_matches_the_system_tz_database_for_a_whole_year():
    ny = ZoneInfo("America/New_York")
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    while t.year == 2026:
        assert alerts.et_offset_hours(t) * 3600 == t.astimezone(ny).utcoffset().total_seconds(), t
        t += timedelta(hours=1)


@pytest.mark.parametrize("utc,is_open,last", [
    ("2026-10-27T13:29", False, date(2026, 10, 26)),     # 09:29 ET before the open
    ("2026-10-27T13:30", True, date(2026, 10, 27)),      # 09:30 ET open
    ("2026-10-27T19:59", True, date(2026, 10, 27)),      # 15:59 ET
    ("2026-10-27T20:00", False, date(2026, 10, 27)),     # 16:00 ET closed: final for today
    ("2026-10-31T15:00", False, date(2026, 10, 30)),     # Saturday -> Friday's session
    ("2026-11-02T20:00", True, date(2026, 11, 2)),       # after DST ends: 15:00 EST, still open
    ("2026-11-02T21:00", False, date(2026, 11, 2)),      # 16:00 EST closed
])
def test_market_state(utc, is_open, last):
    st = alerts.market_state(datetime.fromisoformat(utc).replace(tzinfo=timezone.utc), holidays=[])
    assert st["open"] is is_open and st["last_session"] == last


def test_holiday_is_skipped():
    st = alerts.market_state(datetime(2026, 11, 27, 15, 0, tzinfo=timezone.utc), holidays=[date(2026, 11, 26)])
    assert st["open"] is True and st["last_session"] == date(2026, 11, 27)
    st = alerts.market_state(datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc), holidays=[date(2026, 11, 26)])
    assert st["open"] is False and st["last_session"] == date(2026, 11, 25)


# --------------------------------------------------------------------------- limit rules
def test_position_cap_with_sell_size_and_boundaries():
    # 12% of 10,000 = 1,200 across 12 shares ($100 each); cap 10% => keep 10 shares => sell 2
    a = ev(snap([row("BRK-B", 1200, 12.0, shares=12)]))
    pc = next(x for x in a if x["rule"] == "position_cap")
    assert pc["severity"] == "critical" and "Sell about 2 shares" in pc["action"] and "12.0%" in pc["why"]
    assert not [x for x in ev(snap([row("BRK-B", 1000, 10.0)])) if x["rule"] == "position_cap"]    # at the cap: fine
    assert [x for x in ev(snap([row("BRK-B", 1001, 10.01)])) if x["rule"] == "position_cap"]


def test_fund_cap_uses_fund_limit_and_units():
    f = row("1186063", 4500, 45.0, shares=9.4, kind="fund", units=87, proxy=True)
    a = ev(snap([f]))
    fc = next(x for x in a if x["rule"] == "fund_cap")
    assert "units" in fc["action"] and "40%" in fc["why"]
    assert not [x for x in ev(snap([row("QQQ", 3900, 39.0, kind="fund")])) if x["rule"] == "fund_cap"]
    assert not [x for x in ev(snap([row("QQQ", 3900, 39.0, kind="fund")])) if x["rule"] == "position_cap"]   # funds use their own cap


def test_sector_cap_looks_through_funds_and_names_the_biggest():
    rows = [row("QQQ", 4000, 40.0, kind="fund"), row("PLTR", 800, 8.0)]
    splits = {"QQQ": {"information_technology": 80.0, "communication_services": 20.0}, "PLTR": {"information_technology": 100.0}}
    a = ev(snap(rows), splits=splits)
    sc = next(x for x in a if x["rule"] == "sector_cap")
    assert "Information Technology" in sc["title"] and "40.0%" in sc["why"] and "QQQ 32.0%" in sc["why"]
    assert not [x for x in ev(snap([row("QQQ", 3000, 30.0, kind="fund")]), splits=splits) if x["rule"] == "sector_cap"]
    # unclassified never alerts; no split data => sector rule skipped without error
    assert not [x for x in ev(snap([row("IBIT", 6000, 60.0, kind="fund")]), splits={}) if x["rule"] == "sector_cap"]
    assert not [x for x in ev(snap(rows), splits=None) if x["rule"] == "sector_cap"]


def test_cash_floor():
    assert ("cash_floor", None) in rules(ev(snap([], cash_w=1.0)))
    assert ("cash_floor", None) not in rules(ev(snap([], cash_w=5.0)))


# --------------------------------------------------------------------------- price rules
def test_day_drop_levels_for_stocks_and_funds():
    a = ev(snap([row("KO", 500, 5.0, day=-4.9), row("PG", 500, 5.0, day=-5.0), row("PLTR", 500, 5.0, day=-8.5),
                 row("QQQ", 500, 5.0, kind="fund", day=-3.1), row("SPY", 500, 5.0, kind="fund", day=-5.0)]))
    sev = {x["ticker"]: x["severity"] for x in a if x["rule"] == "day_drop"}
    assert "KO" not in sev and sev["PG"] == "warning" and sev["PLTR"] == "critical"
    assert sev["QQQ"] == "warning" and sev["SPY"] == "critical"


def test_deep_loss_levels():
    a = ev(snap([row("A", 500, 5.0, pl=-14.9), row("B", 500, 5.0, pl=-15.0), row("C", 500, 5.0, pl=-25.0)]))
    sev = {x["ticker"]: x["severity"] for x in a if x["rule"] == "deep_loss"}
    assert "A" not in sev and sev["B"] == "warning" and sev["C"] == "critical"


def test_stop_breached_uses_the_committee_stop_and_skips_proxies():
    run = {"id": "r1", "as_of": "2026-10-25", "invalidations": {"PLTR": 190.0, "1186063": 10.0}, "plan": {}}
    rows = [row("PLTR", 1800, 18.0, shares=10), row("1186063", 100, 1.0, shares=1, proxy=True, units=2)]
    a = ev(snap(rows), run=run)
    sb = [x for x in a if x["rule"] == "stop_breached"]
    assert [x["ticker"] for x in sb] == ["PLTR"] and sb[0]["severity"] == "critical" and "190.00" in sb[0]["why"]
    assert not [x for x in ev(snap([row("PLTR", 1900, 19.0, shares=10)]), run=run) if x["rule"] == "stop_breached"]   # price == stop


def test_pending_committee_action_only_while_position_unchanged():
    run = {"id": "r1", "as_of": "2026-10-25", "invalidations": {},
           "plan": {"QCOM": {"side": "SELL", "shares": 9, "shares_cur": 9, "action": "TRIM"}}}
    assert ("pending_action", "QCOM") in rules(ev(snap([row("QCOM", 1600, 16.0, shares=9)]), run=run))
    assert ("pending_action", "QCOM") not in rules(ev(snap([row("QCOM", 800, 8.0, shares=4)]), run=run))   # already sold some


# --------------------------------------------------------------------------- hygiene + ordering + finality
def test_quote_failed_stale_holdings_and_fx():
    s = snap([{"ticker": "BAD", "error": "no live quote", "asset_type": "stock"}], as_of="2026-10-01", fx_day=-2.0)
    r = rules(ev(s))
    assert ("quote_failed", "BAD") in r and ("stale_holdings", None) in r and ("fx_move", None) in r


def test_final_vs_live_keys_and_ordering():
    s = snap([row("BRK-B", 1200, 12.0, shares=12), row("KO", 500, 5.0, day=-5.5)], cash_w=1.0)
    fin = ev(s, market=CLOSED)
    live = ev(s, market=OPEN)
    assert all(a["final"] for a in fin) and not any(a["final"] for a in live)
    assert fin[0]["key"] == "position_cap|BRK-B|2026-10-26" and live[0]["key"] == "position_cap|BRK-B|live"
    order = [a["severity"] for a in fin]
    assert order == sorted(order, key=lambda s_: alerts.SEV_ORDER[s_])


def test_clean_portfolio_has_no_alerts():
    splits = {"KO": {"consumer_staples": 100.0}, "QQQ": {"information_technology": 80.0, "communication_services": 20.0}}
    assert ev(snap([row("KO", 900, 9.0), row("QQQ", 3000, 30.0, kind="fund")]), splits=splits) == []


def test_rules_catalog_mentions_the_limits():
    txt = " ".join(r["text"] for r in alerts.rules_catalog(PROFILE))
    assert "10%" in txt and "40%" in txt and "35%" in txt


# --------------------------------------------------------------------------- endpoint + persistence
def test_alerts_log_final_alerts_once(tmp_path, tmp_env, monkeypatch):
    monkeypatch.setattr(common, "yf_quote", lambda t: {"price": 100.0 if t != "ILS=X" else 4.0, "previous_close": 100.0,
                                                       "currency": "USD" if t != "ILS=X" else "ILS"})
    monkeypatch.setattr(dashboard, "_snapshot_cache", {"t": 0.0, "key": None, "data": None})
    monkeypatch.setattr(common, "yf_info", lambda t: {"sector": "Technology"})
    shutil.copy2(common.REPO_ROOT / "tests" / "fixtures" / "portfolio_edit" / "portfolio.yaml",
                 tmp_env.config_dir / "portfolio.yaml")
    d1 = dashboard.build_alerts(tmp_path / "runs", now=datetime(2026, 10, 27, 21, 0, tzinfo=timezone.utc))
    assert d1["final"] is True and d1["market"]["last_session"] == "2026-10-27" and d1["rules"]
    n1 = len(d1["history"])
    d2 = dashboard.build_alerts(tmp_path / "runs", now=datetime(2026, 10, 27, 21, 5, tzinfo=timezone.utc))
    assert len(d2["history"]) == n1                    # same keys: logged once
    d3 = dashboard.build_alerts(tmp_path / "runs", now=datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc))
    assert d3["final"] is False and len(d3["history"]) == n1     # live alerts are never logged
    assert (tmp_env.root / "alerts" / "log.json").is_file()


def test_alerts_route_and_page_tab(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "build_alerts", lambda runs_dir, *a, **k: {"alerts": [], "final": True, "rules": []})
    srv = dashboard.make_server(tmp_path, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        d = json.load(urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/api/alerts"))
        assert d["final"] is True
    finally:
        srv.shutdown()
        srv.server_close()
    html = dashboard.load_page().decode("utf-8")
    assert 'data-v="notifications"' in html and 'id="notifview"' in html and "/api/alerts" in html and "Notification" in html
