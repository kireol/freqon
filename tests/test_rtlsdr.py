import ctypes

import pytest

from freqon import rtlsdr


class FakeLib:
    """Two dongles, like librtlsdr would report them."""
    def __init__(self):
        self.devs = [("Realtek", "RTL2838UHIDIR", "00000001"), ("Realtek", "RTL2838UHIDIR", "KITCHEN")]

    def rtlsdr_get_device_count(self):
        return len(self.devs)

    def rtlsdr_get_device_name(self, i):
        return b"Generic RTL2832U OEM"

    def rtlsdr_get_device_usb_strings(self, i, m, p, s):
        for buf, val in zip((m, p, s), self.devs[i], strict=True):
            ctypes.memmove(buf, val.encode() + b"\0", len(val) + 1)
        return 0

    def rtlsdr_get_index_by_serial(self, serial):
        serials = [d[2].encode() for d in self.devs]
        return serials.index(serial) if serial in serials else -3


def test_list_and_find_by_serial():
    lib = FakeLib()
    assert rtlsdr.list_dongles(lib) == [
        {"index": 0, "name": "Generic RTL2832U OEM", "manufacturer": "Realtek", "product": "RTL2838UHIDIR", "serial": "00000001"},
        {"index": 1, "name": "Generic RTL2832U OEM", "manufacturer": "Realtek", "product": "RTL2838UHIDIR", "serial": "KITCHEN"}]
    assert rtlsdr.index_for_serial("KITCHEN", lib) == 1
    with pytest.raises(LookupError, match="NOPE"):
        rtlsdr.index_for_serial("NOPE", lib)


def make_sysfs(tmp_path, devices):
    for name, (vid, pid, serial, bus, dev) in devices.items():
        d = tmp_path / name
        d.mkdir()
        for k, v in (("idVendor", vid), ("idProduct", pid), ("serial", serial), ("busnum", bus), ("devnum", dev)):
            (d / k).write_text(v + "\n")
    return str(tmp_path)


def test_usb_reset_finds_the_dongle_and_resets_it(tmp_path):
    root = make_sysfs(tmp_path, {"1-1.2": ("0bda", "2838", "00000001", "1", "4"),
                                 "1-1.3": ("0bda", "2838", "KITCHEN", "1", "7"),
                                 "1-1.4": ("046d", "c52b", "x", "1", "9")})
    calls = []
    assert rtlsdr.usb_reset(serial="KITCHEN", sysfs=root, reset=calls.append, platform="linux")
    assert calls == ["/dev/bus/usb/001/007"]
    calls.clear()
    (tmp_path / "one").mkdir()
    single = make_sysfs(tmp_path / "one", {"1-1.2": ("0bda", "2838", "", "1", "4")})
    assert rtlsdr.usb_reset(index=0, sysfs=single, reset=calls.append, platform="linux")  # the only dongle
    assert calls == ["/dev/bus/usb/001/004"]


def test_usb_reset_is_a_no_op_off_linux_or_when_missing(tmp_path):
    calls = []
    assert not rtlsdr.usb_reset(serial="X", sysfs=str(tmp_path), reset=calls.append, platform="darwin")
    assert not rtlsdr.usb_reset(serial="X", sysfs=str(tmp_path), reset=calls.append, platform="linux")
    assert calls == []



def test_usb_reset_by_index_refuses_when_ambiguous(tmp_path):
    root = make_sysfs(tmp_path, {"1-1.10": ("0bda", "2838", "00000001", "1", "4"),
                                 "1-1.2": ("0bda", "2838", "00000001", "1", "7")})
    calls = []
    assert not rtlsdr.usb_reset(index=1, sysfs=root, reset=calls.append, platform="linux")  # order unknowable
    assert not rtlsdr.usb_reset(serial="00000001", sysfs=root, reset=calls.append, platform="linux")  # duplicate
    assert calls == []


def test_usb_reset_never_raises(tmp_path):
    d = tmp_path / "1-1"
    d.mkdir()
    for k, v in (("idVendor", "0bda"), ("idProduct", "2838"), ("serial", "A"), ("busnum", "x"), ("devnum", "")):
        (d / k).write_text(v)
    assert rtlsdr.usb_reset(serial="A", sysfs=str(tmp_path), reset=lambda p: None, platform="linux") is False
