import numpy as np
import pytest

from freqon.card import render_lower_third


def status(title="Hey Jude", artist="The Beatles", source="digital"):
    return {"frequency": 97.1, "program": 1, "source": source,
            "station": {"name": "WXYT", "slogan": "Detroit's sports station"},
            "now_playing": {"title": title, "artist": artist}}


@pytest.mark.parametrize("size", [(1280, 720), (640, 360)])
def test_strip_shape_and_offset(size):
    w, h = size
    strip, y = render_lower_third(status(), size)
    assert strip.dtype == np.uint8 and strip.shape[1:] == (w, 4)
    assert abs(strip.shape[0] - 0.22 * h) <= 2
    assert y + strip.shape[0] == h


def test_transparent_above_the_bar():
    strip, _ = render_lower_third(status(), (1280, 720))
    assert strip[0, :, 3].max() == 0
    assert strip[-1, :, 3].min() > 0  # the bar itself is (mostly) opaque


def test_text_is_drawn():
    with_text, _ = render_lower_third(status(), (1280, 720))
    without, _ = render_lower_third(status(title=None, artist=None), (1280, 720))
    bright = lambda s: int((s[..., :3].max(axis=2) > 200).sum())
    assert bright(with_text) > bright(without) > 0  # station name always; song adds more


def test_long_titles_do_not_raise():
    strip, _ = render_lower_third(status(title="A" * 500, artist="B" * 500), (640, 360))
    assert strip.shape[1] == 640


def test_missing_fields():
    strip, _ = render_lower_third({"frequency": None, "source": "none", "station": {}, "now_playing": {}}, (640, 360))
    assert strip.shape[1] == 640


def _art(color=(0, 0, 255)):
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (300, 300), color).save(buf, "JPEG")
    return buf.getvalue()


def test_card_shows_album_art():
    from freqon.card import render_rgb
    plain = render_rgb(status(), (640, 360))
    with_art = render_rgb(status(), (640, 360), art=_art())
    assert plain != with_art
    img = np.frombuffer(with_art, np.uint8).reshape(360, 640, 3)
    assert (img[:, :, 2] > 200).sum() > 10000  # a big blue square


def test_alert_banner_in_card_and_lower_third():
    from freqon.card import render_rgb
    st = {**status(), "alert": {"message": "Tornado warning", "categories": ["Weather"]}}
    img = np.frombuffer(render_rgb(st, (640, 360)), np.uint8).reshape(360, 640, 3)
    red = (img[:, :, 0] > 180) & (img[:, :, 1] < 90) & (img[:, :, 2] < 90)
    assert red.sum() > 3000
    strip, _ = render_lower_third(st, (1280, 720))
    r = strip[..., 0].astype(int)
    assert ((r > 180) & (strip[..., 1] < 90) & (strip[..., 3] > 0)).sum() > 3000


def test_lower_third_with_art_thumbnail():
    plain, _ = render_lower_third(status(), (1280, 720))
    withart, _ = render_lower_third(status(), (1280, 720), art=_art())
    assert (withart[..., 2] > 200).sum() > (plain[..., 2] > 200).sum() + 2000


LIGHT_BLUE = (111, 177, 220)
DARK_GRAY = (70, 74, 80)


def _count(img_rgb, color, tol=12):
    diff = np.abs(img_rgb[..., :3].astype(int) - np.array(color)).max(axis=-1)
    return int((diff <= tol).sum())


def test_hd_badge_in_the_lower_third():
    hd, _ = render_lower_third(status(source="digital"), (1280, 720))
    fm, _ = render_lower_third(status(source="analog"), (1280, 720))
    assert _count(hd, LIGHT_BLUE) > 1500 and _count(hd, LIGHT_BLUE) > 5 * _count(hd, DARK_GRAY)
    assert _count(fm, DARK_GRAY) > 1500 and _count(fm, LIGHT_BLUE) < 100  # (anti-aliased edges aside)


def test_hd_badge_on_the_card():
    from freqon.card import render_rgb
    hd = np.frombuffer(render_rgb(status(source="digital"), (640, 360)), np.uint8).reshape(360, 640, 3)
    none = np.frombuffer(render_rgb(status(source="none"), (640, 360)), np.uint8).reshape(360, 640, 3)
    assert _count(hd, LIGHT_BLUE) > 600
    assert _count(none, DARK_GRAY) > 600 and _count(none, LIGHT_BLUE) < 100
