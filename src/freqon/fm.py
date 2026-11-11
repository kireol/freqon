"""Analog FM broadcast demodulator.

Input:  raw RTL-SDR samples (interleaved unsigned 8-bit I/Q) at 1,488,375 S/s,
        exactly what nrsc5 consumes, so one capture feeds both decoders.
Output: 44.1 kHz interleaved stereo int16 PCM (mono audio duplicated to both channels).

Rate plan (all exact integers/ratios, so no drift):
    1,488,375  --lowpass 120 kHz, /5-->  297,675  --FM discriminator-->
      --de-emphasis + lowpass 15 kHz, /3-->  99,225  --linear resample x4/9-->  44,100
"""

from __future__ import annotations

import numpy as np
from scipy import signal

INPUT_RATE = 1_488_375
AUDIO_RATE = 44_100
_DECIM1 = 5
_RATE1 = INPUT_RATE // _DECIM1  # 297,675
_DECIM2 = 3
_RATE2 = _RATE1 // _DECIM2  # 99,225
_RESAMPLE_STEP = _RATE2 / AUDIO_RATE  # 2.25 input samples per output sample
_DEVIATION_HZ = 75_000


class FMDemodulator:
    """Stateful wide-band FM demodulator; call process() with consecutive chunks."""

    def __init__(self, deemphasis_us: float = 75.0, gain: float = 0.6):
        self.gain = gain
        self._sos_rf = signal.butter(6, 120_000, fs=INPUT_RATE, output="sos")
        self._sos_audio = signal.butter(6, 15_000, fs=_RATE1, output="sos")
        # Single-pole de-emphasis (75 us in the Americas, 50 us elsewhere), via bilinear transform.
        tau = deemphasis_us * 1e-6
        self._deemph_b, self._deemph_a = signal.bilinear([1], [tau, 1], fs=_RATE1)
        self._scale = _RATE1 / (2 * np.pi * _DEVIATION_HZ)
        self.reset()

    def reset(self) -> None:
        self._zi_rf = signal.sosfilt_zi(self._sos_rf).astype(np.complex128) * 0
        self._zi_audio = signal.sosfilt_zi(self._sos_audio) * 0
        self._zi_deemph = signal.lfilter_zi(self._deemph_b, self._deemph_a) * 0
        self._phase1 = 0  # offset of the next sample to keep for the /5 decimation
        self._phase2 = 0  # same, for the /3 decimation
        self._last_iq = np.complex64(0)
        self._resample_pos = 0.0  # fractional read position into the 99,225 Hz stream
        self._resample_tail = np.zeros(0, dtype=np.float64)

    def process(self, cu8: bytes) -> bytes:
        """Demodulate one chunk of raw cu8 I/Q; returns int16 stereo PCM (may be empty)."""
        raw = np.frombuffer(cu8, dtype=np.uint8)
        if raw.size < 2:
            return b""
        raw = raw[: raw.size & ~1].astype(np.float32)
        iq = ((raw[0::2] - 127.5) + 1j * (raw[1::2] - 127.5)).astype(np.complex64)

        iq, self._zi_rf = signal.sosfilt(self._sos_rf, iq, zi=self._zi_rf)
        n0 = iq.size
        iq = iq[self._phase1 :: _DECIM1]
        self._phase1 = self._next_phase(self._phase1, n0, _DECIM1)
        if iq.size == 0:
            return b""

        # Polar discriminator: phase change between consecutive samples.
        prev = np.concatenate(([self._last_iq], iq[:-1]))
        self._last_iq = iq[-1]
        demod = np.angle(iq * np.conj(prev)) * self._scale

        demod, self._zi_deemph = signal.lfilter(self._deemph_b, self._deemph_a, demod, zi=self._zi_deemph)
        demod, self._zi_audio = signal.sosfilt(self._sos_audio, demod, zi=self._zi_audio)
        n1 = demod.size
        demod = demod[self._phase2 :: _DECIM2]
        self._phase2 = self._next_phase(self._phase2, n1, _DECIM2)

        audio = self._resample(demod)
        pcm = np.clip(audio * self.gain * 32767.0, -32768, 32767).astype(np.int16)
        return np.repeat(pcm, 2).tobytes()

    @staticmethod
    def _next_phase(phase: int, n: int, step: int) -> int:
        """Index (into the next chunk) of the next sample a [phase::step] decimator keeps."""
        return (phase - n) % step

    def _resample(self, x: np.ndarray) -> np.ndarray:
        """Linear-interpolation resample 99,225 Hz -> 44,100 Hz, continuous across calls."""
        buf = np.concatenate((self._resample_tail, x))
        # Output samples whose two neighbours are both available: pos, pos+step, ... < len(buf)-1
        count = int(np.floor((buf.size - 1 - self._resample_pos) / _RESAMPLE_STEP)) + 1
        if buf.size < 2 or count <= 0:
            self._resample_tail = buf
            return np.zeros(0)
        pos = self._resample_pos + np.arange(count) * _RESAMPLE_STEP
        out = np.interp(pos, np.arange(buf.size), buf)
        next_pos = self._resample_pos + count * _RESAMPLE_STEP
        keep_from = min(int(np.floor(next_pos)), buf.size)
        self._resample_tail = buf[keep_from:]
        self._resample_pos = next_pos - keep_from
        return out
