# Broadcast Extras and Device Features Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Use every nrsc5/dongle feature hdradio ignores: album art, broadcast logos, emergency alerts, device-lost handling, Linux USB reset, ppm calibration, dongles by serial, audio details, traffic/weather maps, transmitter location and time.

**Architecture:** `Radio._on_event` forwards the new events to a per-tuner `Extras` object (`extras.py`: images, alerts, maps, calibration samples; pure, thread-safe, testable without nrsc5). Controller/API expose them; card/video and web clients display them. Device helpers (`rtlsdr.py`: serial lookup, USB reset) wrap librtlsdr/ioctl behind small functions.

**Tech Stack:** Python 3.11+, aiohttp, Pillow, numpy, nrsc5 (ctypes), librtlsdr (ctypes), BrightScript.

**Spec:** `docs/superpowers/specs/2026-11-11-broadcast-extras-and-device-features-design.md`

## Global Constraints

- New status fields only add; existing tests keep passing.
- Air-sourced images: ≤ 2 MB in, decoded with Pillow, ≤ 1024 px, re-encoded (JPEG art / PNG logos).
- Alert expiry 30 min after the last repeat; cleared on retune.
- Calibration needs ≥ 20 SYNC samples at one frequency; integer ppm; explicit numeric `ppm` wins; `ppm = "auto"` is the default.
- Logos persist in `data_dir/logos/`; art in memory (≤ 20 per tuner).

## Review Focus

1. Malformed/huge image bytes from the air never crash the event thread or get served raw. (Task 1 test.)
2. Song change without art clears the previous song's art (no stale art on the wrong song). (Task 1 test.)
3. Retune clears alerts, art and partial maps from the previous station. (Task 1 test.)
4. LOST_DEVICE triggers the stall path exactly once. (Task 2 test.)
5. `/art/{cid}.jpg` for a channel with no art is a 404 the clients handle (fallback to logo/card). (Task 3 test.)

---

### Task 1: `extras.py` — per-tuner broadcast extras
Files: Create `src/hdradio/extras.py`, `tests/test_extras.py`.
Produces `class Extras`: `lot(program, kind, lot_id, data)` (kind "art"|"logo"), `xhdr(program, param, lot_id)`, `song_changed(program)`, `art(program) -> bytes|None` (JPEG), `logo(program) -> bytes|None` (PNG), `alert(message, categories, now)`, `current_alert(now) -> dict|None`, `here_image(kind, seq, n1, n2, time, data)`, `map_png(kind) -> bytes|None`, `sync_offset(freq_hz, offset_hz)`, `ppm_estimate() -> int|None`, `location`/`utc_offset`/`audio[program]`, `reset()`; `clean_image(data, fmt, max_px) -> bytes|None`.
- [ ] Tests: art follows XHDR / flush / no-XHDR newest; song change clears art; bad and oversized images ignored; logo PNG; alert expiry and cleaning; traffic 3×3 assembly, weather single; calibration needs 20 samples, rounds, one frequency; reset clears all. → implement → commit.

### Task 2: Radio wiring, device lost, USB reset, calibration, serial
Files: Modify `radio.py`; create `rtlsdr.py`; modify `service.py`, `__main__.py`; tests `test_radio_programs.py`, `test_rtlsdr.py`, `test_api.py`.
- Radio forwards LOT (via component mime), ID3 xhdr, EMERGENCY_ALERT, SIS alert/location, STATION_LOCATION, LOCAL_TIME, AUDIO_SERVICE, HERE_IMAGE, SYNC offsets to `self.extras`; `snapshot()` adds `alert`, `station.location`, `station.utc_offset`, `programs[].audio`, `art: bool`. LOST_DEVICE sets `self.lost = True`; `is_stalled()` is true when lost.
- `rtlsdr.py`: `list_dongles()`, `index_for_serial(serial)`, `usb_reset(serial|index)` (Linux ioctl; no-op elsewhere) with injectable lib/opener.
- Calibration: Controller saves `extras.ppm_estimate()` per dongle key into state.json when HD is locked; `__main__` applies saved ppm for `ppm = "auto"`; stall handler calls `usb_reset` first.
- [ ] Tests for each; implement; commit.

### Task 3: API and video
Files: `api.py`, `card.py`, `video.py`, `service.py`; tests `test_api.py`, `test_card.py`, `test_video.py`.
- `/art/{cid}.jpg` (404 when none), `/logo/{cid}.png` prefers saved broadcast logo, `/maps/{traffic|weather}.png` (404 when none); logos persisted to `data_dir/logos/`.
- Card and lower third show album art (left) and a red alert banner when present.
- [ ] Tests; implement; commit.

### Task 4: Web player
Files: `web/index.html`, `web/app.js`, `web/style.css`.
- Art next to the readout (fallback logo), alert banner, details (audio codec, transmitter location, local time), Maps section (shown when a map exists).
- [ ] Browser check (Playwright); commit.

### Task 6: Live verification and docs
- [ ] Live on the dongle: art/logo for 97.1 (and others), calibration sign check and saved ppm, alerts/maps if broadcast (log if not), `--list-dongles`. README/config/design docs. Whole-branch review.
