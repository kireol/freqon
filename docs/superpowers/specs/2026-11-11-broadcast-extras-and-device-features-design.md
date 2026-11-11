# Broadcast extras and device features

Date: 2026-11-11. The owner asked for "all available features" of the dongle and nrsc5 that
hdradio doesn't use yet. Every decision below is the builder's call unless marked otherwise.

## What and why

| Feature | Source | Where it shows |
|---|---|---|
| Album art | LOT files on a channel's PRIMARY_IMAGE component, chosen by the ID3 XHDR frame | web player, video card / lower third, `/art/{channel}.jpg` |
| Station logos | LOT files on the STATION_LOGO component | `/logo/{channel}.png` (Plex, Jellyfin, XMLTV, M3U, web); falls back to the generated logo |
| Emergency alerts | EMERGENCY_ALERT event (and the SIS alert text) | red banner in the web player and the video; `alert` in status |
| Device lost | LOST_DEVICE event | handled like a stall: tuner marked unavailable, service restarts |
| USB reset (Linux) | `USBDEVFS_RESET` ioctl on `/dev/bus/usb/BBB/DDD` | after a stall or lost device, before exiting for restart |
| Frequency calibration | SYNC `freq_offset` while HD is locked | measured ppm saved per dongle and applied when the dongle opens (`ppm = "auto"`, the new default) |
| Dongles by serial | librtlsdr `rtlsdr_get_index_by_serial` | `[[tuners]] serial = "..."`; `hdradio --list-dongles` prints index, name, serial |
| Audio details | AUDIO_SERVICE event (codec mode, blend) | `programs[].audio` in status; web signal line |
| Traffic and weather maps | HERE_IMAGE event (traffic: 3×3 tiles; weather: overlay) | `/maps/traffic.png`, `/maps/weather.png`, a Maps section in the web player |
| Transmitter location, broadcast time | SIS lat/long, STATION_LOCATION, LOCAL_TIME | `station.location`, `station.utc_offset` in status; web player details |

Not possible with the owner's dongle (generic RTL2832U + FC0012): bias tee, direct sampling
(AM). Not built.

## Behaviour

- Images are per channel (frequency + HD program). Album art follows the song: an XHDR frame
  naming a LOT id shows that image once it has arrived; an XHDR "flush" (param 1) or a new song
  without art clears it; with no XHDR seen, the newest primary image is used. Art is kept in
  memory (last 20 per tuner); logos are saved under `data_dir/logos/` so they survive restarts
  and are available before the station is tuned again.
- Images from the air are untrusted: decoded with Pillow, size-capped (≤ 2 MB input, ≤ 1024 px
  output), re-encoded (JPEG art, PNG logos), never served as received.
- Alerts are per tuner (they come from a station), shown to everyone listening to that tuner,
  cleared 30 minutes after the last repeat or on retune. Text is cleaned like station names.
- Calibration: average the offsets of ≥ 20 SYNC events at one frequency, convert to ppm
  (sign established on the real dongle during the build), round to an integer, and save it in
  `state.json` keyed by dongle serial (or index). Applied on the next open; never mid-stream.
  An explicit numeric `ppm` in the config wins.
- USB reset only on Linux, only when the stall handler fires, best effort (errors logged).
- Maps: keep the newest complete set per type; a traffic map is assembled when all tiles of one
  timestamp have arrived.

## Constraints

- Existing API fields and tests keep working; new fields only add.
- Nothing new runs while idle; image work happens on the event loop, not the DSP thread.
- Pi 3 budget: image decoding is occasional (art changes per song), so it's acceptable.
