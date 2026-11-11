import io
from datetime import UTC, datetime

from PIL import Image

from freqon.extras import Extras, clean_image


def img(color=(200, 30, 30), size=(300, 300), fmt="JPEG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, fmt)
    return buf.getvalue()


def pixel(jpeg: bytes):
    return Image.open(io.BytesIO(jpeg)).convert("RGB").getpixel((5, 5))


# -- images -----------------------------------------------------------------------------------

def test_art_follows_xhdr():
    x = Extras()
    x.lot(0, "art", 11, img((255, 0, 0)))
    x.lot(0, "art", 12, img((0, 0, 255)))
    x.xhdr(0, 0, 11)
    assert pixel(x.art(0))[0] > 200  # red, the one named by XHDR
    x.xhdr(0, 0, 13)  # named image not here yet: nothing rather than the wrong one
    assert x.art(0) is None
    x.lot(0, "art", 13, img((0, 255, 0)))
    assert pixel(x.art(0))[1] > 200
    x.xhdr(0, 1, 0)  # flush
    assert x.art(0) is None


def test_without_xhdr_newest_art_is_used_and_programs_are_separate():
    x = Extras()
    x.lot(0, "art", 1, img((255, 0, 0)))
    x.lot(0, "art", 2, img((0, 0, 255)))
    x.lot(1, "art", 3, img((0, 255, 0)))
    assert pixel(x.art(0))[2] > 200
    assert pixel(x.art(1))[1] > 200
    assert x.art(2) is None


def test_song_change_clears_art_until_new_art_named():
    x = Extras()
    x.lot(0, "art", 1, img())
    x.song_changed(0)
    assert x.art(0) is None
    x.lot(0, "art", 2, img((0, 0, 255)))
    assert x.art(0) is not None


def test_bad_or_huge_images_are_ignored():
    x = Extras()
    x.lot(0, "art", 1, b"not an image")
    x.lot(0, "art", 2, b"\xff\xd8" + b"\x00" * (3 * 1024 * 1024))
    assert x.art(0) is None
    assert clean_image(b"garbage", "JPEG", 1024) is None


def test_images_are_re_encoded_and_bounded():
    big = img(size=(3000, 1500), fmt="PNG")
    out = clean_image(big, "JPEG", 1024)
    im = Image.open(io.BytesIO(out))
    assert im.format == "JPEG" and max(im.size) == 1024


def test_logo_is_png():
    x = Extras()
    x.lot(1, "logo", 5, img(fmt="JPEG"))
    assert Image.open(io.BytesIO(x.logo(1))).format == "PNG"
    assert x.logo(0) is None


# -- alerts -----------------------------------------------------------------------------------

def test_alert_is_cleaned_and_expires():
    x = Extras()
    x.alert("Tornado\x00 warning\nfor  Oakland County", ["Weather"], now=1000.0)
    a = x.current_alert(now=1100.0)
    assert a["message"] == "Tornado warning for Oakland County" and a["categories"] == ["Weather"]
    x.alert("Tornado warning for Oakland County", ["Weather"], now=2000.0)  # repeat extends it
    assert x.current_alert(now=2000.0 + 29 * 60)
    assert x.current_alert(now=2000.0 + 31 * 60) is None


def test_empty_alert_ignored():
    x = Extras()
    x.alert("   ", [], now=1.0)
    assert x.current_alert(now=1.0) is None


# -- maps -------------------------------------------------------------------------------------

T0 = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


def test_traffic_map_assembles_when_all_tiles_arrive():
    x = Extras()
    for n in range(1, 9):
        x.here_image("traffic", seq=7, n1=n, n2=9, time=T0, data=img((n * 20, 0, 0), (200, 200), "PNG"))
    assert x.map_png("traffic") is None  # 8 of 9
    x.here_image("traffic", seq=7, n1=9, n2=9, time=T0, data=img((0, 0, 255), (200, 200), "PNG"))
    im = Image.open(io.BytesIO(x.map_png("traffic")))
    assert im.size == (768, 768)  # tiles normalised to 256 px
    assert im.convert("RGB").getpixel((700, 700))[2] > 200  # tile 9 bottom-right


def test_weather_map_single_image_and_bad_tile_ignored():
    x = Extras()
    x.here_image("weather", seq=1, n1=1, n2=1, time=T0, data=b"junk")
    assert x.map_png("weather") is None
    x.here_image("weather", seq=2, n1=36, n2=37, time=T0, data=img(size=(400, 300), fmt="PNG"))  # n1/n2 are
    # just sequence numbers for weather (nrsc5.h), not part counts
    assert Image.open(io.BytesIO(x.map_png("weather"))).size == (400, 300)


# -- calibration ------------------------------------------------------------------------------

def test_ppm_measurement_is_absolute_and_per_station():
    x = Extras()
    assert x.ppm_measurement() is None
    x.sync_offset(97.1e6, 2010.0, applied_ppm=0)  # nrsc5 reports SYNC once per lock: one sample is enough
    freq, ppm = x.ppm_measurement()
    assert freq == 97.1e6 and round(ppm) == -21
    x.sync_offset(101.1e6, 0.0, applied_ppm=-21)  # measured while already corrected: absolute stays -21
    assert round(x.ppm_measurement()[1]) == -21


# -- misc -------------------------------------------------------------------------------------

def test_reset_clears_everything():
    x = Extras()
    x.lot(0, "art", 1, img())
    x.lot(0, "logo", 2, img())
    x.alert("Test", [], now=1.0)
    x.location = (42.5, -83.1)
    x.audio[0] = {"codec_mode": 0}
    x.here_image("weather", seq=1, n1=1, n2=1, time=T0, data=img(fmt="PNG"))
    x.reset()
    assert (x.art(0), x.logo(0), x.current_alert(1.0), x.location, x.audio, x.map_png("weather")) == \
        (None, None, None, None, {}, None)


def test_images_beyond_the_pixel_budget_are_refused_before_decoding():
    huge = img(size=(4000, 2500), fmt="PNG")  # 10 Mpx but a tiny file (one colour)
    assert len(huge) < 2 * 1024 * 1024
    assert clean_image(huge, "JPEG", 1024) is None


def test_map_size_is_bounded_whatever_the_tiles_claim():
    x = Extras()
    for n in range(1, 10):
        size = (1800, 1800) if n == 1 else (200, 200)
        x.here_image("traffic", seq=1, n1=n, n2=9, time=T0, data=img(size=size, fmt="PNG"))
    im = Image.open(io.BytesIO(x.map_png("traffic")))
    assert im.size == (768, 768)  # 3 x 256 px tiles


def test_odd_tile_counts_still_make_a_bounded_grid():
    x = Extras()
    for n in range(1, 9):
        x.here_image("traffic", seq=1, n1=n, n2=8, time=T0, data=img(size=(200, 200), fmt="PNG"))
    assert Image.open(io.BytesIO(x.map_png("traffic"))).size == (768, 768)  # 3 x 3 grid, one empty


def test_utc_offset_only_when_plausible_and_repeated():
    x = Extras()
    x.local_time(140)  # not a quarter hour: corrupted or misconfigured
    x.local_time(140)
    assert x.utc_offset is None
    x.local_time(-300)
    assert x.utc_offset is None  # once isn't enough
    x.local_time(-300)
    assert x.utc_offset == -300
    x.local_time(-40)  # a glitch doesn't replace a confirmed value
    assert x.utc_offset == -300



def test_hostile_formats_are_never_decoded(monkeypatch):
    from PIL import EpsImagePlugin

    def boom(*a, **k):
        raise AssertionError("Ghostscript must never run on broadcast data")
    monkeypatch.setattr(EpsImagePlugin, "Ghostscript", boom)
    eps = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\nshowpage\n"
    assert clean_image(eps, "PNG", 512) is None


def test_corrupt_png_never_raises(monkeypatch):
    # Pillow reports some damaged PNG chunks as SyntaxError("broken PNG file ..."), not OSError.
    data = img(size=(64, 64), fmt="PNG")

    def broken(self):
        raise SyntaxError("broken PNG file (chunk b'IDAX')")
    monkeypatch.setattr(Image.Image, "load", broken)
    from PIL import ImageFile
    monkeypatch.setattr(ImageFile.ImageFile, "load", broken)
    assert clean_image(data, "PNG", 512) is None


def test_same_lot_id_on_two_programs_keeps_both():
    x = Extras()
    x.lot(0, "art", 5, img((255, 0, 0)))
    x.lot(1, "art", 5, img((0, 0, 255)))
    x.xhdr(0, 0, 5)
    x.xhdr(1, 0, 5)
    assert pixel(x.art(0))[0] > 200 and pixel(x.art(1))[2] > 200


def test_new_logos_are_reported_once():
    x = Extras()
    x.lot(0, "logo", 1, img())
    assert set(x.take_new_logos()) == {0}
    assert x.take_new_logos() == {}
