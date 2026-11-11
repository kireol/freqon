"""Band scanning: a fast power sweep to find stations, then an HD check on each one."""

from __future__ import annotations

import logging
import statistics
import time
from collections.abc import Callable

import numpy as np

from .fm import INPUT_RATE
from .stations import Station

log = logging.getLogger(__name__)

# North American FM channels (odd tenths). Override for other regions via config.
NA_CHANNELS = [round(87.9 + 0.2 * i, 1) for i in range(101)]


def band_power_db(cu8: bytes, half_width_hz: float = 100_000) -> float:
    """Average power within +/- half_width of the tuned centre, in dB (relative)."""
    raw = np.frombuffer(cu8, dtype=np.uint8).astype(np.float32) - 127.5
    iq = raw[0::2] + 1j * raw[1::2]
    n = 32768
    if iq.size < n:
        return -200.0
    blocks = iq[: (iq.size // n) * n].reshape(-1, n)
    spec = np.mean(np.abs(np.fft.fftshift(np.fft.fft(blocks * np.hanning(n), axis=1), axes=1)) ** 2, axis=0)
    freqs = np.fft.fftshift(np.fft.fftfreq(n, 1 / INPUT_RATE))
    band = np.abs(freqs) <= half_width_hz
    band &= np.abs(freqs) > 2_000  # skip the RTL-SDR DC spike
    return float(10 * np.log10(np.mean(spec[band]) + 1e-12))


def find_peaks(powers: dict[float, float], threshold_db: float) -> list[float]:
    """Channels clearly above the band's noise floor (median of all channels)."""
    if not powers:
        return []
    floor = statistics.median(powers.values())
    return [f for f, p in sorted(powers.items()) if p >= floor + threshold_db]


def run_scan(
    radio,
    progress: Callable[[dict], None],
    channels: list[float] = NA_CHANNELS,
    scan_gain_db: float = 0.0,
    threshold_db: float = 6.0,
    hd_sync_timeout: float = 5.0,
    hd_info_timeout: float = 6.0,
    cancelled: Callable[[], bool] = lambda: False,
) -> list[Station]:
    """Blocking. Uses the radio exclusively; the caller restores the previous station."""
    saved_gain = radio.source_cfg.gain_db
    powers: dict[float, float] = {}
    try:
        # Pass 1: power sweep at a fixed gain so channels are comparable.
        radio.source_cfg.gain_db = scan_gain_db
        for i, f in enumerate(channels):
            if cancelled():
                return []
            radio.tune(f)
            radio.capture(0.08)  # let the tuner settle
            powers[f] = band_power_db(radio.capture(0.12))
            progress({"phase": "sweep", "frequency": f, "done": i + 1, "total": len(channels)})
    finally:
        radio.source_cfg.gain_db = saved_gain

    candidates = find_peaks(powers, threshold_db)
    log.info("scan: %d candidate stations: %s", len(candidates), candidates)

    # Pass 2: tune each candidate with normal gain and wait for HD info.
    found: list[Station] = []
    for i, f in enumerate(candidates):
        if cancelled():
            break
        progress({"phase": "hd", "frequency": f, "done": i, "total": len(candidates)})
        radio.tune(f)
        st = Station(frequency=f, power_db=round(powers[f], 1))
        if _wait(lambda: radio.state.synced, hd_sync_timeout, cancelled):
            _wait(lambda: radio.state.station_name and radio.state.programs, hd_info_timeout, cancelled)
            time.sleep(1.0)  # extra programs often follow the first
            s = radio.state
            st.hd = True
            st.name = s.station_name
            st.programs = [{"program": p, **info} for p, info in sorted(s.programs.items())] or [
                {"program": 0, "name": "HD1"}
            ]
        found.append(st)
        progress({"phase": "hd", "frequency": f, "done": i + 1, "total": len(candidates),
                  "found": {"frequency": f, "name": st.name, "hd": st.hd}})
    return found


def _wait(cond: Callable[[], object], timeout: float, cancelled: Callable[[], bool]) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        if cancelled():
            return False
        time.sleep(0.1)
    return bool(cond())
