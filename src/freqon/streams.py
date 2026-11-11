"""Shared, on-demand ffmpeg encoders that fan one PCM stream out to many HTTP listeners.

An encoder starts when its first listener subscribes and stops a few seconds after the
last one leaves (the grace period covers players that reconnect when changing channel).
Slow listeners lose chunks rather than holding everyone else back.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable

from .mixer import AUDIO_RATE, BYTES_PER_FRAME

log = logging.getLogger(__name__)

_NO_PROBE = ["-probesize", "32", "-analyzeduration", "0"]  # raw inputs need no probing
_PCM_INPUT = [*_NO_PROBE, "-f", "s16le", "-ar", str(AUDIO_RATE), "-ac", "2", "-i", "pipe:0"]
_MAX_PENDING_INPUT = 2 * 1024 * 1024  # bytes queued to ffmpeg before we start dropping


class Encoder:
    content_type = "application/octet-stream"

    def __init__(self, ffmpeg: str = "ffmpeg", stop_grace: float = 5.0):
        self.ffmpeg = ffmpeg
        self.stop_grace = stop_grace
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._listeners: set[asyncio.Queue] = set()
        self._stop_timer: asyncio.TimerHandle | None = None
        self._starting: asyncio.Lock = asyncio.Lock()

    # -- subclass hooks -------------------------------------------------------------------------

    def args(self) -> list[str]:
        raise NotImplementedError

    def pass_fds(self) -> tuple[int, ...]:
        return ()

    async def after_start(self) -> None:
        pass

    def before_stop(self) -> None:
        pass

    def on_pcm(self, pcm: bytes) -> None:
        pass

    # -- public ---------------------------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._listeners.add(q)
        if self._stop_timer:
            self._stop_timer.cancel()
            self._stop_timer = None
        async with self._starting:
            if not self.active:
                await self._start()
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._listeners.discard(q)
        if not self._listeners and self.active and self._stop_timer is None:
            loop = asyncio.get_running_loop()
            self._stop_timer = loop.call_later(self.stop_grace, lambda: asyncio.ensure_future(self.stop()))

    def end_listeners(self) -> None:
        for q in list(self._listeners):
            if q.full():
                q.get_nowait()
            q.put_nowait(b"")

    def feed(self, pcm: bytes) -> None:
        """Called on the event loop with each chunk of PCM from the radio."""
        if not self.active or self._proc.stdin is None:
            return
        transport = self._proc.stdin.transport
        if transport.is_closing() or transport.get_write_buffer_size() > _MAX_PENDING_INPUT:
            return
        self._proc.stdin.write(pcm)
        self.on_pcm(pcm)

    async def stop(self) -> None:
        self._stop_timer = None
        if self._listeners or self._proc is None:
            return
        proc, self._proc = self._proc, None
        # Take the reader now: a new listener may start a new process (and reader) while we wait.
        reader, self._reader = self._reader, None
        self.before_stop()
        with contextlib.suppress(OSError, RuntimeError):
            proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), 3)
        except TimeoutError:
            proc.kill()
            await proc.wait()
        if reader:
            reader.cancel()
        log.info("%s encoder stopped", type(self).__name__)

    # -- internals --------------------------------------------------------------------------------

    async def _start(self) -> None:
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", *self.args()]
        log.info("starting encoder: %s", " ".join(cmd))
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            pass_fds=self.pass_fds(),
        )
        await self.after_start()
        self._reader = asyncio.create_task(self._read_loop(self._proc))

    async def _read_loop(self, proc: asyncio.subprocess.Process) -> None:
        while True:
            chunk = await proc.stdout.read(16384)
            if not chunk:
                break
            for q in list(self._listeners):
                if q.full():
                    try:
                        q.get_nowait()  # drop the oldest chunk for a slow listener
                    except asyncio.QueueEmpty:
                        pass
                q.put_nowait(chunk)
        if self._proc is proc:
            # ffmpeg died on its own (stop() clears _proc first): end the listeners' responses.
            # After a deliberate stop() the listeners, if any, belong to a newer process.
            self._proc = None
            self.before_stop()
            for q in list(self._listeners):
                q.put_nowait(b"")


class MP3Encoder(Encoder):
    content_type = "audio/mpeg"

    def __init__(self, bitrate: str = "128k", **kw):
        super().__init__(**kw)
        self.bitrate = bitrate

    def args(self) -> list[str]:
        return [*_PCM_INPUT, "-c:a", "libmp3lame", "-b:a", self.bitrate, "-f", "mp3", "pipe:1"]


class TSEncoder(Encoder):
    """MPEG-TS with H.264 video + AAC audio, for Plex Live TV and Jellyfin.

    The video is whatever frame_source() returns (the 'now playing' card, or camera video
    with the bar burned in), fed through a second pipe and paced by the audio clock (one
    frame per AUDIO_RATE / fps audio frames) so the two stay in step.

    Without a video_bitrate the picture is treated as a still card (x264 'stillimage').
    on_active(True/False) is called when the ffmpeg process starts and stops.
    """

    content_type = "video/mp2t"

    def __init__(self, frame_source: Callable[[], bytes], size=(640, 360), fps: int = 2,
                 audio_bitrate: str = "128k", video_encoder: str = "libx264",
                 video_bitrate: str | None = None, on_active: Callable[[bool], None] | None = None,
                 video_link: str | None = None, **kw):
        super().__init__(**kw)
        self.frame_source = frame_source
        self.size = size
        self.fps = fps
        self.audio_bitrate = audio_bitrate
        self.video_encoder = video_encoder
        self.video_bitrate = video_bitrate
        self.on_active = on_active or (lambda active: None)
        # How frames reach ffmpeg: an inherited pipe, or (Windows, which can't pass a child an
        # extra pipe) a TCP connection to 127.0.0.1 that ffmpeg makes to us.
        self.video_link = video_link or ("tcp" if os.name == "nt" else "pipe")
        self._video_r: int | None = None
        self._video_url: str | None = None
        self._video_transport: asyncio.WriteTransport | None = None
        self._frames_until_video = 0

    def args(self) -> list[str]:
        w, h = self.size
        video = ["-c:v", self.video_encoder]
        if self.video_encoder == "libx264":
            # zerolatency: no frame-thread delay, which would otherwise hold back the audio.
            tune = "zerolatency" if self.video_bitrate else "stillimage,zerolatency"
            video += ["-preset", "ultrafast", "-tune", tune]
        if self.video_bitrate:
            video += ["-b:v", self.video_bitrate]
        return [
            *_PCM_INPUT,
            *_NO_PROBE, "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-framerate", str(self.fps),
            "-i", self._video_url if self.video_link == "tcp" else f"pipe:{self._video_r}",
            "-map", "1:v", "-map", "0:a",
            *video, "-pix_fmt", "yuv420p",
            "-g", str(self.fps), "-c:a", "aac", "-b:a", self.audio_bitrate,
            "-f", "mpegts", "-muxdelay", "0", "pipe:1",
        ]

    def pass_fds(self) -> tuple[int, ...]:
        return (self._video_r,) if self.video_link == "pipe" else ()

    async def _start(self) -> None:
        if self.video_link == "tcp":
            await self._start_tcp()
        else:
            await self._start_pipe()
        self._frames_until_video = 0
        self.on_active(True)
        self._write_frame()

    async def _start_tcp(self) -> None:
        connected: asyncio.Future = asyncio.get_running_loop().create_future()

        def on_connect(reader, writer):
            if not connected.done():
                connected.set_result(writer)
            else:
                writer.close()  # only ffmpeg's one connection

        server = await asyncio.start_server(on_connect, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        self._video_url = f"tcp://127.0.0.1:{port}"
        try:
            await super()._start()
            writer = await asyncio.wait_for(connected, 10)
            self._video_transport = writer.transport
        except TimeoutError:
            log.error("ffmpeg never connected for video frames")
        finally:
            server.close()

    async def _start_pipe(self) -> None:
        # ffmpeg reads the video from a second pipe; pass_fds keeps the fd number in the child.
        self._video_r, video_w = os.pipe()
        try:
            await super()._start()
        finally:
            os.close(self._video_r)
        loop = asyncio.get_running_loop()
        self._video_transport, _ = await loop.connect_write_pipe(
            asyncio.Protocol, os.fdopen(video_w, "wb", buffering=0)
        )

    def _write_frame(self) -> None:
        t = self._video_transport
        w, h = self.size
        if t is None or t.is_closing() or t.get_write_buffer_size() > max(8 * 1024 * 1024, 3 * w * h * 3):
            return
        try:
            frame = self.frame_source()
        except Exception:  # a rendering problem must never stop the stream
            log.exception("video frame failed")
            return
        t.write(frame)

    def on_pcm(self, pcm: bytes) -> None:
        self._frames_until_video -= len(pcm) // BYTES_PER_FRAME
        while self._frames_until_video <= 0:
            self._write_frame()
            self._frames_until_video += AUDIO_RATE // self.fps

    def before_stop(self) -> None:
        if self._video_transport:
            self._video_transport.close()
            self._video_transport = None
        self.on_active(False)
