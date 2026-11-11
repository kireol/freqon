import asyncio
import shutil
import time

import numpy as np
import pytest

from freqon import card
from freqon.streams import TSEncoder
from freqon.video import Camera, VideoComposer, camera_command

SIZE = (320, 180)
W, H = SIZE
STATUS = {"frequency": 97.1, "program": 0, "source": "digital",
          "station": {"name": "WXYT", "display_name": "WXYT"}, "now_playing": {"title": "Song", "artist": "Band"}}


class StubCamera:
    def __init__(self, frame: bytes | None, age: float):
        self.frame, self.age = frame, age

    def latest(self):
        return self.frame, time.monotonic() - self.age


def blue() -> bytes:
    a = np.zeros((H, W, 3), np.uint8)
    a[..., 2] = 255
    return a.tobytes()


def test_card_without_camera():
    frame = VideoComposer(None, SIZE, lambda: STATUS).frame()
    assert frame == card.render_rgb(STATUS, SIZE)


def test_card_when_camera_is_stale():
    frame = VideoComposer(StubCamera(blue(), age=6.0), SIZE, lambda: STATUS).frame()
    assert frame == card.render_rgb(STATUS, SIZE)


def test_card_when_camera_never_delivered():
    frame = VideoComposer(StubCamera(None, age=0.0), SIZE, lambda: STATUS).frame()
    assert frame == card.render_rgb(STATUS, SIZE)


def test_camera_frame_gets_the_lower_third():
    frame = VideoComposer(StubCamera(blue(), age=0.5), SIZE, lambda: STATUS).frame()
    img = np.frombuffer(frame, np.uint8).reshape(H, W, 3)
    _, y = card.render_lower_third(STATUS, SIZE)
    assert (img[:y] == [0, 0, 255]).all()  # camera untouched above the bar
    assert (img[y:] != [0, 0, 255]).any(axis=2).mean() > 0.5  # bar drawn over it


def test_composer_follows_status_changes():
    st = dict(STATUS)
    comp = VideoComposer(StubCamera(blue(), age=0.0), SIZE, lambda: st)
    a = comp.frame()
    st = {**STATUS, "now_playing": {"title": "Another song", "artist": "Someone"}}
    assert comp.frame() != a


def test_camera_command():
    cmd = camera_command("rtsp://cam/x", SIZE, 15, "ffmpeg")
    assert cmd[cmd.index("-rtsp_transport") + 1] == "tcp"
    assert cmd[cmd.index("-i") + 1] == "rtsp://cam/x"
    assert "rawvideo" in cmd and "rgb24" in cmd
    assert "-rtsp_transport" not in camera_command("http://go2rtc:1984/api/stream.mjpeg?src=cam", SIZE, 15, "ffmpeg")
    lavfi = camera_command("lavfi:testsrc2=size=640x360:rate=15", SIZE, 15, "ffmpeg")
    assert lavfi[lavfi.index("-i") - 1] == "lavfi" and lavfi[lavfi.index("-i") + 1] == "testsrc2=size=640x360:rate=15"


def test_ts_encoder_video_settings():
    enc = TSEncoder(lambda: b"", size=(1280, 720), fps=15, video_encoder="h264_v4l2m2m", video_bitrate="2500k")
    enc._video_r = 9
    args = enc.args()
    assert args[args.index("-c:v") + 1] == "h264_v4l2m2m"
    assert args[args.index("-b:v") + 1] == "2500k"
    assert args[args.index("-s") + 1] == "1280x720" and args[args.index("-g") + 1] == "15"
    assert "-preset" not in args
    x264 = TSEncoder(lambda: b"", size=(1280, 720), fps=15, video_encoder="libx264", video_bitrate="2500k").args()
    assert x264[x264.index("-tune") + 1] == "zerolatency"
    card_only = TSEncoder(lambda: b"").args()
    assert card_only[card_only.index("-tune") + 1] == "stillimage,zerolatency"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
async def test_camera_reconnects_after_source_ends():
    def short_lived(url, size, fps, ffmpeg):  # a source that ends after 0.5 s, like a dropped camera
        cmd = camera_command(url, size, fps, ffmpeg)
        return [*cmd[:-1], "-t", "0.5", cmd[-1]]

    cam = Camera("lavfi:testsrc2=size=320x180:rate=10", SIZE, 10, "ffmpeg",
                 command=short_lived, min_backoff=0.1)
    await cam.start()
    try:
        async def frame_after(t0):
            for _ in range(100):
                frame, t = cam.latest()
                if frame and t > t0:
                    return t
                await asyncio.sleep(0.05)
            raise AssertionError("no frame")

        first = await frame_after(0.0)
        assert len(cam.latest()[0]) == W * H * 3
        await asyncio.sleep(1.0)  # first ffmpeg has exited by now
        later = await frame_after(time.monotonic())
        assert later > first and cam.starts >= 2
    finally:
        await cam.stop()
    assert cam.starts >= 2


async def test_ts_encoder_reports_inactive_when_ffmpeg_dies():
    events = []
    enc = TSEncoder(lambda: b"\0" * 12, size=(2, 2), ffmpeg="false", on_active=events.append)
    q = await enc.subscribe()
    assert await asyncio.wait_for(q.get(), 2) == b""  # listener told the stream ended
    assert events == [True, False]


def test_camera_logs_never_show_passwords(caplog):
    from freqon.video import redact_url
    url = "rtsp://admin:s3cret@192.168.1.9:554/stream1"
    assert redact_url(url) == "rtsp://***@192.168.1.9:554/stream1"
    assert redact_url("http://go2rtc:1984/api/stream.mjpeg?src=cam") == "http://go2rtc:1984/api/stream.mjpeg?src=cam"
    assert redact_url("lavfi:testsrc2") == "lavfi:testsrc2"
    cam = Camera(url, SIZE, 10, "ffmpeg")
    assert "s3cret" not in cam.redact(f"[rtsp @ 0x1] method DESCRIBE failed for {url}: 401 Unauthorized")
    assert "s3cret" not in repr(cam)


async def test_camera_survives_unexpected_errors():
    calls = []

    def flaky(url, size, fps, ffmpeg):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return camera_command(url, size, fps, ffmpeg)

    cam = Camera("lavfi:testsrc2=size=320x180:rate=10", SIZE, 10, "ffmpeg", command=flaky, min_backoff=0.05)
    await cam.start()
    try:
        for _ in range(100):
            if cam.latest()[0]:
                break
            await asyncio.sleep(0.05)
        assert cam.latest()[0] is not None
    finally:
        await cam.stop()


async def test_stop_does_not_kill_a_newer_encoder(tmp_path):
    from freqon.streams import Encoder

    fake = tmp_path / "fakeffmpeg"
    fake.write_text("#!/bin/sh\nsleep 0.2\nexec cat\n")
    fake.chmod(0o755)

    class Echo(Encoder):
        def args(self):
            return []

    enc = Echo(ffmpeg=str(fake))
    q1 = await enc.subscribe()
    enc.unsubscribe(q1)
    stopping = asyncio.create_task(enc.stop())
    await asyncio.sleep(0.05)  # stop() is now waiting for the old process
    q2 = await enc.subscribe()  # a new listener starts a new process meanwhile
    await stopping
    enc.feed(b"hello")
    assert await asyncio.wait_for(q2.get(), 2) == b"hello"
    enc.unsubscribe(q2)
    enc._stop_timer.cancel()
    await enc.stop()
