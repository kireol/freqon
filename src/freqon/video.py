"""Camera video with the now-playing bar burned in, for the MPEG-TS (Plex / Jellyfin) stream.

Camera: one ffmpeg process decodes the configured source (RTSP, a go2rtc stream, anything
ffmpeg opens) to raw RGB frames at the output size, and the latest frame is kept. If the
source ends, errors or stalls, it is restarted with a growing delay (1 s up to 30 s).

VideoComposer: builds each output frame for one channel: the latest camera frame with that
channel's lower third blended on top, or the plain full-screen card when the camera has
delivered nothing for STALE_AFTER seconds. It never blocks and never fails, so the TS stream
carries on whatever the camera does.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from urllib.parse import urlsplit, urlunsplit

import numpy as np

from . import card

log = logging.getLogger(__name__)

STALE_AFTER = 5.0  # seconds without a camera frame before falling back to the card
STALL_TIMEOUT = 10.0  # restart a camera ffmpeg that has produced nothing for this long


def redact_url(url: str) -> str:
    """The URL with any user:password removed, for logs (camera URLs often carry them)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<camera>"
    if "@" not in parts.netloc:
        return url
    return urlunsplit(parts._replace(netloc="***@" + parts.netloc.rsplit("@", 1)[1]))


def camera_command(url: str, size: tuple[int, int], fps: int, ffmpeg: str) -> list[str]:
    """ffmpeg command that turns `url` into raw rgb24 frames of exactly `size` on stdout.
    `lavfi:<graph>` uses an ffmpeg test source (e.g. lavfi:testsrc2) instead of a camera."""
    w, h = size
    if url.startswith("lavfi:"):
        source = ["-f", "lavfi", "-i", url.removeprefix("lavfi:")]
    elif url.startswith(("rtsp://", "rtsps://")):
        source = ["-rtsp_transport", "tcp", "-i", url]
    else:
        source = ["-i", url]
    # Letterbox to the output size so any camera aspect ratio fits.
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,fps={fps}")
    return [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", *source,
            "-an", "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]


class Camera:
    def __init__(self, url: str, size: tuple[int, int], fps: int, ffmpeg: str = "ffmpeg", *,
                 command: Callable[..., list[str]] = camera_command,
                 min_backoff: float = 1.0, max_backoff: float = 30.0):
        self.url = url
        self.size = size
        self.fps = fps
        self.ffmpeg = ffmpeg
        self._command = command
        self.min_backoff = min_backoff
        self.max_backoff = max_backoff
        self._frame: bytes | None = None
        self._frame_time = 0.0
        self._task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self.starts = 0

    def __repr__(self) -> str:
        return f"Camera({redact_url(self.url)!r})"

    def redact(self, text: str) -> str:
        """Hide this camera's credentials in text such as ffmpeg's error messages."""
        text = text.replace(self.url, redact_url(self.url))
        userinfo = urlsplit(self.url).netloc.rpartition("@")[0] if "@" in self.url else ""
        return text.replace(userinfo, "***") if userinfo else text

    def latest(self) -> tuple[bytes | None, float]:
        """The newest frame (rgb24) and when it arrived (time.monotonic())."""
        return self._frame, self._frame_time

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if not self.running:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._frame = None

    async def _run(self) -> None:
        backoff = self.min_backoff
        frame_bytes = self.size[0] * self.size[1] * 3
        while True:
            got_frame = False
            try:
                self.starts += 1
                cmd = self._command(self.url, self.size, self.fps, self.ffmpeg)
                log.info("camera: starting %s", redact_url(self.url))
                self._proc = await asyncio.create_subprocess_exec(
                    *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, limit=frame_bytes * 2)
                while True:
                    data = await asyncio.wait_for(self._proc.stdout.readexactly(frame_bytes), STALL_TIMEOUT)
                    self._frame, self._frame_time = data, time.monotonic()
                    got_frame = True
            except asyncio.IncompleteReadError:
                pass  # source ended
            except TimeoutError:
                log.warning("camera: no frames for %ss; restarting", STALL_TIMEOUT)
            except OSError as e:
                log.warning("camera: cannot run ffmpeg: %s", e)
            except Exception:  # anything else: log it and retry; the camera must come back
                log.exception("camera: unexpected error")
            finally:
                await self._kill()
            if got_frame:
                backoff = self.min_backoff
            log.info("camera: source stopped; retrying in %.0f s", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.max_backoff)

    async def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        with contextlib.suppress(Exception):
            err = await asyncio.wait_for(proc.stderr.read(), 1)
            if err.strip():
                log.warning("camera: ffmpeg said: %s", self.redact(err.decode(errors="replace").strip())[-300:])
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 2)


class VideoComposer:
    def __init__(self, camera: Camera | None, size: tuple[int, int], status_fn: Callable[[], dict],
                 stale_after: float = STALE_AFTER, art_fn: Callable[[], bytes | None] | None = None):
        self.camera = camera
        self.size = size
        self.status_fn = status_fn
        self.stale_after = stale_after
        self.art_fn = art_fn or (lambda: None)
        self._card: tuple | None = None  # (text key, rgb bytes)
        self._bar: tuple | None = None  # (text key, y, premultiplied rgb, inverse alpha)

    def frame(self) -> bytes:
        st = self.status_fn()
        frame = None
        if self.camera is not None:
            data, t = self.camera.latest()
            if data is not None and time.monotonic() - t < self.stale_after:
                frame = data
        if frame is None:
            return self._card_frame(st)
        return self._blend(frame, st)

    @staticmethod
    def _key(st: dict):
        station, now = st.get("station") or {}, st.get("now_playing") or {}
        return (st.get("frequency"), st.get("program"), st.get("source"), station.get("display_name"),
                station.get("name"), station.get("slogan"), now.get("title"), now.get("artist"),
                st.get("art_key"), (st.get("alert") or {}).get("message"))

    @staticmethod
    def _shown(st: dict) -> dict:
        station = st.get("station") or {}
        return {**st, "station": {**station, "name": station.get("display_name") or station.get("name")}}

    def _card_frame(self, st: dict) -> bytes:
        key = self._key(st)
        if self._card is None or self._card[0] != key:
            self._card = (key, card.render_rgb(self._shown(st), self.size, self.art_fn() if st.get("art_key") else None))
        return self._card[1]

    def _blend(self, frame: bytes, st: dict) -> bytes:
        key = self._key(st)
        if self._bar is None or self._bar[0] != key:
            strip, y = card.render_lower_third(self._shown(st), self.size,
                                               self.art_fn() if st.get("art_key") else None)
            alpha = strip[..., 3:4].astype(np.uint16)
            self._bar = (key, y, strip[..., :3].astype(np.uint16) * alpha, 255 - alpha)
        _, y, premult, inv = self._bar
        w, h = self.size
        img = np.frombuffer(frame, np.uint8).reshape(h, w, 3).copy()
        region = img[y:].astype(np.uint16)
        img[y:] = ((premult + region * inv) // 255).astype(np.uint8)
        return img.tobytes()
