"""Lint the slash commands (``.claude/commands``) and project settings (``.claude/settings.json``).

DESIGN.md §2.2, §2.3 and §7: four commands with frontmatter (``description``, ``argument-hint``,
``allowed-tools``); ``/committee-run`` drives the ``committee.py`` init/next loop and launches
agents in batches of ``concurrency``; settings pre-approve the project scripts and web research,
set UTF-8 for Python and deny ``git push``. Purely static checks; no network, no subprocess.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from scripts import common

REPO_ROOT = Path(__file__).resolve().parents[1]
CLAUDE_DIR = REPO_ROOT / ".claude"
COMMANDS_DIR = CLAUDE_DIR / "commands"
SETTINGS_PATH = CLAUDE_DIR / "settings.json"

COMMANDS = ("committee-setup", "committee-run", "committee-holding", "committee-scorecard")
#: commands whose arguments are required / central -> argument-hint is mandatory
NEEDS_ARGUMENT_HINT = ("committee-run", "committee-holding")
#: commands that launch subagents in waves
ORCHESTRATORS = ("committee-run", "committee-holding")

CLI_BASH_RULE = "Bash(python scripts/committee.py:*)"
CLI_PWSH_RULE = "PowerShell(python scripts/committee.py *)"

#: the model tier the repo must never reference; assembled at runtime so this file stays clean
FORBIDDEN_WORD = "".join(chr(c) for c in (111, 112, 117, 115))

TEXT_SUFFIXES = {".md", ".json", ".yaml", ".yml", ".txt", ".py", ".j2", ".html"}


# ------------------------------------------------------------------------------------- helpers


def _command_path(name: str) -> Path:
    return COMMANDS_DIR / f"{name}.md"


def _split_frontmatter(text: str) -> tuple[dict, str]:
    lines = text.splitlines()
    assert lines and lines[0].strip() == "---", "command must start with a '---' frontmatter line"
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:  # pragma: no cover - failure path
        raise AssertionError("frontmatter is not closed with '---'") from None
    meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    assert isinstance(meta, dict), "frontmatter must be a YAML mapping"
    return meta, "\n".join(lines[end + 1:])


def _load_command(name: str) -> tuple[dict, str]:
    return _split_frontmatter(_command_path(name).read_text(encoding="utf-8"))


def _flat_body(name: str) -> str:
    """Command body with all whitespace runs collapsed (prose is line-wrapped)."""
    return " ".join(_load_command(name)[1].split())


def _allowed_tools(meta: dict) -> list[str]:
    raw = meta.get("allowed-tools")
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    assert isinstance(raw, str) and raw.strip(), "allowed-tools must be a non-empty string or list"
    # split on commas that are not inside a Tool(...) specifier
    return [t.strip() for t in re.split(r",\s*(?![^()]*\))", raw) if t.strip()]


def _load_settings() -> dict:
    return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))


def _rule_prefix(rule: str) -> tuple[str, str]:
    """``Bash(python scripts/committee.py:*)`` -> ("Bash", "python scripts/committee.py")."""
    m = re.fullmatch(r"(\w+)\((.*)\)", rule)
    assert m, f"not a Tool(specifier) rule: {rule}"
    spec = m.group(2)
    for suffix in (":*", " *", "*"):
        if spec.endswith(suffix):
            spec = spec[: -len(suffix)]
            break
    return m.group(1), spec


# ------------------------------------------------------------------------------------- commands


@pytest.mark.parametrize("name", COMMANDS)
def test_command_file_exists_and_is_clean_utf8(name: str) -> None:
    path = _command_path(name)
    assert path.is_file(), f"missing slash command {path}"
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "BOM breaks frontmatter parsing"
    raw.decode("utf-8")  # must be valid UTF-8


@pytest.mark.parametrize("name", COMMANDS)
def test_command_frontmatter(name: str) -> None:
    meta, body = _load_command(name)
    desc = meta.get("description")
    assert isinstance(desc, str) and len(desc.split()) >= 5, "description must be a sentence"
    tools = _allowed_tools(meta)
    assert tools, "allowed-tools must not be empty"
    if name in NEEDS_ARGUMENT_HINT:
        hint = meta.get("argument-hint")
        assert isinstance(hint, str) and hint.strip(), f"{name} needs an argument-hint"
    if "argument-hint" in meta:
        assert isinstance(meta["argument-hint"], str), "argument-hint must be a (quoted) string"
        assert "$ARGUMENTS" in body, "a command with arguments must reference $ARGUMENTS"
    assert body.strip(), "command body must not be empty"


def test_argument_hints_match_design() -> None:
    assert _load_command("committee-run")[0]["argument-hint"] == "[--quick] [--sectors a,b] [--dry-run]"
    assert _load_command("committee-holding")[0]["argument-hint"] == "<TICKER>"


@pytest.mark.parametrize("name", COMMANDS)
def test_allowed_tools_preapprove_committee_cli(name: str) -> None:
    tools = _allowed_tools(_load_command(name)[0])
    assert CLI_BASH_RULE in tools
    assert CLI_PWSH_RULE in tools
    assert "Read" in tools
    for tool in tools:
        assert "git" not in tool.lower(), f"commands must not pre-approve git: {tool}"
        assert tool not in ("Bash", "PowerShell"), "never pre-approve an unrestricted shell"


@pytest.mark.parametrize("name", ORCHESTRATORS)
def test_orchestrators_can_launch_agents_and_track_tasks(name: str) -> None:
    tools = _allowed_tools(_load_command(name)[0])
    assert "Agent" in tools
    assert "TodoWrite" in tools
    if name == "committee-run":
        # the chair review (committee-chair.md) lets the main session verify disputed facts itself
        assert "WebFetch" in tools and "WebSearch" in tools
    else:
        # otherwise the main session never researches itself; agents do
        assert "WebFetch" not in tools and "WebSearch" not in tools


#: the only files /committee-setup may edit without a permission prompt
SETUP_CONFIG_FILES = ("config/investor_profile.yaml", "config/portfolio.yaml", "config/universe.yaml")
#: built-in tools that modify files; an unscoped pre-approval would cover every path
FILE_WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def _file_write_rules(tools: list[str]) -> list[tuple[str, str | None]]:
    """(tool, path specifier or None for a bare tool) for every file-writing rule."""
    out: list[tuple[str, str | None]] = []
    for tool in tools:
        if tool in FILE_WRITE_TOOLS:
            out.append((tool, None))
            continue
        m = re.fullmatch(r"(\w+)\((.*)\)", tool)
        if m and m.group(1) in FILE_WRITE_TOOLS:
            out.append((m.group(1), m.group(2).strip()))
    return out


def _backticked_patterns(body: str) -> list[re.Pattern[str]]:
    """Every inline-code span that is an anchored regex (``^...$``), compiled."""
    return [re.compile(p) for p in re.findall(r"`(\^[^`]+\$)`", body)]


def test_setup_tools() -> None:
    tools = _allowed_tools(_load_command("committee-setup")[0])
    for tool in ("AskUserQuestion", "Read"):
        assert tool in tools, f"/committee-setup needs {tool}"
    for path in SETUP_CONFIG_FILES:
        assert f"Edit({path})" in tools, f"/committee-setup must pre-approve Edit({path})"
        assert f"Write({path})" in tools, f"/committee-setup must pre-approve Write({path})"
    rules = _file_write_rules(tools)
    unscoped = [t for t, spec in rules if spec is None]
    assert not unscoped, f"/committee-setup must not pre-approve bare {unscoped} (any path)"
    stray = [f"{t}({spec})" for t, spec in rules if spec not in SETUP_CONFIG_FILES]
    assert not stray, f"/committee-setup may only pre-approve edits to its three configs: {stray}"


@pytest.mark.parametrize("name", [c for c in COMMANDS if c != "committee-setup"])
def test_other_commands_preapprove_no_file_writes(name: str) -> None:
    """Orchestrators and the scorecard never edit files; committee.py does all writing."""
    rules = _file_write_rules(_allowed_tools(_load_command(name)[0]))
    assert not rules, f"{name} must not pre-approve file edits: {rules}"


def test_run_command_loop_contract() -> None:
    body = _flat_body("committee-run")
    for needle in (
        "committee.py next",
        "committee.py init",
        "committee.py dry-run",
        "dry-run",
        "init",
        "concurrency",
        "validate-config",
        "/committee-setup",
        "redispatch",
        "status --run",
        "600000",
        "Execute work order <order_path>",
        "subagent_type",
        "ONE message",
        "--quick",
        "--sectors",
        "memo_md",
        "memo_html",
        "ledger",
        "restart Claude Code",
        "not personalized financial advice",
    ):
        assert needle in body, f"/committee-run body must mention {needle!r}"
    for loop_type in ('"type": "continue"', '"type": "agents"', '"type": "done"'):
        assert loop_type in body, f"/committee-run must handle {loop_type}"
    # rough usage numbers from the design
    assert "20" in body and "85-100" in body


def test_run_command_resume_run_id_pattern() -> None:
    """--resume accepts the suffixed ids committee.py creates (DESIGN §2) and nothing shell-unsafe."""
    body = _flat_body("committee-run")
    patterns = [p for p in _backticked_patterns(body) if p.match("2026-10-03_1015")]
    assert len(patterns) == 1, "/committee-run must state exactly one RUN_ID pattern"
    run_id_re = patterns[0]
    for good in ("2026-10-03_1015", "2026-10-03_1015_2", "2026-10-03_1015_3", "2026-10-03_1015_dryrun"):
        assert run_id_re.match(good), f"RUN_ID pattern must accept {good}"
    for bad in ("2026-10-03", "2026-10-03_101", "x2026-10-03_1015", "2026-10-03_1015;rm -rf x",
                "2026-10-03_1015 --quick", "2026-10-03_1015_$(id)", "../2026-10-03_1015",
                "2026-10-03_1015_"):
        assert not run_id_re.match(bad), f"RUN_ID pattern must reject {bad!r}"


def test_run_command_resume_steps() -> None:
    """A resumed run skips §4 (init), so the resume steps must create the task list themselves."""
    body = _flat_body("committee-run")
    resume = body[body.index("`/committee-run --resume <run_id>` does this for you"):]
    for needle in (
        "status --run <run_id>",
        "current_step",
        "Create the §4.3 task list",
        "completed",
        "in progress",
        "redispatch",
        "\"dry_run\": true",
        "§5 loop",
    ):
        assert needle in resume, f"/committee-run --resume steps must mention {needle!r}"
    assert resume.index("Create the §4.3 task list") < resume.index("§5 loop")
    assert "_dryrun" in body


def test_run_command_names_every_agent_type() -> None:
    body = _flat_body("committee-run")
    for agent_type in common.AGENT_TYPES.values():
        assert f"`{agent_type}`" in body, f"preflight must check for subagent {agent_type}"


def test_run_command_names_every_sector_key() -> None:
    body = _flat_body("committee-run")
    for key in common.SECTOR_KEYS:
        assert key in body, f"--sectors help must list {key}"


def test_holding_command_contract() -> None:
    body = _flat_body("committee-holding")
    for needle in (
        "committee.py holding <TICKER>",
        "committee.py next --run",
        "concurrency",
        "Execute work order <order_path>",
        "ONE message",
        "trial_<TICKER>.md",
        "validate-config",
        "redispatch",
        "600000",
        "not personalized financial advice",
    ):
        assert needle in body, f"/committee-holding body must mention {needle!r}"


def test_setup_command_contract() -> None:
    body = _flat_body("committee-setup")
    for needle in (
        "config/investor_profile.yaml",
        "config/portfolio.yaml",
        "config/universe.yaml",
        "AskUserQuestion",
        "Edit",
        "validate-config",
        "sec_contact_email",
        "proxy_ticker",
        "TASE",
        "horizon_years",
        "risk_tolerance",
        "max_position_pct",
        "max_fund_position_pct",
        "max_sector_pct",
        "min_trade_usd",
        "target_cash_pct",
        "cost_basis",
        "purchase_date",
        "cash_usd",
        "min_market_cap_usd",
        "min_avg_dollar_volume_usd",
        "exclusions",
        "additions",
        "passwords",
        "API keys",
    ):
        assert needle in body, f"/committee-setup body must mention {needle!r}"
    assert re.search(r"[Qq]uote tickers and dates", body)


def test_setup_proxy_funds_leave_cost_basis_null() -> None:
    """A TASE unit's ILS cost cannot be compared with the proxy's USD price (portfolio.py skips
    the tax estimate for proxy-priced holdings), so setup must not ask for it."""
    body = _flat_body("committee-setup")
    for needle in (
        "Leave `cost_basis` `null`** for proxy-priced holdings",
        "never estimates a capital gain or tax for a holding with a `proxy_ticker`",
        "use `weight_pct`, not `shares`",
        "Skip this question for proxy-priced holdings",
        "**in USD**",
    ):
        assert needle in body, f"/committee-setup proxy guidance must mention {needle!r}"
    assert "`cost_basis_currency` to the fund's real currency" not in body
    assert "which enables the capital-gains note" not in body


def test_scorecard_ledger_path_rule() -> None:
    """Git Bash strips unquoted backslashes, so Windows paths must be converted before the call."""
    body = _flat_body("committee-scorecard")
    for needle in ("backslash", "forward slash", "Git Bash", "C:/Users/me/ledger/recs.csv"):
        assert needle in body, f"/committee-scorecard --ledger rule must mention {needle!r}"
    patterns = [p for p in _backticked_patterns(body) if p.match("ledger/recommendations.csv")]
    assert len(patterns) == 1, "/committee-scorecard must state exactly one --ledger path pattern"
    path_re = patterns[0]
    for good in ("ledger/recommendations.csv", "C:\\Users\\mrkrp\\ledger\\recs.csv",
                 "C:/Users/mrkrp/ledger/recs.csv", "../backup/recs_2026-10.csv"):
        assert path_re.match(good), f"--ledger pattern must accept {good!r}"
    for bad in ("recs.txt", "my ledger/recs.csv", "recs.csv;rm -rf x", "$(id).csv", "`id`.csv",
                "a|b.csv", "a&b.csv", "a>b.csv", "'recs.csv'", '"recs.csv"', "recs.csv.bak"):
        assert not path_re.match(bad), f"--ledger pattern must reject {bad!r}"


def test_scorecard_command_contract() -> None:
    body = _flat_body("committee-scorecard")
    for needle in (
        "committee.py scorecard",
        "hit rate",
        "By role",
        "By action",
        "min_age_days",
        "Small samples",
        "Survivorship",
        "not personalized financial advice",
    ):
        assert needle in body, f"/committee-scorecard body must mention {needle!r}"


def _section(body: str, start: str, end: str) -> str:
    i = body.index(start)
    j = body.index(end, i + len(start))
    return body[i:j]


def test_run_dry_run_reports_only_printed_paths() -> None:
    """dry-run prints memo_md/memo_html and run_dir but no ledger-preview path; the command must
    build the preview path from run_dir instead of telling the orchestrator it was printed."""
    body = _flat_body("committee-run")
    dry = _section(body, "## 2. `--dry-run`", "## 3. Preflight")
    for needle in ("memo_md", "memo_html", "run_dir", "`<run_dir>/phase4/ledger_preview.csv`",
                   "no ledger path"):
        assert needle in dry, f"/committee-run --dry-run report must mention {needle!r}"
    assert "plus `ledger_preview.csv`" not in dry
    src = (REPO_ROOT / "scripts" / "committee.py").read_text(encoding="utf-8")
    assert '"phase4" / "ledger_preview.csv"' in src, "dry-run preview location changed in committee.py"


def test_redispatch_continue_result_launches_nothing() -> None:
    """redispatch returns {"type": "continue"} when nothing is dispatched; the commands must say
    to launch no agents then, instead of 'handle it exactly like an agents response'."""
    run = _flat_body("committee-run")
    sec7 = _section(run, "## 7. Errors and resuming", "`/committee-run --resume <run_id>` does this")
    resume = _section(run, "`/committee-run --resume <run_id>` does this", "## Usage")
    holding = _flat_body("committee-holding")
    hold = _section(holding, "redispatch --run <run_id>", "## 5. Finish")
    for where, text in (("committee-run §7", sec7), ("committee-run --resume", resume),
                        ("committee-holding", hold)):
        assert "redispatch" in text, f"{where} must mention redispatch"
        assert "continue" in text, f"{where} must handle a redispatch 'continue' result"
        assert "nothing was dispatched" in text, f"{where} must say 'continue' means nothing dispatched"
        assert "launch no agents" in text, f"{where} must say to launch no agents on 'continue'"
    assert "Handle its output exactly like an `agents` response" not in run
    src = (REPO_ROOT / "scripts" / "committee.py").read_text(encoding="utf-8")
    redispatch_src = src[src.index("def run_redispatch"):src.index("def run_status")]
    assert '"type": "continue"' in redispatch_src


def test_scorecard_reads_printed_scorecard_path() -> None:
    """With --ledger the scorecard lands next to that ledger, so the command must use the printed
    scorecard_md path rather than a hard-coded ledger/ location."""
    body = _flat_body("committee-scorecard")
    assert "`scorecard_md`" in body
    assert "ledger/scorecard_<date>.md" not in body
    assert "Read the file at the printed `scorecard_md` path" in body
    src = (REPO_ROOT / "scripts" / "committee.py").read_text(encoding="utf-8")
    assert '"scorecard_md":' in src


@pytest.mark.parametrize("name", COMMANDS)
def test_commands_never_instruct_a_push(name: str) -> None:
    _, body = _load_command(name)
    for line in body.splitlines():
        if "git push" in line:
            assert re.search(r"\b[Nn]ever\b", line), f"'git push' only allowed in a prohibition: {line}"


# ------------------------------------------------------------------------------------- settings


def test_settings_json_parses_and_sets_utf8_env() -> None:
    raw = SETTINGS_PATH.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    settings = _load_settings()
    env = settings.get("env", {})
    assert env.get("PYTHONUTF8") == "1"
    assert env.get("PYTHONIOENCODING") == "utf-8"
    assert all(isinstance(v, str) for v in env.values()), "env values must be strings"


def test_settings_permissions() -> None:
    perms = _load_settings()["permissions"]
    allow, deny = perms["allow"], perms["deny"]
    assert isinstance(allow, list) and isinstance(deny, list)
    for rule in (
        "Bash(python scripts/*)",
        "Bash(python -m pytest*)",
        "PowerShell(python scripts/*)",
        "PowerShell(python -m pytest*)",
        "Write(runs/**)",
        "Edit(runs/**)",
        "Read(runs/**)",
        "Write(data/**)",
        "Write(ledger/**)",
        "WebSearch",
        "WebFetch",
    ):
        assert rule in allow, f"settings allow list misses {rule}"
    assert "Bash(git push*)" in deny
    assert "PowerShell(git push*)" in deny
    assert not set(allow) & set(deny)
    assert not any("git" in rule.lower() for rule in allow), "never pre-approve git"
    assert len(allow) == len(set(allow)) and len(deny) == len(set(deny))


def test_settings_cover_command_shell_rules() -> None:
    """Every Bash/PowerShell rule a command pre-approves is also inside the project allow list."""
    allow = [_rule_prefix(r) for r in _load_settings()["permissions"]["allow"] if "(" in r]
    for name in COMMANDS:
        for tool in _allowed_tools(_load_command(name)[0]):
            if not tool.startswith(("Bash(", "PowerShell(")):
                continue
            kind, spec = _rule_prefix(tool)
            assert any(kind == k and spec.startswith(p) for k, p in allow), (
                f"{name}: {tool} is not covered by .claude/settings.json"
            )


# ------------------------------------------------------------------------------------- hygiene


def _claude_text_files() -> list[Path]:
    files = [p for p in CLAUDE_DIR.rglob("*") if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES]
    return sorted(files) + [Path(__file__).resolve()]


def test_forbidden_model_word_absent() -> None:
    offenders = []
    for path in _claude_text_files():
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        if FORBIDDEN_WORD in text:
            offenders.append(path.relative_to(REPO_ROOT).as_posix())
    assert not offenders, f"forbidden model name found in: {offenders}"


# ------------------------------------------------------------------ review fixes (dry-run flags, background, outage)
def test_run_dry_run_supports_no_other_flag() -> None:
    """The dry-run fixtures cover only the default full run: --dry-run with --quick/--sectors is refused."""
    body = _flat_body("committee-run")
    table_row = _section(body, "| `--dry-run` |", "| `--resume RUN_ID` |")
    assert "supports no other flag" in table_row
    parse = _section(body, "## 1. Parse `$ARGUMENTS`", "## 2. `--dry-run`")
    assert "`--dry-run` combined with any other flag" in parse and "show the usage line" in parse
    dry = _section(body, "## 2. `--dry-run`", "## 3. Preflight")
    assert "Run exactly `python scripts/committee.py dry-run`" in dry
    assert "dry-run --quick" not in dry, "§2 must not tell the orchestrator to pass --quick to dry-run"
    assert "adding the validated" not in dry
    src = (REPO_ROOT / "scripts" / "committee.py").read_text(encoding="utf-8")
    assert "dry-run does not support" in src, "committee.py no longer refuses dry-run flags: revisit §1/§2"


def test_run_agent_calls_are_foreground_and_outage_stops_the_loop() -> None:
    body = _flat_body("committee-run")
    loop = _section(body, "## 5. The loop", "## 6. Finish")
    assert "`run_in_background: false`" in loop and "background by default" in loop
    assert "produced no output" in loop and "usage limit" in loop
    assert "**STOP**" in loop and "redispatch --run <run_id>" in loop
    assert loop.index("produced no output") > loop.index("**Anything else**")
    src = (REPO_ROOT / "scripts" / "committee.py").read_text(encoding="utf-8")
    assert "produced no output - likely a usage limit or outage" in src, "circuit-breaker message changed"


# ====================================================================================================
# Hard rule: no agent trades (every command), the settings that enforce it, and the /trader-desk command
# ====================================================================================================
NO_TRADE_SENTENCE = (
    "No agent trades: never place, modify or cancel an order, never move money, never record a yes; "
    "at most prepare information or a ticket for the investor to place himself."
)
TRADING_MCP_PREFIX = "mcp__d8f551b2-444f-46a3-9438-69c0af787baf__"
#: every state-changing tool of the connected trading MCP server (orders, positions, money, trading modes,
#: watchlist, paper account, order-suggestion cards): all denied
TRADING_STATE_CHANGING = (
    "execute_order", "execute_orders_batch", "execute_tpsl", "execute_prediction_order",
    "close_position", "close_positions_batch", "close_prediction_position",
    "cancel_order", "cancel_prediction_order", "modify_position", "update_leverage",
    "deposit_earn", "withdraw_earn",
    "enable_trading", "enable_automated_trading", "enable_paper_trading",
    "disable_automated_trading", "disable_paper_trading",
    "create_onramp_session", "generate_deposit_address", "payment_link", "show_deposit",
    "edit_watchlist", "reset_paper_account",
    "suggest_order", "suggest_trade", "suggest_trades_batch",
)
ALPACA_STATE_CHANGING = (
    "place_stock_order", "place_option_order", "place_crypto_order", "cancel_order_by_id", "replace_order_by_id",
    "close_position", "close_all_positions", "cancel_all_orders", "exercise_options_position",
    "do_not_exercise_options_position", "update_account_config",
)
ALPACA_PATTERNS = ("place_*", "cancel_*", "close_*")
SKILL_SCRIPTS = {
    "trader-scanner": "scan.py", "trader-news": "news.py", "trader-pelosi": "ptr.py", "trader-analyst": "facts.py",
    "trader-entry": "entry.py", "trader-exit": "exit.py", "trader-alerts": "alerts.py",
    "trader-timemachine": "timemachine.py", "trader-budget": "size.py", "trader-risk": "check.py",
    "trader-broker": "ticket.py",
}
SKILLS_ABS = "C:/Users/mrkrp/.claude/skills"
SKILLS_HOME = "~/.claude/skills"
ALL_COMMANDS = sorted(p.stem for p in COMMANDS_DIR.glob("*.md"))


@pytest.mark.parametrize("name", ALL_COMMANDS)
def test_every_command_carries_the_hard_rule_no_agent_trades(name: str) -> None:
    _, body = _load_command(name)
    assert "## Hard rule: no agent trades" in body, f"{name}: missing the hard-rule section"
    assert " ".join(body.split()).count(NO_TRADE_SENTENCE) == 1, f"{name}: the no-trading sentence must appear once"


def test_readme_and_contracts_have_the_hard_rule_section() -> None:
    for fname in ("README.md", "CONTRACTS.md"):
        text = (REPO_ROOT / fname).read_text(encoding="utf-8")
        assert re.search(r"^## Hard rule: no agent trades\s*$", text, re.M), f"{fname}: missing the section"
        assert NO_TRADE_SENTENCE in " ".join(text.split()), f"{fname}: missing the no-trading sentence"


def test_every_state_changing_trading_mcp_tool_stays_denied() -> None:
    deny = set(_load_settings()["permissions"]["deny"])
    allow = set(_load_settings()["permissions"]["allow"])
    for tool in TRADING_STATE_CHANGING:
        assert TRADING_MCP_PREFIX + tool in deny, f"settings must deny {tool}"
        assert TRADING_MCP_PREFIX + tool not in allow
    for tool in ALPACA_STATE_CHANGING:
        assert f"mcp__alpaca__{tool}" in deny, f"settings must deny the Alpaca tool {tool}"
    for pat in ALPACA_PATTERNS:
        assert f"mcp__*alpaca*__{pat}" in deny, f"settings must deny the pattern {pat}"
    assert not [r for r in allow if r.startswith("mcp__")], "no MCP tool is pre-approved"


def test_existing_git_push_denies_are_kept() -> None:
    deny = _load_settings()["permissions"]["deny"]
    for rule in ("Bash(git push*)", "Bash(git * push*)", "Bash(git -C * push*)", "PowerShell(git push*)",
                 "PowerShell(git * push*)", "PowerShell(git -C * push*)"):
        assert rule in deny


def test_no_bare_or_broad_shell_allow_rule() -> None:
    allow = _load_settings()["permissions"]["allow"]
    for rule in allow:
        assert rule not in ("Bash", "PowerShell", "Bash(*)", "PowerShell(*)"), f"bare shell allow: {rule}"
        if rule.startswith(("Bash(", "PowerShell(")):
            spec = rule.split("(", 1)[1]
            assert spec.startswith("python "), f"shell allow rules are python invocations only: {rule}"
            assert not spec.startswith(("python -c", "python3", "python -m pip", "python *", "python:")), rule
            assert "pip" not in spec.split()[:3] and "curl" not in spec and "git" not in spec.lower(), rule
        assert "python3" not in rule


def test_each_skill_script_has_exactly_the_expected_allow_rules() -> None:
    allow = _load_settings()["permissions"]["allow"]
    for skill, script in SKILL_SCRIPTS.items():
        for base in (SKILLS_ABS, SKILLS_HOME):
            subs = {"trader-entry": ["check"], "trader-broker": ["show", "log", "flatten"]}.get(skill, [None])
            for sub in subs:
                cmd = f"python {base}/{skill}/{script}" + (f" {sub}" if sub else "")
                assert f"Bash({cmd}:*)" in allow, f"missing Bash allow for {cmd}"
                assert f"PowerShell({cmd} *)" in allow, f"missing PowerShell allow for {cmd}"
    skill_rules = [r for r in allow if ".claude/skills" in r]
    assert len(skill_rules) == len(set(skill_rules))
    for rule in skill_rules:
        assert "approve" not in rule and "decline" not in rule and " fill" not in rule and "result" not in rule


def test_broker_recording_modes_and_entry_watch_are_denied() -> None:
    deny = _load_settings()["permissions"]["deny"]
    for sub in ("approve", "decline", "result", "fill"):
        for base in (SKILLS_ABS, SKILLS_HOME):
            assert f"Bash(python {base}/trader-broker/ticket.py {sub}:*)" in deny
            assert f"PowerShell(python {base}/trader-broker/ticket.py {sub} *)" in deny
    for base in (SKILLS_ABS, SKILLS_HOME):
        assert f"Bash(python {base}/trader-entry/entry.py watch:*)" in deny
    allow = _load_settings()["permissions"]["allow"]
    assert not set(allow) & set(deny)


# ---- /trader-desk
DESK_COMMAND = "trader-desk"


def test_trader_desk_command_frontmatter_and_tools() -> None:
    path = _command_path(DESK_COMMAND)
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    meta, body = _load_command(DESK_COMMAND)
    assert meta["argument-hint"] == "[scan | pick TICKER | status] [--symbols A,B] [--holdings]"
    assert "$ARGUMENTS" in body
    assert isinstance(meta["description"], str) and len(meta["description"].split()) >= 5
    tools = _allowed_tools(meta)
    settings_allow = [_rule_prefix(r) for r in _load_settings()["permissions"]["allow"] if "(" in r]
    for tool in tools:
        assert tool not in ("Bash", "PowerShell", "Write", "Edit", "WebFetch", "WebSearch"), tool
        assert "ticket.py" not in tool and "git" not in tool.lower(), tool
        if tool.startswith(("Bash(", "PowerShell(")):
            kind, spec = _rule_prefix(tool)
            assert any(kind == k and spec.startswith(p) for k, p in settings_allow), f"{tool} not in settings"
    assert "Write(runs/desk/**)" in tools
    for needed in ("Agent", "TodoWrite", "AskUserQuestion", "Read"):
        assert needed in tools
    assert {r for r in tools if r.startswith(("Write(", "Edit("))} == {"Write(runs/desk/**)"}


def test_trader_desk_command_contract() -> None:
    body = _flat_body(DESK_COMMAND)
    for needle in (
        "Nothing was ordered. You place the order yourself.",
        "The investor places the order himself",
        "never record a yes",
        "Paper by default",
        "\"paper\": true",
        "stop before sizing",
        "Config check",
        "Never invent the investor's risk limits",
        "desk_limits_confirmed",
        "Market-hours check, always first",
        "alerts.py clock",
        "alerts.py quote",
        "weekend, holiday",
        "Education and tooling, not financial advice. Most people who day trade lose money.",
        "AskUserQuestion",
        "You never pick for him",
        "chosen by the chair from the scout list",
        "at most 3",
        "Execute desk order",
        "subagent_type",
        "desk-scout", "desk-timer", "desk-quant", "desk-risk",
        "desk_note.md",
        "send an agent back",
        "once",
        "BLOCK stops the line",
        "python scripts/desk.py init-day",
        "python scripts/desk.py validate",
        "--schema scout", "--schema timer", "--schema quant", "--schema risk",
        "Long side only",
        "TASE units",
        "run_in_background: false",
    ):
        assert needle in body, f"/trader-desk body must mention {needle!r}"
    last = [ln for ln in body.split("`") if ln.strip()]
    assert "`Nothing was ordered. You place the order yourself.`" in _load_command(DESK_COMMAND)[1]
    # the pick order: scan -> news -> pick -> timer -> quant -> risk -> note
    positions = [body.index(h) for h in ("## 3. Scout", "## 4. The pick", "## 5. Timer", "## 6. Quant",
                                         "## 7. Risk and tickets", "## 8. The desk note")]
    assert positions == sorted(positions)
    assert last  # silence unused warning in strict linters


def test_trader_desk_never_instructs_an_order_or_a_yes() -> None:
    _, body = _load_command(DESK_COMMAND)
    for para in re.split(r"\n\s*\n", body):
        line = " ".join(para.split())
        if re.search(r"ticket\.py[`'\" ]+(approve|decline|result|fill)\b", line):
            assert re.search(r"\bnever\b", line, re.I), line
        if re.search(r"\b(place|places|placing|execute)\b", line, re.I) and "Execute desk order" not in line:
            assert re.search(r"\b(never|not|nothing|himself|yourself|investor|cannot|no)\b", line, re.I), line
        assert "git push" not in line or "Never" in line
    assert "ticket.py" in body  # named only to forbid it


def test_desk_note_ends_with_the_required_last_line() -> None:
    body = _flat_body(DESK_COMMAND)
    section = _section(body, "## 8. The desk note", "## 9. Sending an agent back")
    assert "last line** is exactly" in section
    assert "`Nothing was ordered. You place the order yourself.`" in section
    assert "Most people who day trade lose money" in section
