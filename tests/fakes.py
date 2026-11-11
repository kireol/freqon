"""Stand-ins for the radio hardware and ffmpeg encoders."""

import asyncio
import itertools

from freqon.mixer import Mixer
from freqon.radio import RadioState, SourceConfig, validate_frequency

_clock = itertools.count(1)


class FakeRadio:
    def __init__(self, index: int = 0):
        self.index = index
        self.state = RadioState()
        self.source_cfg = SourceConfig()
        self.on_pcm = lambda program, pcm: None
        self.on_change = lambda: None
        self.tunes = []
        self.tuned_at = 0.0

    @property
    def extras(self):
        return self.state.extras

    def tune(self, frequency):
        frequency = validate_frequency(frequency)
        if self.state.running and self.state.frequency == frequency:
            return
        self.tunes.append(frequency)
        self.tuned_at = float(next(_clock))
        self.state.running = True
        self.state.frequency = frequency

    def open_program(self, program):
        if program not in self.state.mixers:
            self.state.mixers = {**self.state.mixers, program: Mixer()}
        return self.state.mixers[program]

    def close_program(self, program):
        self.state.mixers = {p: m for p, m in self.state.mixers.items() if p != program}

    def set_mode(self, program, mode):
        self.open_program(program).set_mode(mode)

    def stop(self):
        self.state.running = False

    def capture(self, seconds):
        return b""


class FakeEncoder:
    content_type = "audio/mpeg"

    def __init__(self, name: str):
        self.name = name
        self._listeners: set[asyncio.Queue] = set()
        self.fed: list[bytes] = []
        self.stopped = False
        self.started = 0

    @property
    def active(self):
        return bool(self._listeners)

    async def subscribe(self):
        q: asyncio.Queue = asyncio.Queue()
        self._listeners.add(q)
        self.started += 1
        self.stopped = False
        return q

    def unsubscribe(self, q):
        self._listeners.discard(q)

    def end_listeners(self):
        for q in list(self._listeners):
            q.put_nowait(b"")

    def feed(self, pcm):
        self.fed.append(pcm)
        for q in list(self._listeners):
            q.put_nowait(f"{self.name}:".encode() + pcm)

    async def stop(self):
        if not self._listeners:
            self.stopped = True


def fake_encoders(made: dict | None = None):
    """make_encoders factory for Controller; records what it made in `made` by channel key."""
    def make(key):
        pair = (FakeEncoder(f"mp3-{key.frequency}-{key.program}"), FakeEncoder(f"ts-{key.frequency}-{key.program}"))
        if made is not None:
            made[key] = pair
        return pair
    return make
