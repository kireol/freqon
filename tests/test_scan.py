import numpy as np

from freqon.fm import INPUT_RATE
from freqon.scan import band_power_db, find_peaks


def cu8(signal: np.ndarray) -> bytes:
    out = np.empty(2 * signal.size, dtype=np.uint8)
    out[0::2] = np.clip(np.round(signal.real * 127.5 + 127.5), 0, 255)
    out[1::2] = np.clip(np.round(signal.imag * 127.5 + 127.5), 0, 255)
    return out.tobytes()


def test_station_at_centre_has_more_power_than_one_offset():
    rng = np.random.default_rng(1)
    n = 65536 * 2
    noise = 0.02 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    t = np.arange(n) / INPUT_RATE
    centred = noise + 0.5 * np.exp(2j * np.pi * 30_000 * t)
    offset = noise + 0.5 * np.exp(2j * np.pi * 400_000 * t)  # neighbouring channel
    assert band_power_db(cu8(centred)) > band_power_db(cu8(offset)) + 15


def test_find_peaks_uses_median_floor():
    powers = {88.1: -40, 88.3: -41, 88.5: -39, 97.1: -25, 99.5: -31, 101.1: -37}
    assert find_peaks(powers, 6) == [97.1, 99.5]
