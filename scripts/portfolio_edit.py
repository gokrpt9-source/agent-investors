"""portfolio_edit.py - safe, comment-preserving edits of config/portfolio.yaml.

Used by the dashboard's "Edit holdings" form. The file is edited as TEXT (line by line), so the header
documentation, per-holding notes and everything else survive; the result is validated with the project's
own config schema BEFORE anything is written, and a timestamped backup goes to ``data/backups/``.

    apply_edits(text, edits) -> new text           # pure
    save_edits(edits, path=None) -> dict            # validate + backup + atomic write

``edits`` = {
    "cash_usd": 1285.32,                           # optional, USD figure for all cash
    "holdings": {"KO": {"shares": 10, "cost_basis": 60.0, "units": 73, "unit_cost_ils": 267.75}},
    "remove": ["PG"],
    "add": [{"ticker": "KO", "shares": 10, "cost_basis": 60.0, "currency": "USD"}],   # currency USD|ILS
}
Only the keys shown are ever touched. Anything invalid raises ``EditError`` with a readable message.
"""
from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import common  # noqa: E402

import re  # noqa: E402
import shutil  # noqa: E402
from datetime import datetime  # noqa: E402
from typing import Any  # noqa: E402

import yaml  # noqa: E402

EDITABLE = ("shares", "cost_basis", "units", "unit_cost_ils")
TICKER_LINE = re.compile(r'^  - ticker:\s*"?([^"\s#]+)"?\s*$')


class EditError(ValueError):
    """The requested edit is invalid (message is shown to the user)."""


def _num(x: Any, name: str, *, allow_none: bool = False, positive: bool = False) -> float | None:
    if x is None or x == "":
        if allow_none:
            return None
        raise EditError(f"{name}: a number is required")
    try:
        v = float(x)
    except (TypeError, ValueError):
        raise EditError(f"{name}: {x!r} is not a number") from None
    if v != v or v in (float("inf"), float("-inf")):
        raise EditError(f"{name}: not a finite number")
    if v < 0 or (positive and v <= 0):
        raise EditError(f"{name}: must be {'greater than' if positive else 'at least'} 0")
    return v


def _fmt(v: float | None) -> str:
    if v is None:
        return "null"
    s = format(v, ".10g")
    return s


def _split_blocks(lines: list[str]) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """(header lines before the first holding, [(TICKER, block lines)...]). Blocks run to the next holding."""
    head: list[str] = []
    blocks: list[tuple[str, list[str]]] = []
    cur: tuple[str, list[str]] | None = None
    in_holdings = False
    for ln in lines:
        if ln.startswith("holdings:"):
            in_holdings = True
            head.append(ln)
            continue
        m = TICKER_LINE.match(ln) if in_holdings else None
        if m:
            cur = (m.group(1).upper(), [ln])
            blocks.append(cur)
        elif cur is not None:
            cur[1].append(ln)
        else:
            head.append(ln)
    return head, blocks


def _set_key(block: list[str], key: str, value: str) -> None:
    pat = re.compile(rf"^(    {re.escape(key)}:)\s*.*$")
    for i, ln in enumerate(block):
        if pat.match(ln):
            block[i] = f"    {key}: {value}"
            return
    # insert after the last plain `    key: value` line before a `note:` line (or at the end of the block)
    at = len(block)
    for i, ln in enumerate(block):
        if ln.startswith("    note:"):
            at = i
            break
    block.insert(at, f"    {key}: {value}")


def _get(block: list[str], key: str, text: bool = False) -> Any:
    for ln in block:
        m = re.match(rf"^    {re.escape(key)}:\s*(.*?)\s*(#.*)?$", ln)
        if m:
            v = m.group(1).strip().strip('"')
            if text:
                return v
            try:
                return float(v)
            except ValueError:
                return None
    return None


def _new_block(add: dict) -> list[str]:
    t = str(add.get("ticker") or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-=^]{0,19}", t):
        raise EditError(f"ticker {add.get('ticker')!r} is not a valid symbol")
    cur = str(add.get("currency") or "USD").upper()
    if cur not in ("USD", "ILS"):
        raise EditError("currency must be USD or ILS")
    shares = _num(add.get("shares"), f"{t} shares", positive=True)
    cb = _num(add.get("cost_basis"), f"{t} average cost", allow_none=True)
    asset = "stock" if str(add.get("asset_type") or "stock") != "fund" else "fund"
    lines = [f'  - ticker: "{t}"', f"    asset_type: {asset}", f"    shares: {_fmt(shares)}", "    weight_pct: null",
             f"    cost_basis: {_fmt(cb)}", f"    cost_basis_currency: {cur}", "    purchase_date: null",
             f"    currency: {cur}", "    proxy_ticker: null", "    sector_override: null",
             '    note: "Added in the dashboard."']
    return lines


def apply_edits(text: str, edits: dict) -> str:
    """Return ``text`` with the edits applied. Raises ``EditError`` for unknown tickers or bad values."""
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.replace("\r\n", "\n").split("\n")
    head, blocks = _split_blocks(lines)
    by = {t: blk for t, blk in blocks}

    if "cash_usd" in edits and edits["cash_usd"] is not None:
        cash = _num(edits["cash_usd"], "cash", allow_none=False)
        note = edits.get("cash_note")
        done = False
        for i, ln in enumerate(head):
            if re.match(r"^cash_usd:", ln):
                head[i] = f"cash_usd: {_fmt(round(cash, 2))}" + (f"   # {note}" if note else "")
                done = True
        if not done:
            raise EditError("portfolio.yaml has no cash_usd line to edit")
    if edits.get("as_of"):
        for i, ln in enumerate(head):
            if re.match(r"^as_of:", ln):
                head[i] = f'as_of: "{edits["as_of"]}"' + "   # edited in the dashboard"

    for t, ch in (edits.get("holdings") or {}).items():
        key = str(t).upper()
        if key not in by:
            raise EditError(f"{t}: not in portfolio.yaml")
        blk = by[key]
        old_units, old_shares = _get(blk, "units"), _get(blk, "shares")   # before any change below
        for k, v in ch.items():
            if k not in EDITABLE:
                raise EditError(f"{t}: field {k!r} is not editable here")
            if k in ("shares", "units"):
                val = _num(v, f"{t} {k}", positive=True)
            else:
                val = _num(v, f"{t} {k}", allow_none=True)
            _set_key(blk, k, _fmt(val))
        # a proxy-priced fund (e.g. a TASE fund valued through QQQ): changing its units rescales the
        # proxy-equivalent `shares` proportionally, otherwise the valuation would silently go stale
        if "units" in ch and "shares" not in ch:
            if _get(blk, "proxy_ticker", text=True) not in (None, "null") and old_units and old_shares:
                _set_key(blk, "shares", _fmt(round(old_shares * float(ch["units"]) / old_units, 6)))

    removes = {str(x).upper() for x in (edits.get("remove") or [])}
    for r in removes:
        if r not in by:
            raise EditError(f"{r}: cannot remove, not in portfolio.yaml")
    kept = [(t, b) for t, b in blocks if t not in removes]
    for add in edits.get("add") or []:
        nb = _new_block(add)
        t = nb[0].split('"')[1]
        if any(t == k for k, _ in kept):
            raise EditError(f"{t} is already in the portfolio - edit it instead")
        kept.append((t, nb))
    if not kept:
        raise EditError("the portfolio would be empty")

    out = list(head)
    # make sure there is no trailing blank/garbage between header and first holding
    for _, b in kept:
        out.extend(b)
    res = nl.join(out)
    return res if res.endswith(nl) else res + nl


def save_edits(edits: dict, path: str | Path | None = None, backup_dir: str | Path | None = None) -> dict:
    """Validate and write. Returns ``{"path", "backup"}``. Nothing is written when validation fails."""
    p = Path(path) if path else common.config_dir() / "portfolio.yaml"
    old = p.read_bytes().decode("utf-8-sig")     # raw: keep CRLF/LF exactly as the file has it
    new = apply_edits(old, edits)
    try:
        data = yaml.safe_load(new)
    except yaml.YAMLError as exc:
        raise EditError(f"the edited file is not valid YAML: {exc}") from exc
    errs = common.validate_config("portfolio", data, require_portfolio_values=True)
    if errs:
        raise EditError("portfolio.yaml would be invalid: " + "; ".join(str(e) for e in errs[:4]))
    if new == old:
        return {"path": p.as_posix(), "backup": None, "changed": False}
    bdir = Path(backup_dir) if backup_dir else common.cache_dir().parent / "backups"   # data/backups by default
    bdir.mkdir(parents=True, exist_ok=True)
    backup = bdir / f"portfolio_{datetime.now().strftime('%Y%m%d_%H%M%S')}.yaml"
    shutil.copy2(p, backup)
    common.write_text(p, new)
    common.clear_caches()
    return {"path": p.as_posix(), "backup": backup.as_posix(), "changed": True}
