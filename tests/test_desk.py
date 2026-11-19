"""Tests for ``scripts/desk.py`` (the day-trading desk helper) and the ``schemas/desk_*.schema.json`` contracts.

Offline and hermetic: temp repo roots, a fake skills folder, no network, no subprocess. The helper must have no
code path that sends, records or drafts an order (checked on the source).
"""
from __future__ import annotations

import ast
import copy
import datetime as dt
import json
import re
from pathlib import Path

import pytest

from scripts import desk

REPO_ROOT = Path(__file__).resolve().parents[1]
DESK_PY = REPO_ROOT / "scripts" / "desk.py"

EXAMPLE_CONFIG = {
    "budget": 5000, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_usd": 4000, "min_order_usd": 5,
    "max_loss_per_trade_usd": 50, "max_daily_loss_usd": 150, "max_trades_per_day": 3, "max_orders_per_day": 10,
    "min_reward_risk": 1.0, "require_stop": True, "no_entries_before": "09:45", "no_entries_after": "15:30",
    "flat_by": "15:55", "fractional": False, "broker": "alpaca", "paper": True, "blocked": [],
    "allow_options": False, "allow_margin": False, "allow_short": False, "pdt_guard": False,
    "sec_user_agent": "Your Name you@example.com",
}
DISCLAIMER = "Education and tooling, not financial advice. Most people who day trade lose money."


# ----------------------------------------------------------------------------------------- fixtures
@pytest.fixture
def skills(tmp_path: Path) -> Path:
    d = tmp_path / "skills" / "trader-budget"
    d.mkdir(parents=True)
    (d / "config.example.json").write_text(json.dumps(EXAMPLE_CONFIG, indent=2) + "\n", encoding="utf-8")
    return tmp_path / "skills"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    (r / "config").mkdir(parents=True)
    return r


def _cfg(root: Path) -> Path:
    return root / "trader" / "config.json"


# --------------------------------------------------------------------------------------------- dates
def test_new_york_offset_around_the_2026_dst_changes():
    utc = dt.timezone.utc
    assert desk.ny_offset_hours(dt.datetime(2026, 3, 8, 6, 59, tzinfo=utc)) == -5
    assert desk.ny_offset_hours(dt.datetime(2026, 3, 8, 7, 0, tzinfo=utc)) == -4
    assert desk.ny_offset_hours(dt.datetime(2026, 11, 1, 5, 59, tzinfo=utc)) == -4
    assert desk.ny_offset_hours(dt.datetime(2026, 11, 1, 6, 0, tzinfo=utc)) == -5
    assert desk.ny_offset_hours(dt.datetime(2026, 7, 4, 12, 0, tzinfo=utc)) == -4
    assert desk.ny_offset_hours(dt.datetime(2026, 1, 4, 12, 0, tzinfo=utc)) == -5


def test_ny_today_uses_the_market_calendar_not_the_local_date():
    utc = dt.timezone.utc
    # 03:50 UTC on 2026-10-20 is still the evening of 2026-10-19 in New York
    assert desk.ny_today(dt.datetime(2026, 10, 20, 3, 50, tzinfo=utc)) == dt.date(2026, 10, 19)
    assert desk.ny_today(dt.datetime(2026, 10, 20, 14, 15, tzinfo=utc)) == dt.date(2026, 10, 20)


def test_trading_week_range():
    assert desk.trading_week(dt.date(2026, 10, 21)) == ("2026-10-19", "2026-10-23")   # Wednesday
    assert desk.trading_week(dt.date(2026, 11, 1)) == ("2026-11-02", "2026-11-06")    # Sunday -> coming week
    assert desk.trading_week(dt.date(2026, 10, 24)) == ("2026-10-26", "2026-10-30")   # Saturday


# ----------------------------------------------------------------------------------------- holdings
YAML = """\
as_of: "2026-10-29"
holdings:
  - ticker: "SPMO"   # a fund
    shares: 59
    currency: USD
    proxy_ticker: null
  - ticker: "BRK.B"
    shares: 6
    currency: USD
    proxy_ticker: null
  - ticker: "1186063"
    shares: null
    weight_pct: 10
    currency: USD
    proxy_ticker: "QQQ"
  - ticker: "LUMI.TA"
    shares: 5
    currency: ILS
    proxy_ticker: null
  - ticker: "TEVA"
    shares: 3
    currency: ILS
    proxy_ticker: null
  - ticker: "OLD"
    shares: 0
    currency: USD
    proxy_ticker: null
  - ticker: "spmo"
    shares: 1
    currency: USD
    proxy_ticker: null
  - ticker: "PLTR"
    shares: 12
    currency: USD
    proxy_ticker: null
cash_usd: 16.72
"""


def test_holdings_symbols_skip_tase_units_proxies_zero_shares_and_duplicates():
    assert desk.holdings_symbols(YAML) == ["SPMO", "BRK-B", "PLTR"]


def test_yahoo_symbol_conversion():
    assert desk.yahoo_symbol("brk.b") == "BRK-B"
    assert desk.yahoo_symbol("BF/B") == "BF-B"
    assert desk.yahoo_symbol("TEVA.TA") == "TEVA.TA"


def test_real_portfolio_yields_us_symbols_only():
    syms = desk.holdings_symbols((REPO_ROOT / "config" / "portfolio.yaml").read_text(encoding="utf-8"))
    if "PLTR" not in syms:
        pytest.skip("config/portfolio.yaml is the empty template (run /committee-setup)")
    assert "PLTR" in syms and "BRK-B" in syms
    assert not [s for s in syms if s.isdigit() or s.endswith(".TA")]


def test_holdings_symbols_command(root: Path, capsys):
    (root / "config" / "portfolio.yaml").write_text(YAML, encoding="utf-8")
    assert desk.main(["--root", str(root), "holdings-symbols"]) == 0
    assert capsys.readouterr().out.strip() == "SPMO,BRK-B,PLTR"
    assert desk.main(["--root", str(root), "holdings-symbols", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["symbols"] == ["SPMO", "BRK-B", "PLTR"]
    assert desk.main(["--root", str(root), "holdings-symbols", "--portfolio", str(root / "nope.yaml")]) == 2


# --------------------------------------------------------------------------------------- init-day
def test_init_day_creates_the_run_dir_and_copies_the_example_config_unchanged(root: Path, skills: Path):
    ctx, rc = desk.init_day(root, dt.date(2026, 11, 2), skills)
    assert rc == 0
    assert (root / "runs" / "desk" / "2026-11-02" / "orders").is_dir()
    assert ctx["config_created"] is True and ctx["limits_set"] is False
    assert ctx["stop_before_sizing"] is True and ctx["limits_changed_keys"] == []
    assert "UNCHANGED" in ctx["message"] and "max_loss_per_trade_usd" in ctx["message"]
    assert _cfg(root).read_bytes() == (skills / "trader-budget" / "config.example.json").read_bytes()
    saved = json.loads((root / "runs" / "desk" / "2026-11-02" / "context.json").read_text(encoding="utf-8"))
    assert saved == ctx and saved["run_dir"] == "runs/desk/2026-11-02"
    assert saved["week_range"] == ["2026-11-02", "2026-11-06"] and saved["ptr_since"] == "2026-10-03"


def test_init_day_second_run_keeps_the_config_and_still_reports_unset_limits(root: Path, skills: Path):
    desk.init_day(root, dt.date(2026, 11, 2), skills)
    before = _cfg(root).read_bytes()
    ctx, rc = desk.init_day(root, dt.date(2026, 11, 2), skills)
    assert rc == 0 and ctx["config_created"] is False
    assert ctx["limits_set"] is False and ctx["stop_before_sizing"] is True
    assert "still equals" in ctx["message"] and "desk_limits_confirmed" in ctx["message"]
    assert _cfg(root).read_bytes() == before


def test_init_day_detects_the_investors_own_limits(root: Path, skills: Path):
    desk.init_day(root, dt.date(2026, 11, 2), skills)
    cfg = json.loads(_cfg(root).read_text(encoding="utf-8"))
    cfg.update(max_loss_per_trade_usd=20, budget=2000)
    _cfg(root).write_text(json.dumps(cfg), encoding="utf-8")
    ctx, _ = desk.init_day(root, dt.date(2026, 11, 2), skills)
    assert ctx["limits_set"] is True and ctx["stop_before_sizing"] is False
    assert ctx["limits_changed_keys"] == ["budget", "max_loss_per_trade_usd"]


def test_init_day_a_non_limit_change_does_not_count(root: Path, skills: Path):
    desk.init_day(root, dt.date(2026, 11, 2), skills)
    cfg = json.loads(_cfg(root).read_text(encoding="utf-8"))
    cfg["sec_user_agent"] = "Real Name real@example.com"
    cfg["blocked"] = ["GME"]
    _cfg(root).write_text(json.dumps(cfg), encoding="utf-8")
    ctx, _ = desk.init_day(root, dt.date(2026, 11, 2), skills)
    assert ctx["limits_set"] is False and ctx["config_sec_user_agent_set"] is True


def test_init_day_confirmation_flag_accepts_the_example_numbers(root: Path, skills: Path):
    desk.init_day(root, dt.date(2026, 11, 2), skills)
    cfg = json.loads(_cfg(root).read_text(encoding="utf-8"))
    cfg["desk_limits_confirmed"] = True
    _cfg(root).write_text(json.dumps(cfg), encoding="utf-8")
    ctx, _ = desk.init_day(root, dt.date(2026, 11, 2), skills)
    assert ctx["limits_set"] is True and ctx["limits_confirmed_flag"] is True and ctx["limits_changed_keys"] == []


def test_init_day_paper_flag_and_live_note(root: Path, skills: Path):
    desk.init_day(root, dt.date(2026, 11, 2), skills)
    assert json.loads((root / "runs/desk/2026-11-02/context.json").read_text(encoding="utf-8"))["paper"] is True
    cfg = json.loads(_cfg(root).read_text(encoding="utf-8"))
    cfg["paper"] = False
    _cfg(root).write_text(json.dumps(cfg), encoding="utf-8")
    ctx, _ = desk.init_day(root, dt.date(2026, 11, 2), skills)
    assert ctx["paper"] is False and "you place any order yourself" in ctx["message"]


def test_init_day_reads_sec_contact_email_holdings_and_universe_age(root: Path, skills: Path):
    (root / "config" / "investor_profile.yaml").write_text(
        "horizon_years: 55\nsec_contact_email: me@example.com   # for SEC\n", encoding="utf-8")
    (root / "config" / "portfolio.yaml").write_text(YAML, encoding="utf-8")
    (root / "trader").mkdir()
    (root / "trader" / "universe.txt").write_text("AAPL|Apple\n", encoding="utf-8")
    ctx, _ = desk.init_day(root, dt.date(2026, 11, 2), skills)
    assert ctx["sec_user_agent"] == "agent-investors research me@example.com"
    assert ctx["holdings_symbols"] == ["SPMO", "BRK-B", "PLTR"]
    assert ctx["universe_age_days"] == 0


def test_init_day_fails_clearly_without_the_budget_skill_or_with_a_broken_config(root: Path, skills: Path, tmp_path):
    ctx, rc = desk.init_day(root, dt.date(2026, 11, 2), tmp_path / "no-skills")
    assert rc == 2 and "not installed" in ctx["message"] and not (root / "trader" / "config.json").exists()
    (root / "trader").mkdir()
    _cfg(root).write_text("{not json", encoding="utf-8")
    ctx, rc = desk.init_day(root, dt.date(2026, 11, 2), skills)
    assert rc == 2 and "not valid JSON" in ctx["message"]


def test_init_day_cli_prints_json(root: Path, skills: Path, capsys):
    rc = desk.main(["--root", str(root), "init-day", "--date", "2026-11-03", "--skills-dir", str(skills)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["date"] == "2026-11-03" and out["config_created"] is True
    with pytest.raises(SystemExit):
        desk.main(["--root", str(root), "init-day", "--date", "11/03/2026"])


# -------------------------------------------------------------------------------------------- schemas
def _base(agent: str) -> dict:
    return {"schema_version": "1.0", "agent": agent, "date": "2026-11-02", "disclaimer": DISCLAIMER,
            "nothing_ordered": True, "missing_data": [], "commands_run": ["python x"]}


def valid_scout() -> dict:
    cong = {"recent_ptr": False, "note": "none found"}
    earn = {"this_week": "unknown", "next_report_date": None, "source": None}
    d = _base("desk-scout")
    d.update(market={"state": "closed", "note": "NYSE closed, weekend.", "next_open": "2026-11-02 09:30"},
             scan={"mode": "daily", "universe_size": 5942, "charts_fetched": 5900, "scanned": 5000, "hits": 3,
                   "partial": True, "symbols_passed": [], "note": "PARTIAL"},
             candidates=[{"symbol": "CAT", "rank": 1, "setups": ["oversold"], "why": "RSI 23.8 under 30",
                          "price": 622.75, "news_tags": ["earnings"], "news_note": "headlines only", "moving": True,
                          "congress": cong, "earnings": earn}],
             holdings=[{"symbol": "PLTR", "setups": [], "news_tags": [], "congress": cong, "earnings": earn}])
    return d


def valid_timer() -> dict:
    d = _base("desk-timer")
    d.update(symbol="TSLA", chosen_by="user", result="ready",
             entry={"status": "triggered", "setup": "orb", "trigger_time": "10:15 AM", "trigger_price": 364.18,
                    "or_high": 362.18, "or_low": 354.47, "note": "bar 8", "written": True},
             plan={"written": True, "entry": 364.18, "stop": 358.32, "stop_rule": "or-mid", "target": 375.9,
                   "target_r": 2, "risk_per_share": 5.86, "reward_risk": 2.0, "flat_by": "15:55",
                   "refused_reason": None, "stop_is_a_plan_not_a_guarantee": True},
             alerts={"added": [{"id": "tsla-plan-stop", "type": "stop", "level": 358.32},
                               {"id": "tsla-plan-target", "type": "target", "level": 375.9},
                               {"id": "tsla-flat-by", "type": "time", "at": "15:55"}],
                     "market_clock": "NYSE OPEN (2026-10-19 10:15 New York, regular session)."})
    return d


BRACKET = {"symbol": "TSLA", "side": "buy", "qty": "8", "type": "market", "time_in_force": "day",
           "order_class": "bracket", "take_profit_limit_price": "375.90", "stop_loss_stop_price": "358.32"}


def valid_quant() -> dict:
    d = _base("desk-quant")
    d.update(symbol="TSLA", result="ready", limits_check={"limits_set": True, "paper": True},
             simulation={"ran": True, "seed": 19, "replays": 10000, "until": "15:55", "target_first_pct": 3.5,
                         "stop_first_pct": 24.3, "neither_pct": 72.2, "green_pct": 50.4, "avg_r": 0.047,
                         "history": {"sessions": 59, "fired": 33, "target": 3, "stop": 17, "neither": 13,
                                     "avg_r": -0.168},
                         "verdict": "Strongly toward the stop: ...", "extra_lines": [],
                         "not_a_prediction_said": True},
             sizing={"sized": True, "shares": 8, "dollars": 2913.44, "max_loss": 46.88,
                     "deciding_limit": "max_loss",
                     "limit_shares": {"max_loss": 8, "position_cap": 10, "max_order": 10, "cash": 12},
                     "refused_reason": None},
             order={"written": True, "placed": False, "path": "trader/orders.json", "bracket": BRACKET})
    return d


def valid_risk() -> dict:
    d = _base("desk-risk")
    ticket = {"plain": "BUY 8 TSLA ...", "order_json": BRACKET, "ready": True}
    d.update(symbol="TSLA", verdict="PASS", reasons=[], numbers={"max_loss": 46.88, "max_loss_limit": 50},
             entry_ticket=ticket,
             flat_ticket={"plain": "SELL 8 TSLA", "order_json": {"symbol": "TSLA", "side": "sell", "qty": "8",
                                                                  "type": "market", "time_in_force": "day"},
                          "ready": False, "written_to": "trader/flatten.json"},
             user_steps=["Place the order yourself in your own broker."], placed_by_investor_only=True)
    return d


VALID = {"scout": valid_scout, "timer": valid_timer, "quant": valid_quant, "risk": valid_risk}


def _errors(doc: dict, kind: str) -> list[str]:
    return desk.validate_doc(doc, desk.schema_file(kind))


@pytest.mark.parametrize("kind", sorted(VALID))
def test_valid_examples_pass(kind: str):
    assert _errors(VALID[kind](), kind) == []


@pytest.mark.parametrize("kind", sorted(VALID))
def test_schema_files_are_draft_2020_12_with_unique_urn_ids(kind: str):
    from jsonschema import Draft202012Validator
    schema = json.loads(desk.schema_file(kind).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == f"urn:agent-investors:desk_{kind}"
    assert schema["additionalProperties"] is False
    assert schema["properties"]["nothing_ordered"] == {
        "const": True, "description": schema["properties"]["nothing_ordered"]["description"]}


@pytest.mark.parametrize("kind", sorted(VALID))
def test_every_result_must_say_nothing_was_ordered_and_carry_the_disclaimers(kind: str):
    doc = VALID[kind]()
    for mutate in (lambda d: d.pop("nothing_ordered"), lambda d: d.update(nothing_ordered=False),
                   lambda d: d.update(disclaimer="Education and tooling, not financial advice."),
                   lambda d: d.update(disclaimer="Most people who day trade lose money."),
                   lambda d: d.update(agent="desk-other"), lambda d: d.update(extra_key=1),
                   lambda d: d.update(date="Nov 2")):
        bad = copy.deepcopy(doc)
        mutate(bad)
        assert _errors(bad, kind), f"{kind}: should reject {bad.keys()}"


def test_scout_rejects_too_many_candidates_and_unknown_candidate_keys():
    doc = valid_scout()
    doc["candidates"] = [dict(doc["candidates"][0], rank=i + 1) for i in range(11)]
    assert _errors(doc, "scout")
    doc = valid_scout()
    doc["candidates"][0]["recommendation"] = "x"
    assert _errors(doc, "scout")
    doc = valid_scout()
    doc["candidates"][0]["symbol"] = "cat"
    assert _errors(doc, "scout")


def test_timer_allows_at_most_three_alerts_and_requires_the_stop_note():
    doc = valid_timer()
    doc["alerts"]["added"].append({"id": "x", "type": "stop"})
    assert _errors(doc, "timer")
    doc = valid_timer()
    doc["plan"]["stop_is_a_plan_not_a_guarantee"] = False
    assert _errors(doc, "timer")


def test_quant_proposal_is_never_placed_and_is_a_long_whole_share_bracket():
    doc = valid_quant()
    doc["order"]["placed"] = True
    assert _errors(doc, "quant")
    doc = valid_quant()
    doc["order"]["bracket"]["side"] = "sell"
    assert _errors(doc, "quant")
    doc = valid_quant()
    doc["order"]["bracket"]["qty"] = "8.5"
    assert _errors(doc, "quant")
    doc = valid_quant()
    doc["order"]["path"] = "trader/other.json"
    assert _errors(doc, "quant")
    doc = valid_quant()
    doc["result"] = "stopped_limits_not_set"
    doc["sizing"] = {"sized": False, "shares": None, "refused_reason": "limits not set"}
    doc["order"] = {"written": False, "placed": False, "path": None, "bracket": None}
    assert _errors(doc, "quant") == []


def test_risk_block_must_carry_no_order_json_and_never_a_ready_ticket():
    doc = valid_risk()
    doc["verdict"] = "BLOCK"
    assert _errors(doc, "risk"), "a BLOCK with a ticket JSON must be rejected"
    doc["entry_ticket"] = {"plain": None, "order_json": None, "ready": False}
    doc["flat_ticket"] = {"plain": None, "order_json": None, "ready": False}
    doc["reasons"] = ["loss: 9 shares x 5.86 = $52.74, over your $50 max loss per trade"]
    assert _errors(doc, "risk") == []
    doc["verdict"] = "NOT_RUN"
    assert _errors(doc, "risk") == []
    doc["entry_ticket"]["ready"] = True
    assert _errors(doc, "risk")


def test_risk_requires_the_investor_places_it():
    doc = valid_risk()
    doc["placed_by_investor_only"] = False
    assert _errors(doc, "risk")


# ------------------------------------------------------------------------------------ validate / status
def test_validate_command(tmp_path: Path, capsys):
    good = tmp_path / "scout.json"
    good.write_text(json.dumps(valid_scout()), encoding="utf-8")
    assert desk.main(["validate", "--file", str(good), "--schema", "scout"]) == 0
    assert "OK" in capsys.readouterr().out
    assert desk.main(["validate", "--file", str(good), "--schema", "desk_scout"]) == 0
    capsys.readouterr()
    assert desk.main(["validate", "--file", str(good), "--schema", "risk"]) == 1
    capsys.readouterr()
    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    assert desk.main(["validate", "--file", str(bad), "--schema", "scout"]) == 1
    assert "not valid JSON" in capsys.readouterr().out
    assert desk.main(["validate", "--file", str(tmp_path / "missing.json"), "--schema", "scout"]) == 1
    assert "not found" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        desk.main(["validate", "--file", str(good), "--schema", "broker"])


def test_status_lists_todays_files_and_trader_state(root: Path, skills: Path, capsys):
    desk.init_day(root, dt.date(2026, 11, 2), skills)
    run = root / "runs" / "desk" / "2026-11-02"
    (run / "scout.json").write_text(json.dumps(valid_scout()), encoding="utf-8")
    (run / "timer_TSLA.json").write_text(json.dumps({"agent": "desk-timer"}), encoding="utf-8")
    (run / "desk_note.md").write_text("x", encoding="utf-8")
    assert desk.main(["--root", str(root), "status", "--date", "2026-11-02"]) == 0
    out = json.loads(capsys.readouterr().out)
    by_path = {f["path"]: f for f in out["files"]}
    assert by_path["runs/desk/2026-11-02/scout.json"]["valid"] is True
    assert by_path["runs/desk/2026-11-02/timer_TSLA.json"]["valid"] is False
    assert by_path["runs/desk/2026-11-02/context.json"].get("schema") is None
    assert out["desk_note"] is True and out["trader_files"]["config.json"] is True
    assert out["trader_files"]["orders.json"] is False
    desk.main(["--root", str(root), "status", "--date", "2026-11-09"])
    assert json.loads(capsys.readouterr().out)["run_dir_exists"] is False


def test_result_kind_names():
    assert desk.result_kind("scout.json") == "scout"
    assert desk.result_kind("timer_BRK-B.json") == "timer"
    assert desk.result_kind("risk_TSLA.json") == "risk"
    assert desk.result_kind("context.json") is None and desk.result_kind("desk_note.md") is None


def test_no_subcommand_prints_help_and_exits_2(capsys):
    assert desk.main([]) == 2


# --------------------------------------------------------------------------- no order code path at all
FORBIDDEN_IMPORTS = {"subprocess", "socket", "urllib", "http", "requests", "ftplib", "smtplib", "ssl",
                     "asyncio", "multiprocessing", "ctypes", "webbrowser", "yfinance", "os"}


def test_desk_py_has_no_network_subprocess_or_broker_code():
    src = DESK_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & FORBIDDEN_IMPORTS, f"desk.py imports {sorted(imported & FORBIDDEN_IMPORTS)}"
    assert imported <= {"__future__", "argparse", "datetime", "json", "re", "shutil", "sys", "pathlib", "typing",
                        "jsonschema"}, imported
    for needle in ("ticket.py", "place_stock_order", "execute_order", "approve", "orders.json\"].write",
                   "mcp__", "alpaca", "os.system", "eval(", "exec("):
        assert needle not in src, f"desk.py must not contain {needle!r}"
    # the only files desk.py writes: the day's context.json, and the copied example config
    writes = re.findall(r"_write_text\(([^,]+),", src)
    assert [w.strip() for w in writes[1:]] == ['run_dir / "context.json"'] * (len(writes) - 1), writes
    assert src.count("shutil.copyfile(") == 1 and "shutil.copyfile(example, config_path)" in src
    assert src.count("orders.json") == 1  # only in the read-only TRADER_FILES listing


def test_init_day_never_creates_or_changes_any_order_file(root: Path, skills: Path):
    desk.init_day(root, dt.date(2026, 11, 2), skills)
    trader = sorted(p.name for p in (root / "trader").rglob("*") if p.is_file())
    assert trader == ["config.json"]
    (root / "trader" / "orders.json").write_text('{"orders": []}\n', encoding="utf-8")
    before = (root / "trader" / "orders.json").read_bytes()
    desk.init_day(root, dt.date(2026, 11, 2), skills)
    assert (root / "trader" / "orders.json").read_bytes() == before
