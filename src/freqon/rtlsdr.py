"""Small helpers that talk to RTL-SDR dongles directly (nrsc5 only opens them by index).

- list_dongles() / index_for_serial(): pick a dongle by its EEPROM serial number, which stays
  the same when USB order changes. Uses librtlsdr (the library nrsc5 itself is built on).
- usb_reset(): on Linux, reset a dongle at the USB level (like unplugging it), used to recover
  one that stopped sending samples. Needs write access to /dev/bus/usb/..., which the usual
  rtl-sdr udev rules give.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import sys
from collections.abc import Callable

log = logging.getLogger(__name__)

RTL_IDS = {("0bda", "2832"), ("0bda", "2838")}  # Realtek RTL2832U dongles
USBDEVFS_RESET = 0x5514  # _IO('U', 20)


def _lib():
    path = os.environ.get("RTLSDR_LIB") or ctypes.util.find_library("rtlsdr")
    if not path:
        for guess in ("/opt/homebrew/lib/librtlsdr.dylib", "/usr/local/lib/librtlsdr.dylib", "librtlsdr.so.0"):
            if guess.startswith("lib") or os.path.exists(guess):
                path = guess
                break
    lib = ctypes.CDLL(path)
    lib.rtlsdr_get_device_count.restype = ctypes.c_uint32
    lib.rtlsdr_get_device_name.restype = ctypes.c_char_p
    lib.rtlsdr_get_device_name.argtypes = [ctypes.c_uint32]
    lib.rtlsdr_get_index_by_serial.argtypes = [ctypes.c_char_p]
    return lib


def list_dongles(lib=None) -> list[dict]:
    lib = lib or _lib()
    out = []
    for i in range(lib.rtlsdr_get_device_count()):
        m, p, s = (ctypes.create_string_buffer(256) for _ in range(3))
        lib.rtlsdr_get_device_usb_strings(i, m, p, s)
        name = lib.rtlsdr_get_device_name(i) or b""
        out.append({"index": i, "name": name.decode(errors="replace"), "manufacturer": m.value.decode(errors="replace"),
                    "product": p.value.decode(errors="replace"), "serial": s.value.decode(errors="replace")})
    return out


def index_for_serial(serial: str, lib=None) -> int:
    lib = lib or _lib()
    idx = lib.rtlsdr_get_index_by_serial(serial.encode())
    if idx < 0:
        raise LookupError(f"no RTL-SDR dongle with serial {serial!r} (see: freqon --list-dongles)")
    return idx


def _ioctl_reset(path: str) -> None:
    import fcntl  # Linux/macOS only; imported lazily for portability

    fd = os.open(path, os.O_WRONLY)
    try:
        fcntl.ioctl(fd, USBDEVFS_RESET, 0)
    finally:
        os.close(fd)


def usb_reset(serial: str | None = None, index: int | None = None, *, sysfs: str = "/sys/bus/usb/devices",
              reset: Callable[[str], None] = _ioctl_reset, platform: str = sys.platform) -> bool:
    """Reset a dongle at the USB level: the one with this serial, or the only dongle present.
    Refuses when it can't be sure which dongle is meant. Returns True if a reset was issued.
    Linux only; anywhere else it does nothing. Never raises."""
    try:
        return _usb_reset(serial, index, sysfs, reset, platform)
    except Exception as e:  # noqa: BLE001 -- a failed reset must never block the restart after it
        log.warning("USB reset skipped: %s", e)
        return False


def _usb_reset(serial, index, sysfs, reset, platform) -> bool:
    if not platform.startswith("linux") or not os.path.isdir(sysfs):
        return False

    def read(d, name):
        try:
            with open(os.path.join(sysfs, d, name)) as f:
                return f.read().strip()
        except OSError:
            return ""

    rtl = sorted(d for d in os.listdir(sysfs) if (read(d, "idVendor"), read(d, "idProduct")) in RTL_IDS)
    if serial:
        match = [d for d in rtl if read(d, "serial") == serial]
    elif len(rtl) == 1 and index in (0, None):
        match = rtl  # the only dongle
    else:
        # librtlsdr's index follows libusb's order, which sysfs names can't tell us: with several
        # dongles and no serial, resetting a guess could hit a healthy one.
        log.warning("not resetting: %d RTL dongles and no serial to pick one (set serial = ...)", len(rtl))
        return False
    if len(match) != 1:
        if match:
            log.warning("not resetting: %d dongles share serial %r", len(match), serial)
        return False
    d = match[0]
    path = f"/dev/bus/usb/{int(read(d, 'busnum')):03d}/{int(read(d, 'devnum')):03d}"
    try:
        reset(path)
    except OSError as e:
        log.warning("USB reset of %s failed: %s", path, e)
        return False
    log.warning("reset the dongle at %s", path)
    return True
