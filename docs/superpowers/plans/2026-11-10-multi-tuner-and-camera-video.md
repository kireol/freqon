# Multiple Tuners and Camera Video Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let hdradio drive several tuners with per-client channels, and use an RTSP/go2rtc camera as the Plex/Jellyfin video with the now-playing lower third burned in.

**Architecture:** A `Radio` (one nrsc5 session = one tuner) gains one `Mixer` per HD program and emits PCM per program. A new `TunerPool` picks tuners for requests; the `Controller` tracks per-client sessions and per-channel encoders. A `Camera` task keeps the latest decoded frame; a `VideoComposer` blends the lower third onto it (or returns the card) and feeds each channel's TS encoder, paced by audio.

**Tech Stack:** Python 3.11+, aiohttp, numpy, Pillow, ffmpeg (libx264 / h264_v4l2m2m), nrsc5.

**Spec:** `docs/superpowers/specs/2026-11-10-multi-tuner-and-camera-video-design.md`

## Global Constraints

- Existing single-tuner config keys (`device_index`, `rtltcp`, `iq_file`, `gain`, `ppm`) keep working when `tuners` is absent.
- Existing API fields of `/api/status` stay, now describing the requesting client's channel; add `tuners`.
- Channel id format stays `<MHz*10>.<HD number>`; program numbers stay 0-based.
- Video config keys and defaults: `video_source` (None), `video_size = "1280x720"`, `video_fps = 15`, `video_encoder = "libx264"`, `video_bitrate = "2500k"`.
- Camera considered lost after **5 s** without a frame; fall back to the full-screen card; the TS output must never stop because of the camera.
- Override bumps the tuner **tuned longest ago**. Bumped mp3 sessions follow to the new station; bumped per-channel streams end.
- HDHomeRun `TunerCount` = number of tuners.
- All security rules (JSON bodies, host allowlist, cross-site refusal, no override via GET) still apply.

## Review Focus

1. A client retunes while its `/stream.mp3` is open: audio must switch to the new channel without the HTTP response ending. (Task 4 test.)
2. Two clients on different HD programs of the same frequency: both play, neither is told busy, one tuner used. (Task 3 test.)
3. Camera URL unreachable at startup / drops mid-stream: TS keeps producing frames (card), recovers when the camera returns. (Task 6 test with a fake frame source.)
4. Last listener of a channel leaves while others remain on the same tuner: that channel's encoders stop, tuner stays. (Task 3 test.)
5. Override when the requester's own tuner is the oldest: it is the one bumped, and the requester's session moves. (Task 2 test.)

---

### Task 1: Per-program mixers in Radio

**Files:** Modify `src/hdradio/radio.py`; Test `tests/test_radio_programs.py`

**Interfaces — Produces:**
- `Radio.tune(frequency: float) -> None` (no program; keeps programs' mixers, resets them)
- `Radio.open_program(program: int) -> Mixer` / `Radio.close_program(program: int) -> None`
- `Radio.on_pcm: Callable[[int, bytes], None]` (program, pcm)
- `Radio.set_mode(program: int, mode: str)`; `Radio.state.snapshot(program: int) -> dict` (same keys as today; `program`, `mode`, `source` are per program)
- `Radio.tuned_at: float` (monotonic time of last retune), `Radio.index: int`

- [ ] Test: feeding analog PCM through `Radio._dsp_mix(analog: bytes)` (extract from `_dsp_loop`) with programs 0 and 1 open calls `on_pcm(0, …)` with analog and `on_pcm(1, …)` with silence; pushing digital audio for program 1 (via `_on_event` AUDIO) after prefill makes program 1 digital while program 0 stays analog.
- [ ] Test: `snapshot(1)["now_playing"]` reflects ID3 for program 1 only; `set_mode(0,"analog")` doesn't change program 1's mode.
- [ ] Implement; keep `RadioState` station-level fields shared, move `mode`/`source` per program into the mixers.
- [ ] Run `uv run pytest -q` (existing radio-adjacent tests may need the new signatures) → pass; commit.

### Task 2: TunerPool allocation

**Files:** Create `src/hdradio/pool.py`; Test `tests/test_pool.py`

**Interfaces — Produces:**
- `class TunerPool(radios: list[Radio])`
- `TunerPool.choose(frequency: float, client_id: str, holders: dict[int, set[str]], override: bool) -> Radio` — `holders` maps radio index → client_ids listening on it; raises `NoTunerFree(busy: list[dict])` where each dict is `{"tuner": index, "frequency": f, "holders": [...]}`.
- Rule order exactly as the spec: same frequency → requester's own (no other holders) → idle → override bumps min `tuned_at` → else raise.
- `TunerPool.own_tuner(client_id, sessions)` helper may live in Controller instead; implementer's choice.

- [ ] Tests: `test_same_frequency_reused`, `test_own_tuner_preferred_when_alone`, `test_idle_tuner_used`, `test_busy_raises_with_holders`, `test_override_bumps_oldest`, `test_override_bumps_requesters_own_when_oldest` (Review Focus 5).
- [ ] Implement; run; commit.

### Task 3: Controller sessions and per-channel encoders

**Files:** Modify `src/hdradio/service.py`; Test `tests/test_api.py` (update FakeRadio to Task 1 interface), new `tests/test_sessions.py`

**Interfaces — Produces:**
- `Controller(pool: TunerPool, stations, *, video: VideoComposerFactory | None, …)`
- `Controller.tune(frequency, program, *, client_id, override) -> dict` sets `sessions[client_id] = ChannelKey(tuner, frequency, program)`
- `Controller.status(client_id: str | None) -> dict` → today's fields for that client's channel + `"tuners": [{index, running, frequency, programs_open, listeners}]`
- `Controller.channel(key) -> Channel` with `.mp3`, `.ts`, `.listeners`; created on demand, encoders stopped when last listener leaves
- `Controller.session_changed(client_id) -> asyncio.Event`-style signal for following streams (implementer picks mechanism)
- `Busy.to_dict()` now lists every busy tuner.

- [ ] Tests: `test_two_programs_same_station_share_tuner` (Review Focus 2), `test_second_station_uses_second_tuner`, `test_busy_when_all_tuners_held`, `test_override_moves_bumped_sessions`, `test_bumped_channel_streams_end`, `test_channel_encoders_stop_when_last_listener_leaves_but_tuner_stays` (Review Focus 4), `test_idle_release_per_tuner`, `test_scan_uses_idle_tuner_and_is_busy_when_none` (scan picks a tuner through `TunerPool.choose` with a sentinel frequency that never matches; restores that tuner afterwards).
- [ ] Update existing API tests: `test_different_hd_program_is_a_conflict` becomes `…_is_not_a_conflict` (intended rule change).
- [ ] Implement; run all; commit.

### Task 4: API — client-aware status, following stream, config of tuners

**Files:** Modify `src/hdradio/api.py`, `src/hdradio/__main__.py`, `src/hdradio/web/app.js`, `deploy/config.example.toml`, `README.md`; Test `tests/test_api.py`

- `/api/status?client_id=…`, `/api/events?client_id=…` report that client's channel.
- `/stream.mp3` streams the caller's session channel and **switches encoder on retune without ending the response** (Review Focus 1); 409 if the client has no session and no channel is playing.
- `/discover.json` `TunerCount = len(pool)`.
- `__main__`: build radios from `[[tuners]]` (keys: `device_index`, `rtltcp`, `iq_file`, `gain`, `ppm`), else one from top-level keys.
- Web client: pass `client_id` on status fetch and WebSocket; show "Tuner N" in the signal line when there is more than one tuner.

- [ ] Tests: `test_status_is_per_client`, `test_stream_follows_retune` (two chunks from different channel encoders arrive on one response; use a fake encoder), `test_discover_tuner_count`, `test_config_tuners_list` (load_config with a temp TOML).
- [ ] Implement; run; live check with the real dongle (tune, listen, retune while listening); commit.

### Task 5: Lower third rendering

**Files:** Modify `src/hdradio/card.py`; Test `tests/test_card.py`

**Interfaces — Produces:** `render_lower_third(status: dict, size: tuple[int,int]) -> tuple[np.ndarray, int]` returning an RGBA strip (`H_strip × W × 4`, uint8) and its top y offset; strip height ≈ 22% of frame height.

- [ ] Tests: shape/offset for 1280×720 and 640×360; transparent above the bar (alpha 0 in top row); text pixels present when title given; long titles don't raise.
- [ ] Implement; run; commit.

### Task 6: Camera reader and video composer

**Files:** Create `src/hdradio/video.py`; Modify `src/hdradio/streams.py` (TSEncoder takes size/fps/encoder/bitrate and a frame callable); Test `tests/test_video.py`

**Interfaces — Produces:**
- `class Camera(url, size, fps, ffmpeg)`: `async start()`, `async stop()`, `latest() -> tuple[bytes | None, float]` (rgb24 frame, monotonic time received). Runs `ffmpeg -rtsp_transport tcp -i URL -vf scale=W:H,fps=F -f rawvideo -pix_fmt rgb24 pipe:1` (omit `-rtsp_transport` for non-rtsp URLs); restarts with backoff 1 s → 30 s.
- `class VideoComposer(camera: Camera | None, size, status_fn: Callable[[], dict])`: `frame() -> bytes` (rgb24 W×H×3). Camera frame younger than 5 s → copy + blend lower third into its strip; else full-screen `render_rgb(status, size)`.
- TSEncoder args: `-c:v {video_encoder} -b:v {video_bitrate}`; for libx264 keep `-preset ultrafast -tune zerolatency`; `-g {fps}`; frame pacing at `fps` from the audio clock (existing mechanism).
- Camera is started once (shared) when the first TS listener appears and stopped when no TS listener remains.

- [ ] Tests: composer returns card when camera is None or stale (fake camera with `latest()` returning old timestamp) — Review Focus 3; composer blends: a solid-blue camera frame keeps blue above the strip and changes pixels inside it; Camera restarts after its ffmpeg exits (use `ffmpeg -f lavfi -i testsrc` as the URL with `-t 1` via an injectable command builder) and `latest()` timestamp advances again.
- [ ] Live check: run `ffmpeg -re -f lavfi -i testsrc2=size=1280x720:rate=15 -f rtsp rtsp://…` only if an RTSP server is available; otherwise use a `testsrc2` lavfi URL through the command builder; fetch 10 s of `/stream/97.1/0.ts`, extract a frame, confirm camera + lower third; kill the source and confirm the stream continues with the card.
- [ ] Config keys + README section; commit.

### Task 7: Final verification

- [ ] `uv run ruff check src tests` clean; `uv run pytest -q` all pass.
- [ ] Live: two programs of 97.1 from two clients at once on one dongle; busy + override behaviour with one dongle; shutdown time < 2 s.
- [ ] Update `docs/design.md` "Next" and the README API table; commit.
