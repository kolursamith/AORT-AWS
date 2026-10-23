"""Dependency-free SVG grouped-bar charts for the evidence package (render on GitHub, light and dark).

Colours: validated reference palette, categorical slots 1-3 (blue, orange, aqua) in fixed order;
text uses text colours, never the series colour; every bar carries its value label, and the same
numbers are in the CSV next to the figure (table view).
"""

from __future__ import annotations

import math
from html import escape

LIGHT = {
    "surface": "#fcfcfb",
    "text": "#0b0b0b",
    "muted": "#52514e",
    "grid": "#e4e3df",
    "series": ["#2a78d6", "#eb6834", "#1baf7a"],
}
DARK = {
    "surface": "#1a1a19",
    "text": "#f0efec",
    "muted": "#c3c2b7",
    "grid": "#3a3936",
    "series": ["#3987e5", "#d95926", "#199e70"],
}


def _fmt(v: float | None, unit: str) -> str:
    if v is None:
        return "n/a"
    if unit == "share":
        return f"{v:.0%}"
    if abs(v) >= 100:
        return f"{v:,.0f}"
    if 0 < abs(v) < 1:
        return f"{v:.2f}"
    return f"{v:,.1f}".rstrip("0").rstrip(".")


def _nice_step(raw: float) -> float:
    """Round a tick step up to 1, 2, 2.5 or 5 x 10^k."""
    if raw <= 0:
        return 1.0
    mag = 10 ** math.floor(math.log10(raw))
    return next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)


def grouped_bars(
    title: str,
    categories: list[str],
    series: dict[str, list[float | None]],
    *,
    unit: str = "",
    subtitle: str = "",
    width: int = 860,
    height: int = 380,
) -> str:
    names = list(series)[:3]  # at most three series: the validated all-pairs slots
    vals = [v for n in names for v in series[n] if v is not None]
    if unit == "share":
        step, ticks = 0.25, 4  # 0 % .. 100 %
    else:
        step = _nice_step(max(vals + [1e-9]) / 4)
        ticks = max(1, math.ceil(max(vals + [1e-9]) / step))
    vmax = step * ticks * 1.08  # headroom for the value labels above the tallest bar
    left, right, top = 56, 16, 64 + (16 if subtitle else 0)
    group_w = (width - left - right) / max(1, len(categories))
    rotate = max((len(c) for c in categories), default=0) * 6.2 > group_w - 6
    bottom = 40 + (max(len(c) for c in categories) * 4.6 if rotate else 30)
    height = max(height, int(top + 220 + bottom))
    pw, ph = width - left - right, height - top - bottom
    bar_w = min(28.0, (group_w * 0.7) / len(names))
    gap = 2.0
    css_vars = (
        "".join(f"--s{i + 1}:{c};" for i, c in enumerate(LIGHT["series"]))
        + f"--bg:{LIGHT['surface']};--tx:{LIGHT['text']};--mu:{LIGHT['muted']};--gr:{LIGHT['grid']};"
    )
    dark_vars = (
        "".join(f"--s{i + 1}:{c};" for i, c in enumerate(DARK["series"]))
        + f"--bg:{DARK['surface']};--tx:{DARK['text']};--mu:{DARK['muted']};--gr:{DARK['grid']};"
    )
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" '
        f'height="{height}" role="img" aria-label="{escape(title)}" font-family="system-ui, sans-serif">',
        f"<style>svg{{{css_vars}}}@media (prefers-color-scheme: dark){{svg{{{dark_vars}}}}}"
        ".t{fill:var(--tx)}.m{fill:var(--mu)}</style>",
        f'<rect width="{width}" height="{height}" fill="var(--bg)"/>',
        f'<text x="{left}" y="26" class="t" font-size="16" font-weight="600">{escape(title)}</text>',
    ]
    if subtitle:
        out.append(f'<text x="{left}" y="46" class="m" font-size="12">{escape(subtitle)}</text>')
    # legend (always, for >= 2 series), one row above the plot
    lx = left
    ly = top - 14
    for i, n in enumerate(names):
        out.append(
            f'<rect x="{lx}" y="{ly - 9}" width="10" height="10" rx="2" fill="var(--s{i + 1})"/>'
        )
        out.append(f'<text x="{lx + 14}" y="{ly}" class="t" font-size="12">{escape(n)}</text>')
        lx += 22 + 7 * len(n)
    # recessive grid + y labels
    for k in range(ticks + 1):
        v = step * k
        y = top + ph - ph * v / vmax
        out.append(
            f'<line x1="{left}" x2="{left + pw}" y1="{y:.1f}" y2="{y:.1f}" stroke="var(--gr)" stroke-width="1"/>'
        )
        out.append(
            f'<text x="{left - 6}" y="{y + 4:.1f}" class="m" font-size="11" text-anchor="end">{_fmt(v, unit)}</text>'
        )
    for gi, cat in enumerate(categories):
        gx = left + gi * group_w + (group_w - (bar_w + gap) * len(names) + gap) / 2
        for si, n in enumerate(names):
            v = series[n][gi]
            x = gx + si * (bar_w + gap)
            if v is None:
                out.append(
                    f'<text x="{x + bar_w / 2:.1f}" y="{top + ph - 4}" class="m" font-size="10" text-anchor="middle">n/a</text>'
                )
                continue
            h = max(0.0, ph * v / vmax)
            y = top + ph - h
            r = min(4.0, bar_w / 2, h)
            # 4 px rounded data end, square at the baseline
            out.append(
                f'<path d="M{x:.1f},{top + ph:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} '
                f'H{x + bar_w - r:.1f} Q{x + bar_w:.1f},{y:.1f} {x + bar_w:.1f},{y + r:.1f} V{top + ph:.1f} Z" '
                f'fill="var(--s{si + 1})"><title>{escape(n)} | {escape(cat)}: {_fmt(v, unit)}</title></path>'
            )
            out.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{y - 4:.1f}" class="t" font-size="10" text-anchor="middle">{_fmt(v, unit)}</text>'
            )
        cx = left + gi * group_w + group_w / 2
        if rotate:  # long labels: rotated, never truncated or overlapping
            out.append(
                f'<text x="{cx:.1f}" y="{top + ph + 14}" class="m" font-size="11" text-anchor="end" '
                f'transform="rotate(-35 {cx:.1f} {top + ph + 14})">{escape(cat)}</text>'
            )
        else:
            out.append(
                f'<text x="{cx:.1f}" y="{top + ph + 16}" class="m" font-size="11" '
                f'text-anchor="middle">{escape(cat)}</text>'
            )
    out.append(
        f'<line x1="{left}" x2="{left + pw}" y1="{top + ph}" y2="{top + ph}" stroke="var(--mu)" stroke-width="1"/>'
    )
    if unit and unit != "share":
        out.append(
            f'<text x="14" y="{top + ph / 2}" class="m" font-size="11" transform="rotate(-90 14 {top + ph / 2})" text-anchor="middle">{escape(unit)}</text>'
        )
    out.append("</svg>")
    return "\n".join(out)
