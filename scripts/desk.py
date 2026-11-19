#!/usr/bin/env python
"""desk.py - thin helper for the day-trading desk (``/trader-desk``).

The desk is a short-term variant of the committee: four ``desk-*`` subagents plus the chair (the main
session) prepare a day-trade *plan* with the eleven day-trader skills. This helper only handles the
files around it. It is deliberately tiny:

* NO network, NO subprocess, NO broker access of any kind. There is no code path in this file that sends,
  records or even drafts an order; a test enforces that (``tests/test_desk.py``).
* Standard library only, except ``validate`` which imports ``jsonschema`` (already a repo requirement).
* UTF-8 everywhere.

Subcommands
-----------
``init-day [--date D]``
    Create ``runs/desk/<date>/`` (and ``orders/``), make sure ``trader/config.json`` exists (copied
    UNCHANGED from the trader-budget skill's ``config.example.json`` when missing), report whether the user
    has set his own risk limits, and write ``runs/desk/<date>/context.json``. The date defaults to today in
    New York (the market's calendar).
``holdings-symbols [--portfolio P] [--json]``
    Yahoo symbols of ``config/portfolio.yaml`` holdings, skipping TASE units and proxy-priced funds.
``validate --file F --schema NAME``
    Validate a desk result against ``schemas/desk_<NAME>.schema.json`` (NAME: scout, timer, quant, risk).
``status [--date D]``
    List the desk files of the day and the state of the ``trader/`` files.

Exit codes: 0 ok, 1 validation failed / limits not set is reported inside the JSON (still 0), 2 usage or
environment error.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = Path.home() / ".claude" / "skills"
EXAMPLE_REL = Path("trader-budget") / "config.example.json"

#: the keys the investor must set himself (the desk never invents his risk limits)
LIMIT_KEYS = (
    "budget",
    "max_loss_per_trade_usd",
    "max_position_pct",
    "cash_buffer_pct",
    "max_daily_loss_usd",
    "max_trades_per_day",
    "no_entries_before",
    "no_entries_after",
)
#: optional flag in trader/config.json: the investor confirms the example numbers really are his
CONFIRM_FLAG = "desk_limits_confirmed"
SEC_PLACEHOLDER = "Your Name you@example.com"

SCHEMA_NAMES = {"scout": "desk_scout", "timer": "desk_timer", "quant": "desk_quant", "risk": "desk_risk"}
#: result file stem -> schema key (``timer_TSLA.json`` -> timer)
RESULT_PREFIXES = {"scout": "scout", "timer": "timer", "quant": "quant", "risk": "risk"}
TRADER_FILES = (
    "config.json", "watchlist.json", "entry.json", "plan.json", "timemachine.json", "orders.json",
    "flatten.json", "alerts.json", "prices.json", "state/risk.json",
)
TASE_CURRENCIES = {"ILS", "ILA", "AGOROT"}
#: how far back the scout looks for House Periodic Transaction Reports (they arrive up to 45 days late)
PTR_WINDOW_DAYS = 30


# ------------------------------------------------------------------------------------------ small io
def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _dump(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------------------- New York date
def _nth_sunday(year: int, month: int, n: int) -> dt.date:
    d = dt.date(year, month, 1)
    first_sunday = d + dt.timedelta(days=(6 - d.weekday()) % 7)
    return first_sunday + dt.timedelta(weeks=n - 1)


def ny_offset_hours(utc_now: dt.datetime) -> int:
    """-4 during US daylight time (2nd Sunday of March 07:00 UTC to 1st Sunday of November 06:00 UTC), else -5."""
    y = utc_now.year
    start = dt.datetime.combine(_nth_sunday(y, 3, 2), dt.time(7, 0))
    end = dt.datetime.combine(_nth_sunday(y, 11, 1), dt.time(6, 0))
    naive = utc_now.replace(tzinfo=None)
    return -4 if start <= naive < end else -5


def ny_today(utc_now: dt.datetime | None = None) -> dt.date:
    """Today's date in New York, computed without time-zone files (like the trader skills do)."""
    if utc_now is None:
        utc_now = dt.datetime.now(dt.timezone.utc)
    return (utc_now + dt.timedelta(hours=ny_offset_hours(utc_now))).date()


def trading_week(date: dt.date) -> tuple[str, str]:
    """(Monday, Friday) of the week containing ``date``; on a weekend, of the coming week."""
    monday = date - dt.timedelta(days=date.weekday())
    if date.weekday() >= 5:
        monday += dt.timedelta(days=7)
    return monday.isoformat(), (monday + dt.timedelta(days=4)).isoformat()


def _parse_date(text: str | None) -> dt.date:
    if not text:
        return ny_today()
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise SystemExit(f"desk.py: --date wants YYYY-MM-DD, got {text!r}")


# ------------------------------------------------------------------------------------- portfolio.yaml
def _strip_comment(line: str) -> str:
    quote = ""
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i].rstrip()
    return line.rstrip()


def _unquote(value: str) -> str | None:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    elif v.lower() in ("null", "~", "none", ""):
        return None
    return v


def parse_holdings(text: str) -> list[dict[str, str | None]]:
    """Minimal reader for the ``holdings:`` list of config/portfolio.yaml (flat ``key: value`` entries)."""
    holdings: list[dict[str, str | None]] = []
    cur: dict[str, str | None] | None = None
    in_list = False
    for raw in text.splitlines():
        line = _strip_comment(raw)
        if not line.strip():
            continue
        if re.match(r"^holdings\s*:", line):
            in_list = True
            continue
        if not in_list:
            continue
        if not line.startswith((" ", "\t", "-")):
            break  # next top-level key
        m = re.match(r"^\s*-\s*ticker\s*:\s*(.*)$", line)
        if m:
            cur = {"ticker": _unquote(m.group(1))}
            holdings.append(cur)
            continue
        m = re.match(r"^\s+([A-Za-z_]\w*)\s*:\s*(.*)$", line)
        if m and cur is not None:
            cur[m.group(1)] = _unquote(m.group(2))
    return holdings


def yahoo_symbol(ticker: str) -> str:
    t = ticker.strip().upper().replace("/", "-")
    if re.fullmatch(r"[A-Z]{1,5}\.[A-Z]", t):  # share class: BRK.B -> BRK-B (exchange suffixes stay)
        t = t.replace(".", "-")
    return t


def holdings_symbols(text: str) -> list[str]:
    out: list[str] = []
    for h in parse_holdings(text):
        ticker = h.get("ticker")
        if not ticker:
            continue
        if h.get("proxy_ticker"):  # a TASE unit priced through a US proxy
            continue
        if re.fullmatch(r"\d+", ticker) or ticker.upper().endswith(".TA"):
            continue
        if (h.get("currency") or "").upper() in TASE_CURRENCIES:
            continue
        shares = h.get("shares")
        if shares is not None:
            try:
                if float(shares) <= 0:
                    continue
            except ValueError:
                pass
        sym = yahoo_symbol(ticker)
        if sym not in out:
            out.append(sym)
    return out


# ------------------------------------------------------------------------------------------- init-day
def _example_path(skills_dir: Path | None) -> Path:
    return (skills_dir or SKILLS_DIR) / EXAMPLE_REL


def compare_limits(config: dict[str, Any], example: dict[str, Any]) -> list[str]:
    """Names of the limit keys where the user's config differs from the shipped example."""
    return [k for k in LIMIT_KEYS if config.get(k) != example.get(k)]


def _sec_email(root: Path) -> str | None:
    path = root / "config" / "investor_profile.yaml"
    if not path.is_file():
        return None
    for raw in _read_text(path).splitlines():
        m = re.match(r"^sec_contact_email\s*:\s*(.*)$", _strip_comment(raw))
        if m:
            v = _unquote(m.group(1))
            return v if v and "@" in v else None
    return None


def _universe_age_days(trader: Path) -> int | None:
    uni = trader / "universe.txt"
    if not uni.is_file():
        return None
    age = dt.datetime.now().timestamp() - uni.stat().st_mtime
    return max(0, int(age // 86400))


def init_day(root: Path, date: dt.date, skills_dir: Path | None = None) -> tuple[dict[str, Any], int]:
    run_dir = root / "runs" / "desk" / date.isoformat()
    (run_dir / "orders").mkdir(parents=True, exist_ok=True)
    trader = root / "trader"
    config_path = trader / "config.json"
    example = _example_path(skills_dir)
    ctx: dict[str, Any] = {
        "date": date.isoformat(),
        "run_dir": run_dir.relative_to(root).as_posix(),
        "orders_dir": (run_dir / "orders").relative_to(root).as_posix(),
        "config_path": "trader/config.json",
        "config_created": False,
        "limits_set": False,
        "limits_changed_keys": [],
        "limits_confirmed_flag": False,
        "paper": None,
        "sec_user_agent": None,
        "stop_before_sizing": True,
        "message": "",
        "holdings_symbols": [],
        "universe_age_days": _universe_age_days(trader),
        "weekday": date.strftime("%A"),
        "ptr_since": (date - dt.timedelta(days=PTR_WINDOW_DAYS)).isoformat(),
        "week_range": list(trading_week(date)),
    }
    email = _sec_email(root)
    if email:
        ctx["sec_user_agent"] = f"agent-investors research {email}"

    pf = root / "config" / "portfolio.yaml"
    if pf.is_file():
        ctx["holdings_symbols"] = holdings_symbols(_read_text(pf))

    if not example.is_file():
        ctx["message"] = (f"The trader-budget skill is not installed ({example.as_posix()} is missing). "
                          "Install the day-trader skills first.")
        _write_text(run_dir / "context.json", _dump(ctx))
        return ctx, 2
    example_cfg = json.loads(_read_text(example))

    if not config_path.is_file():
        trader.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(example, config_path)  # UNCHANGED: the desk never invents the investor's limits
        ctx["config_created"] = True
    try:
        cfg = json.loads(_read_text(config_path))
    except json.JSONDecodeError as exc:
        ctx["message"] = f"trader/config.json is not valid JSON: {exc}"
        _write_text(run_dir / "context.json", _dump(ctx))
        return ctx, 2
    if not isinstance(cfg, dict):
        ctx["message"] = "trader/config.json must be a JSON object"
        _write_text(run_dir / "context.json", _dump(ctx))
        return ctx, 2

    changed = compare_limits(cfg, example_cfg)
    confirmed = cfg.get(CONFIRM_FLAG) is True
    ctx["limits_changed_keys"] = changed
    ctx["limits_confirmed_flag"] = confirmed
    ctx["limits_set"] = bool(changed) or confirmed
    ctx["paper"] = cfg.get("paper", True) is not False
    ctx["config_sec_user_agent_set"] = bool(cfg.get("sec_user_agent")) and cfg.get("sec_user_agent") != SEC_PLACEHOLDER
    ctx["stop_before_sizing"] = not ctx["limits_set"]

    if ctx["config_created"]:
        ctx["message"] = (
            "trader/config.json was just created from the skills' example, UNCHANGED. Set your own numbers "
            "before the desk sizes any trade: budget, max_loss_per_trade_usd, max_position_pct, "
            "cash_buffer_pct, max_daily_loss_usd, max_trades_per_day, no_entries_before and no_entries_after. "
            "Scanning, news, entry timing and the time machine can still run; sizing and the risk check wait.")
    elif not ctx["limits_set"]:
        ctx["message"] = (
            "trader/config.json still equals the skills' example on every limit. Set your own numbers "
            "(max loss per trade, position cap, cash buffer, daily loss limit, trades per day, entry window, "
            f"budget), or add \"{CONFIRM_FLAG}\": true if the example numbers really are yours. "
            "The desk stops before sizing until then.")
    else:
        ctx["message"] = "Limits differ from the example (or are confirmed): sizing may run."
    if ctx["paper"] is False:
        ctx["message"] += (" NOTE: config says \"paper\": false. The desk still only prepares tickets; "
                           "you place any order yourself.")
    _write_text(run_dir / "context.json", _dump(ctx))
    return ctx, 0


# --------------------------------------------------------------------------------------------- validate
def schema_file(name: str, root: Path | None = None) -> Path:
    """Schemas ship with the code (next to this script); ``root`` only moves the data folders, not the schemas."""
    key = name.lower().removeprefix("desk_").removesuffix(".schema.json").removesuffix(".json")
    if key not in SCHEMA_NAMES:
        raise SystemExit(f"desk.py: unknown schema {name!r}; use one of {', '.join(sorted(SCHEMA_NAMES))}")
    return REPO_ROOT / "schemas" / f"{SCHEMA_NAMES[key]}.schema.json"


def validate_doc(doc: Any, schema_path: Path) -> list[str]:
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError:  # pragma: no cover - jsonschema is a repo requirement
        raise SystemExit("desk.py validate needs the 'jsonschema' package (pip install -r requirements.txt)")
    schema = json.loads(_read_text(schema_path))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(doc), key=lambda e: [str(p) for p in e.absolute_path])
    return [f"{e.json_path}: {e.message}" for e in errors]


def validate_file(file: Path, name: str, root: Path | None = None) -> list[str]:
    try:
        doc = json.loads(_read_text(file))
    except FileNotFoundError:
        return [f"{file.as_posix()}: file not found"]
    except json.JSONDecodeError as exc:
        return [f"{file.as_posix()}: not valid JSON ({exc})"]
    return validate_doc(doc, schema_file(name, root))


# ----------------------------------------------------------------------------------------------- status
def result_kind(filename: str) -> str | None:
    m = re.match(r"^([a-z]+)(?:_[A-Za-z0-9.\-]+)?\.json$", filename)
    if m and m.group(1) in RESULT_PREFIXES:
        return RESULT_PREFIXES[m.group(1)]
    return None


def status(root: Path, date: dt.date) -> dict[str, Any]:
    run_dir = root / "runs" / "desk" / date.isoformat()
    files: list[dict[str, Any]] = []
    if run_dir.is_dir():
        for p in sorted(run_dir.rglob("*")):
            if not p.is_file():
                continue
            rec: dict[str, Any] = {"path": p.relative_to(root).as_posix(), "bytes": p.stat().st_size}
            kind = result_kind(p.name) if p.parent == run_dir else None
            if kind:
                errs = validate_file(p, kind, root)
                rec["schema"] = SCHEMA_NAMES[kind]
                rec["valid"] = not errs
                if errs:
                    rec["errors"] = errs[:5]
            files.append(rec)
    trader = root / "trader"
    trader_files = {rel: (trader / rel).is_file() for rel in TRADER_FILES}
    return {
        "date": date.isoformat(),
        "run_dir": run_dir.relative_to(root).as_posix(),
        "run_dir_exists": run_dir.is_dir(),
        "files": files,
        "desk_note": (run_dir / "desk_note.md").is_file(),
        "trader_files": trader_files,
    }


# ------------------------------------------------------------------------------------------------ main
def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover
            pass
    ap = argparse.ArgumentParser(prog="desk.py", description="Helper for the day-trading desk (/trader-desk).")
    ap.add_argument("--root", default=str(REPO_ROOT), help="repository root (default: this repo; for tests)")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("init-day", help="create runs/desk/<date>/, ensure trader/config.json, report the limits")
    p.add_argument("--date", help="YYYY-MM-DD (default: today in New York)")
    p.add_argument("--skills-dir", help="folder with the installed skills (default ~/.claude/skills)")
    p = sub.add_parser("holdings-symbols", help="Yahoo symbols of config/portfolio.yaml (no TASE units)")
    p.add_argument("--portfolio", help="portfolio.yaml path (default config/portfolio.yaml)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("validate", help="validate a desk result file against its schema")
    p.add_argument("--file", required=True)
    p.add_argument("--schema", required=True, help="scout | timer | quant | risk")
    p = sub.add_parser("status", help="list today's desk files and the trader/ files")
    p.add_argument("--date", help="YYYY-MM-DD (default: today in New York)")
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2
    root = Path(args.root).resolve()

    if args.cmd == "init-day":
        ctx, rc = init_day(root, _parse_date(args.date), Path(args.skills_dir) if args.skills_dir else None)
        sys.stdout.write(_dump(ctx))
        return rc
    if args.cmd == "holdings-symbols":
        path = Path(args.portfolio) if args.portfolio else root / "config" / "portfolio.yaml"
        if not path.is_file():
            print(f"desk.py: {path.as_posix()} not found", file=sys.stderr)
            return 2
        syms = holdings_symbols(_read_text(path))
        sys.stdout.write(_dump({"symbols": syms}) if args.json else ",".join(syms) + "\n")
        return 0
    if args.cmd == "validate":
        errors = validate_file(Path(args.file), args.schema, root)
        if errors:
            print("\n".join(errors))
            return 1
        print(f"OK {Path(args.file).as_posix()} matches {schema_file(args.schema, root).name}")
        return 0
    if args.cmd == "status":
        sys.stdout.write(_dump(status(root, _parse_date(args.date))))
        return 0
    return 2  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
