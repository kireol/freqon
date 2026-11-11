"""Chooses between digital (HD) and analog audio and blends between them.

The output is clocked by the analog path: every chunk of analog PCM produced from the
radio yields exactly one equally-sized chunk of output. Digital audio arrives in bursts
from nrsc5, so it is held in a small jitter buffer and drawn from at the same pace.

Modes:
    auto     digital when it is locked and buffered, otherwise analog (HD1 only; HD2+ have
             no analog counterpart, so they go silent instead)
    digital  digital only; silence when it is unavailable
    analog   analog only

Weak HD (auto, HD1, with an HDPolicy): nrsc5 keeps delivering audio on a weak signal, but
frames it couldn't decode are silence flagged UNAVAILABLE/DECODING_ERROR, so a buffer-only
rule plays seconds of nothing. With a policy the mixer measures the share of real audio over
the last QUALITY_WINDOW seconds and
    * falls back to analog once HD has been weak for `fallback_seconds`,
    * returns to HD only after it has been solid for `return_seconds`,
    * after `max_fallbacks` fallbacks within `window_seconds`, holds analog until the mode is
      chosen again or the station changes (reset()).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

AUDIO_RATE = 44_100
BYTES_PER_FRAME = 4  # int16 stereo

MODES = ("auto", "digital", "analog")
SOURCE_ANALOG = "analog"
SOURCE_DIGITAL = "digital"
SOURCE_NONE = "none"

QUALITY_WINDOW = 2.0  # seconds; nrsc5 delivers HD audio in bursts about every 1.5 s
GOOD_RATIO = 0.9  # share of real (not concealed) audio that counts as a solid signal


@dataclass(frozen=True)
class HDPolicy:
    fallback_seconds: float = 1.5  # weak this long -> analog
    return_seconds: float = 5.0  # solid this long -> back to HD
    max_fallbacks: int = 3  # this many fallbacks...
    window_seconds: float = 300.0  # ...within this long -> stay on analog


class Mixer:
    def __init__(
        self,
        prefill_seconds: float = 1.0,
        max_buffer_seconds: float = 4.0,
        crossfade_seconds: float = 0.15,
        policy: HDPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._lock = threading.Lock()
        self._prefill = int(prefill_seconds * AUDIO_RATE) * BYTES_PER_FRAME
        self._max_buffer = int(max_buffer_seconds * AUDIO_RATE) * BYTES_PER_FRAME
        self._crossfade_frames = max(1, int(crossfade_seconds * AUDIO_RATE))
        self.mode = "auto"
        self.program = 0
        self.source = SOURCE_NONE
        self._digital = bytearray()
        self._prev_tail: np.ndarray | None = None
        self.policy = policy
        self._clock = clock
        self._frames: deque[tuple[float, float, bool]] = deque()  # (arrival, seconds, real audio)
        self._weak_since: float | None = None
        self._solid_since: float | None = None
        self._fallbacks: deque[float] = deque()
        self.hd_hold = False  # stuck on analog after too many fallbacks
        self.hd_quality: float | None = None  # share of real audio lately (None: no HD)

    # -- inputs ---------------------------------------------------------------------------

    def push_digital(self, program: int, pcm: bytes, ok: bool = True) -> None:
        """Called (from nrsc5's thread) with decoded HD audio for any program. `ok` is False
        for nrsc5's concealment silence (a frame it couldn't receive or decode)."""
        if program != self.program:
            return
        with self._lock:
            self._frames.append((self._clock(), len(pcm) / BYTES_PER_FRAME / AUDIO_RATE, ok))
            self._digital += pcm
            if len(self._digital) > self._max_buffer:
                # Bursty input or clock drift: drop the oldest audio, keep ~prefill buffered.
                del self._digital[: len(self._digital) - self._prefill]

    def reset(self, program: int | None = None) -> None:
        """Forget buffered digital audio, e.g. after a retune or program change."""
        with self._lock:
            if program is not None:
                self.program = program
            self._digital.clear()
            self._frames.clear()
            self._weak_since = self._solid_since = None
            self._fallbacks.clear()
            self.hd_hold = False  # a new station gets a fresh chance at HD
            self.hd_quality = None

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        with self._lock:
            self.mode = mode
            # Choosing a mode (even Auto again) is someone asking for HD another try.
            self.hd_hold = False
            self._fallbacks.clear()

    @property
    def digital_buffered_seconds(self) -> float:
        return len(self._digital) / BYTES_PER_FRAME / AUDIO_RATE

    # -- output ---------------------------------------------------------------------------

    def mix(self, analog_pcm: bytes) -> bytes:
        """Produce one output chunk the same length as the analog chunk."""
        n = len(analog_pcm)
        if n == 0:
            return b""
        with self._lock:
            want = self._choose_source(n)
            if want == SOURCE_DIGITAL:
                chunk = bytes(self._digital[:n])
                del self._digital[:n]
            elif want == SOURCE_ANALOG:
                chunk = analog_pcm
            else:
                chunk = bytes(n)

        out = np.frombuffer(chunk, dtype=np.int16).reshape(-1, 2).astype(np.float32)
        if want != self.source and self._prev_tail is not None:
            out = self._crossfade(out)
        self.source = want
        self._prev_tail = out[-1:].copy()
        return out.astype(np.int16).tobytes()

    def _choose_source(self, n: int) -> str:
        have = len(self._digital)
        if self.source == SOURCE_DIGITAL:
            digital_ok = have >= n  # keep playing until the buffer runs dry
        else:
            digital_ok = have >= max(n, self._prefill)  # only switch once well buffered
        if self.mode == "analog":
            return SOURCE_ANALOG
        has_analog = self.mode == "auto" and self.program == 0
        if has_analog and self.policy is not None:
            allowed = self._hd_rule()  # always: its timers must run while the buffer fills too
            digital_ok = digital_ok and allowed
        if digital_ok:
            return SOURCE_DIGITAL
        if has_analog:
            return SOURCE_ANALOG
        return SOURCE_NONE

    def _hd_rule(self) -> bool:
        """Weak-HD rule (caller holds the lock): may HD play now?"""
        p = self.policy
        now = self._clock()
        while self._frames and now - self._frames[0][0] > QUALITY_WINDOW:
            self._frames.popleft()
        total = sum(f[1] for f in self._frames)
        good = sum(f[1] for f in self._frames if f[2])
        self.hd_quality = good / total if total else None
        solid = total > 0 and good / total >= GOOD_RATIO
        if solid:
            self._weak_since = None
            self._solid_since = self._solid_since if self._solid_since is not None else now
        else:
            self._solid_since = None
            self._weak_since = self._weak_since if self._weak_since is not None else now
        if self.hd_hold:
            return False
        if self.source == SOURCE_DIGITAL:
            if self._weak_since is not None and now - self._weak_since >= p.fallback_seconds:
                while self._fallbacks and now - self._fallbacks[0] > p.window_seconds:
                    self._fallbacks.popleft()
                self._fallbacks.append(now)
                if len(self._fallbacks) >= p.max_fallbacks:
                    self.hd_hold = True
                return False
            return True
        return self._solid_since is not None and now - self._solid_since >= p.return_seconds

    def _crossfade(self, out: np.ndarray) -> np.ndarray:
        """Fade from the last sample of the previous source into the new one (avoids clicks)."""
        k = min(self._crossfade_frames, out.shape[0])
        ramp = np.linspace(0.0, 1.0, k, dtype=np.float32)[:, None]
        out[:k] = out[:k] * ramp + self._prev_tail * (1.0 - ramp)
        return out
