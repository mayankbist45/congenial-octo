"""Helpers for parsing Indian financial figures out of HTML.

Screener.in renders numbers the way Indian financial statements do: rupee
symbols, lakh-crore grouping, percent suffixes and ``+`` expanders. These
helpers turn that text into floats without silently inventing zeros.
"""

from __future__ import annotations

import re
from typing import List, Optional

_CLEAN_PATTERN = re.compile(r"[^0-9eE.\-+]")
CRORE = 1e7


def clean_number(text: Optional[str]) -> Optional[float]:
    """Parse a rendered figure. Returns None when there is no usable number."""
    if text is None:
        return None

    raw = " ".join(str(text).split())
    if not raw or raw in {"-", "--", "NA", "N/A", ""}:
        return None

    is_negative = raw.startswith("-") or (raw.startswith("(") and raw.endswith(")"))
    stripped = _CLEAN_PATTERN.sub("", raw.replace(",", ""))
    stripped = stripped.lstrip("+-")

    if not stripped or stripped == ".":
        return None

    try:
        value = float(stripped)
    except ValueError:
        return None

    return -value if is_negative and value > 0 else value


def clean_percent(text: Optional[str]) -> Optional[float]:
    """Parse a percentage, returned in percentage points (12.5 for '12.5%')."""
    return clean_number(text)


def clean_crore(text: Optional[str]) -> Optional[float]:
    """Parse a figure quoted in crore into absolute rupees."""
    value = clean_number(text)
    return value * CRORE if value is not None else None


def normalise_label(text: Optional[str]) -> str:
    """Lowercase a row label and drop the trailing '+' expander marker."""
    if not text:
        return ""
    cleaned = " ".join(str(text).split())
    return cleaned.rstrip("+").strip().lower()


def last_non_null(values: List[Optional[float]]) -> Optional[float]:
    for value in reversed(values):
        if value is not None:
            return value
    return None


def safe_divide(
    numerator: Optional[float], denominator: Optional[float]
) -> Optional[float]:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def growth_pct(
    current: Optional[float], previous: Optional[float]
) -> Optional[float]:
    """Period-over-period growth. Undefined when the base is non-positive."""
    if current is None or previous is None or previous <= 0:
        return None
    return (current / previous - 1.0) * 100.0
