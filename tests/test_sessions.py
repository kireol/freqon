"""Several tuners, several clients: who gets which tuner, and what happens to the others."""

import asyncio
import time

import pytest
from fakes import FakeRadio, fake_encoders

from freqon.pool import TunerPool
from freqon.service import Busy, ChannelKey, Controller
from freqon.stations import StationStore


@pytest.fixture
def made():
    return {}


@pytest.fixture
async def ctl(tmp_path, made):
    radios = [FakeRadio(0), FakeRadio(1)]
    c = Controller(TunerPool(radios), StationStore(str(tmp_path / "s.json")), idle_timeout=0,
                   make_encoders=fake_encoders(made), channel_grace=0)
    await c.start()
    yield c
    await c.close()


def radios(ctl):
    return ctl.pool.radios


async def listen(ctl, client, freq, program=0):
    await ctl.tune(freq, program, client_id=client)
    return ctl.add_listener(client, client.title(), "web")


async def test_two_programs_same_station_share_tuner(ctl):
    await listen(ctl, "a", 97.1, 0)
    await listen(ctl, "b", 97.1, 1)
    r0, r1 = radios(ctl)
    assert r0.tunes == [97.1] and r1.tunes == []
    assert set(r0.state.mixers) == {0, 1}
    assert ctl.status("a")["program"] == 0 and ctl.status("b")["program"] == 1
    assert ctl.status("b")["channel_id"] == "971.2"


async def test_second_station_uses_second_tuner(ctl):
    await listen(ctl, "a", 97.1)
    await listen(ctl, "b", 99.5)
    r0, r1 = radios(ctl)
    assert r0.state.frequency == 97.1 and r1.state.frequency == 99.5
    assert [t["frequency"] for t in ctl.status("a")["tuners"]] == [97.1, 99.5]


async def test_busy_when_all_tuners_held(ctl):
    await listen(ctl, "a", 97.1)
    await listen(ctl, "b", 99.5)
    with pytest.raises(Busy) as e:
        await ctl.tune(101.9, client_id="c")
    d = e.value.to_dict()
    assert d["error"] == "busy"
    assert [t["tuner"] for t in d["tuners"]] == [0, 1]
    assert {h["client_id"] for h in d["holders"]} == {"a", "b"}
    assert "A" in d["message"] and "B" in d["message"]
    assert radios(ctl)[0].state.frequency == 97.1 and radios(ctl)[1].state.frequency == 99.5


async def test_own_tuner_reused_when_alone(ctl):
    await listen(ctl, "a", 97.1)
    await ctl.tune(99.5, client_id="a")
    r0, r1 = radios(ctl)
    assert r0.state.frequency == 99.5 and r1.tunes == []


async def test_override_moves_bumped_sessions(ctl):
    await listen(ctl, "a", 97.1)  # tuned first -> oldest
    await listen(ctl, "b", 99.5)
    await ctl.tune(101.9, 1, client_id="c", override=True)
    r0, r1 = radios(ctl)
    assert r0.state.frequency == 101.9 and r1.state.frequency == 99.5
    assert ctl.sessions["a"] == ChannelKey(0, 101.9, 1)
    assert ctl.status("a")["frequency"] == 101.9
    assert ctl.status("b")["frequency"] == 99.5


async def test_bumped_channel_streams_end(ctl, made):
    lst = await listen(ctl, "a", 97.1)
    await listen(ctl, "b", 99.5)
    changed = ctl.session_changed("a")
    mp3, ts = ctl.channel(lst.key).mp3, ctl.channel(lst.key).ts
    q = await ts.subscribe()
    await ctl.tune(101.9, client_id="c", override=True)
    assert q.get_nowait() == b""
    assert changed.is_set()
    assert mp3 is made[ChannelKey(0, 97.1, 0)][0]


async def test_channel_encoders_stop_when_last_listener_leaves_but_tuner_stays(ctl, made):
    await listen(ctl, "a", 97.1, 0)
    b = await listen(ctl, "b", 97.1, 1)
    ctl.channel(b.key)  # encoders exist for b's channel
    ctl.remove_listener(b)
    await asyncio.sleep(0.05)
    mp3, ts = made[ChannelKey(0, 97.1, 1)]
    assert mp3.stopped and ts.stopped
    r0 = radios(ctl)[0]
    assert r0.state.running and 0 in r0.state.mixers
    assert ChannelKey(0, 97.1, 1) not in ctl.channels


async def test_pcm_reaches_the_right_channel(ctl, made):
    a = await listen(ctl, "a", 97.1, 0)
    b = await listen(ctl, "b", 97.1, 1)
    ca, cb = ctl.channel(a.key), ctl.channel(b.key)
    ctl._feed(0, 1, b"x")
    assert cb.mp3.fed == [b"x"] and ca.mp3.fed == []


async def test_idle_release_per_tuner(ctl):
    await listen(ctl, "a", 97.1)
    await ctl.tune(99.5, client_id="b")  # b never starts listening
    await ctl.release_idle()
    r0, r1 = radios(ctl)
    assert r0.state.running and not r1.state.running


async def test_scan_uses_idle_tuner_and_is_busy_when_none(ctl):
    await listen(ctl, "a", 97.1)
    await ctl.start_scan(client_id="c")
    await wait_scan_done(ctl)
    assert ctl.scan.get("finished")
    r0, r1 = radios(ctl)
    assert r0.tunes == [97.1]  # the listened tuner was never touched
    assert len(r1.tunes) > 50 and not r1.state.running  # scanned, then released
    await listen(ctl, "b", 99.5)
    with pytest.raises(Busy):
        await ctl.start_scan(client_id="c")


class StubCamera:
    def __init__(self):
        self.running = False
        self.starts = self.stops = 0

    async def start(self):
        self.running = True
        self.starts += 1

    async def stop(self):
        self.running = False
        self.stops += 1

    def latest(self):
        return None, 0.0


async def test_camera_runs_while_any_video_stream_does(tmp_path):
    video = {"source": "rtsp://cam/x", "size": (640, 360), "fps": 10, "encoder": "libx264", "bitrate": "1000k"}
    ctl = Controller(TunerPool([FakeRadio(0)]), StationStore(str(tmp_path / "s.json")), video=video)
    await ctl.start()
    cam = ctl.camera = StubCamera()
    a, b = ctl.channel(ChannelKey(0, 97.1, 0)).ts, ctl.channel(ChannelKey(0, 97.1, 1)).ts
    assert (a.size, a.fps, a.video_bitrate) == ((640, 360), 10, "1000k")
    a.on_active(True)
    b.on_active(True)
    await asyncio.sleep(0.01)
    assert cam.running and cam.starts == 1
    a.on_active(False)
    await asyncio.sleep(0.01)
    assert cam.running
    b.on_active(False)
    await asyncio.sleep(0.01)
    assert not cam.running
    await ctl.close()


async def test_no_video_source_means_card_video(tmp_path):
    ctl = Controller(TunerPool([FakeRadio(0)]), StationStore(str(tmp_path / "s.json")))
    ts = ctl.channel(ChannelKey(0, 97.1, 0)).ts
    assert ctl.camera is None and (ts.size, ts.fps, ts.video_bitrate) == ((640, 360), 2, None)
    assert len(ts.frame_source()) == 640 * 360 * 3


async def test_plex_uses_a_second_tuner_for_a_second_viewer(ctl):
    await ctl.tune(97.1, client_id="plex", prefer_idle=True)
    first = ctl.add_listener("plex", "Plex", "plex")
    ts = ctl.channel(first.key).ts
    q = await ts.subscribe()
    await ctl.tune(101.9, client_id="plex", prefer_idle=True)
    r0, r1 = radios(ctl)
    assert r0.state.frequency == 97.1 and r1.state.frequency == 101.9
    assert q.empty()  # first viewer's stream untouched


async def wait_scan_done(ctl):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not ctl.scan.get("running") and ctl._scan_tuner is None:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"scan still running: scan={ctl.scan} scan_tuner={ctl._scan_tuner}")


class SlowScanRadio(FakeRadio):
    def capture(self, seconds):
        import time
        time.sleep(0.01)
        return b""


@pytest.fixture
async def one_tuner(tmp_path):
    c = Controller(TunerPool([SlowScanRadio(0)]), StationStore(str(tmp_path / "s.json")), idle_timeout=0,
                   make_encoders=fake_encoders(), channel_grace=0)
    await c.start()
    yield c
    await c.close()


async def test_override_of_a_scanning_tuner_moves_its_pre_scan_sessions(one_tuner):
    ctl = one_tuner
    a = await listen(ctl, "a", 97.1)
    q = await ctl.channel(a.key).ts.subscribe()
    await ctl.start_scan(client_id="b", override=True)
    await asyncio.sleep(0.05)
    await ctl.tune(101.9, client_id="c", override=True)
    assert ctl.sessions["a"] == ChannelKey(0, 101.9, 0)
    assert q.get_nowait() == b""  # per-channel stream on the old station ended
    await wait_scan_done(ctl)
    assert radios(ctl)[0].state.frequency == 101.9  # the scan did not restore over the takeover


async def test_cancelled_scan_restores_the_station(one_tuner):
    ctl = one_tuner
    await listen(ctl, "a", 97.1)
    await ctl.start_scan(client_id="b", override=True)
    await asyncio.sleep(0.05)
    ctl.cancel_scan()
    await wait_scan_done(ctl)
    r = radios(ctl)[0]
    assert r.state.running and r.state.frequency == 97.1


async def test_double_scan_request_starts_one_scan(ctl):
    async with ctl._tune_lock:  # e.g. a retune is in progress when a double-click arrives
        both = asyncio.gather(ctl.start_scan(client_id="x"), ctl.start_scan(client_id="y"))
        await asyncio.sleep(0.01)
    await both
    await wait_scan_done(ctl)
    scanned = [r for r in radios(ctl) if len(r.tunes) > 50]
    assert len(scanned) == 1 and len(scanned[0].tunes) == 101


class HangingRadio(FakeRadio):
    """A tuner whose control call never returns (dongle stopped delivering samples)."""
    def __init__(self, index=0):
        super().__init__(index)
        import threading
        self.release = threading.Event()

    def tune(self, frequency):
        self.release.wait(5)
        super().tune(frequency)


async def test_hung_tuner_answers_unavailable_instead_of_hanging(tmp_path):
    radio = HangingRadio(0)
    ctl = Controller(TunerPool([radio]), StationStore(str(tmp_path / "s.json")), make_encoders=fake_encoders(),
                     tuner_timeout=0.2)
    await ctl.start()
    with pytest.raises(RuntimeError, match="not responding"):
        await asyncio.wait_for(ctl.tune(97.1, client_id="a"), 2)
    with pytest.raises(RuntimeError):  # still answers (doesn't queue forever) on the next try
        await asyncio.wait_for(ctl.tune(99.5, client_id="b"), 2)
    radio.release.set()
    await ctl.close()


async def test_stalled_tuner_is_reported_once(tmp_path):
    radio = FakeRadio(0)
    stalled = []
    ctl = Controller(TunerPool([radio]), StationStore(str(tmp_path / "s.json")), make_encoders=fake_encoders(),
                     on_tuner_stalled=stalled.append)
    await ctl.tune(97.1, client_id="a")
    radio.stalled = True
    ctl.check_tuners()
    ctl.check_tuners()
    assert stalled == [radio]
    assert "stopped" in ctl.status("a")["error"]


def fake_scan(found, seen=None, step=0.01):
    """A stand-in for run_scan: walks 'channels' slowly, honouring cancellation."""
    import time

    def scan(radio, progress, cancelled, **kw):
        for f in (88.1, 92.3, 98.7, 103.5):
            if cancelled():
                return []
            radio.tune(f)
            if seen is not None:
                seen.append(radio.index)
            time.sleep(step)
        return found
    return scan


async def make_ctl(tmp_path, n=1, scan=None, **kw):
    c = Controller(TunerPool([FakeRadio(i) for i in range(n)]), StationStore(str(tmp_path / "s.json")),
                   make_encoders=fake_encoders(), channel_grace=0, scan_fn=scan, **kw)
    await c.start()
    return c


async def test_background_scan_uses_a_free_tuner_and_only_adds(tmp_path):
    from freqon.stations import Station
    seen = []
    ctl = await make_ctl(tmp_path, n=2, scan=fake_scan([Station(98.7, name="WMGC", hd=True,
                                                               programs=[{"program": 0, "name": "HD1"}])], seen))
    ctl.stations.upsert(Station(97.1, name="WXYT"))
    await listen(ctl, "a", 97.1)  # tuner 0 busy with a listener
    assert await ctl.auto_scan_once() is True
    assert set(seen) == {1}  # only the free tuner scanned
    assert {s.frequency for s in ctl.stations.all()} == {97.1, 98.7}
    assert radios(ctl)[0].tunes == [97.1] and not radios(ctl)[1].state.running
    assert ctl.status("a")["scan"].get("running") is not True  # background scans don't show as a user scan
    await ctl.close()


async def test_background_scan_skipped_when_every_tuner_is_in_use(tmp_path):
    seen = []
    ctl = await make_ctl(tmp_path, n=1, scan=fake_scan([], seen))
    await listen(ctl, "a", 97.1)
    assert await ctl.auto_scan_once() is False
    assert seen == [] and radios(ctl)[0].tunes == [97.1]
    await ctl.close()


async def test_listening_during_a_background_scan_wins_without_busy(tmp_path):
    from freqon.stations import Station
    ctl = await make_ctl(tmp_path, n=1, scan=fake_scan([Station(98.7)], step=0.2))
    task = asyncio.create_task(ctl.auto_scan_once())
    await asyncio.sleep(0.05)
    st = await asyncio.wait_for(ctl.tune(101.9, client_id="a"), 3)  # no Busy, no long wait
    assert st["frequency"] == 101.9 and radios(ctl)[0].state.frequency == 101.9
    await task
    assert radios(ctl)[0].state.frequency == 101.9 and radios(ctl)[0].state.running  # scan didn't undo it
    assert ctl.stations.get(98.7) is None  # cancelled scan adds nothing
    await ctl.close()


async def test_user_scan_takes_over_a_background_scan(tmp_path):
    ctl = await make_ctl(tmp_path, n=1, scan=fake_scan([], step=0.2))
    task = asyncio.create_task(ctl.auto_scan_once())
    await asyncio.sleep(0.05)
    st = await asyncio.wait_for(ctl.start_scan(client_id="a"), 3)
    assert st["scan"]["running"] is True
    await task
    await ctl.close()


async def test_user_scan_may_run_longer_than_the_tuner_timeout(tmp_path):
    ctl = await make_ctl(tmp_path, n=1, scan=fake_scan([], step=0.1), tuner_timeout=0.1)  # scan takes ~0.4 s
    await ctl.start_scan(client_id="a")
    for _ in range(100):
        if not ctl.scan.get("running"):
            break
        await asyncio.sleep(0.02)
    assert ctl.scan.get("finished") and not ctl.scan.get("error")
    await ctl.close()


class Clock:
    def __init__(self, t=1_794_000_000.0):  # a realistic wall-clock time
        self.t = t

    def __call__(self):
        return self.t


DAY, HOUR = 86400.0, 3600.0


async def scheduled_ctl(tmp_path, n=1, seen=None, clock=None):
    return await make_ctl(tmp_path, n=n, scan=fake_scan([], seen, step=0), auto_scan_idle_days=30,
                          auto_scan_active_hours=1, clock=clock, state_path=str(tmp_path / "state.json"))


async def test_idle_scans_every_30_days(tmp_path):
    seen, clock = [], Clock()
    ctl = await scheduled_ctl(tmp_path, seen=seen, clock=clock)
    ctl.last_scan = clock.t - 29 * DAY
    assert await ctl.auto_scan_tick() is False and not seen
    clock.t += 2 * DAY
    assert await ctl.auto_scan_tick() is True and seen
    assert ctl.last_scan == clock.t
    await ctl.close()


async def test_in_use_scans_hourly_on_a_free_tuner(tmp_path):
    seen, clock = [], Clock()
    ctl = await scheduled_ctl(tmp_path, n=2, seen=seen, clock=clock)
    ctl.last_scan = clock.t - 59 * 60
    await listen(ctl, "a", 97.1)
    assert await ctl.auto_scan_tick() is False and not seen
    clock.t += 2 * 60
    assert await ctl.auto_scan_tick() is True and set(seen) == {1}  # the free tuner, not a's
    await ctl.close()


async def test_hourly_scan_waits_for_the_only_tuner_then_runs_after_listening(tmp_path):
    seen, clock = [], Clock()
    ctl = await scheduled_ctl(tmp_path, n=1, seen=seen, clock=clock)
    ctl.last_scan = clock.t - 2 * HOUR
    lst = await listen(ctl, "a", 97.1)
    assert await ctl.auto_scan_tick() is False and not seen  # due, but the only tuner is in use
    assert ctl.last_scan == clock.t - 2 * HOUR  # a skipped attempt doesn't count as a scan
    ctl.remove_listener(lst)
    await asyncio.sleep(0.01)
    clock.t += 10 * 60  # listened 10 minutes ago: still "in use", so the hourly rule applies
    assert await ctl.auto_scan_tick() is True and seen
    await ctl.close()


async def test_last_scan_time_survives_a_restart(tmp_path):
    clock = Clock()
    ctl = await scheduled_ctl(tmp_path, clock=clock)
    ctl.last_scan = 0.0
    assert await ctl.auto_scan_tick() is True
    await ctl.close()
    again = await scheduled_ctl(tmp_path, clock=clock)
    assert again.last_scan == clock.t
    assert await again.auto_scan_tick() is False  # not due again for 30 days
    await again.close()


async def test_user_scan_counts_as_the_last_scan(tmp_path):
    clock = Clock()
    ctl = await scheduled_ctl(tmp_path, clock=clock)
    ctl.last_scan = 0.0
    await ctl.start_scan(client_id="a")
    for _ in range(100):
        if not ctl.scan.get("running"):
            break
        await asyncio.sleep(0.02)
    assert ctl.last_scan == clock.t
    await ctl.close()


async def test_calibration_is_saved_per_dongle_and_applied_next_time(tmp_path):
    from freqon.radio import SourceConfig
    state = str(tmp_path / "state.json")
    radio = FakeRadio(0)
    radio.source_cfg = SourceConfig(serial="KITCHEN", ppm=0, ppm_auto=True)
    ctl = Controller(TunerPool([radio]), StationStore(str(tmp_path / "s.json")), make_encoders=fake_encoders(),
                     state_path=state)
    await ctl.tune(97.1, client_id="a")
    radio.extras.sync_offset(97.1e6, 971.0, applied_ppm=3)  # opened with ppm 3, still 10 ppm off -> -7
    ctl.check_tuners()
    assert radio.source_cfg.ppm == 0  # one station isn't enough to trust
    radio.state.frequency = 101.9
    radio.extras.sync_offset(101.9e6, 1019.0 * 1.1, applied_ppm=3)  # a second station: -8 absolute
    ctl.check_tuners()
    assert radio.source_cfg.ppm in (-7, -8)
    fresh = FakeRadio(0)
    fresh.source_cfg = SourceConfig(serial="KITCHEN", ppm=0, ppm_auto=True)
    Controller(TunerPool([fresh]), StationStore(str(tmp_path / "s.json")), make_encoders=fake_encoders(),
               state_path=state)
    assert fresh.source_cfg.ppm in (-7, -8)
    manual = FakeRadio(0)
    manual.source_cfg = SourceConfig(serial="KITCHEN", ppm=5, ppm_auto=False)  # explicit ppm wins
    Controller(TunerPool([manual]), StationStore(str(tmp_path / "s.json")), make_encoders=fake_encoders(),
               state_path=state)
    assert manual.source_cfg.ppm == 5


async def test_calibration_compares_absolute_values_across_corrections(tmp_path):
    from freqon.radio import SourceConfig
    radio = FakeRadio(0)
    radio.source_cfg = SourceConfig(serial="X", ppm_auto=True)
    ctl = Controller(TunerPool([radio]), StationStore(str(tmp_path / "s.json")), make_encoders=fake_encoders(),
                     state_path=str(tmp_path / "state.json"))
    await ctl.tune(97.1, client_id="a")
    radio.extras.sync_offset(97.1e6, 97.1 * 20, applied_ppm=0)  # -20 absolute
    ctl.check_tuners()
    radio.state.frequency = 101.9
    radio.extras.sync_offset(101.9e6, 0.0, applied_ppm=-20)  # already corrected: also -20 absolute
    ctl.check_tuners()
    assert radio.source_cfg.ppm == -20


async def test_calibration_ignores_implausible_or_disagreeing_stations(tmp_path):
    from freqon.radio import SourceConfig
    radio = FakeRadio(0)
    radio.source_cfg = SourceConfig(serial="X", ppm_auto=True)
    ctl = Controller(TunerPool([radio]), StationStore(str(tmp_path / "s.json")), make_encoders=fake_encoders(),
                     state_path=str(tmp_path / "state.json"))
    await ctl.tune(97.1, client_id="a")
    for freq, offset in ((97.1, 97.1 * 400), (101.9, 101.9 * 400)):  # 400 ppm: no real dongle is that far off
        radio.state.frequency = freq
        radio.extras.sync_offset(freq * 1e6, offset, applied_ppm=0)
        ctl.check_tuners()
    assert radio.source_cfg.ppm == 0
    for freq, ppm_off in ((99.5, 10), (103.1, 40)):  # two stations that disagree
        radio.state.frequency = freq
        radio.extras.sync_offset(freq * 1e6, freq * ppm_off, applied_ppm=0)
        ctl.check_tuners()
    assert radio.source_cfg.ppm == 0


async def test_late_scan_progress_never_reopens_a_finished_scan(tmp_path):
    # Progress comes from the scan's thread through the event loop. If the scan finishes
    # before those updates are applied (seen with a 1.5 ms fake scan on a busy machine, when
    # asyncio completes run_in_executor without queueing), they must not mark it running again.
    import threading
    captured = []

    def scan(radio, progress, cancelled, **kw):
        captured.append(progress)
        return []

    ctl = await make_ctl(tmp_path, scan=scan)
    await ctl.start_scan(client_id="c")
    await wait_scan_done(ctl)
    t = threading.Thread(target=captured[0], args=({"phase": "sweep", "done": 101, "total": 101},))
    t.start()
    t.join()
    await asyncio.sleep(0.05)  # let the late update reach the loop
    assert ctl.scan.get("running") is False and ctl.scan.get("finished")
    await ctl.close()
