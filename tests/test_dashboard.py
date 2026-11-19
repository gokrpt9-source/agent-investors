"""dashboard.py: read-only live progress page. Offline; local HTTP server on an ephemeral port."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from scripts import common, dashboard


def _run(tmp_path, rid="2026-10-25_1100"):
    rd = tmp_path / rid
    (rd / "phase2" / "reports").mkdir(parents=True)
    (rd / "phase4").mkdir()
    done = rd / "phase2" / "reports" / "fundamental_KO.json"
    done.write_text(json.dumps({"stance": "bullish", "conviction": 7, "thesis": "Steady cash flows " * 30,
                                "extensions": {"valuation": {"fair_value_base": 70.5}},
                                "red_flags": [{"severity": "high"}]}), encoding="utf-8")
    landed = rd / "phase2" / "reports" / "technical_KO.json"
    landed.write_text("{not json", encoding="utf-8")  # malformed output must not break the page
    manifest = {
        "run_id": rid, "status": "in_progress", "mode_label": "full", "as_of": "2026-10-25",
        "steps": [{"name": "p2.wave_a", "kind": "agents", "status": "dispatched", "summary": "x"}],
        "orders": {
            "p2-a-fundamental-KO": {"step": "p2.wave_a", "agent_type": "fundamental-analyst", "agent_role": "fundamental",
                                    "ticker": "KO", "status": "done", "attempt": 1, "output": str(done), "resolved_output": str(done)},
            "p2-a-technical-KO": {"step": "p2.wave_a", "agent_type": "technical-analyst", "agent_role": "technical",
                                  "ticker": "KO", "status": "dispatched", "attempt": 1, "output": str(landed)},
            "p2-a-osint-KO": {"step": "p2.wave_a", "agent_type": "osint-sentiment", "agent_role": "osint", "ticker": "KO",
                              "status": "dispatched", "attempt": 1, "output": str(rd / "phase2" / "reports" / "osint_KO.json")},
            "p2-a-operator-KO": {"step": "p2.wave_a", "agent_type": "operator-ceo", "agent_role": "operator_ceo", "ticker": "KO",
                                 "status": "dispatched", "attempt": 2, "output": str(rd / "x.json"), "errors": ["bad figure"]},
            "p2-b-da-KO": {"step": "p2.wave_b", "agent_type": "devils-advocate", "agent_role": "devils_advocate", "ticker": "KO",
                           "status": "pending", "attempt": 1, "output": str(rd / "y.json")},
        },
    }
    (rd / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (rd / "run_log.md").write_text("- a\n- b\n", encoding="utf-8")
    return rd


def test_state_classifies_every_order(tmp_path):
    _run(tmp_path)
    s = dashboard.build_state(tmp_path, None)
    ui = {o["id"]: o["ui"] for o in s["orders"]}
    assert ui == {"p2-a-fundamental-KO": "done", "p2-a-technical-KO": "writing", "p2-a-osint-KO": "working",
                  "p2-a-operator-KO": "retry", "p2-b-da-KO": "pending"}
    assert s["counts"]["done"] == 1 and s["status"] == "in_progress" and s["log"] == ["- a", "- b"]
    done = next(o for o in s["orders"] if o["ui"] == "done")["result"]
    assert done["headline"] == "bullish" and done["conviction"] == 7 and done["badge"] == "fair value 70.50"
    assert done["flags"] == 1 and len(done["text"]) <= dashboard.TEXT_LIMIT
    assert next(o for o in s["orders"] if o["ui"] == "writing")["result"] == {}  # malformed json tolerated


def test_digest_roles_never_raise():
    assert dashboard.digest("devils_advocate", {"verdict_on_thesis": "broken"})["tone"] == "bad"
    assert "BUY" in dashboard.digest("sector_head", {"verdicts": [{"ticker": "MRK", "action": "BUY"}]})["headline"]
    assert dashboard.digest("cio", {"final_actions": [{"override": True}], "executive_summary": ["x"]})["badge"]
    assert dashboard.digest("macro", None) == {} and dashboard.digest("etf", {"stance": 5}) != None  # noqa: E711
    assert dashboard.digest("fundamental", {"status": "failed"})["tone"] == "bad"


def test_no_runs_is_empty_state(tmp_path):
    assert dashboard.build_state(tmp_path / "none", None)["empty"] is True


def test_server_endpoints_and_safety(tmp_path):
    rd = _run(tmp_path)
    srv = dashboard.make_server(tmp_path, port=0)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        assert b"Committee Live" in urllib.request.urlopen(base + "/").read()
        st = json.load(urllib.request.urlopen(base + "/api/state"))
        assert st["run_id"] == rd.name and st["memo"] is None
        (rd / "phase4" / "memo.html").write_text("<html>memo</html>", encoding="utf-8")
        assert json.load(urllib.request.urlopen(base + "/api/state"))["memo"]
        assert urllib.request.urlopen(base + f"/memo?run={rd.name}").read() == b"<html>memo</html>"
        for bad in ("/api/state?run=../../etc", "/memo?run=..%2F..%2Fsecret"):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + bad)
            assert e.value.code == 400
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/etc/passwd")
        assert e.value.code == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_refuses_non_local_bind(tmp_path):
    with pytest.raises(common.ConfigError):
        dashboard.make_server(tmp_path, port=0, host="0.0.0.0")


def test_page_shows_the_whole_roster_even_for_unused_agents():
    html = dashboard.load_page().decode("utf-8")
    assert 'id="bench"' in html and "The whole team" in html
    # every agent type is on the bench or at a desk, and every sector gets a room even without orders
    for role in ("sector_head", "fundamental", "technical", "operator_ceo", "industry_ops", "osint",
                 "devils_advocate", "etf"):
        assert f"'{role}'" in html
    for sector in ("information_technology", "health_care", "financials", "energy", "industrials",
                   "consumer_discretionary", "consumer_staples", "utilities", "materials", "real_estate",
                   "communication_services", "funds"):
        assert sector + ":" in html
    assert "off duty" in html and "not used this run" in html


# --------------------------------------------------------------------------- live portfolio tab
def _quotes(table):
    def fn(t):
        v = table[t]
        if isinstance(v, Exception):
            raise v
        return v
    return fn


PCFG = {
    "as_of": "2026-10-23", "cash_usd": 1000.0,
    "holdings": [
        {"ticker": "KO", "shares": 10, "cost_basis": 50.0, "cost_basis_currency": "USD", "currency": "USD", "asset_type": "stock"},
        {"ticker": "LUMI.TA", "shares": 100, "cost_basis": 60.0, "cost_basis_currency": "ILS", "currency": "ILS", "asset_type": "stock"},
        {"ticker": "1186063", "shares": 10.0, "units": 80, "unit_cost_ils": 250.0, "proxy_ticker": "QQQ",
         "currency": "USD", "asset_type": "fund"},
        {"ticker": "NOCOST", "shares": 2, "cost_basis": None, "currency": "USD"},
        {"ticker": "BAD", "shares": 1, "currency": "USD"},
    ],
}
QUOTES = {
    "ILS=X": {"price": 4.0, "previous_close": 4.0, "currency": "ILS"},
    "KO": {"price": 60.0, "previous_close": 59.0, "currency": "USD"},
    "LUMI.TA": {"price": 7500.0, "previous_close": 7400.0, "currency": "ILA"},   # agorot: ILS 75.00
    "QQQ": {"price": 100.0, "previous_close": 100.0, "currency": "USD"},
    "NOCOST": {"price": 10.0, "previous_close": 10.0, "currency": "USD"},
    "BAD": RuntimeError("no price"),
}


def test_portfolio_snapshot_math_and_currencies():
    d = dashboard.portfolio_snapshot(PCFG, _quotes(QUOTES))
    rows = {r["ticker"]: r for r in d["rows"]}
    ko = rows["KO"]
    assert ko["value_usd"] == 600 and ko["pl_native"] == 100 and round(ko["pl_pct"], 2) == 20.0
    assert round(ko["day_pct"], 3) == round((60 / 59 - 1) * 100, 3)
    # Leumi: agorot -> ILS 75.00; value 7,500 ILS = $1,875 at 4.0; P/L in ILS = 100 x (75 - 60)
    lu = rows["LUMI.TA"]
    assert lu["last"] == 75.0 and lu["native_currency"] == "ILS" and lu["value_usd"] == 1875
    assert lu["pl_native"] == 1500 and round(lu["pl_pct"], 2) == 25.0 and lu["pl_usd"] == 375
    # proxy-valued TASE fund: 10 QQQ-equivalent shares x $100 = $1000 = ILS 4000 for 80 units (ILS 50/unit)
    fu = rows["1186063"]
    assert fu["value_usd"] == 1000 and fu["last"] == 50.0 and fu["native_currency"] == "ILS"
    assert fu["pl_native"] == 4000 - 80 * 250.0 and fu["note"] == "valued through QQQ"
    # no cost basis: valued but excluded from the P/L total, with a warning; failed quote: error row
    assert rows["NOCOST"]["value_usd"] == 20 and rows["NOCOST"]["pl_native"] is None
    assert rows["BAD"]["error"] and "BAD" in " ".join(d["warnings"])
    assert any("NOCOST" in w for w in d["warnings"])
    t = d["totals"]
    assert t["value_usd"] == 600 + 1875 + 1000 + 20 + 1000          # holdings + cash
    assert t["invested_cost_usd"] == 500 + 1500 + 5000              # USD cost + ILS costs converted at 4.0
    assert t["pl_usd"] == 100 + 375 + (4000 - 20000) / 4.0
    assert round(sum(r["weight_pct"] for r in d["rows"] if "weight_pct" in r) + d["cash"]["weight_pct"], 6) == 100.0


def test_portfolio_snapshot_without_fx_degrades():
    q = dict(QUOTES, **{"ILS=X": RuntimeError("down")})
    d = dashboard.portfolio_snapshot(PCFG, _quotes(q))
    assert d["fx"]["usdils"] is None and any("USD/ILS" in w for w in d["warnings"])
    assert d["totals"]["value_ils"] is None
    ko = next(r for r in d["rows"] if r["ticker"] == "KO")
    assert ko["value_usd"] == 600                       # USD holdings still valued


def test_portfolio_endpoint_returns_json(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "yf_quote", lambda t: {"price": 10.0, "previous_close": 9.0, "currency": "USD"})
    monkeypatch.setattr(dashboard, "_snapshot_cache", {"t": 0.0, "key": None, "data": None})
    srv = dashboard.make_server(tmp_path, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        d = json.load(urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/api/portfolio"))
        assert "rows" in d and "totals" in d and d["rows"]          # repo config/portfolio.yaml, fake quotes
    finally:
        srv.shutdown()
        srv.server_close()


def test_page_has_the_portfolio_tab():
    html = dashboard.load_page().decode("utf-8")
    assert 'data-v="portfolio"' in html and 'id="portview"' in html and "/api/portfolio" in html


# --------------------------------------------------------------------------- editing holdings (POST)
import http.client  # noqa: E402


@pytest.fixture
def edit_server(tmp_path, tmp_env, monkeypatch):
    shutil_copy = __import__("shutil").copy2
    shutil_copy(common.REPO_ROOT / "tests" / "fixtures" / "portfolio_edit" / "portfolio.yaml", tmp_env.config_dir / "portfolio.yaml")
    monkeypatch.setattr(common, "yf_quote", lambda t: {"price": 100.0 if t != "ILS=X" else 4.0,
                                                       "previous_close": 99.0 if t != "ILS=X" else 4.0, "currency": "USD" if t != "ILS=X" else "ILS"})
    monkeypatch.setattr(dashboard, "_snapshot_cache", {"t": 0.0, "key": None, "data": None})
    srv = dashboard.make_server(tmp_path / "runs", port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    token = srv.RequestHandlerClass.token
    yield port, token, tmp_env.config_dir / "portfolio.yaml", tmp_env.root
    srv.shutdown()
    srv.server_close()


def _post(port, body, token="", host=None, origin=None):
    c = http.client.HTTPConnection("127.0.0.1", port)
    h = {"Content-Type": "application/json", "X-Dash-Token": token}
    if origin:
        h["Origin"] = origin
    c.putrequest("POST", "/api/portfolio/edit", skip_host=bool(host))
    if host:
        c.putheader("Host", host)
    raw = json.dumps(body).encode()
    h["Content-Length"] = str(len(raw))
    for k, v in h.items():
        c.putheader(k, v)
    c.endheaders(raw)
    r = c.getresponse()
    return r.status, json.loads(r.read() or b"{}")


def test_edit_endpoint_saves_with_token_and_returns_fresh_snapshot(edit_server):
    port, token, pf, tmp = edit_server
    code, j = _post(port, {"holdings": {"PLTR": {"shares": "20", "cost_basis": "150"}},
                           "cash": {"usd": "500", "ils": "400"}}, token, origin=f"http://127.0.0.1:{port}")
    assert code == 200 and j["ok"] and j["changed"] and j["backup"]
    import yaml
    d = yaml.safe_load(pf.read_text(encoding="utf-8"))
    assert next(h for h in d["holdings"] if h["ticker"] == "PLTR")["shares"] == 20
    assert d["cash_usd"] == 600.0                     # 500 + 400 ILS / 4.0
    assert next(r for r in j["snapshot"]["rows"] if r["ticker"] == "PLTR")["shares"] == 20
    assert (tmp / "backups").is_dir()


@pytest.mark.parametrize("token_ok,host,origin,code", [
    (False, None, None, 403),                              # no / wrong token
    (True, "evil.example:80", None, 403),                  # DNS-rebinding style Host
    (True, None, "http://evil.example", 403),              # cross-site origin
])
def test_edit_endpoint_rejects_untrusted_requests(edit_server, token_ok, host, origin, code):
    port, token, pf, _ = edit_server
    before = pf.read_text(encoding="utf-8")
    status, j = _post(port, {"holdings": {"PLTR": {"shares": "99"}}}, token if token_ok else "wrong", host=host, origin=origin)
    assert status == code and "error" in j
    assert pf.read_text(encoding="utf-8") == before


def test_edit_endpoint_validation_errors_are_400_and_leave_file_alone(edit_server):
    port, token, pf, _ = edit_server
    before = pf.read_text(encoding="utf-8")
    status, j = _post(port, {"holdings": {"PLTR": {"shares": "-3"}}}, token)
    assert status == 400 and "greater than" in j["error"]
    status, j = _post(port, {"remove": ["NOPE"]}, token)
    assert status == 400
    assert pf.read_text(encoding="utf-8") == before


def test_edit_endpoint_other_paths_and_verbs(edit_server):
    port, token, _, _ = edit_server
    c = http.client.HTTPConnection("127.0.0.1", port)
    c.request("POST", "/api/state", body=b"{}", headers={"Content-Type": "application/json"})
    assert c.getresponse().status == 404
    html = dashboard.load_page("abc123").decode("utf-8")
    assert "abc123" in html and "__TOKEN__" not in html
