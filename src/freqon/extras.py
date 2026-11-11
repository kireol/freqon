"""Everything a station broadcasts besides audio and text: album art, logos, emergency
alerts, traffic/weather maps, transmitter location, audio details, and the frequency offset
used to calibrate the dongle.

One Extras per tuner. Methods are called from nrsc5's thread and read from the event loop,
so a lock guards the state. Images come from the air and are untrusted: they're decoded with
Pillow, size-limited and re-encoded before anyone gets them.
"""

from __future__ import annotations

import io
import math
import statistics
import threading
from collections import OrderedDict
from datetime import datetime

from PIL import Image

from .stations import clean_name

MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_IMAGE_PX = 1024
MAX_PIXELS = 8_000_000  # refuse anything bigger before decoding it (decompression bombs)
TILE_PX = 256  # map tiles are normalised to this, so a map is at most 4 x 4 tiles = 1024 px
ART_KEEP = 20  # LOT files remembered per tuner
ALERT_TTL = 30 * 60  # seconds an alert stays up after its last repeat
CALIBRATION_SAMPLES = 1  # nrsc5 reports SYNC once per lock, so one per tuning is what there is
MAX_PPM = 100  # offsets implying a worse dongle than this are ignored (off-frequency or bogus signal)



def clean_image(data: bytes, fmt: str, max_px: int = MAX_IMAGE_PX) -> bytes | None:
    """Decode, bound and re-encode an image from the air; None if it isn't a sane image."""
    if not data or len(data) > MAX_IMAGE_BYTES:
        return None
    try:
        # Only plain bitmap formats: never let broadcast bytes reach e.g. EPS (Ghostscript).
        with Image.open(io.BytesIO(data), formats=("JPEG", "PNG", "GIF")) as im:
            w, h = im.size  # from the header, before any pixels are decoded
            if w * h > MAX_PIXELS:
                return None
            if fmt == "JPEG" and im.format == "JPEG":
                im.draft("RGB", (max_px, max_px))  # let the decoder downscale big JPEGs cheaply
            im.load()
            im = im.convert("RGBA" if fmt == "PNG" else "RGB")
            im.thumbnail((max_px, max_px))
            out = io.BytesIO()
            im.save(out, fmt, **({"quality": 88} if fmt == "JPEG" else {}))
            return out.getvalue()
    except Exception:  # noqa: BLE001 -- untrusted bytes: Pillow can raise SyntaxError and more
        return None


class Extras:
    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        """Forget everything (retune: it all belonged to the previous station)."""
        with getattr(self, "_lock", threading.Lock()):
            self._art: OrderedDict[tuple[int, int], bytes] = OrderedDict()  # (program, lot id) -> jpeg
            self._wanted: dict[int, int | None] = {}  # program -> lot id named by XHDR (None = flushed)
            self._latest: dict[int, int] = {}  # program -> newest art lot id (when no XHDR seen)
            self._logos: dict[int, bytes] = {}
            self._new_logos: dict[int, bytes] = {}  # arrived since the last take_new_logos()
            self._alert: dict | None = None
            self._tiles: dict[str, dict] = {}
            self._maps: dict[str, bytes] = {}
            self._offsets: list[float] = []
            self._offset_freq: float | None = None
            self.location: tuple[float, float] | None = None
            self.utc_offset: int | None = None  # minutes, once confirmed
            self._utc_candidate: int | None = None
            self.audio: dict[int, dict] = {}

    # -- images -----------------------------------------------------------------------------

    def lot(self, program: int, kind: str, lot_id: int, data: bytes) -> bool:
        """A LOT file arrived on a channel's image component. True if it was usable."""
        if kind == "logo":
            png = clean_image(data, "PNG", 512)
            if png is None:
                return False
            with self._lock:
                if self._logos.get(program) != png:
                    self._new_logos[program] = png
                self._logos[program] = png
            return True
        jpeg = clean_image(data, "JPEG")
        if jpeg is None:
            return False
        with self._lock:
            self._art[(program, lot_id)] = jpeg  # LOT ids are per port: two programs may reuse one
            self._art.move_to_end((program, lot_id))
            while len(self._art) > ART_KEEP:
                self._art.popitem(last=False)
            self._latest[program] = lot_id
        return True

    def xhdr(self, program: int, param: int, lot_id: int) -> None:
        """ID3 XHDR: param 0 = show the image with this LOT id, 1 = no image (flush)."""
        with self._lock:
            self._wanted[program] = lot_id if param == 0 else None

    def song_changed(self, program: int) -> None:
        """New title: the old song's art no longer applies until the station names new art."""
        with self._lock:
            self._wanted.pop(program, None)  # nothing named yet for the new song
            self._latest.pop(program, None)

    def art(self, program: int) -> bytes | None:
        with self._lock:
            if program in self._wanted:
                lot_id = self._wanted[program]
                if lot_id is None:
                    return None
                return self._art.get((program, lot_id))
            lot_id = self._latest.get(program)
            return self._art.get((program, lot_id)) if lot_id is not None else None

    def logo(self, program: int) -> bytes | None:
        with self._lock:
            return self._logos.get(program)

    def take_new_logos(self) -> dict[int, bytes]:
        """Logos that arrived (or changed) since the last call, so they can be saved."""
        with self._lock:
            new, self._new_logos = self._new_logos, {}
            return new

    # -- alerts -----------------------------------------------------------------------------

    def alert(self, message: str, categories: list[str], now: float) -> None:
        text = clean_name(" ".join((message or "").replace("\x00", "").split()))
        if not text:
            return
        with self._lock:
            self._alert = {"message": text, "categories": [str(c) for c in categories][:4], "received": now}

    def current_alert(self, now: float) -> dict | None:
        with self._lock:
            a = self._alert
            if a is None or now - a["received"] > ALERT_TTL:
                return None
            return dict(a)

    # -- maps -------------------------------------------------------------------------------

    def here_image(self, kind: str, seq: int, n1: int, n2: int, time: datetime | None, data: bytes) -> None:
        """Traffic maps arrive as tiles n1 of n2 (usually 9). A weather map is one image; its
        n1/n2 are just sequence numbers (nrsc5.h)."""
        if kind != "traffic":
            image = clean_image(data, "PNG", MAX_IMAGE_PX)
            if image is not None:
                with self._lock:
                    self._maps[kind] = image
            return
        tile = clean_image(data, "PNG", TILE_PX)
        if tile is None or not 1 <= n1 <= n2 <= 16:
            return
        with self._lock:
            pending = self._tiles.get(kind)
            if pending is None or pending["time"] != time or pending["n2"] != n2:
                pending = self._tiles[kind] = {"time": time, "n2": n2, "tiles": {}}
            pending["tiles"][n1] = tile
            if len(pending["tiles"]) == n2:
                self._maps[kind] = _assemble(pending["tiles"], n2)
                del self._tiles[kind]

    def map_png(self, kind: str) -> bytes | None:
        with self._lock:
            return self._maps.get(kind)

    def local_time(self, minutes: int) -> None:
        """Station's UTC offset. Weak signals garble it, so accept only whole quarter hours
        within +-14 h, seen twice in a row."""
        if minutes % 15 or abs(minutes) > 14 * 60:
            return
        with self._lock:
            if self._utc_candidate == minutes:
                self.utc_offset = minutes
            self._utc_candidate = minutes

    # -- calibration ------------------------------------------------------------------------

    def sync_offset(self, freq_hz: float, offset_hz: float, applied_ppm: int = 0) -> None:
        """An HD lock's frequency offset, measured while the dongle used `applied_ppm`.
        Stored as the absolute correction the dongle needs, so values measured under different
        corrections compare directly."""
        if not freq_hz or abs(offset_hz) > freq_hz * MAX_PPM * 1e-6:
            return
        with self._lock:
            if freq_hz != self._offset_freq:
                self._offset_freq, self._offsets = freq_hz, []
            self._offsets.append(applied_ppm - offset_hz / freq_hz * 1e6)
            del self._offsets[:-200]

    def ppm_measurement(self) -> tuple[float, float] | None:
        """(frequency Hz, correction ppm) that would cancel the offset measured on the current
        station; None until there are enough samples."""
        with self._lock:
            if self._offset_freq is None or len(self._offsets) < CALIBRATION_SAMPLES:
                return None
            return self._offset_freq, statistics.median(self._offsets)

    def ppm_estimate(self) -> int | None:
        m = self.ppm_measurement()
        return round(m[1]) if m else None


def _assemble(tiles: dict[int, bytes], n: int) -> bytes:
    cols = math.ceil(n ** 0.5)  # near-square grid: at most 4 x 4 tiles
    rows = (n + cols - 1) // cols
    w = h = TILE_PX  # fixed tile size: the sheet is bounded whatever the tiles claimed
    sheet = Image.new("RGBA", (w * cols, h * rows))
    for i, b in tiles.items():
        im = Image.open(io.BytesIO(b)).convert("RGBA").resize((w, h))
        r, c = divmod(i - 1, cols)
        sheet.paste(im, (c * w, r * h))
    out = io.BytesIO()
    sheet.save(out, "PNG")
    return out.getvalue()
