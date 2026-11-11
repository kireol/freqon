"""Renders simple images: the 'now playing' video card for Plex, and channel logos."""

from __future__ import annotations

import io
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageFont

BG = (18, 22, 30)
FG = (236, 239, 244)
DIM = (140, 150, 165)
ACCENT = {"digital": (64, 196, 140), "analog": (232, 170, 70), "none": (110, 116, 128)}
ALERT = (200, 30, 30)
HD_ON = (111, 177, 220)  # light blue: HD audio is playing
HD_OFF = (70, 74, 80)  # dark grey: FM or no signal


def _hd_badge(d: ImageDraw.ImageDraw, right: float, top: float, height: int, digital: bool, alpha: bool = False) -> int:
    """Draw an "HD" badge whose right edge is at `right`; returns its width."""
    font = _font(max(10, int(height * 0.72)))
    tw = d.textlength("HD", font=font)
    width = int(tw + height * 0.7)
    fill, ink = (HD_ON, BG) if digital else (HD_OFF, (150, 156, 166))
    if alpha:
        fill, ink = (*fill, 255), (*ink, 255)
    d.rounded_rectangle((right - width, top, right, top + height), radius=max(3, height // 5), fill=fill)
    d.text((right - width + (width - tw) / 2, top + height * 0.1), "HD", font=font, fill=ink)
    return width


def _art_image(art: bytes | None, side: int):
    """Album art bytes (already cleaned JPEG) -> square RGB image, or None."""
    if not art:
        return None
    try:
        im = Image.open(io.BytesIO(art)).convert("RGB")
    except OSError:
        return None
    w, h = im.size
    s = min(w, h)
    return im.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s)).resize((side, side))


def _alert_text(status: dict) -> str | None:
    a = status.get("alert") or {}
    msg = a.get("message")
    if not msg:
        return None
    cats = ", ".join(a.get("categories") or [])
    return f"ALERT{' (' + cats + ')' if cats else ''}: {msg}"


@lru_cache(maxsize=16)
def _font(size: int) -> ImageFont.ImageFont:
    for name in ("DejaVuSans-Bold.ttf", "Arial Bold.ttf", "Helvetica.ttc"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _fit(draw: ImageDraw.ImageDraw, text: str, size: int, max_width: int) -> tuple[str, ImageFont.ImageFont]:
    font = _font(size)
    while size > 12 and draw.textlength(text, font=font) > max_width:
        size -= 2
        font = _font(size)
    if draw.textlength(text, font=font) > max_width:
        while text and draw.textlength(text + "…", font=font) > max_width:
            text = text[:-1]
        text += "…"
    return text, font


def render_card(status: dict, size=(640, 360), art: bytes | None = None) -> Image.Image:
    w, h = size
    img = Image.new("RGB", size, BG)
    d = ImageDraw.Draw(img)
    pad = w // 16
    cover = _art_image(art, int(h * 0.5))
    text_x = pad
    if cover is not None:
        img.paste(cover, (pad, int(h * 0.26)))
        text_x = pad + cover.size[0] + pad // 2
    freq = status.get("frequency")
    program = status.get("program", 0)
    source = status.get("source", "none")
    name = (status.get("station") or {}).get("name") or (f"{freq:.1f} FM" if freq else "Radio")
    np_ = status.get("now_playing") or {}

    label = {"digital": f"HD{program + 1}", "analog": "FM", "none": "NO SIGNAL"}[source]
    tag, tag_font = _fit(d, label, h // 14, w)
    tw = d.textlength(tag, font=tag_font)
    d.rounded_rectangle((pad, pad, pad + tw + 24, pad + h // 14 + 16), radius=8, fill=ACCENT[source])
    d.text((pad + 12, pad + 6), tag, font=tag_font, fill=BG)
    badge_w = _hd_badge(d, w - pad, pad, h // 14 + 16, source == "digital")
    if freq:
        ftxt, ffont = _fit(d, f"{freq:.1f} MHz", h // 14, w // 2)
        d.text((w - pad - badge_w - pad // 2 - d.textlength(ftxt, font=ffont), pad + 6), ftxt, font=ffont, fill=DIM)

    title, tfont = _fit(d, name, h // 6, w - pad - text_x)
    d.text((text_x, h * 0.32), title, font=tfont, fill=FG)
    lines = [x for x in (np_.get("title"), np_.get("artist")) if x]
    if not lines:
        slogan = (status.get("station") or {}).get("slogan")
        lines = [slogan] if slogan else []
    y = h * 0.62
    for i, line in enumerate(lines[:2]):
        txt, font = _fit(d, line, h // (11 if i == 0 else 14), w - pad - text_x)
        d.text((text_x, y), txt, font=font, fill=FG if i == 0 else DIM)
        y += h // 9
    alert = _alert_text(status)
    if alert:
        bh = h // 7
        d.rectangle((0, h - bh, w, h), fill=ALERT)
        txt, font = _fit(d, alert, bh // 2, w - 2 * pad)
        d.text((pad, h - bh + bh // 4), txt, font=font, fill=(255, 255, 255))
    return img


LOWER_THIRD = 0.22  # share of the frame height
BAR_ALPHA = 200


def render_lower_third(status: dict, size=(1280, 720), art: bytes | None = None) -> tuple[np.ndarray, int]:
    """The now-playing bar to lay over camera video: an RGBA strip (height x width x 4) and
    the y where it goes. Its top quarter fades in from fully transparent; text stays out of
    the bottom ~5% that some TVs crop."""
    w, h = size
    sh = round(h * LOWER_THIRD)
    img = Image.new("RGBA", (w, sh), (*BG, 0))
    d = ImageDraw.Draw(img)
    fade = sh // 4
    for i in range(fade):
        d.line((0, i, w, i), fill=(*BG, BAR_ALPHA * i // fade))
    d.rectangle((0, fade, w, sh), fill=(*BG, BAR_ALPHA))

    freq = status.get("frequency")
    program = status.get("program", 0) or 0
    source = status.get("source") or "none"
    station = status.get("station") or {}
    np_ = status.get("now_playing") or {}
    pad = right = w // 32
    body = sh - fade
    d.rectangle((0, fade, max(4, w // 160), sh), fill=(*ACCENT.get(source, ACCENT["none"]), 255))
    cover = _art_image(art, body - 2 * (body // 10))
    if cover is not None:
        img.paste(cover.convert("RGBA"), (pad, fade + body // 10))
        pad = pad + cover.size[0] + w // 64
    alert = _alert_text(status)
    if alert:  # the fade above the bar becomes a solid red alert band
        d.rectangle((0, 0, w, fade), fill=(*ALERT, 255))
        atxt, afont = _fit(d, alert, fade * 2 // 3, w - w // 16)
        d.text((w // 32, fade // 6), atxt, font=afont, fill=(255, 255, 255, 255))

    tag = {"digital": f"HD{program + 1}", "analog": "FM", "none": "NO SIGNAL"}.get(source, "")
    if freq:
        tag = f"{freq:.1f}  {tag}"
    badge_h = body * 3 // 10
    badge_w = _hd_badge(d, w - right, fade + body * 0.08, badge_h, source == "digital", alpha=True)
    tag_txt, tag_font = _fit(d, tag, body // 4, w // 3)
    tag_w = d.textlength(tag_txt, font=tag_font) + badge_w + right // 2
    d.text((w - right - tag_w, fade + body * 0.08), tag_txt, font=tag_font, fill=(*DIM, 255))

    name = station.get("name") or (f"{freq:.1f} FM" if freq else "Radio")
    name_txt, name_font = _fit(d, name, body * 9 // 25, w - 3 * pad - int(tag_w))
    d.text((pad, fade + body * 0.02), name_txt, font=name_font, fill=(*FG, 255))

    line = " — ".join(x for x in (np_.get("title"), np_.get("artist")) if x) or station.get("slogan") or ""
    if line:
        txt, font = _fit(d, line, body * 13 // 50, w - 2 * pad)
        d.text((pad, fade + body * 0.40), txt, font=font, fill=(*DIM, 255) if not np_.get("title") else (*FG, 255))
    return np.asarray(img, dtype=np.uint8), h - sh


def render_rgb(status: dict, size=(640, 360), art: bytes | None = None) -> bytes:
    return render_card(status, size, art).tobytes()


@lru_cache(maxsize=256)
def render_logo_png(text: str, sub: str = "", size: int = 256) -> bytes:
    img = Image.new("RGB", (size, size), BG)
    d = ImageDraw.Draw(img)
    t, f = _fit(d, text, size // 4, size - 24)
    tw = d.textlength(t, font=f)
    d.text(((size - tw) / 2, size * 0.32), t, font=f, fill=FG)
    if sub:
        s, sf = _fit(d, sub, size // 8, size - 24)
        sw = d.textlength(s, font=sf)
        d.text(((size - sw) / 2, size * 0.62), s, font=sf, fill=ACCENT["digital"])
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
