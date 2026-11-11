# Multiple tuners and camera video with burned-in now playing

Date: 2026-11-10. Builds on `docs/design.md`.

## Why

- The owner may add more USB tuners, and wants several people able to listen to different
  things at once later ("not right now", so build the plumbing, keep the UI simple).
- Plex and Jellyfin will play the video stream. The owner wants a live camera feed (RTSP or
  go2rtc) as the picture, with the station and now-playing text **embedded in the video**
  so it appears in Plex and Jellyfin alike.
- The service may move from a Raspberry Pi 3 to a Linux VM/container on a Dell R720XD
  (Proxmox, USB passthrough). Plenty of CPU there; the Pi must still work at reduced settings.

## Part 1: multiple tuners

### Owner's decisions
- One tuner on one frequency serves **every HD channel of that station at once**
  (97.1 HD1, HD2, HD3 to different people simultaneously). Each extra dongle adds a frequency.
- When every tuner is busy and someone takes over, **the system picks** which to bump:
  a previous channel.

### Behaviour
- Config lists tuners; the existing single-tuner keys keep working:
  ```toml
  [[tuners]]
  device_index = 0
  [[tuners]]
  rtltcp = "192.168.1.50:1234"
  gain = 20.0
  ```
- **Channels**, not the tuner, are what clients play: `(frequency, HD program)`. Each channel
  has its own mixer (and its own digital/analog mode) and its own encoders.
- **Each client has its own current channel.** `POST /api/tune` sets it; `/stream.mp3` plays
  *your* channel and follows it when you retune. `/api/status` and the WebSocket report your
  channel (same fields as today, so the web client keeps working) plus a `tuners` list.
  Clients pass `client_id` as before.
- **Picking a tuner for a tune request**, in order:
  1. a tuner already on that frequency (any HD channel joins freely; no conflict);
  2. the requester's own tuner if nobody else is using it;
  3. any idle tuner (no listeners);
  4. otherwise **busy** (409 / 503, listing what each tuner is playing and for whom), unless
     `override`, which bumps the tuner **tuned longest ago**.
- **Bumped listeners:** web sessions on the bumped tuner move to the new station (as today).
  Per-channel streams (Plex's `/stream/{f}/{p}.ts`) end, so Plex shows the channel stopped
  rather than playing the wrong station under the old guide entry. *(Builder's call.)*
- Digital/analog mode is per channel and never needs an override.
- Idle release is per tuner. A scan uses an idle tuner (busy rules apply if none).
- HDHomeRun emulation reports `TunerCount` = number of tuners. *(Builder's call: Plex then
  won't try two HD channels of one station beyond that count; acceptable for now.)*

## Part 2: camera video with now playing burned in

### Owner's decisions
- Optional RTSP / go2rtc source; the now-playing info is **part of the video**, for Plex and Jellyfin.
- One camera source for all channels; lower-third bar (station + HD channel, then song/show);
  if the camera drops, fall back to the plain card.

### Behaviour
- Config:
  ```toml
  video_source = "rtsp://go2rtc.local:8554/camera"   # anything ffmpeg can open
  video_size = "1280x720"        # Pi 3: "640x360"
  video_fps = 15                 # Pi 3: 10
  video_encoder = "libx264"      # Pi: "h264_v4l2m2m"; server: libx264
  video_bitrate = "2500k"
  ```
- A camera reader (its own ffmpeg) decodes the source to raw frames at the output size and
  keeps the **latest frame**. It reconnects with backoff when the source drops.
- The TS encoder's video is fed by the service, paced by the audio clock (as today): each frame
  is the latest camera frame with the lower third alpha-blended on top. If no camera frame has
  arrived for 5 s, the plain full-screen card is used instead. **The output stream never breaks**
  when the camera comes and goes.
- The lower third is re-rendered only when the text changes; blending touches only that strip.
- Without `video_source`, behaviour is unchanged (the card at 2 fps).

## Non-goals (now)
- UI for picking a specific tuner; per-user accounts.
- AM band (needs a direct-sampling dongle and a different antenna).
- HLS output (comes with time shifting).

## Constraints
- Must still run on a Pi 3 at reduced video settings; must run in a Linux VM with USB passthrough.
- Existing API fields keep working; existing tests keep passing (updated where the "different
  HD channel on the same station is busy" rule intentionally changes; it no longer conflicts).
