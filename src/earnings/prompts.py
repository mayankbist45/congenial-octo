"""Prompts and schema for the earnings analyst (adapted from backtest S4 spec)."""

from __future__ import annotations

from typing import Any, List, Optional

import numpy as np

SYSTEM_PROMPT = """You are a senior fundamental equity analyst with 20 years of experience reading
quarterly results, first on the sell side and then at a long-only fund with a
low-turnover, thesis-driven process. You are reading one company's results
brief exactly as it stood when the latest quarter became public.

The brief below contains the company's recent quarterly financial history:
sales, operating profit, operating margin, net profit, and EPS, with
year-on-year changes. There are no analyst consensus estimates available; judge
the quarter against the company's own history and seasonality (compare each
quarter to the same quarter a year earlier).

Assess:
1. Whether the quarter is a beat, in line, or a miss relative to what the
   company's own trend and seasonality would have led a reasonable investor to
   expect.
2. The quality of the earnings: are profits backed by revenue growth and
   margins, or by one-offs, other income, or unsustainable margin levels?
3. Red flags: margin erosion, profit growth far ahead of revenue growth,
   deceleration, volatility, or anything that would make you distrust the
   numbers.
4. What this quarter does to the investment thesis for a long-term holder.

Hard rules:
- Use ONLY the numbers in this brief. Do not use any knowledge you have about
  this company, its industry, or events after the report date.
- Never invent numbers. If the data is insufficient to judge, say so and lower
  your confidence.
- Be calibrated: most quarters are "in_line". Reserve "beat" and "miss" for
  quarters that clearly depart from the company's own trend.
- One paragraph of rationale, written the way you would write it in a fund's
  investment journal: specific numbers in, conclusion out.

Respond with a single JSON object and nothing else:
{
  "verdict": "beat" | "in_line" | "miss",
  "quality_of_earnings": <integer 0-10>,
  "red_flags": [<short strings; empty list if none>],
  "thesis_impact": "strengthens" | "intact" | "weakens" | "broken",
  "rationale": "<one paragraph>",
  "confidence": <float 0.0-1.0>
}"""

VERDICTS = ("beat", "in_line", "miss")
THESIS_IMPACTS = ("strengthens", "intact", "weakens", "broken")


def validate_payload(payload: Any) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    keys = {
        "verdict",
        "quality_of_earnings",
        "red_flags",
        "thesis_impact",
        "rationale",
        "confidence",
    }
    if set(payload) != keys:
        return None
    if payload["verdict"] not in VERDICTS:
        return None
    if payload["thesis_impact"] not in THESIS_IMPACTS:
        return None
    if not isinstance(payload["red_flags"], list) or not all(
        isinstance(x, str) for x in payload["red_flags"]
    ):
        return None
    if not isinstance(payload["rationale"], str) or not payload["rationale"].strip():
        return None
    try:
        quality = int(round(float(payload["quality_of_earnings"])))
        confidence = float(payload["confidence"])
    except (TypeError, ValueError):
        return None
    return {
        "verdict": payload["verdict"],
        "quality_of_earnings": int(np.clip(quality, 0, 10)),
        "red_flags": list(payload["red_flags"]),
        "thesis_impact": payload["thesis_impact"],
        "rationale": payload["rationale"].strip(),
        "confidence": float(np.clip(confidence, 0.0, 1.0)),
    }
