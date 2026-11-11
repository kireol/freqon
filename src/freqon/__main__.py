"""freqon service entry point.

Settings come from (lowest to highest priority): built-in defaults, a TOML config file
(--config, default /etc/freqon/config.toml or ~/.config/freqon/config.toml if present),
then command-line flags.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import tomllib

DEFAULTS = {
    "host": "0.0.0.0",
    "port": 8080,
    "public_url": None,  # e.g. "http://raspberrypi.local:8080" if clients see a different address
    "friendly_name": "Freqon",
    "allowed_hosts": [],  # extra host names clients use, e.g. ["radio.home.arpa"]
    "data_dir": None,  # station list lives here; default ~/.local/share/freqon
    "device_index": 0,
    "rtltcp": None,  # "host:port" to use a remote rtl_tcp server instead of a local dongle
    "iq_file": None,  # play a cu8 recording instead of a dongle (development)
    "gain": None,  # dB, or omit for automatic
    "ppm": "auto",  # frequency correction: "auto" = measured from HD signals and saved; or a number
    # More than one tuner: a list of tables with the keys in TUNER_KEYS ([[tuners]] in TOML).
    # Keys a tuner leaves out come from the top-level settings above.
    "tuners": None,
    "analog_gain": 0.6,  # analog loudness relative to digital
    "idle_timeout": 30,  # seconds without listeners before the tuner is released (dongle powered down)
    "nrsc5_lib": None,  # path to libnrsc5.so / .dylib if not installed system-wide
    "ffmpeg": "ffmpeg",
    "log_file": None,
    # Taking a busy tuner: the web player's "Take over", and Plex/Jellyfin opening another
    # channel. false: whoever has the tuner keeps it; others get "busy".
    "allow_takeovers": True,
    # Weak HD (Auto mode, HD1): nrsc5 fills undecodable stretches with silence. Switch to FM
    # after HD has been weak this many seconds (0 = off, the old rule), back to HD after it
    # has been solid this many, and stay on FM after hd_max_fallbacks drops within
    # hd_fallback_window_seconds (until the mode is chosen again or the station changes).
    "hd_fallback_seconds": 1.5,
    "hd_return_seconds": 5,
    "hd_max_fallbacks": 3,
    "hd_fallback_window_seconds": 300,
    # Optional login for the web player (both, or neither). The password can also come from
    # the FREQON_WEB_PASSWORD environment variable. Plex/Jellyfin endpoints stay open.
    "web_username": None,
    "web_password": None,  # append logs (and crash tracebacks) here instead of the console
    # Camera picture for the Plex/Jellyfin video stream, with the now-playing bar burned in.
    "video_source": None,  # e.g. "rtsp://go2rtc.local:8554/camera"; anything ffmpeg can open
    "video_size": "1280x720",  # Pi 3: "640x360"
    "video_fps": 15,  # Pi 3: 10
    "video_encoder": "libx264",  # Pi: "h264_v4l2m2m" (hardware)
    "video_bitrate": "2500k",
    "scan_threshold_db": 6.0,
    # Background scans for new stations/HD channels, on a free tuner only (adds, never removes;
    # gives way at once to anyone who starts listening). While the radio is in use (a stream open
    # within the last hour): every auto_scan_active_hours; otherwise every auto_scan_idle_days. 0 = off.
    "auto_scan_idle_days": 30,
    "auto_scan_active_hours": 1,
    "scan_gain_db": 0.0,
    "log_level": "INFO",
    "access_log": False,
    # A dongle that stops sending samples can't be reset from inside the process (nrsc5 hangs),
    # so exit and let the service manager (systemd: Restart=on-failure) start a fresh one.
    "exit_on_stall": True,  # log every HTTP request (handy when setting up Plex or a new client)
}

TUNER_KEYS = ("device_index", "serial", "rtltcp", "iq_file", "gain", "ppm")

CONFIG_LOCATIONS = ["/etc/freqon/config.toml", os.path.expanduser("~/.config/freqon/config.toml")]
# Where configs lived before the project was named Freqon; used only when no new one exists.
LEGACY_CONFIG_LOCATIONS = ["/etc/hdradio/config.toml", os.path.expanduser("~/.config/hdradio/config.toml")]
LEGACY_DATA_DIR = "~/.local/share/hdradio"


def migrate_legacy_data(new: str, old: str) -> bool:
    """Move the data folder from before the Freqon name (stations, logos, calibration) to its new
    place. Only when the new folder doesn't exist yet; never overwrites. True if moved."""
    if os.path.exists(new) or not os.path.isdir(old):
        return False
    os.makedirs(os.path.dirname(os.path.abspath(new)), exist_ok=True)
    os.rename(old, new)
    return True


def load_config(argv: list[str] | None = None) -> dict:
    p = argparse.ArgumentParser(prog="freqon", description="Network HD Radio / FM tuner service")
    p.add_argument("--config", help="TOML config file")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--public-url", dest="public_url")
    p.add_argument("--data-dir", dest="data_dir")
    p.add_argument("--device-index", dest="device_index", type=int)
    p.add_argument("--rtltcp", help="use an rtl_tcp server, host:port")
    p.add_argument("--iq-file", dest="iq_file", help="play a cu8 I/Q recording (1488375 S/s)")
    p.add_argument("--gain", type=float, help="tuner gain in dB (default: automatic)")
    p.add_argument("--ppm", help='frequency correction in ppm, or "auto"')
    p.add_argument("--list-dongles", dest="list_dongles", action="store_true", default=None,
                   help="list the RTL-SDR dongles (index, name, serial) and exit")
    p.add_argument("--idle-timeout", dest="idle_timeout", type=float)
    p.add_argument("--nrsc5-lib", dest="nrsc5_lib")
    p.add_argument("--ffmpeg")
    p.add_argument("--video-source", dest="video_source", help="camera URL for the video stream")
    p.add_argument("--log-level", dest="log_level")
    p.add_argument("--log-file", dest="log_file", help="append logs to this file")
    p.add_argument("--web-username", dest="web_username", help="require this username for the web player")
    p.add_argument("--web-password", dest="web_password",
                   help="its password (or set FREQON_WEB_PASSWORD, which other users can't see)")
    p.add_argument("--access-log", dest="access_log", action="store_true", default=None,
                   help="log every HTTP request")
    args = p.parse_args(argv)

    cfg = dict(DEFAULTS)
    path = args.config or next((c for c in CONFIG_LOCATIONS + LEGACY_CONFIG_LOCATIONS if os.path.exists(c)), None)
    if path:
        with open(path, "rb") as f:
            file_cfg = tomllib.load(f)
        unknown = set(file_cfg) - set(DEFAULTS)
        if unknown:
            p.error(f"unknown config keys in {path}: {', '.join(sorted(unknown))}")
        for i, t in enumerate(file_cfg.get("tuners") or []):
            bad = set(t) - set(TUNER_KEYS) if isinstance(t, dict) else {"(not a table)"}
            if bad:
                p.error(f"unknown keys in tuner {i + 1} in {path}: {', '.join(sorted(bad))}; "
                        f"allowed: {', '.join(TUNER_KEYS)}")
        cfg.update(file_cfg)
    cfg.update({k: v for k, v in vars(args).items() if v is not None and k != "config"})
    if not cfg["web_password"] and os.environ.get("FREQON_WEB_PASSWORD"):
        cfg["web_password"] = os.environ["FREQON_WEB_PASSWORD"]
    if bool(cfg["web_username"]) != bool(cfg["web_password"]):
        p.error("a web login needs both web_username and web_password (or neither, for no login)")
    for key in ("hd_fallback_seconds", "hd_return_seconds", "hd_fallback_window_seconds"):
        if not isinstance(cfg[key], (int, float)) or cfg[key] < 0:
            p.error(f"{key} must be a number of seconds (0 or more)")
    if not isinstance(cfg["hd_max_fallbacks"], int) or cfg["hd_max_fallbacks"] < 1:
        p.error("hd_max_fallbacks must be a whole number, 1 or more")
    if not isinstance(cfg["allow_takeovers"], bool):
        p.error("allow_takeovers must be true or false")
    cfg["data_dir_default"] = not cfg["data_dir"]
    cfg["data_dir"] = os.path.expanduser(cfg["data_dir"] or "~/.local/share/freqon")
    cfg["_config_path"] = path
    return cfg


def stall_handler(cfg: dict, exit=os._exit, reset=None):
    """What to do when a tuner's dongle stalls: reset it at the USB level (Linux), then exit
    (code 75) so the service gets restarted with a fresh session."""
    def on_stall(radio) -> None:
        log = logging.getLogger("freqon")
        src = getattr(radio, "source_cfg", None)
        if src is not None and not src.rtltcp and not src.iq_file:
            from . import rtlsdr
            try:
                (reset or rtlsdr.usb_reset)(serial=src.serial, index=None if src.serial else src.device_index)
            except Exception:
                log.exception("USB reset failed; restarting anyway")
        if not cfg.get("exit_on_stall", True):
            log.error("tuner %d stalled; exit_on_stall is off, so it stays unavailable", radio.index)
            return
        log.critical("tuner %d stalled (no samples from the dongle); exiting so the service restarts",
                     radio.index)
        logging.shutdown()
        exit(75)
    return on_stall


def hd_policy(cfg: dict):
    """The weak-HD rule from the settings, or None when hd_fallback_seconds is 0."""
    from .mixer import HDPolicy

    if not cfg["hd_fallback_seconds"]:
        return None
    return HDPolicy(fallback_seconds=float(cfg["hd_fallback_seconds"]),
                    return_seconds=float(cfg["hd_return_seconds"]),
                    max_fallbacks=int(cfg["hd_max_fallbacks"]),
                    window_seconds=float(cfg["hd_fallback_window_seconds"]))


def tuner_sources(cfg: dict):
    """One SourceConfig per tuner: from [[tuners]] if given, else the top-level settings."""
    from .radio import SourceConfig

    def ppm(value):
        if value in (None, "auto"):
            return 0, True
        try:
            return int(value), False
        except (TypeError, ValueError):
            sys.exit(f'ppm must be a whole number or "auto", not {value!r}')

    out = []
    for t in cfg.get("tuners") or [{}]:
        p, auto = ppm(t.get("ppm", cfg["ppm"]))
        out.append(SourceConfig(device_index=t.get("device_index", cfg["device_index"]), serial=t.get("serial"),
                                rtltcp=t.get("rtltcp", cfg["rtltcp"]), iq_file=t.get("iq_file", cfg["iq_file"]),
                                gain_db=t.get("gain", cfg["gain"]), ppm=p, ppm_auto=auto))
    return out


def video_options(cfg: dict) -> dict:
    size = str(cfg["video_size"]).lower()
    try:
        w, h = (int(x) for x in size.split("x"))
        if not (16 <= w <= 3840 and 16 <= h <= 2160 and w % 2 == 0 and h % 2 == 0):
            raise ValueError
    except ValueError:
        sys.exit(f"video_size must look like 1280x720 (even numbers), not {cfg['video_size']!r}")
    return {"source": cfg["video_source"], "size": (w, h), "fps": int(cfg["video_fps"]),
            "encoder": cfg["video_encoder"], "bitrate": str(cfg["video_bitrate"])}


def setup_logging(cfg: dict) -> None:
    if cfg.get("log_file"):
        path = os.path.expanduser(cfg["log_file"])
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        # stderr too, so an uncaught exception's traceback lands in the file.
        sys.stderr = open(path, "a", buffering=1, encoding="utf-8", errors="replace")  # noqa: SIM115
    logging.basicConfig(level=cfg["log_level"].upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    cfg = load_config(argv)
    if cfg.get("list_dongles"):
        from . import rtlsdr
        dongles = rtlsdr.list_dongles()
        for d in dongles:
            print(f"{d['index']}: {d['name']} — {d['manufacturer']} {d['product']}, serial {d['serial']}")
        if not dongles:
            print("no RTL-SDR dongles found")
        sys.exit(0)
    if cfg.get("data_dir_default") and migrate_legacy_data(cfg["data_dir"], os.path.expanduser(LEGACY_DATA_DIR)):
        print(f"freqon: moved {LEGACY_DATA_DIR} to {cfg['data_dir']}", file=sys.stderr)  # before the name change
    setup_logging(cfg)
    log = logging.getLogger("freqon")

    if cfg["nrsc5_lib"]:
        os.environ["NRSC5_LIB"] = os.path.abspath(os.path.expanduser(cfg["nrsc5_lib"]))
    if not shutil.which(cfg["ffmpeg"]):
        sys.exit(f"ffmpeg not found ({cfg['ffmpeg']}); install it or set 'ffmpeg' in the config")

    # Imported late so NRSC5_LIB is set before the library loads.
    from aiohttp import web

    from .api import make_app
    from .pool import TunerPool
    from .radio import Radio
    from .service import Controller
    from .stations import StationStore

    policy = hd_policy(cfg)
    radios = [Radio(src, analog_gain=cfg["analog_gain"], index=i, hd_policy=policy)
              for i, src in enumerate(tuner_sources(cfg))]
    stations = StationStore(os.path.join(cfg["data_dir"], "stations.json"))
    controller = Controller(TunerPool(radios), stations, idle_timeout=cfg["idle_timeout"], ffmpeg=cfg["ffmpeg"],
                            scan_options={"threshold_db": cfg["scan_threshold_db"],
                                          "scan_gain_db": cfg["scan_gain_db"]},
                            video=video_options(cfg), on_tuner_stalled=stall_handler(cfg),
                            auto_scan_idle_days=float(cfg["auto_scan_idle_days"]),
                            auto_scan_active_hours=float(cfg["auto_scan_active_hours"]),
                            state_path=os.path.join(cfg["data_dir"], "state.json"),
                            logos_dir=os.path.join(cfg["data_dir"], "logos"),
                            allow_takeovers=cfg["allow_takeovers"])
    app = make_app(controller, cfg)
    log.info("config: %s; data: %s; %d tuner(s)", cfg["_config_path"] or "defaults", cfg["data_dir"], len(radios))
    access = logging.getLogger("freqon.access") if cfg["access_log"] else None
    web.run_app(app, host=cfg["host"], port=cfg["port"], access_log=access,
                access_log_format='%a "%r" %s %b "%{User-Agent}i" %Tfs',
                print=lambda msg: log.info(msg.strip()))


if __name__ == "__main__":
    main()
