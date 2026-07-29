"""Server-rendered SVG charts.

Drawing the charts on the server keeps the dashboard a single self-contained
page with no JavaScript charting library, no CDN dependency and no build step.
For a NAV series of a few hundred points this is entirely adequate.
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

WIDTH = 900
HEIGHT = 320
PADDING_LEFT = 56
PADDING_RIGHT = 16
PADDING_TOP = 16
PADDING_BOTTOM = 34

FUND_COLOUR = "#2563eb"
BENCHMARK_COLOUR = "#94a3b8"


def _nice_bounds(values: Sequence[float]) -> Tuple[float, float]:
    """Pad the data range slightly so the line never touches the frame."""
    low = min(values)
    high = max(values)
    if high == low:
        return low - 1.0, high + 1.0
    padding = (high - low) * 0.08
    return low - padding, high + padding


def _points(
    series: Sequence[float], low: float, high: float
) -> List[Tuple[float, float]]:
    plot_width = WIDTH - PADDING_LEFT - PADDING_RIGHT
    plot_height = HEIGHT - PADDING_TOP - PADDING_BOTTOM
    span = high - low or 1.0
    count = len(series)

    points: List[Tuple[float, float]] = []
    for index, value in enumerate(series):
        x = PADDING_LEFT + (plot_width * index / max(count - 1, 1))
        y = PADDING_TOP + plot_height * (1.0 - (value - low) / span)
        points.append((round(x, 2), round(y, 2)))
    return points


def _path(points: Sequence[Tuple[float, float]]) -> str:
    return " ".join(
        f"{'M' if index == 0 else 'L'}{x},{y}" for index, (x, y) in enumerate(points)
    )


def line_chart(
    dates: Sequence[str],
    fund: Sequence[float],
    benchmark: Optional[Sequence[float]] = None,
    benchmark_label: str = "Benchmark",
) -> str:
    """Render a rebased performance chart as an inline SVG string."""
    if len(fund) < 2:
        return (
            '<p class="empty">Not enough history to chart yet. The line appears '
            "once the fund has two or more daily observations.</p>"
        )

    benchmark = list(benchmark or [])
    combined = list(fund) + (benchmark if len(benchmark) == len(fund) else [])
    low, high = _nice_bounds(combined)

    fund_points = _points(fund, low, high)
    parts: List[str] = [
        f'<svg viewBox="0 0 {WIDTH} {HEIGHT}" class="chart" '
        f'role="img" aria-label="Fund performance versus {benchmark_label}">'
    ]

    # Horizontal gridlines with value labels.
    for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
        value = low + (high - low) * fraction
        y = PADDING_TOP + (HEIGHT - PADDING_TOP - PADDING_BOTTOM) * (1.0 - fraction)
        parts.append(
            f'<line x1="{PADDING_LEFT}" y1="{y:.1f}" x2="{WIDTH - PADDING_RIGHT}" '
            f'y2="{y:.1f}" class="grid" />'
        )
        parts.append(
            f'<text x="{PADDING_LEFT - 8}" y="{y + 4:.1f}" class="axis" '
            f'text-anchor="end">{value:.1f}</text>'
        )

    # The 100 baseline: the level at which the fund has neither made nor lost.
    if low < 100.0 < high:
        baseline_y = PADDING_TOP + (HEIGHT - PADDING_TOP - PADDING_BOTTOM) * (
            1.0 - (100.0 - low) / (high - low)
        )
        parts.append(
            f'<line x1="{PADDING_LEFT}" y1="{baseline_y:.1f}" '
            f'x2="{WIDTH - PADDING_RIGHT}" y2="{baseline_y:.1f}" class="baseline" />'
        )

    if len(benchmark) == len(fund):
        parts.append(
            f'<path d="{_path(_points(benchmark, low, high))}" fill="none" '
            f'stroke="{BENCHMARK_COLOUR}" stroke-width="1.6" '
            f'stroke-dasharray="4 3" />'
        )

    parts.append(
        f'<path d="{_path(fund_points)}" fill="none" stroke="{FUND_COLOUR}" '
        f'stroke-width="2.2" stroke-linejoin="round" />'
    )

    # Date labels at the ends and midpoint only; a dense axis is unreadable.
    if dates:
        label_positions = {0: dates[0], len(dates) - 1: dates[-1]}
        if len(dates) > 4:
            label_positions[len(dates) // 2] = dates[len(dates) // 2]
        for index, label in sorted(label_positions.items()):
            x = fund_points[min(index, len(fund_points) - 1)][0]
            anchor = (
                "start" if index == 0 else "end" if index == len(dates) - 1 else "middle"
            )
            parts.append(
                f'<text x="{x:.1f}" y="{HEIGHT - 10}" class="axis" '
                f'text-anchor="{anchor}">{label}</text>'
            )

    parts.append("</svg>")
    return "".join(parts)


def bar_chart(labels: Sequence[str], values: Sequence[float]) -> str:
    """Horizontal bars, used for sector weights and attribution."""
    if not labels:
        return '<p class="empty">Nothing to show yet.</p>'

    scale = max((abs(v) for v in values), default=1.0) or 1.0
    rows: List[str] = ['<div class="bars">']
    for label, value in zip(labels, values):
        width = abs(value) / scale * 100.0
        direction = "neg" if value < 0 else "pos"
        rows.append(
            f'<div class="bar-row"><span class="bar-label">{label}</span>'
            f'<span class="bar-track"><span class="bar-fill {direction}" '
            f'style="width:{width:.1f}%"></span></span>'
            f'<span class="bar-value">{value:+.2f}</span></div>'
        )
    rows.append("</div>")
    return "".join(rows)
