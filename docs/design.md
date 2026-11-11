# Freqon design notes (2026-11-10, named Freqon 2026-11-11)

## Intent

Make one RTL-SDR on a Raspberry Pi 3 behave like a household radio tuner that other
apps control: it plays HD Radio when it can and analog FM when it can't, the way a
car radio blends. Clients, in priority order: web player (most
important), then Plex Live TV (via xTeVe or directly). Later: time shifting,
artwork, richer now-playing.

## Decisions made with the owner

- Digital preferred, analog fallback; clients can force either.
- The web player offers "Tune to station" by typed frequency, a station list and scanning.
- A different-station request from another client is told **busy** when every tuner is in
  use, with an **override** option that bumps a previous channel.
- Several tuners (more dongles), and one tuner serves every HD channel of its station at
  once. Several simultaneous users are possible but not a current need. (See
  `docs/superpowers/specs/2026-11-10-multi-tuner-and-camera-video-design.md`.)
- Optional camera (RTSP / go2rtc) as the Plex/Jellyfin picture, with the now-playing text
  burned into the video. The service may move to a Linux VM on a Proxmox server.
- Same-station requests just join; switching digital/analog never needs an override.
- Switching speed matters more than perfect audio; a few seconds behind live is fine.
- Target hardware is a Raspberry Pi 3. Language and tools were left to the builder.

## Builder's calls

- Python service (aiohttp) with numpy/scipy DSP; ffmpeg for encoding. nrsc5 is used as
  a library (its official ctypes wrapper, vendored) and **owns the dongle**; its raw-I/Q
  event feeds the analog demodulator, so no second process competes for the device.
  This also gives rtl_tcp support for free.
- Output is clocked by the analog path (dongle clock). Digital audio sits in a ~1 s jitter
  buffer; HD takes over only once 1 s is buffered and hands back to analog when the buffer
  runs dry, with a 150 ms crossfade. Analog and digital are not time-aligned; the crossfade
  hides the seam.
- Analog is mono, at a gain matched to digital loudness (measured: both -18.6 dB mean).
- HD2+ in auto mode go silent rather than falling back to HD1's analog audio.
- "Holders" are open streams. All Plex requests share the identity "plex".
- Plex gets MPEG-TS with a 640×360, 2 fps H.264 "now playing" card (x264 zerolatency;
  without it the frame-thread delay cost ~6 s). Raw-input probing is disabled so a channel
  starts in about 0.5 s.
- Channel numbers are `<MHz×10>.<HD number>` (97.1 HD2 → 971.2) so they never collide.
- Plex gets both an M3U/XMLTV pair (xTeVe) and HDHomeRun emulation (Plex direct).
  The guide is a placeholder of hourly blocks; radio has no schedule.
- Scan: 101-channel power sweep at fixed gain (threshold: band median + 6 dB), then an HD
  check (5 s for sync, 6 s for name/programs) on each candidate. About 80 s in practice.
  Rescans keep user renames/hides.
- Idle release after 30 s without listeners (was 120 s): closing the dongle powers down its demodulator, and nothing runs while idle.

## Measured on the dev machine (Apple Silicon, FC0012 dongle, Detroit)

- Analog audio within ~1 s of a tune; HD takeover at ~4 s on 97.1.
- Analog DSP: 74 ms per 2 s of signal (Pi 3 estimate: ~1/3 of a core).
- MP3 and TS streams run in real time; TS first byte at ~0.5 s.
- Scan found 12 stations (4 with HD) in 83 s.

- Multiple tuners: `TunerPool` picks a tuner (same station → your own → idle → override
  bumps the one tuned longest ago). Each client has a session (its channel); `/stream.mp3`
  follows it across retunes on one connection. Bumped Plex streams end. Each tuner has
  its own control thread, so tuners never wait for each other.
- Camera: one shared ffmpeg reader (started with the first video stream), letterboxed to
  the output size; each channel blends its lower third onto the latest frame, or shows
  the card after 5 s without one. Camera URLs are redacted in logs.

## Measured with the multi-tuner build (same machine, one dongle)

- Retune HD1 → HD2 on one open `/stream.mp3`: response never ended; ~1.7 s of silence
  while HD2 buffered (no analog version of HD2).
- 97.1 HD1 and HD2 to two clients at once on one dongle: both real time.
- 1280×720 at 15 fps camera TS: real time on the dev machine; camera dropped and
  reconnected (backoff 1, 2, 4, 8 s) with the stream showing the card meanwhile.
- Shutdown with MP3 + camera TS streams open: 0.2 s.
- Weak HD (11.9% bit errors, MER −1.6 dB) gives runs of silence from nrsc5's frame
  concealment, while analog is clean.

- Broadcast extras (2026-11-11): album art (LOT on PRIMARY_IMAGE components, chosen by ID3
  XHDR), station logos (STATION_LOGO components, kept in data_dir/logos), emergency alerts,
  traffic/weather maps (HERE images, assembled from 256 px tiles), transmitter location,
  audio-service details. Images from the air are size-checked before decoding and re-encoded.
  The station UTC offset is shown only when plausible and repeated (97.1 sends it garbled).
- Device: dongles by serial (librtlsdr), LOST_DEVICE treated as a stall, USB reset (Linux
  ioctl) before the restart, ppm calibration from HD SYNC offsets saved per dongle once two
  stations agree. 97.1 sends per-channel logos every few minutes and mostly XHDR "no image"
  (talk); its song art is rare.

## Next

1. Time shifting: keep a rolling PCM/AAC buffer on disk; serve HLS so players can seek.
2. Stereo analog (19 kHz pilot / 38 kHz L−R).
3. Auto mode could prefer analog while HD bit errors are high (see the weak-HD note above).
4. HLS output (with time shifting).
