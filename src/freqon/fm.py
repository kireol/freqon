"""Analog FM broadcast demodulator.

Input:  raw RTL-SDR samples (interleaved unsigned 8-bit I/Q) at 1,488,375 S/s,
        exactly what nrsc5 consumes, so one capture feeds both decoders.
Output: 44.1 kHz interleaved stereo int16 PCM (mono audio duplicated to both channels).

Rate plan (all exact integer ratios, so no drift):
    1,488,375  --lowpass 100 kHz, /5-->  297,675  --FM discriminator-->
      --lowpass 15 kHz, x4/27-->  44,100  --de-emphasis-->

This shares a Raspberry Pi 3 with nrsc5, so it has to be cheap. Both resampling filters run
in the frequency domain (overlap-save): one FFT of a block of input, multiply by the
filter's response, keep only the bins below the output Nyquist frequency and inverse-FFT
at the output rate. That costs a few dozen operations per input sample whatever the
filter length, needs no BLAS (Pi OS numpy uses the slow reference one) and runs in C with
the GIL released, so nrsc5's thread isn't held up.
"""

from __future__ import annotations

import numpy as np
from scipy import fft, signal

INPUT_RATE = 1_488_375
AUDIO_RATE = 44_100
_DECIM1 = 5
_RATE1 = INPUT_RATE // _DECIM1  # 297,675
_UP2, _DOWN2 = 4, 27  # 297,675 * 4 / 27 = 44,100
_DEVIATION_HZ = 75_000


def _kaiser_lowpass(fs: float, pass_hz: float, stop_hz: float, atten_db: float) -> np.ndarray:
    numtaps, beta = signal.kaiserord(atten_db, (stop_hz - pass_hz) / (fs / 2))
    return signal.firwin(numtaps, (pass_hz + stop_hz) / 2, window=("kaiser", beta), fs=fs)


class _FFTResampler:
    """Stateful FIR filter + rational resample (by up/down) using overlap-save FFTs.

    `h` must stop everything above the output Nyquist frequency: the resampling drops the
    bins outside it. Each block takes `down * m` inputs (the first `overlap` repeated from
    the previous block) and yields `up * m` outputs, of which the first `overlap*up/down`
    are discarded. The output doesn't depend on how the input is chunked.
    """

    def __init__(self, h: np.ndarray, up: int, down: int, m: int, complex_input: bool):
        self.up, self.down = up, down
        self._complex = complex_input
        self._n_in = down * m
        self._n_out = up * m
        overlap = -(-(h.size - 1) // down) * down  # >= taps - 1, a whole number of `down`s
        if overlap >= self._n_in:
            raise ValueError("block too short for the filter")
        self._overlap = overlap
        self._skip = overlap * up // down
        dtype = np.complex64 if complex_input else np.float32
        self._dtype = dtype
        gain = self._n_out / self._n_in  # inverse FFT at the smaller size scales by 1/n_out
        if complex_input:
            hf = fft.fft(h, self._n_in) * gain
            half = self._n_out // 2
            self._H = np.concatenate((hf[:half], hf[self._n_in - half :])).astype(np.complex64)
            self._H[half] = 0  # output Nyquist bin: the filter has stopped it anyway
        else:
            hf = fft.rfft(h, self._n_in) * gain
            self._H = hf[: self._n_out // 2 + 1].astype(np.complex64)
            self._H[-1] = 0
        self.reset()

    def reset(self) -> None:
        self._buf = np.zeros(self._overlap, dtype=self._dtype)

    def process(self, x: np.ndarray) -> np.ndarray:
        buf = np.concatenate((self._buf, x))
        step = self._n_in - self._overlap
        blocks = (buf.size - self._overlap) // step
        if blocks <= 0:
            self._buf = buf
            return np.zeros(0, dtype=self._dtype)
        # All blocks of this call in one batched FFT: rows of n_in, advancing by `step`.
        frames = np.lib.stride_tricks.sliding_window_view(buf, self._n_in)[::step][:blocks]
        half = self._n_out // 2
        if self._complex:
            spec = fft.fft(frames, axis=1, workers=1)
            kept = np.concatenate((spec[:, : half + 1], spec[:, self._n_in - half + 1 :]), axis=1)
            # kept[:, half] is bin +half; it's zeroed by H (the Nyquist bin of the output)
            kept *= self._H
            y = fft.ifft(kept, axis=1, workers=1)
        else:
            spec = fft.rfft(frames, axis=1, workers=1)
            kept = spec[:, : half + 1] * self._H
            y = fft.irfft(kept, self._n_out, axis=1, workers=1)
        self._buf = buf[blocks * step :].copy()
        return y[:, self._skip :].reshape(-1).astype(self._dtype, copy=False)


class FMDemodulator:
    """Stateful wide-band FM demodulator; call process() with consecutive chunks."""

    def __init__(self, deemphasis_us: float = 75.0, gain: float = 0.6):
        self.gain = gain
        # Stage 1: the station (mono FM fits in +/-90 kHz), stopped by the output Nyquist
        # frequency (148.8 kHz) so dropping the outer bins aliases nothing in.
        h1 = _kaiser_lowpass(INPUT_RATE, 100_000, 145_000, 60)
        self._rf = _FFTResampler(h1, 1, _DECIM1, m=1024, complex_input=True)
        # Stage 2: mono audio to 15 kHz; stops the 19 kHz stereo pilot, the stereo
        # subcarrier and RDS.
        h2 = _kaiser_lowpass(_RATE1, 15_000, 19_000, 60)
        self._audio = _FFTResampler(h2, _UP2, _DOWN2, m=256, complex_input=False)
        # Single-pole de-emphasis (75 us in the Americas, 50 us elsewhere), via bilinear transform.
        tau = deemphasis_us * 1e-6
        self._deemph_b, self._deemph_a = signal.bilinear([1], [tau, 1], fs=AUDIO_RATE)
        self._scale = np.float32(_RATE1 / (2 * np.pi * _DEVIATION_HZ))
        self.reset()

    def reset(self) -> None:
        self._rf.reset()
        self._audio.reset()
        self._zi_deemph = signal.lfilter_zi(self._deemph_b, self._deemph_a) * 0
        self._last_iq = np.complex64(0)
        self._odd = b""  # a lone I byte left over from the previous chunk

    def process(self, cu8: bytes) -> bytes:
        """Demodulate one chunk of raw cu8 I/Q; returns int16 stereo PCM (may be empty)."""
        if self._odd:
            cu8 = self._odd + cu8
        raw = np.frombuffer(cu8, dtype=np.uint8)
        self._odd = cu8[-1:] if raw.size & 1 else b""
        raw = raw[: raw.size & ~1]
        if raw.size == 0:
            return b""

        # Interleaved I/Q float32 is complex64 already. The 127.5 DC offset passes through
        # the unity-gain filter; it's removed after decimating, on a fifth of the samples.
        iq = self._rf.process(raw.astype(np.float32).view(np.complex64))
        if iq.size == 0:
            return b""
        iq -= np.complex64(127.5 + 127.5j)

        # Polar discriminator: phase change between consecutive samples.
        prev = np.empty_like(iq)
        prev[0] = self._last_iq
        prev[1:] = iq[:-1]
        self._last_iq = iq[-1]
        demod = np.angle(iq * np.conj(prev))
        demod *= self._scale

        audio = self._audio.process(demod)
        if audio.size == 0:
            return b""
        audio, self._zi_deemph = signal.lfilter(self._deemph_b, self._deemph_a, audio, zi=self._zi_deemph)
        pcm = np.clip(audio * (self.gain * 32767.0), -32768, 32767).astype(np.int16)
        return np.repeat(pcm, 2).tobytes()
