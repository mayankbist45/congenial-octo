"""Build the quarterly brief the earnings analyst reads."""

from __future__ import annotations

from datetime import date
from typing import Optional

import pandas as pd


def _yoy_pct(current: Optional[float], prior: Optional[float]) -> Optional[float]:
    if current is None or prior is None or prior == 0:
        return None
    return (current / prior - 1.0) * 100.0


def _fmt(value: Optional[float], suffix: str = "", places: int = 1) -> str:
    if value is None or (isinstance(value, float) and not pd.notna(value)):
        return "n/a"
    return f"{value:,.{places}f}{suffix}"


def build_brief(
    symbol: str,
    company_name: str,
    sector: str,
    quarters: pd.DataFrame,
    period_end: date,
) -> str:
    """Quarterly history up to and including ``period_end``."""
    frame = quarters.copy()
    frame["period_end"] = pd.to_datetime(frame["period_end"]).dt.date
    frame = frame[frame["period_end"] <= period_end].sort_values("period_end")
    if frame.empty:
        return f"Company: {symbol}\nNo quarterly data available."

    latest = frame.iloc[-1]
    lines = [
        f"Company: {symbol} ({company_name})",
        f"Sector: {sector}",
        f"Fiscal period under review: quarter ended "
        f"{latest['period_end'].strftime('%b %Y')}",
        "(Flows in Rs crore; EPS in Rs; YoY = vs same quarter prior year)",
        "",
        "Quarter | Sales (YoY) | Op profit (YoY) | OPM % (ΔYoY bps) | "
        "Net profit (YoY) | EPS (YoY)",
    ]

    indexed = frame.set_index("period_end")
    for row in frame.itertuples():
        period = row.period_end
        prior_period = period.replace(year=period.year - 1)
        prior = indexed.loc[prior_period] if prior_period in indexed.index else None

        sales_yoy = _yoy_pct(row.sales_cr, prior["sales_cr"] if prior is not None else None)
        op_yoy = _yoy_pct(
            row.operating_profit_cr,
            prior["operating_profit_cr"] if prior is not None else None,
        )
        np_yoy = _yoy_pct(row.net_profit_cr, prior["net_profit_cr"] if prior is not None else None)
        eps_yoy = _yoy_pct(row.eps_rs, prior["eps_rs"] if prior is not None else None)
        opm_delta = None
        if prior is not None and row.opm_pct is not None and prior["opm_pct"] is not None:
            opm_delta = (row.opm_pct - prior["opm_pct"]) * 100.0

        lines.append(
            f"{period.strftime('%b %Y')} | "
            f"{_fmt(row.sales_cr)} ({_fmt(sales_yoy, '%')}) | "
            f"{_fmt(row.operating_profit_cr)} ({_fmt(op_yoy, '%')}) | "
            f"{_fmt(row.opm_pct, '%')} ({_fmt(opm_delta, ' bps', 0)}) | "
            f"{_fmt(row.net_profit_cr)} ({_fmt(np_yoy, '%')}) | "
            f"{_fmt(row.eps_rs)} ({_fmt(eps_yoy, '%')})"
        )
    return "\n".join(lines)


def latest_period(quarters: pd.DataFrame) -> Optional[date]:
    if quarters.empty:
        return None
    frame = quarters.copy()
    frame["period_end"] = pd.to_datetime(frame["period_end"]).dt.date
    return frame["period_end"].max()
