"""The radio engine: one tuner, decoded two ways at once.

nrsc5 owns the RTL-SDR (or an rtl_tcp server, or a recorded I/Q file for development).
It decodes HD Radio itself and also hands us every raw I/Q buffer it reads; a DSP thread
runs those through the analog FM demodulator and the mixer, producing one continuous
44.1 kHz stereo PCM stream regardless of which source is audible.

One tuner on one frequency serves every HD program of that station at once: each open
program has its own Mixer (and so its own digital/analog mode), and on_pcm is called with
(program, pcm) for each of them.

Threads:
    nrsc5 worker   -> _on_event(): metadata, digital audio -> that program's mixer, raw I/Q -> queue
    dsp thread     -> FM demod -> each mixer.mix() -> on_pcm(program, pcm)
    caller         -> tune()/stop()/... (serialised by _control_lock; these block briefly)
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from . import nrsc5
from .extras import Extras
from .fm import INPUT_RATE, FMDemodulator
from .mixer import HDPolicy, Mixer

log = logging.getLogger(__name__)

# The analog DSP works on at least this much I/Q per call (bytes; ~0.1 s). nrsc5 hands over
# small blocks, and per-call overhead on blocks that small costs a Pi 3 a whole core.
DSP_MIN_BATCH = INPUT_RATE * 2 // 10
FM_MIN_MHZ = 87.5
FM_MAX_MHZ = 108.0


@dataclass
class SourceConfig:
    device_index: int = 0
    rtltcp: str | None = None  # "host:port"
    iq_file: str | None = None  # cu8 recording at 1,488,375 S/s (development / testing)
    gain_db: float | None = None  # None = nrsc5 automatic gain
    ppm: int = 0
    serial: str | None = None  # pick the dongle by EEPROM serial instead of device_index
    ppm_auto: bool = False  # ppm comes from calibration (measured from HD signals), not the config

    @property
    def key(self) -> str:
        """Stable name for this dongle (calibration is saved under it)."""
        if self.serial:
            return f"serial:{self.serial}"
        if self.rtltcp:
            return f"rtltcp:{self.rtltcp}"
        if self.iq_file:
            return f"file:{self.iq_file}"
        return f"index:{self.device_index}"


@dataclass
class RadioState:
    running: bool = False
    frequency: float | None = None  # MHz
    synced: bool = False
    station_name: str | None = None
    slogan: str | None = None
    message: str | None = None
    programs: dict[int, dict] = field(default_factory=dict)  # program -> {name, type}
    id3: dict[int, dict] = field(default_factory=dict)  # program -> {title, artist, album, genre}
    mer_db: float | None = None
    ber: float | None = None
    rf_power_db: float | None = None
    gain_db: float | None = None
    error: str | None = None
    mixers: dict[int, Mixer] = field(default_factory=dict, repr=False)  # open programs
    extras: Extras = field(default_factory=Extras, repr=False)  # art, logos, alerts, maps, ...

    def snapshot(self, program: int = 0) -> dict:
        now = self.id3.get(program, {})
        mixer = self.mixers.get(program)
        return {
            "running": self.running,
            "frequency": self.frequency,
            "program": program,
            "mode": mixer.mode if mixer else "auto",
            "source": mixer.source if mixer else "none",
            "digital": {"synced": self.synced, "mer_db": self.mer_db, "ber": self.ber,
                        # share of real HD audio lately; hd_hold: staying on FM after repeated drops
                        "quality": (round(mixer.hd_quality, 2) if mixer and mixer.hd_quality is not None
                                    else None),
                        "hd_hold": bool(mixer and mixer.hd_hold)},
            "station": {"name": self.station_name, "slogan": self.slogan, "message": self.message,
                        "location": ({"latitude": self.extras.location[0], "longitude": self.extras.location[1]}
                                     if self.extras.location else None),
                        "utc_offset": self.extras.utc_offset},
            "programs": [{"program": p, **info} for p, info in sorted(self.programs.items())],
            "now_playing": {k: now.get(k) for k in ("title", "artist", "album", "genre")},
            "rf_power_db": self.rf_power_db,
            "gain_db": self.gain_db,
            "error": self.error,
            "art": self.extras.art(program) is not None,
            "logo": self.extras.logo(program) is not None,
            "alert": self.extras.current_alert(time.time()),
            "audio": self.extras.audio.get(program),
            "maps": [k for k in ("traffic", "weather") if self.extras.map_png(k)],
        }


def validate_frequency(mhz: float) -> float:
    mhz = round(float(mhz), 1)
    if not FM_MIN_MHZ <= mhz <= FM_MAX_MHZ:
        raise ValueError(f"frequency must be between {FM_MIN_MHZ} and {FM_MAX_MHZ} MHz")
    return mhz


class Radio:
    def __init__(
        self,
        source: SourceConfig,
        on_pcm: Callable[[int, bytes], None] = lambda program, pcm: None,
        on_change: Callable[[], None] = lambda: None,
        analog_gain: float = 0.6,
        index: int = 0,
        hd_policy: HDPolicy | None = None,
    ):
        self.index = index
        self.hd_policy = hd_policy  # weak-HD rule for every channel's mixer (None: off)
        self.tuned_at = 0.0  # time.monotonic() of the last retune
        self.last_iq = 0.0  # time.monotonic() of the last sample block from the dongle
        self.lost = False  # nrsc5 reported the dongle gone (unplugged, USB error)
        self.applied_ppm = 0  # frequency correction the current session was opened with
        self.source_cfg = source
        self.on_pcm = on_pcm
        self.on_change = on_change
        self.state = RadioState()
        self.fm = FMDemodulator(gain=analog_gain)
        self._radio: nrsc5.NRSC5 | None = None
        self._control_lock = threading.RLock()
        self._iq: queue.Queue[bytes | None] = queue.Queue(maxsize=200)
        self._dsp_thread: threading.Thread | None = None
        self._file_thread: threading.Thread | None = None
        self._file_stop = threading.Event()
        self._capture: bytearray | None = None
        self._capture_bytes = 0
        self._capture_done = threading.Event()
        self._power_acc: list[float] = []

    # -- public control (blocking; call from a worker thread, not the event loop) -----------

    def tune(self, frequency: float) -> None:
        """Tune the station. Open programs keep their mixers (and modes) but start afresh."""
        frequency = validate_frequency(frequency)
        with self._control_lock:
            if self.state.running and self.state.frequency == frequency:
                return
            self._ensure_open()
            self._retune(frequency)
            for program, mixer in list(self.state.mixers.items()):
                mixer.reset(program)
            self.tuned_at = self.last_iq = time.monotonic()
            self.state.running = True
            self.state.error = None
        self.on_change()

    @property
    def extras(self) -> Extras:
        return self.state.extras

    def is_stalled(self, now: float | None = None, limit: float = 10.0) -> bool:
        """True when the tuner should be running but the dongle has sent nothing for `limit` s.
        (nrsc5 then can't even be stopped: its stop waits for the next sample block.)"""
        if self.lost and self.state.running:
            return True
        if not self.state.running or not self.last_iq:
            return False
        return (now if now is not None else time.monotonic()) - self.last_iq > limit

    def open_program(self, program: int) -> Mixer:
        """Start producing PCM for an HD program (0 = HD1). Opening twice is harmless."""
        if not 0 <= program <= 7:
            raise ValueError("program must be 0-7 (HD1-HD8)")
        mixers = self.state.mixers
        if program not in mixers:
            mixer = Mixer(policy=self.hd_policy)
            mixer.reset(program)
            # Copy-on-write so the DSP and nrsc5 threads never see the dict change under them.
            self.state.mixers = {**mixers, program: mixer}
            self.on_change()
        return self.state.mixers[program]

    def close_program(self, program: int) -> None:
        if program in self.state.mixers:
            self.state.mixers = {p: m for p, m in self.state.mixers.items() if p != program}
            self.on_change()

    def set_mode(self, program: int, mode: str) -> None:
        self.open_program(program).set_mode(mode)
        self.on_change()

    def set_gain(self, gain_db: float | None) -> None:
        """Change gain (None = automatic). Takes effect immediately by restarting the decoder."""
        with self._control_lock:
            self.source_cfg.gain_db = gain_db
            if self.state.running and self.state.frequency is not None:
                freq = self.state.frequency
                self._retune(freq, force=True)
        self.on_change()

    def stop(self) -> None:
        """Release the tuner (idle). The last station is remembered for status."""
        with self._control_lock:
            if self._radio is None:
                return
            self._stop_source()
            try:
                self._radio.close()
            except nrsc5.NRSC5Error:
                pass
            self._radio = None
            self._iq.put(None)
            if self._dsp_thread:
                self._dsp_thread.join(timeout=2)
            self._dsp_thread = None
            self.state.running = False
            self.state.synced = False
            for program, mixer in self.state.mixers.items():
                mixer.reset(program)
                mixer.source = "none"
        self.on_change()

    def capture(self, seconds: float, timeout: float = 5.0) -> bytes:
        """Return the next `seconds` of raw cu8 I/Q from the running tuner."""
        self._capture_bytes = int(seconds * INPUT_RATE) * 2
        self._capture_done.clear()
        self._capture = bytearray()
        self._capture_done.wait(timeout)
        data, self._capture = bytes(self._capture or b""), None
        return data

    # -- internals ----------------------------------------------------------------------------

    def _ensure_open(self) -> None:
        if self._radio is not None:
            return
        self._radio = self._open_session()
        self.lost = False
        self._dsp_thread = threading.Thread(target=self._dsp_loop, name="freqon-dsp", daemon=True)
        self._dsp_thread.start()

    def _open_session(self) -> nrsc5.NRSC5:
        r = nrsc5.NRSC5(lambda t, e: self._on_event(t, e))
        cfg = self.source_cfg
        if cfg.iq_file:
            r.open_pipe()
        elif cfg.rtltcp:
            host, _, port = cfg.rtltcp.rpartition(":")
            r.open_rtltcp(host, int(port))
        else:
            index = cfg.device_index
            if cfg.serial:
                from . import rtlsdr
                index = rtlsdr.index_for_serial(cfg.serial)
            r.open(index)
        self.applied_ppm = 0
        if not cfg.iq_file and cfg.ppm:
            r.set_freq_correction(cfg.ppm)
            self.applied_ppm = cfg.ppm
        return r

    def _apply_gain(self) -> None:
        if self.source_cfg.iq_file:
            return
        if self.source_cfg.gain_db is None:
            self._radio.set_auto_gain(True)
        else:
            self._radio.set_auto_gain(False)
            self._radio.set_gain(self.source_cfg.gain_db)

    def _retune(self, frequency: float, force: bool = False) -> None:
        # Stop first: nrsc5 keeps delivering the old station's events until stop() returns, and
        # they mustn't land in the new station's state (alerts, art, location...).
        if self.source_cfg.iq_file:
            # Pipe sessions have no stop/start; a fresh session is the cleanest reset.
            self._stop_source()
            self._reset_station_state()
            self.fm.reset()
            self._radio.close()
            self._radio = self._open_session()
            self.state.frequency = frequency
            self._radio.start()
            self._start_file_source()
            return
        self._radio.stop()
        self._reset_station_state()
        self.fm.reset()
        if force:
            self._radio.reset()
        self._apply_gain()
        self._radio.set_frequency(frequency * 1e6)
        self.state.frequency = frequency
        self._radio.start()

    def _reset_station_state(self) -> None:
        s = self.state
        s.synced = False
        s.station_name = s.slogan = s.message = None
        s.programs = {}
        s.id3 = {}
        s.mer_db = s.ber = None
        s.extras.reset()
        self._power_acc = []

    def _stop_source(self) -> None:
        if self._file_thread:
            self._file_stop.set()
            self._file_thread.join(timeout=2)
            self._file_thread = None
        elif self._radio is not None and not self.source_cfg.iq_file:
            self._radio.stop()

    def _start_file_source(self) -> None:
        self._file_stop.clear()
        radio = self._radio
        path = self.source_cfg.iq_file

        def run():
            chunk = 32768 * 4
            rate = INPUT_RATE * 2  # bytes per second
            start = time.monotonic()
            sent = 0
            while not self._file_stop.is_set():
                with open(path, "rb") as f:
                    while not self._file_stop.is_set():
                        data = f.read(chunk)
                        if len(data) < 4:
                            break  # loop the recording
                        data = data[: len(data) & ~3]
                        radio.pipe_samples_cu8(data)
                        sent += len(data)
                        ahead = sent / rate - (time.monotonic() - start)
                        if ahead > 0:
                            time.sleep(ahead)

        self._file_thread = threading.Thread(target=run, name="freqon-iqfile", daemon=True)
        self._file_thread.start()

    def _dsp_loop(self) -> None:
        last_status = 0.0
        while True:
            data = self._iq.get()
            if data is None:
                return
            # Gather ~0.1 s of samples to amortise numpy call overhead. Waiting for them is
            # fine: the dongle delivers continuously, and the mixer is clocked by this path.
            parts = [data]
            size = len(data)
            while size < DSP_MIN_BATCH:
                try:
                    more = self._iq.get(timeout=0.25)
                except queue.Empty:
                    break
                if more is None:
                    self._iq.put(None)
                    break
                parts.append(more)
                size += len(more)
            data = b"".join(parts)

            if self._capture is not None and len(self._capture) < self._capture_bytes:
                self._capture += data
                if len(self._capture) >= self._capture_bytes:
                    self._capture_done.set()

            raw = np.frombuffer(data, dtype=np.uint8)[::64].astype(np.float32) - 127.5
            self._power_acc.append(float(np.mean(raw * raw)) / (127.5 * 127.5))

            try:
                self._dsp_mix(self.fm.process(data))
            except Exception:  # never let a DSP glitch kill the audio thread
                log.exception("DSP failure")
                continue

            now = time.monotonic()
            if now - last_status > 1.0 and self._power_acc:
                last_status = now
                p = sum(self._power_acc) / len(self._power_acc)
                self._power_acc = []
                self.state.rf_power_db = round(10 * np.log10(max(p, 1e-12)), 1)

    def _dsp_mix(self, analog: bytes) -> None:
        """Run one chunk of analog PCM through every open program's mixer."""
        changed = False
        for program, mixer in self.state.mixers.items():
            before = mixer.source
            pcm = mixer.mix(analog)
            changed |= mixer.source != before
            if pcm:
                self.on_pcm(program, pcm)
        if changed:
            self.on_change()

    def _on_event(self, evt_type: nrsc5.EventType, evt) -> None:
        """nrsc5 callback (its worker thread). Keep it quick. Broadcast data is untrusted: an
        error handling one event is logged and the event dropped, never raised into nrsc5."""
        try:
            self._handle_event(evt_type, evt)
        except Exception:
            log.exception("could not handle nrsc5 event %s", getattr(evt_type, "name", evt_type))

    def _handle_event(self, evt_type: nrsc5.EventType, evt) -> None:
        E = nrsc5.EventType
        s = self.state
        changed = True
        if evt_type == E.IQ:
            self.last_iq = time.monotonic()
            try:
                self._iq.put_nowait(evt.data)
            except queue.Full:
                log.warning("DSP falling behind; dropping I/Q")
            return
        if evt_type == E.AUDIO:
            mixer = self.state.mixers.get(evt.program)
            if mixer is not None:
                # Flagged frames are nrsc5's concealment silence (not received or not decodable).
                mixer.push_digital(evt.program, evt.data, ok=not evt.flags)
            return
        if evt_type == E.LOST_DEVICE:
            log.error("nrsc5 lost the dongle")
            self.lost = True
        elif evt_type == E.SYNC:
            s.synced = True
            if s.frequency and evt is not None:
                s.extras.sync_offset(s.frequency * 1e6, evt.freq_offset, self.applied_ppm)
        elif evt_type == E.LOST_SYNC:
            s.synced = False
        elif evt_type == E.MER:
            s.mer_db = round((evt.lower + evt.upper) / 2, 1)
            changed = False
        elif evt_type == E.BER:
            s.ber = round(evt.cber, 4)
            changed = False
        elif evt_type == E.STATION_NAME:
            s.station_name = evt.name
        elif evt_type == E.STATION_SLOGAN:
            s.slogan = evt.slogan
        elif evt_type == E.STATION_MESSAGE:
            s.message = evt.message
        elif evt_type == E.SIS:
            if evt.alert:
                s.extras.alert(evt.alert, _category_names(evt.alert_categories), time.time())
            if evt.latitude is not None:
                s.extras.location = (round(evt.latitude, 4), round(evt.longitude, 4))
            s.station_name = evt.name or s.station_name
            s.slogan = evt.slogan or s.slogan
            s.message = evt.message or s.message
            for svc in evt.audio_services:
                info = s.programs.setdefault(svc.program, {"name": f"HD{svc.program + 1}"})
                if svc.type != nrsc5.ProgramType.UNDEFINED:
                    info["type"] = nrsc5.NRSC5.program_type_name(svc.type)
        elif evt_type == E.SIG:
            for svc in evt:
                if svc.type == nrsc5.ServiceType.AUDIO:
                    info = s.programs.setdefault(svc.number - 1, {"name": f"HD{svc.number}"})
                    if svc.name:
                        info["name"] = svc.name
        elif evt_type == E.ID3:
            info = s.id3.setdefault(evt.program, {})
            new = {"title": evt.title, "artist": evt.artist, "album": evt.album, "genre": evt.genre}
            changed = new != {k: info.get(k) for k in new}
            if "title" in info and info["title"] != evt.title and evt.title:
                s.extras.song_changed(evt.program)  # new song (also after an untitled ad break)
            info.update(new)
            xhdr = getattr(evt, "xhdr", None)
            if xhdr is not None and xhdr.mime == nrsc5.MIMEType.PRIMARY_IMAGE:
                s.extras.xhdr(evt.program, xhdr.param, xhdr.lot)
                changed = True
        elif evt_type == E.LOT:
            data = evt.component.data if evt.component is not None else None
            kind = {nrsc5.MIMEType.PRIMARY_IMAGE: "art", nrsc5.MIMEType.STATION_LOGO: "logo"}.get(
                getattr(data, "mime", None))
            if kind:
                program = evt.service.number - 1 if evt.service.type == nrsc5.ServiceType.AUDIO else 0
                changed = s.extras.lot(program, kind, evt.lot, evt.data)
                log.info("tuner %d: %s for HD%d from %s (%d bytes)%s", self.index, kind, program + 1, s.frequency,
                         len(evt.data), "" if changed else ", unusable")
            else:
                log.debug("tuner %d: LOT file on an unused component (%d bytes)", self.index, len(evt.data))
                changed = False
        elif evt_type == E.EMERGENCY_ALERT:
            s.extras.alert(evt.message, _category_names(evt.categories), time.time())
            shown = s.extras.current_alert(time.time())
            log.warning("emergency alert on %s: %r", s.frequency, shown["message"] if shown else "")
        elif evt_type == E.STATION_LOCATION:
            s.extras.location = (round(evt.latitude, 4), round(evt.longitude, 4))
        elif evt_type == E.LOCAL_TIME:
            s.extras.local_time(int(evt.utc_offset))
            changed = False
        elif evt_type == E.AUDIO_SERVICE:
            s.extras.audio[evt.program] = {"codec_mode": int(evt.codec_mode),
                                           "blend": getattr(evt.blend_control, "name", str(evt.blend_control)).lower()}
            changed = False
        elif evt_type == E.HERE_IMAGE:
            s.extras.here_image(evt.image_type.name.lower(), evt.seq, evt.n1, evt.n2, evt.time_utc, evt.data)
        elif evt_type == E.AGC:
            if evt.is_final:
                s.gain_db = round(evt.gain_db, 1)
        else:
            changed = False
        if changed:
            self.on_change()


def _category_names(categories) -> list[str]:
    return [getattr(c, "name", str(c)).replace("_", " ").title() for c in categories or []]
