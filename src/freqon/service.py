"""The controller: shares the tuners between clients and drives everything else.

Words
    * A *channel* is one HD program of one station on one tuner: ChannelKey(tuner, MHz, program).
      Each channel has its own mixer (in the Radio), its own digital/analog mode and its own
      encoders, created when someone listens.
    * Each client has a *session*: the channel it last tuned. /stream.mp3 plays your session's
      channel and follows it when you retune.
    * A *holder* of a tuner is anyone actively listening (an open stream) to a channel on it.

Sharing rules (see pool.py for how a tuner is picked)
    * Any HD program of a station that is already tuned can be joined freely.
    * Otherwise a free tuner is used; if none is free the request fails with Busy, unless
      override=True, which bumps the tuner tuned longest ago. Clients following that tuner
      (/stream.mp3) move to the new station; per-channel streams on it (Plex) end.
    * Switching digital/analog mode never needs an override.
    * A tuner with no listeners for `idle_timeout` seconds is released.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import itertools
import json
import logging
import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import NamedTuple

from .mixer import MODES
from .pool import NoTunerFree, TunerPool
from .radio import validate_frequency
from .scan import run_scan
from .stations import Station, StationStore, clean_name
from .streams import MP3Encoder, TSEncoder
from .video import Camera, VideoComposer

log = logging.getLogger(__name__)

SCAN_HOLDER = {"client_id": "scan", "name": "a station scan", "kind": "scan"}
_NO_FREQUENCY = -1.0  # matches no tuner, so a scan never "joins" a tuned station


class TunerNotResponding(RuntimeError):
    pass


class ChannelKey(NamedTuple):
    tuner: int
    frequency: float
    program: int

    @property
    def channel_id(self) -> str:
        return f"{round(self.frequency * 10)}.{self.program + 1}"


class Busy(Exception):
    def __init__(self, tuners: list[dict], holders: list[dict], status: dict | None = None,
                 can_take_over: bool = True):
        super().__init__("tuner busy")
        self.tuners = tuners  # [{"tuner", "frequency", "station", "holders": [names]}]
        self.holders = holders  # listener dicts of everyone in the way
        self.status = status or {}
        self.can_take_over = can_take_over  # False: takeovers are turned off

    def to_dict(self) -> dict:
        parts = []
        for t in self.tuners:
            who = ", ".join(t["holders"]) or "another client"
            parts.append(f"{t['station'] or '?'} for {who}")
        prefix = "The tuner is playing " if len(self.tuners) == 1 else "Every tuner is busy: "
        message = prefix + "; ".join(parts) + "."
        if not self.can_take_over:
            message += " Taking over a tuner is turned off."
        return {
            "error": "busy",
            "message": message,
            "can_take_over": self.can_take_over,
            "holders": self.holders,
            "tuners": self.tuners,
            "status": self.status,
        }


@dataclass
class Listener:
    id: int
    client_id: str
    name: str
    kind: str  # web / plex / jellyfin / other
    key: ChannelKey
    since: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {"client_id": self.client_id, "name": self.name, "kind": self.kind, "since": self.since,
                "tuner": self.key.tuner, "channel_id": self.key.channel_id}


@dataclass
class Channel:
    key: ChannelKey
    mp3: object
    ts: object
    listeners: dict[int, Listener] = field(default_factory=dict)

    @property
    def encoders(self):
        return (self.mp3, self.ts)

    def end_streams(self) -> None:
        for enc in self.encoders:
            enc.end_listeners()


class Controller:
    def __init__(self, pool: TunerPool, stations: StationStore, *, idle_timeout: float = 120.0,
                 ffmpeg: str = "ffmpeg", scan_options: dict | None = None,
                 make_encoders: Callable[[ChannelKey], tuple] | None = None, channel_grace: float = 5.0,
                 video: dict | None = None, tuner_timeout: float = 20.0,
                 on_tuner_stalled: Callable | None = None, scan_fn: Callable | None = None,
                 auto_scan_idle_days: float = 0, auto_scan_active_hours: float = 0,
                 state_path: str | None = None, clock: Callable[[], float] | None = None,
                 logos_dir: str | None = None, allow_takeovers: bool = True):
        """`video`: {"source", "size": (w, h), "fps", "encoder", "bitrate"} to use a camera as the
        TS picture; without a source the picture is the still 'now playing' card."""
        self.pool = pool
        self.stations = stations
        self.idle_timeout = idle_timeout
        self.scan_options = scan_options or {}
        self.ffmpeg = ffmpeg
        self.scan_fn = scan_fn or run_scan
        # Background add-only scans on a free tuner: every `auto_scan_active_hours` while the radio
        # is in use (a stream open within the last hour), else every `auto_scan_idle_days` (0 = off).
        self.auto_scan_idle_days = auto_scan_idle_days
        self.auto_scan_active_hours = auto_scan_active_hours
        self.clock = clock or time.time
        self.state_path = state_path
        self.logos_dir = logos_dir  # broadcast station logos are kept here across restarts
        state = self._load_state()
        self.last_scan = state.get("last_scan", 0.0)  # wall-clock time of the last full scan
        self._ppm: dict[str, int] = dict(state.get("ppm", {}))  # measured correction per dongle
        self._ppm_seen: dict[str, dict[float, float]] = {}  # dongle -> {station Hz: measured ppm}
        for r in pool:
            cfg = getattr(r, "source_cfg", None)
            if cfg is not None and cfg.ppm_auto and cfg.key in self._ppm:
                cfg.ppm = self._ppm[cfg.key]
        self._last_listening = 0.0
        self._scan_background = False  # the current scan is a background one (yields to listeners)
        self._background_done = asyncio.Event()
        self._background_done.set()
        self._auto_task: asyncio.Task | None = None
        self.tuner_timeout = tuner_timeout  # a tuner call taking longer than this is reported, not waited on
        self.on_tuner_stalled = on_tuner_stalled
        self._stalled: set[int] = set()
        self.channel_grace = channel_grace  # keep a channel's encoders this long after its last listener
        self._make_encoders = make_encoders or self._default_encoders
        self.channels: dict[ChannelKey, Channel] = {}
        self.sessions: dict[str, ChannelKey] = {}
        self._session_waiters: dict[str, set[asyncio.Event]] = {}
        self._ids = itertools.count(1)
        self._executors = {r.index: ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"freqon-tuner{r.index}")
                           for r in pool}
        self._tune_lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: dict[asyncio.Queue, str | None] = {}
        self._notify_pending = False
        self._activity = {r.index: time.monotonic() for r in pool}
        self._idle_task: asyncio.Task | None = None
        self.scan: dict = {"running": False}
        self.allow_takeovers = allow_takeovers  # False: a busy tuner is never taken from anyone
        self._scan_number = 0  # which scan the progress updates in flight belong to
        self._scan_tuner: int | None = None
        self._scan_prev: float | None = None  # what the scanning tuner played before the scan
        self._scan_taken = False  # an override took the scanning tuner
        self._scan_cancel = False
        self.video = video if video and video.get("source") else None
        self.camera: Camera | None = None
        if self.video:
            self.camera = Camera(self.video["source"], tuple(self.video["size"]), int(self.video["fps"]),
                                 ffmpeg=ffmpeg)
        self._video_streams = 0  # TS encoders running; the camera runs while this is > 0
        self._camera_on = False
        for r in pool:
            r.on_pcm = lambda program, pcm, idx=r.index: self._pcm_from_thread(idx, program, pcm)
            r.on_change = self._change_from_thread

    def _default_encoders(self, key: ChannelKey) -> tuple:
        def status():
            return self.channel_status(key)

        if self.video:
            size = tuple(self.video["size"])
            comp = VideoComposer(self.camera, size, status, art_fn=lambda: self.art(key.frequency, key.program))
            ts = TSEncoder(comp.frame, size=size, fps=int(self.video["fps"]), video_encoder=self.video["encoder"],
                           video_bitrate=self.video["bitrate"], on_active=self._video_active, ffmpeg=self.ffmpeg)
        else:
            ts = TSEncoder(VideoComposer(None, (640, 360), status,
                                         art_fn=lambda: self.art(key.frequency, key.program)).frame,
                           ffmpeg=self.ffmpeg)
        return MP3Encoder(ffmpeg=self.ffmpeg), ts

    def _video_active(self, active: bool) -> None:
        """Start the shared camera with the first video stream, stop it after the last."""
        self._video_streams = max(0, self._video_streams + (1 if active else -1))
        want = self._video_streams > 0
        if self.camera is None or want == self._camera_on:
            return
        self._camera_on = want
        asyncio.ensure_future(self.camera.start() if want else self.camera.stop())

    def radio(self, index: int):
        return self.pool.radios[index]

    # -- lifecycle ------------------------------------------------------------------------------

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._idle_task = asyncio.create_task(self._idle_watch())
        if self.auto_scan_idle_days > 0 or self.auto_scan_active_hours > 0:
            self._auto_task = asyncio.create_task(self._auto_scan_loop())

    async def close(self) -> None:
        if self._idle_task:
            self._idle_task.cancel()
        if self._auto_task:
            self._auto_task.cancel()
        self._scan_cancel = True
        for ch in list(self.channels.values()):
            for enc in ch.encoders:
                enc._listeners.clear()
                await enc.stop()
        if self.camera:
            await self.camera.stop()
        for r in self.pool:
            with contextlib.suppress(TunerNotResponding):
                await self._run(r, r.stop)
        for ex in self._executors.values():
            ex.shutdown(wait=False)

    def end_streams(self) -> None:
        """Finish every open audio/video response (used at shutdown)."""
        for ch in self.channels.values():
            ch.end_streams()

    async def _run(self, radio, fn, *args, timeout: float | None = -1):
        """Run blocking radio work on that tuner's own thread (tuners never wait for each other).
        A tuner that doesn't answer in time raises TunerNotResponding instead of hanging callers.
        (Scans pass timeout=None: they legitimately take a minute or more.)"""
        fut = asyncio.get_running_loop().run_in_executor(self._executors[radio.index], fn, *args)
        try:
            return await asyncio.wait_for(fut, self.tuner_timeout if timeout == -1 else timeout)
        except TimeoutError:
            raise TunerNotResponding(f"tuner {radio.index + 1} is not responding (dongle stalled?)") from None

    def check_tuners(self) -> None:
        """Report a tuner whose dongle stopped delivering samples (once per stall), and save
        any frequency calibration measured since the last check."""
        for r in self.pool:
            self._calibrate(r)
            if r.index != self._scan_tuner:
                self._save_new_logos(r)
            stalled = r.is_stalled() if callable(getattr(r, "is_stalled", None)) else bool(getattr(r, "stalled", False))
            if stalled and r.index not in self._stalled:
                self._stalled.add(r.index)
                r.state.error = "The tuner stopped delivering signal; restarting it."
                log.error("tuner %d: no samples from the dongle; it has stalled", r.index)
                self.notify()
                if self.on_tuner_stalled:
                    self.on_tuner_stalled(r)
            elif not stalled:
                self._stalled.discard(r.index)

    # -- status -------------------------------------------------------------------------------------

    def default_key(self) -> ChannelKey | None:
        """What a client with no session of its own sees: the most-listened channel, else any
        running tuner's first open program."""
        live = [ch for ch in self.channels.values() if ch.listeners and self._current(ch.key)]
        if live:
            return max(live, key=lambda ch: len(ch.listeners)).key
        for r in self.pool:
            if r.state.running and r.index != self._scan_tuner:
                program = min(r.state.mixers, default=0)
                return ChannelKey(r.index, r.state.frequency, program)
        return None

    def key_for(self, client_id: str | None) -> ChannelKey | None:
        key = self.sessions.get(client_id) if client_id else None
        return key or self.default_key()

    def join(self, client_id: str, key: ChannelKey) -> None:
        """Make `key` this client's session (e.g. a new player joining what is already playing)."""
        if client_id:
            self._set_session(client_id, key)

    def is_current(self, key: ChannelKey) -> bool:
        return self._current(key)

    def _current(self, key: ChannelKey) -> bool:
        """Is the key's tuner still on the key's station?"""
        return self.radio(key.tuner).state.frequency == key.frequency

    def channel_status(self, key: ChannelKey | None) -> dict:
        r = self.radio(key.tuner) if key else self.pool.radios[0]
        st = r.state.snapshot(key.program if key else 0)
        if key and not self._current(key):  # tuner was taken for a scan; report the session's station
            st.update(frequency=key.frequency, running=False, source="none")
        st["tuner"] = r.index
        station = self.stations.get(st["frequency"]) if st["frequency"] else None
        if station and station.custom_name:
            st["station"]["display_name"] = station.custom_name
        else:
            st["station"]["display_name"] = st["station"]["name"] or (
                station.display_name if station else (f"{st['frequency']:.1f} FM" if st["frequency"] else None))
        st["channel_id"] = key.channel_id if key else None
        art = self.art(key.frequency, key.program) if key else None
        st["art_key"] = hashlib.sha1(art).hexdigest()[:8] if art else None  # changes with the picture
        return st

    # -- broadcast images ---------------------------------------------------------------------

    def _radio_on(self, frequency: float):
        for r in self.pool:
            if r.state.running and r.state.frequency == frequency and r.index != self._scan_tuner:
                return r
        return None

    def art(self, frequency: float, program: int) -> bytes | None:
        """Album art the station is sending now for this channel (only while it's tuned)."""
        r = self._radio_on(frequency)
        return r.state.extras.art(program) if r is not None else None

    def broadcast_logo(self, frequency: float, program: int) -> bytes | None:
        """The station's own logo: from the air if it's tuned (and saved), else the saved copy."""
        name = f"{round(frequency * 10)}.{program + 1}.png"
        path = os.path.join(self.logos_dir, name) if self.logos_dir else None
        r = self._radio_on(frequency)
        live = r.state.extras.logo(program) if r is not None else None
        if live is not None:
            return live
        if path:
            try:
                with open(path, "rb") as f:
                    return f.read()
            except OSError:
                pass
        return None

    def _save_new_logos(self, r) -> None:
        """Keep logos as they arrive, so they're there after restarts and before a station is
        tuned again (whether or not anyone asked for them)."""
        extras = getattr(r.state, "extras", None)
        if extras is None or not self.logos_dir or not r.state.frequency:
            return
        for program, png in extras.take_new_logos().items():
            path = os.path.join(self.logos_dir, f"{round(r.state.frequency * 10)}.{program + 1}.png")
            try:
                os.makedirs(self.logos_dir, exist_ok=True)
                tmp = path + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(png)
                os.replace(tmp, path)
                log.info("saved the station logo %s", os.path.basename(path))
            except OSError as e:
                log.warning("could not save station logo %s: %s", path, e)

    def map_png(self, kind: str) -> bytes | None:
        for r in self.pool:
            if r.state.running:
                m = r.state.extras.map_png(kind)
                if m:
                    return m
        return None

    def status(self, client_id: str | None = None) -> dict:
        st = self.channel_status(self.key_for(client_id))
        st["listeners"] = [l.to_dict() for ch in self.channels.values() for l in ch.listeners.values()]
        st["tuners"] = [self.tuner_info(r) for r in self.pool]
        st["scan"] = self.scan
        st["takeovers"] = self.allow_takeovers
        return st

    def tuner_info(self, r) -> dict:
        return {"index": r.index, "running": r.state.running, "frequency": r.state.frequency,
                "programs_open": sorted(r.state.mixers),
                "listeners": sum(len(ch.listeners) for ch in self.channels.values() if ch.key.tuner == r.index),
                "scanning": r.index == self._scan_tuner}

    def subscribe(self, client_id: str | None = None) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=16)
        self._subscribers[q] = client_id
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.pop(q, None)

    def notify(self) -> None:
        """Coalesce bursts of changes into one status push every ~250 ms."""
        if self._notify_pending or self._loop is None:
            return
        self._notify_pending = True
        self._loop.call_later(0.25, self._push_status)

    def _push_status(self) -> None:
        self._notify_pending = False
        for q, client_id in list(self._subscribers.items()):
            msg = {"type": "status", "status": self.status(client_id)}
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(msg)

    def _change_from_thread(self) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self.notify)

    def _pcm_from_thread(self, tuner: int, program: int, pcm: bytes) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._feed, tuner, program, pcm)

    def _feed(self, tuner: int, program: int, pcm: bytes) -> None:
        ch = self.channels.get(ChannelKey(tuner, self.radio(tuner).state.frequency, program))
        if ch is not None:
            for enc in ch.encoders:
                enc.feed(pcm)

    # -- sessions -----------------------------------------------------------------------------------

    def session_changed(self, client_id: str) -> asyncio.Event:
        """An event that is set the next time this client's session moves to another channel."""
        ev = asyncio.Event()
        self._session_waiters.setdefault(client_id, set()).add(ev)
        return ev

    def _set_session(self, client_id: str, key: ChannelKey) -> None:
        if self.sessions.get(client_id) == key:
            return
        self.sessions[client_id] = key
        for ev in self._session_waiters.pop(client_id, set()):
            ev.set()

    # -- tuner sharing ------------------------------------------------------------------------------

    def _holders(self) -> dict[int, set[str]]:
        h: dict[int, set[str]] = {}
        for ch in self.channels.values():
            for l in ch.listeners.values():
                h.setdefault(ch.key.tuner, set()).add(l.client_id)
        return h

    def _busy(self, busy: list[dict]) -> Busy:
        tuners, holders = [], []
        for b in busy:
            names = sorted({l.name for ch in self.channels.values() if ch.key.tuner == b["tuner"]
                            for l in ch.listeners.values()})
            holders += [l.to_dict() for ch in self.channels.values() if ch.key.tuner == b["tuner"]
                        for l in ch.listeners.values()]
            if b.get("reserved"):
                names, station = [SCAN_HOLDER["name"]], "a station scan"
                holders.append(SCAN_HOLDER)
            else:
                st = self.stations.get(b["frequency"]) if b["frequency"] else None
                station = (st.display_name if st else f"{b['frequency']:.1f} FM") if b["frequency"] else None
            tuners.append({"tuner": b["tuner"], "frequency": b["frequency"], "station": station, "holders": names})
        return Busy(tuners, holders, can_take_over=self.allow_takeovers)

    async def tune(self, frequency: float, program: int = 0, *, client_id: str = "",
                   override: bool = False, prefer_idle: bool = False) -> dict:
        """`prefer_idle`: for an identity shared by several viewers (Plex), use a free tuner
        before retuning the one this identity already holds."""
        frequency = validate_frequency(frequency)
        program = int(program)
        if not 0 <= program <= 7:
            raise ValueError("program must be 0-7 (HD1-HD8)")
        async with self._tune_lock:
            holders = self._holders()
            own = self.sessions.get(client_id)
            if own is not None and client_id:
                # Your own (unlistened) session counts as yours, so retuning reuses that tuner.
                holders.setdefault(own.tuner, set()).add(client_id)
            # A user's scan holds its tuner; a background scan gives way to anyone who wants to listen.
            reserved = {self._scan_tuner} if self._scan_tuner is not None and not self._scan_background else set()
            try:
                radio = self.pool.choose(frequency, client_id, holders, override and self.allow_takeovers,
                                         reserved, prefer_idle)
            except NoTunerFree as e:
                busy = self._busy(e.busy)
                busy.status = self.status(client_id)
                raise busy from None
            if radio.index == self._scan_tuner:
                # Took the scanning tuner (override, or a background scan giving way): stop the
                # scan, and treat the station it played before the scan as the one being bumped.
                self._scan_cancel = True
                self._scan_taken = True
                old = self._scan_prev
            else:
                old = radio.state.frequency if radio.state.running else None
            self._activity[radio.index] = time.monotonic()
            await self._run(radio, radio.tune, frequency)
            key = ChannelKey(radio.index, frequency, program)
            if old is not None and old != frequency:
                self._bump(radio.index, old, key)
            self._set_session(client_id, key)
            self._sync_programs(radio)
            if own is not None and own.tuner != radio.index:
                self._sync_programs(self.radio(own.tuner))
        self.notify()
        return self.status(client_id)

    def _bump(self, tuner: int, old_frequency: float, new_key: ChannelKey) -> None:
        """The tuner left `old_frequency`: move sessions on it, end its per-channel streams."""
        for cid, key in list(self.sessions.items()):
            if key.tuner == tuner and key.frequency == old_frequency:
                self._set_session(cid, new_key)
        for key, ch in list(self.channels.items()):
            if key.tuner == tuner and key.frequency == old_frequency:
                ch.end_streams()

    def _sync_programs(self, radio) -> None:
        """Open exactly the programs someone is using on this tuner's current station."""
        f = radio.state.frequency
        want = {k.program for k in self.sessions.values() if k.tuner == radio.index and k.frequency == f}
        want |= {k.program for k, ch in self.channels.items()
                 if k.tuner == radio.index and k.frequency == f and ch.listeners}
        for p in set(radio.state.mixers) - want:
            radio.close_program(p)
        for p in want:
            radio.open_program(p)

    def add_station(self, frequency: float) -> Station:
        """Put a station on the list (for ones a scan missed). If a tuner is on it now, what it
        has decoded (name, HD channels) is saved too. A user's rename is kept."""
        frequency = validate_frequency(frequency)
        st = Station(frequency=frequency)
        live = next((r for r in self.pool if r.state.running and r.state.frequency == frequency
                     and r.index != self._scan_tuner), None)
        if live is not None:
            s = live.state
            st.name = clean_name(s.station_name) or None
            st.programs = [{"program": p, **{k: (clean_name(v) if isinstance(v, str) else v) for k, v in info.items()}}
                           for p, info in sorted(s.programs.items())]
            st.hd = bool(s.synced or st.programs)
            st.power_db = s.rf_power_db
        prev = self.stations.get(frequency)
        if prev is not None:
            st.custom_name = prev.custom_name
            if live is None:  # nothing new heard; keep what the scan found
                st = Station(**{**prev.__dict__, "hidden": False})
        self.stations.upsert(st)
        self.notify()
        return st

    async def set_mode(self, mode: str, *, client_id: str = "") -> dict:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        key = self.key_for(client_id)
        if key is None:
            raise ValueError("nothing is tuned")
        self.radio(key.tuner).set_mode(key.program, mode)
        self.notify()
        return self.status(client_id)

    # -- channels and listeners --------------------------------------------------------------------

    def channel(self, key: ChannelKey) -> Channel:
        ch = self.channels.get(key)
        if ch is None:
            mp3, ts = self._make_encoders(key)
            ch = self.channels[key] = Channel(key, mp3, ts)
        return ch

    def add_listener(self, client_id: str, name: str, kind: str, key: ChannelKey | None = None) -> Listener:
        key = key or self.key_for(client_id)
        if key is None:
            raise ValueError("nothing is tuned")
        self._last_listening = self.clock()
        lst = Listener(next(self._ids), client_id, name, kind, key)
        self.channel(key).listeners[lst.id] = lst
        self._sync_programs(self.radio(key.tuner))
        self._activity[key.tuner] = time.monotonic()
        self.notify()
        return lst

    def move_listener(self, lst: Listener, key: ChannelKey) -> None:
        """A following stream switched channel along with its client's session."""
        self._detach(lst)
        lst.key = key
        self.channel(key).listeners[lst.id] = lst
        self._sync_programs(self.radio(key.tuner))
        self.notify()

    def remove_listener(self, lst: Listener) -> None:
        self._detach(lst)
        self.notify()

    def _detach(self, lst: Listener) -> None:
        ch = self.channels.get(lst.key)
        if ch is None:
            return
        ch.listeners.pop(lst.id, None)
        self._last_listening = self.clock()
        self._activity[lst.key.tuner] = time.monotonic()
        if not ch.listeners and self._loop is not None:
            self._loop.call_later(self.channel_grace, lambda: asyncio.ensure_future(self._retire(ch)))

    async def _retire(self, ch: Channel) -> None:
        """Stop a channel nobody has listened to for channel_grace seconds."""
        if ch.listeners or self.channels.get(ch.key) is not ch:
            return
        for enc in ch.encoders:
            await enc.stop()
        if ch.listeners:  # someone arrived while encoders were stopping
            return
        del self.channels[ch.key]
        if self._current(ch.key):
            self._sync_programs(self.radio(ch.key.tuner))

    async def release_idle(self) -> None:
        holders = self._holders()
        now = time.monotonic()
        for r in self.pool:
            if (r.state.running and not holders.get(r.index) and r.index != self._scan_tuner
                    and now - self._activity[r.index] >= self.idle_timeout):
                log.info("tuner %d: no listeners for %ss; releasing it", r.index, self.idle_timeout)
                await self._run(r, r.stop)
                self.notify()

    async def _idle_watch(self) -> None:
        while True:
            await asyncio.sleep(5)
            self.check_tuners()
            with contextlib.suppress(TunerNotResponding):
                await self.release_idle()

    # -- scanning ----------------------------------------------------------------------------------

    async def start_scan(self, *, client_id: str = "", override: bool = False) -> dict:
        if self.scan.get("running"):
            return self.status(client_id)
        if self._scan_background and self._scan_tuner is not None:
            self._scan_cancel = True  # a user's scan replaces a background one
            await self._background_done.wait()
        async with self._tune_lock:
            if self.scan.get("running"):  # another request started one while we waited
                return self.status(client_id)
            try:
                radio = self.pool.choose(_NO_FREQUENCY, client_id, self._holders(),
                                         override and self.allow_takeovers)
            except NoTunerFree as e:
                busy = self._busy(e.busy)
                busy.status = self.status(client_id)
                raise busy from None
            self._scan_tuner = radio.index
            self._scan_prev = radio.state.frequency if radio.state.running else None
            self._scan_taken = False
            self._scan_cancel = False
            self.scan = {"running": True, "phase": "sweep", "done": 0, "total": 0, "found": [],
                         "tuner": radio.index}
            self._scan_number += 1
        self.notify()
        asyncio.create_task(self._scan_task(radio))
        return self.status(client_id)

    async def _scan_task(self, radio) -> None:
        prev = self._scan_prev
        loop = asyncio.get_running_loop()
        number = self._scan_number

        def progress(p: dict) -> None:
            def apply():
                # Updates travel from the scan's thread through the loop and can arrive after
                # the scan has ended; they must never mark it running again.
                if number != self._scan_number or not self.scan.get("running"):
                    return
                found = self.scan.get("found", [])
                if "found" in p:
                    found = [*found, p["found"]]
                self.scan = {**self.scan, **p, "found": found, "running": True}
                self.notify()
            loop.call_soon_threadsafe(apply)

        try:
            found = await self._run(radio, lambda: self.scan_fn(
                radio, progress, cancelled=lambda: self._scan_cancel, **self.scan_options), timeout=None)
            if self._scan_cancel:
                self.scan = {"running": False, "cancelled": True}
            else:
                self.stations.merge_scan(found)
                self._scanned()
                self.scan = {"running": False, "finished": time.time(),
                             "found": [{"frequency": s.frequency, "name": s.name, "hd": s.hd} for s in found]}
            if self._scan_tuner == radio.index and not self._scan_taken:
                # Give the tuner back: the pre-scan station if anyone still wants it, else off.
                if prev is not None and self._holders().get(radio.index):
                    await self._run(radio, radio.tune, prev)
                    self._sync_programs(radio)
                elif not self._holders().get(radio.index):
                    await self._run(radio, radio.stop)
        except Exception as e:
            log.exception("scan failed")
            self.scan = {"running": False, "error": str(e)}
        finally:
            if self._scan_tuner == radio.index:
                self._scan_tuner = None
            self.notify()

    def cancel_scan(self) -> None:
        if not self._scan_background:
            self._scan_cancel = True

    # -- background scans --------------------------------------------------------------------

    def _load_state(self) -> dict:
        if not self.state_path or not os.path.exists(self.state_path):
            return {}
        try:
            with open(self.state_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _scanned(self) -> None:
        """A full scan finished: remember when (survives restarts)."""
        self.last_scan = self.clock()
        self._save_state()

    def _save_state(self) -> None:
        if not self.state_path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.state_path)), exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"last_scan": self.last_scan, "ppm": self._ppm}, f)
        os.replace(tmp, self.state_path)

    def _calibrate(self, r) -> None:
        """Turn the HD frequency offset measured on this tuner into a saved ppm correction,
        used the next time the dongle opens (never mid-stream)."""
        cfg = getattr(r, "source_cfg", None)
        extras = getattr(r.state, "extras", None)
        if cfg is None or extras is None or not cfg.ppm_auto:
            return
        m = extras.ppm_measurement()
        if m is None:
            return
        freq, needed = m  # absolute correction this dongle needs, as measured on this station
        seen = self._ppm_seen.setdefault(cfg.key, {})
        seen[freq] = needed
        # Trust it only when another station agrees (within 3 ppm): one off-frequency or bogus
        # transmitter mustn't be able to detune the dongle.
        agree = [d for f, d in seen.items() if f != freq and abs(d - needed) <= 3]
        if not agree:
            return
        total = round((needed + agree[0]) / 2)
        if abs(total) > 150:
            return
        if self._ppm.get(cfg.key) != total:
            log.info("tuner %d (%s): measured frequency error; correction %+d ppm from next start",
                     r.index, cfg.key, total)
            self._ppm[cfg.key] = total
            cfg.ppm = total
            self._save_state()

    def in_use(self) -> bool:
        """Someone is listening, or was within the last hour."""
        if any(ch.listeners for ch in self.channels.values()):
            self._last_listening = self.clock()
            return True
        return self.clock() - self._last_listening < 3600

    def scan_interval(self) -> float | None:
        """Seconds between background scans right now (None = off)."""
        if self.in_use() and self.auto_scan_active_hours > 0:
            return self.auto_scan_active_hours * 3600
        if self.auto_scan_idle_days > 0:
            return self.auto_scan_idle_days * 86400
        return None

    async def auto_scan_tick(self) -> bool:
        """Run a background scan if one is due. True if a scan completed."""
        interval = self.scan_interval()
        if interval is None or self.clock() - self.last_scan < interval:
            return False
        return await self.auto_scan_once()

    async def _auto_scan_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await self.auto_scan_tick()
            except Exception:
                log.exception("background scan failed")

    async def auto_scan_once(self) -> bool:
        """Sweep the band on a free tuner and add anything new (never removes). Returns False
        when no tuner was free or the scan gave way to a listener."""
        if self._scan_tuner is not None:
            return False
        async with self._tune_lock:
            try:
                radio = self.pool.choose(_NO_FREQUENCY, "background-scan", self._holders(), False)
            except NoTunerFree:
                return False
            if self._holders().get(radio.index):
                return False
            self._scan_tuner = radio.index
            self._scan_prev = None
            self._scan_background = True
            self._scan_taken = False
            self._scan_cancel = False
            self._background_done.clear()
        self.notify()
        try:
            found = await self._run(radio, lambda: self.scan_fn(
                radio, lambda p: None, cancelled=lambda: self._scan_cancel, **self.scan_options), timeout=None)
            completed = not self._scan_cancel
            if completed:
                self._scanned()
                added = self.stations.add_found(found)
                if added:
                    log.info("background scan added %d channel(s): %s", len(added), ", ".join(added))
            if not self._scan_taken and not self._holders().get(radio.index):
                await self._run(radio, radio.stop)
        finally:
            self._scan_tuner = None
            self._scan_background = False
            self._background_done.set()
            self.notify()
        return completed
