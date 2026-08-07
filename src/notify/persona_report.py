"""Build end-of-day persona digests from saved intraday summaries."""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from src.config import PROJECT_ROOT
from src.market.registry import get_market, list_markets
from src.market.scheduling import enabled_intraday_personas


def load_persona_summary(persona_id: str, root: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    base = root or PROJECT_ROOT
    path = base / "data" / "intraday" / f"{persona_id}_latest.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _summary_matches_day(summary: Dict[str, Any], day: date) -> bool:
    for key in ("start_date", "end_date"):
        raw = summary.get(key)
        if not raw:
            continue
        try:
            if date.fromisoformat(str(raw)[:10]) == day:
                return True
        except ValueError:
            continue
    return False


def collect_market_summaries(
    market_id: str,
    day: date,
    root: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Load today's summaries for scheduled intraday personas in a market."""
    rows: List[Dict[str, Any]] = []
    for persona_id in enabled_intraday_personas(market_id):
        summary = load_persona_summary(persona_id, root=root)
        if summary is None:
            rows.append(
                {
                    "persona_id": persona_id,
                    "missing": True,
                    "start_date": day.isoformat(),
                }
            )
            continue
        if not _summary_matches_day(summary, day):
            rows.append(
                {
                    "persona_id": persona_id,
                    "missing": True,
                    "stale_date": summary.get("start_date"),
                    "start_date": day.isoformat(),
                }
            )
            continue
        rows.append(summary)
    return rows


def _currency_symbol(market_id: str) -> str:
    try:
        return get_market(market_id).currency_symbol
    except ValueError:
        return ""


def _fmt_money(value: Any, symbol: str) -> str:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "n/a"
    sign = "+" if num > 0 else ""
    return f"{sign}{symbol}{num:,.2f}"


def _skip_reasons(summary: Dict[str, Any], limit: int = 3) -> str:
    skipped = summary.get("skipped_signals") or []
    if not skipped:
        return ""
    counts = Counter(str(s.get("reason") or "unknown")[:48] for s in skipped)
    parts = [f"{reason}×{n}" for reason, n in counts.most_common(limit)]
    return "; ".join(parts)


def _trade_lines(summary: Dict[str, Any], symbol: str, limit: int = 4) -> List[str]:
    trades = summary.get("completed_trades") or summary.get("trades") or []
    lines: List[str] = []
    for trade in trades[:limit]:
        side = trade.get("side", "?")
        strat = trade.get("strategy", "?")
        sym = trade.get("symbol", "?")
        pnl = _fmt_money(trade.get("net_pnl"), symbol)
        reason = trade.get("exit_reason") or ""
        lines.append(f"  • {sym} {side} {strat} → {pnl}" + (f" ({reason})" if reason else ""))
    if len(trades) > limit:
        lines.append(f"  • … +{len(trades) - limit} more")
    return lines


def format_market_report(
    market_id: str,
    day: date,
    summaries: Optional[List[Dict[str, Any]]] = None,
    root: Optional[Path] = None,
    max_chars: int = 3500,
) -> str:
    """WhatsApp-friendly digest for one market session."""
    market = get_market(market_id)
    symbol = market.currency_symbol
    summaries = summaries if summaries is not None else collect_market_summaries(
        market_id, day, root=root
    )

    lines: List[str] = [
        f"*Fund EOD — {market.label} — {day.isoformat()}*",
        f"Session: {market.session.open}-{market.session.close} {market.tz_abbrev}",
        "",
    ]

    total_pnl = 0.0
    total_trades = 0
    for summary in summaries:
        pid = str(summary.get("persona_id", "?"))
        if summary.get("missing"):
            stale = summary.get("stale_date")
            note = f"no session file for {day}" if not stale else f"stale file ({stale})"
            lines.append(f"*{pid}*: — ({note})")
            continue
        trades = int(summary.get("total_trades") or 0)
        pnl = float(summary.get("daily_net_pnl") or summary.get("total_net_pnl") or 0.0)
        ret = summary.get("total_return_pct")
        total_pnl += pnl
        total_trades += trades
        ret_s = f" | {float(ret):+.2f}%" if ret is not None else ""
        lines.append(
            f"*{pid}*: {trades} trades | {_fmt_money(pnl, symbol)}{ret_s}"
        )
        wl = summary.get("watchlist") or []
        if wl:
            names = ", ".join(str(w.get("symbol", "?")) for w in wl[:5])
            lines.append(f"  watch: {names}")
        lockout = summary.get("lockout_reason")
        if lockout:
            lines.append(f"  lockout: {lockout}")
        skips = _skip_reasons(summary)
        if skips:
            lines.append(f"  skipped: {skips}")
        lines.extend(_trade_lines(summary, symbol))

    lines.extend(
        [
            "",
            f"*Book total*: {total_trades} trades | {_fmt_money(total_pnl, symbol)}",
        ]
    )

    text = "\n".join(lines).strip() + "\n"
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20].rstrip() + "\n…(truncated)\n"


def format_combined_report(
    day: date,
    market_ids: Optional[Iterable[str]] = None,
    root: Optional[Path] = None,
    max_chars: int = 3500,
) -> str:
    markets = list(market_ids) if market_ids is not None else [m.id for m in list_markets()]
    chunks: List[str] = []
    for market_id in markets:
        try:
            get_market(market_id)
        except ValueError:
            continue
        chunks.append(
            format_market_report(market_id, day, root=root, max_chars=max_chars)
        )
    text = "\n".join(chunks).strip() + "\n"
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20].rstrip() + "\n…(truncated)\n"


def default_session_day(market_id: str, now: Optional[datetime] = None) -> date:
    """Exchange-local calendar date for the session that just ended."""
    market = get_market(market_id)
    from zoneinfo import ZoneInfo

    now = now or datetime.now(ZoneInfo(market.timezone))
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo(market.timezone))
    else:
        now = now.astimezone(ZoneInfo(market.timezone))
    return now.date()
