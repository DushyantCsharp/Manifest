"""Report charts, drawn with Pillow: reliability diagrams and the precision/coverage curve.

Deliberately plain (no plotting library): axes, gridlines, bars or a line, labels. Every number
a chart shows is also in ``report.json``.
"""

from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from manifest_evals.metrics import Bin, Point

WHITE, INK, GRID, MUTED = (255, 255, 255), (33, 37, 41), (222, 226, 230), (134, 142, 150)
BAR, ACCENT, TARGET = (66, 133, 244), (219, 68, 55), (15, 157, 88)
PANEL = 520  # plot area, px
MARGIN = 70
ZERO, ONE = Decimal(0), Decimal(1)
Font = ImageFont.ImageFont | ImageFont.FreeTypeFont


def _font(size: int) -> Font:
    return ImageFont.load_default(size=size)


class _Axes:
    """A plot area with gridlines and tick labels, mapping data (x 0..1, y low..1) to pixels."""

    def __init__(self, draw: ImageDraw.ImageDraw, left: int, top: int, y_low: Decimal) -> None:
        self.draw, self.left, self.top, self.y_low = draw, left, top, y_low

    def x(self, value: Decimal) -> float:
        return self.left + float(value) * PANEL

    def y(self, value: Decimal) -> float:
        return self.top + PANEL - float((value - self.y_low) / (ONE - self.y_low)) * PANEL

    def frame(self, title: str, x_label: str, y_label: str) -> None:
        draw, small = self.draw, _font(12)
        bottom, right = self.top + PANEL, self.left + PANEL
        for step in range(11):
            fraction = Decimal(step) / 10
            x = self.x(fraction)
            draw.line([(x, self.top), (x, bottom)], fill=GRID)
            draw.text((x, bottom + 6), f"{fraction:.1f}", fill=MUTED, font=small, anchor="ma")
            level = self.y_low + (ONE - self.y_low) * fraction
            y = self.y(level)
            draw.line([(self.left, y), (right, y)], fill=GRID)
            places = 3 if ONE - self.y_low < Decimal("0.2") else 2  # a narrow range needs more
            draw.text(
                (self.left - 6, y), f"{level:.{places}f}", fill=MUTED, font=small, anchor="rm"
            )
        draw.rectangle([self.left, self.top, right, bottom], outline=INK)
        middle = self.left + PANEL / 2
        draw.text((middle, self.top - 28), title, fill=INK, font=_font(16), anchor="mm")
        draw.text((middle, bottom + 34), x_label, fill=INK, font=_font(13), anchor="mm")
        draw.text((self.left - 58, self.top - 12), y_label, fill=INK, font=_font(13), anchor="lm")


Panel = tuple[str, Sequence[Bin], Decimal | None]


def reliability(path: Path, title: str, panels: Sequence[Panel]) -> None:
    """One reliability diagram per (label, bins, ECE): accuracy per confidence bin against the
    diagonal a perfectly calibrated score would follow; bar labels are sample counts."""
    width = MARGIN + len(panels) * (PANEL + MARGIN)
    image = Image.new("RGB", (width, PANEL + 2 * MARGIN + 40), WHITE)
    draw = ImageDraw.Draw(image)
    draw.text((width / 2, 16), title, fill=INK, font=_font(18), anchor="mm")
    for index, (label, bins, error) in enumerate(panels):
        axes = _Axes(draw, MARGIN + index * (PANEL + MARGIN), MARGIN + 20, ZERO)
        ece = "n/a" if error is None else f"{error:.4f}"
        axes.frame(f"{label} (ECE {ece})", "confidence", "accuracy")
        diagonal = [(axes.x(ZERO), axes.y(ZERO)), (axes.x(ONE), axes.y(ONE))]
        draw.line(diagonal, fill=MUTED, width=2)
        for b in bins:
            if not b.count or b.accuracy is None or b.confidence is None:
                continue
            x0, x1 = axes.x(b.low) + 2, axes.x(b.high) - 2
            draw.rectangle([x0, axes.y(b.accuracy), x1, axes.y(ZERO)], fill=BAR)
            mid, dot = (x0 + x1) / 2, axes.y(b.confidence)
            draw.ellipse([mid - 4, dot - 4, mid + 4, dot + 4], fill=ACCENT)
            top = axes.y(b.accuracy) - 4
            draw.text((mid, top), str(b.count), fill=INK, font=_font(11), anchor="mb")
    legend = "bars: accuracy per bin (label: samples)   dots: mean confidence   line: perfect"
    draw.text((width / 2, image.height - 14), legend, fill=MUTED, font=_font(12), anchor="mm")
    image.save(path)


def precision_coverage(
    path: Path,
    points: Sequence[Point],
    *,
    threshold: Decimal | None,
    target: Decimal,
    title: str,
) -> None:
    """Precision against coverage over every threshold, with the target precision and the
    chosen auto-approve threshold marked."""
    low = min([p.precision for p in points] + [target])
    y_low = max(ZERO, (low - Decimal("0.02")).quantize(Decimal("0.01")))
    size = PANEL + 2 * MARGIN + 40
    image = Image.new("RGB", (size, size), WHITE)
    draw = ImageDraw.Draw(image)
    axes = _Axes(draw, MARGIN + 20, MARGIN + 20, y_low)
    axes.frame(title, "coverage (share of values auto-approved)", "precision")
    goal = axes.y(target)
    draw.line([(axes.x(ZERO), goal), (axes.x(ONE), goal)], fill=TARGET, width=2)
    note = f"target {target}"
    draw.text((axes.x(Decimal("0.02")), goal - 4), note, fill=TARGET, font=_font(12), anchor="lb")
    ordered = sorted(points, key=lambda p: p.coverage)
    line = [(axes.x(p.coverage), axes.y(p.precision)) for p in ordered]
    if len(line) > 1:
        draw.line(line, fill=BAR, width=3)
    for x, y in line:
        draw.ellipse([x - 2, y - 2, x + 2, y + 2], fill=BAR)
    chosen = next((p for p in points if p.threshold == threshold), None)
    if chosen is not None:
        x, y = axes.x(chosen.coverage), axes.y(chosen.precision)
        draw.ellipse([x - 6, y - 6, x + 6, y + 6], outline=ACCENT, width=3)
        label = (
            f"threshold {chosen.threshold}: coverage {chosen.coverage:.3f},"
            f" precision {chosen.precision:.4f}"
        )
        draw.text((x - 10, y - 10), label, fill=ACCENT, font=_font(12), anchor="rb")
    image.save(path)
