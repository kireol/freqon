"""Draws the Freqon brand assets: the dial mark (SVG), the logo (mark + wordmark, PNG for the
README in light and dark), and the favicon. Run from the repo root: python scripts/make_brand.py
(needs Pillow; uses the Barlow font in docs/brand/fonts, SIL OFL).

The mark is a tuning-dial window: tick marks, a blue station mark for HD, and the red needle,
the same elements as the web player's dial.
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
FONT = ROOT / "docs" / "brand" / "fonts" / "BarlowCondensed-SemiBold.ttf"

LIGHT = {"ink": "#1d2a2e", "panel": "#eef2ef", "needle": "#c42f27", "hd": "#2a6a93", "fm": "#a86d12"}
DARK = {"ink": "#dce5e1", "panel": "#1c2528", "needle": "#ef5a4f", "hd": "#6fb1dc", "fm": "#e3a54a"}


def mark_svg(c: dict, size: int = 64) -> str:
    """The dial mark on a 64-unit grid."""
    ticks = []
    for i, x in enumerate(range(12, 56, 6)):
        h = 12 if i % 2 == 0 else 7
        ticks.append(f'<rect x="{x - 1}" y="{44 - h}" width="2" height="{h}" rx="1" fill="{c["ink"]}"/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 64 64">'
            f'<rect x="4" y="14" width="56" height="36" rx="9" fill="{c["panel"]}" stroke="{c["ink"]}" stroke-width="3.5"/>'
            + "".join(ticks) +
            f'<rect x="21" y="20" width="4" height="10" rx="2" fill="{c["hd"]}"/>'
            f'<rect x="31" y="20" width="4" height="10" rx="2" fill="{c["fm"]}"/>'
            f'<rect x="39" y="7" width="5" height="50" rx="2.5" fill="{c["needle"]}"/>'
            '</svg>')


def draw_mark(d: ImageDraw.ImageDraw, x: float, y: float, s: float, c: dict) -> None:
    """The same mark drawn with Pillow at (x, y), s pixels per grid unit."""
    def r(x0, y0, w, h, rad, fill=None, outline=None, width=0):
        d.rounded_rectangle((x + x0 * s, y + y0 * s, x + (x0 + w) * s, y + (y0 + h) * s), radius=rad * s,
                            fill=fill, outline=outline, width=round(width * s))
    r(4, 14, 56, 36, 9, fill=c["panel"], outline=c["ink"], width=3.5)
    for i, tx in enumerate(range(12, 56, 6)):
        h = 12 if i % 2 == 0 else 7
        r(tx - 1, 44 - h, 2, h, 1, fill=c["ink"])
    r(21, 20, 4, 10, 2, fill=c["hd"])
    r(31, 20, 4, 10, 2, fill=c["fm"])
    r(39, 7, 5, 50, 2.5, fill=c["needle"])


def logo_png(c: dict, height: int = 160) -> Image.Image:
    font = ImageFont.truetype(str(FONT), int(height * 0.78))
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    left, top, right, bottom = probe.textbbox((0, 0), "Freqon", font=font)
    s = height / 64
    gap = height * 0.18
    img = Image.new("RGBA", (int(height + gap + (right - left) + 8), height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    draw_mark(d, 0, 0, s, c)
    d.text((height + gap - left, (height - (bottom - top)) / 2 - top), "Freqon", font=font, fill=c["ink"])
    return img


def main() -> None:
    brand = ROOT / "docs" / "brand"
    brand.mkdir(parents=True, exist_ok=True)
    (brand / "freqon-mark.svg").write_text(mark_svg(LIGHT, 256) + "\n")
    (brand / "freqon-mark-dark.svg").write_text(mark_svg(DARK, 256) + "\n")
    logo_png(LIGHT).save(brand / "freqon-logo-light.png")
    logo_png(DARK).save(brand / "freqon-logo-dark.png")
    (ROOT / "src" / "freqon" / "web" / "icon.svg").write_text(mark_svg(LIGHT, 32) + "\n")
    print("wrote docs/brand/* and src/freqon/web/icon.svg")


if __name__ == "__main__":
    main()
