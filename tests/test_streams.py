import asyncio
import shutil
import subprocess

import pytest

from freqon.streams import TSEncoder

W, H = 64, 36


async def _run(link: str, seconds: float = 2.0) -> bytes:
    enc = TSEncoder(lambda: bytes(W * H * 3), size=(W, H), fps=2, video_link=link)
    q = await enc.subscribe()
    out = b""
    chunk = bytes(4410 * 4)  # 0.1 s of silence, stereo int16
    for _ in range(int(seconds * 10)):
        enc.feed(chunk)
        await asyncio.sleep(0.1)
        while not q.empty():
            out += q.get_nowait()
    enc.unsubscribe(q)
    enc._stop_timer.cancel()
    await enc.stop()
    return out


def _streams(ts: bytes) -> list[str]:
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0", "-"],
                           input=ts, capture_output=True, check=False)
    return sorted(set(probe.stdout.decode().split()))


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
async def test_video_over_tcp_for_windows():
    # Windows can't hand ffmpeg an extra pipe, so there the frames go over a local TCP socket.
    enc = TSEncoder(lambda: b"", video_link="tcp")
    assert enc.pass_fds() == ()
    ts = await _run("tcp")
    assert len(ts) > 2000 and _streams(ts) == ["aac", "h264"]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
async def test_video_over_a_pipe_elsewhere():
    ts = await _run("pipe")
    assert len(ts) > 2000 and _streams(ts) == ["aac", "h264"]


def test_default_link_depends_on_the_os(monkeypatch):
    import freqon.streams as s
    monkeypatch.setattr(s.os, "name", "nt")
    assert TSEncoder(lambda: b"").video_link == "tcp"
    monkeypatch.setattr(s.os, "name", "posix")
    assert TSEncoder(lambda: b"").video_link == "pipe"
