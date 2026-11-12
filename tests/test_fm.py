import numpy as np

from freqon.fm import AUDIO_RATE, INPUT_RATE, FMDemodulator


def make_fm_cu8(tone_hz: float, seconds: float, deviation: float = 50_000) -> bytes:
    n = int(INPUT_RATE * seconds)
    t = np.arange(n) / INPUT_RATE
    phase = 2 * np.pi * deviation * np.sin(2 * np.pi * tone_hz * t) / (2 * np.pi * tone_hz)
    iq = 0.8 * np.exp(1j * phase)
    out = np.empty(2 * n, dtype=np.uint8)
    out[0::2] = np.clip(np.round(iq.real * 127.5 + 127.5), 0, 255)
    out[1::2] = np.clip(np.round(iq.imag * 127.5 + 127.5), 0, 255)
    return out.tobytes()


def left_channel(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype=np.int16)[0::2].astype(np.float64)


def test_output_rate_matches_input_duration():
    demod = FMDemodulator()
    first = len(demod.process(make_fm_cu8(1000, 1.0))) // 4
    # The block filters hold back a little audio (well under 50 ms)...
    assert AUDIO_RATE * 0.95 < first <= AUDIO_RATE
    # ...and after that the output keeps exact pace with the input.
    later = sum(len(demod.process(make_fm_cu8(1000, 1.0))) // 4 for _ in range(3))
    assert abs(first + later - 4 * AUDIO_RATE) < AUDIO_RATE * 0.05
    assert abs(later - 3 * AUDIO_RATE) < 2_000  # within one filter block


def test_rejects_adjacent_station():
    # A station 200 kHz away (strong) must not leak into the wanted one's audio.
    n = INPUT_RATE
    t = np.arange(n) / INPUT_RATE
    wanted = 0.3 * np.exp(1j * 50_000 * np.sin(2 * np.pi * 1000 * t) / 1000)
    other = 0.6 * np.exp(1j * (2 * np.pi * 200_000 * t + 50_000 * np.sin(2 * np.pi * 3100 * t) / 3100))
    iq = wanted + other
    raw = np.empty(2 * n, dtype=np.uint8)
    raw[0::2] = np.clip(np.round(iq.real * 127.5 + 127.5), 0, 255)
    raw[1::2] = np.clip(np.round(iq.imag * 127.5 + 127.5), 0, 255)
    audio = left_channel(FMDemodulator(deemphasis_us=0.0001).process(raw.tobytes()))[4410:]
    spectrum = np.abs(np.fft.rfft(audio * np.hanning(audio.size)))
    freqs = np.fft.rfftfreq(audio.size, 1 / AUDIO_RATE)
    assert abs(freqs[np.argmax(spectrum)] - 1000) < 5
    tone = np.abs(freqs - 1000) <= 50
    leak = (~tone) & (freqs < 15_000)
    assert np.sum(spectrum[leak] ** 2) / np.sum(spectrum[tone] ** 2) < 10 ** (-60 / 10)


def test_recovers_tone_frequency():
    demod = FMDemodulator(deemphasis_us=0.0001)
    audio = left_channel(demod.process(make_fm_cu8(1000, 1.0)))[4410:]  # skip filter settling
    spectrum = np.abs(np.fft.rfft(audio * np.hanning(audio.size)))
    peak_hz = np.argmax(spectrum) * AUDIO_RATE / audio.size
    assert abs(peak_hz - 1000) < 5
    assert np.max(np.abs(audio)) > 3000  # clearly audible, not silence


def test_chunking_does_not_change_output():
    data = make_fm_cu8(700, 0.5)
    whole = left_channel(FMDemodulator().process(data))
    demod = FMDemodulator()
    pieces = []
    step = 2 * 12_345  # odd-sized chunks exercise the decimation/resampling state
    for i in range(0, len(data), step):
        pieces.append(demod.process(data[i : i + step]))
    chunked = left_channel(b"".join(pieces))
    n = min(whole.size, chunked.size)
    assert abs(whole.size - chunked.size) <= 1
    assert np.max(np.abs(whole[:n] - chunked[:n])) <= 2


def test_silence_on_empty_input():
    assert FMDemodulator().process(b"") == b""
