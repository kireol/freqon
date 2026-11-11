"""One tuner, several HD programs at once: each program has its own mixer and mode."""

import time
from types import SimpleNamespace

import numpy as np

from freqon import nrsc5
from freqon.radio import Radio, SourceConfig

CHUNK = 4410 * 4  # 0.1 s of stereo int16


def tone(value: int, nbytes: int = CHUNK) -> bytes:
    return np.full(nbytes // 2, value, dtype=np.int16).tobytes()


def level(pcm: bytes) -> int:
    return int(np.frombuffer(pcm, dtype=np.int16)[-1])


def make_radio():
    out: list[tuple[int, bytes]] = []
    radio = Radio(SourceConfig(), on_pcm=lambda program, pcm: out.append((program, pcm)))
    return radio, out


def audio_event(program: int, pcm: bytes, flags: int = 0):
    # flags like nrsc5's: non-zero marks concealment silence (frame missing or undecodable)
    return SimpleNamespace(program=program, data=pcm, flags=flags)


def test_each_open_program_gets_its_own_pcm():
    radio, out = make_radio()
    radio.open_program(0)
    radio.open_program(1)
    radio._dsp_mix(tone(1000))
    by_program = dict(out)
    assert set(by_program) == {0, 1}
    assert level(by_program[0]) == 1000  # HD1 falls back to analog
    assert level(by_program[1]) == 0  # HD2 has no analog version


def test_digital_for_one_program_leaves_the_other_alone():
    radio, out = make_radio()
    radio.open_program(0)
    radio.open_program(1)
    radio._on_event(nrsc5.EventType.AUDIO, audio_event(1, tone(2000, CHUNK * 15)))  # 1.5 s > prefill
    radio._dsp_mix(tone(1000))
    by_program = dict(out)
    assert level(by_program[1]) == 2000
    assert level(by_program[0]) == 1000
    assert radio.state.snapshot(1)["source"] == "digital"
    assert radio.state.snapshot(0)["source"] == "analog"


def test_closed_program_gets_no_pcm():
    radio, out = make_radio()
    radio.open_program(0)
    radio.open_program(2)
    radio.close_program(2)
    radio._dsp_mix(tone(1000))
    assert [p for p, _ in out] == [0]


def test_now_playing_and_mode_are_per_program():
    radio, _ = make_radio()
    radio.open_program(0)
    radio.open_program(1)
    radio._on_event(nrsc5.EventType.ID3, SimpleNamespace(program=1, title="News at 9", artist="WWJ",
                                                         album=None, genre=None))
    assert radio.state.snapshot(1)["now_playing"]["title"] == "News at 9"
    assert radio.state.snapshot(0)["now_playing"]["title"] is None
    radio.set_mode(0, "analog")
    assert radio.state.snapshot(0)["mode"] == "analog"
    assert radio.state.snapshot(1)["mode"] == "auto"
    assert radio.state.snapshot(1)["program"] == 1


def test_tuner_identity_and_tune_time():
    radio = Radio(SourceConfig(), index=3)
    assert radio.index == 3
    assert radio.tuned_at == 0.0


def test_stall_detection():
    radio = Radio(SourceConfig())
    assert not radio.is_stalled(now=100.0)  # not running
    radio.state.running = True
    radio.last_iq = 90.0
    assert not radio.is_stalled(now=95.0)
    assert radio.is_stalled(now=101.0)  # nothing from the dongle for 11 s


# -- broadcast extras ---------------------------------------------------------------------------

import io

from PIL import Image

from freqon.nrsc5 import (
    XHDR,
    AlertCategory,
    Blend,
    HEREImageType,
    MIMEType,
    ServiceType,
    SIGComponent,
    SIGDataComponent,
    SIGService,
)


def jpeg(color=(255, 0, 0)):
    buf = io.BytesIO()
    Image.new("RGB", (100, 100), color).save(buf, "JPEG")
    return buf.getvalue()


def lot_event(program, mime, lot_id, data):
    comp = SIGComponent(1, 1, None, SIGDataComponent(0x1000, None, None, mime))
    svc = SIGService(ServiceType.AUDIO, program + 1, "", [comp], None)
    return SimpleNamespace(lot=lot_id, data=data, component=comp, service=svc)


def test_album_art_and_logo_reach_the_right_program():
    radio, _ = make_radio()
    radio._on_event(nrsc5.EventType.LOT, lot_event(1, MIMEType.PRIMARY_IMAGE, 7, jpeg()))
    radio._on_event(nrsc5.EventType.LOT, lot_event(0, MIMEType.STATION_LOGO, 8, jpeg((0, 0, 255))))
    assert radio.extras.art(1) is not None and radio.extras.art(0) is None
    assert radio.extras.logo(0) is not None
    assert radio.state.snapshot(1)["art"] is True and radio.state.snapshot(0)["art"] is False


def test_id3_xhdr_and_song_change_drive_the_art():
    radio, _ = make_radio()
    radio._on_event(nrsc5.EventType.LOT, lot_event(0, MIMEType.PRIMARY_IMAGE, 5, jpeg()))
    id3 = {"program": 0, "title": "Song A", "artist": "X", "album": None, "genre": None, "ufid": None, "comments": [], "commercial": None}
    radio._on_event(nrsc5.EventType.ID3, SimpleNamespace(**id3, xhdr=XHDR(MIMEType.PRIMARY_IMAGE, 0, 5)))
    assert radio.extras.art(0) is not None
    radio._on_event(nrsc5.EventType.ID3, SimpleNamespace(**{**id3, "title": "Song B"}, xhdr=None))
    assert radio.extras.art(0) is None  # new song, its art not named/arrived yet


def test_alert_location_time_audio_and_maps():
    radio, _ = make_radio()
    radio._on_event(nrsc5.EventType.EMERGENCY_ALERT, SimpleNamespace(
        message="Flash flood warning", control_data=b"", categories=[AlertCategory.WEATHER],
        location_format=None, locations=[]))
    radio._on_event(nrsc5.EventType.STATION_LOCATION, SimpleNamespace(latitude=42.47, longitude=-83.25, altitude=300))
    radio._on_event(nrsc5.EventType.LOCAL_TIME, SimpleNamespace(utc_offset=-300, dst_regional=True, dst_local=True,
                                                                 dst_schedule=1))
    radio._on_event(nrsc5.EventType.LOCAL_TIME, SimpleNamespace(utc_offset=-300, dst_regional=True, dst_local=True,
                                                                 dst_schedule=1))
    radio._on_event(nrsc5.EventType.AUDIO_SERVICE, SimpleNamespace(
        program=0, access=None, type=None, codec_mode=1, blend_control=Blend.ENABLE, digital_audio_gain=0,
        common_delay=0, latency=0))
    buf = io.BytesIO()
    Image.new("RGB", (50, 40)).save(buf, "PNG")
    radio._on_event(nrsc5.EventType.HERE_IMAGE, SimpleNamespace(
        image_type=HEREImageType.WEATHER, seq=1, n1=1, n2=1, time_utc=None, latitude1=0, longitude1=0,
        latitude2=0, longitude2=0, name="w.png", data=buf.getvalue()))
    radio.open_program(0)
    snap = radio.state.snapshot(0)
    assert snap["alert"]["message"] == "Flash flood warning" and snap["alert"]["categories"] == ["Weather"]
    assert snap["station"]["location"] == {"latitude": 42.47, "longitude": -83.25}
    assert snap["station"]["utc_offset"] == -300
    assert snap["audio"] == {"codec_mode": 1, "blend": "enable"}
    assert snap["maps"] == ["weather"]


def test_lost_device_counts_as_stalled():
    radio, _ = make_radio()
    radio.state.running = True
    radio._on_event(nrsc5.EventType.LOST_DEVICE, None)
    assert radio.is_stalled()


def test_sync_feeds_calibration_and_retune_clears_extras():
    radio, _ = make_radio()
    radio.state.frequency = 97.1
    radio._on_event(nrsc5.EventType.SYNC, SimpleNamespace(freq_offset=971.0, psmi=0, pli=0, hppi=0, aabi=0, rdbi=0))
    assert radio.extras.ppm_estimate() == -10
    radio._on_event(nrsc5.EventType.LOT, lot_event(0, MIMEType.STATION_LOGO, 1, jpeg()))
    radio._reset_station_state()
    assert radio.extras.logo(0) is None and radio.extras.ppm_estimate() is None


def test_alert_text_is_logged_cleaned(caplog):
    radio, _ = make_radio()
    radio._on_event(nrsc5.EventType.EMERGENCY_ALERT, SimpleNamespace(
        message="Real alert\n2026-11-11 ERROR fake log line", control_data=b"", categories=[],
        location_format=None, locations=[]))
    logged = [r.getMessage() for r in caplog.records if "alert" in r.getMessage()]
    assert logged and all("\n" not in m for m in logged)



def test_bad_broadcast_data_never_escapes_the_callback(monkeypatch):
    radio, _ = make_radio()
    def explode(*a, **k):
        raise RuntimeError("bad data")
    monkeypatch.setattr(radio.extras, "lot", explode)
    radio._on_event(nrsc5.EventType.LOT, lot_event(0, MIMEType.STATION_LOGO, 1, jpeg()))  # must not raise


def test_ad_art_does_not_carry_onto_the_next_song():
    radio, _ = make_radio()
    base = {"artist": None, "album": None, "genre": None, "ufid": None, "comments": [], "commercial": None, "xhdr": None}
    radio._on_event(nrsc5.EventType.ID3, SimpleNamespace(program=0, title="Song A", **base))
    radio._on_event(nrsc5.EventType.ID3, SimpleNamespace(program=0, title=None, **base))  # ad break
    radio._on_event(nrsc5.EventType.LOT, lot_event(0, MIMEType.PRIMARY_IMAGE, 9, jpeg()))  # ad art
    radio._on_event(nrsc5.EventType.ID3, SimpleNamespace(program=0, title="Song B", **base))
    assert radio.extras.art(0) is None


def test_retune_drops_events_the_old_station_sends_while_stopping():
    radio, _ = make_radio()

    class Session:
        def stop(self):  # nrsc5 keeps delivering the old station's events until stop returns
            radio._on_event(nrsc5.EventType.EMERGENCY_ALERT, SimpleNamespace(
                message="Old station alert", control_data=b"", categories=[], location_format=None, locations=[]))

        def set_auto_gain(self, on): pass
        def set_frequency(self, hz): pass
        def start(self): pass

    radio._radio = Session()
    radio._retune(101.9)
    assert radio.extras.current_alert(time.time()) is None


def test_flagged_hd_frames_count_as_missing_audio():
    from freqon.mixer import HDPolicy
    radio, _out = make_radio()
    radio.hd_policy = HDPolicy()
    radio.open_program(0)
    radio._on_event(nrsc5.EventType.AUDIO, audio_event(0, tone(2000, CHUNK * 3)))
    radio._on_event(nrsc5.EventType.AUDIO, audio_event(0, tone(0, CHUNK), flags=1))  # UNAVAILABLE
    radio._dsp_mix(tone(1000))
    assert radio.state.snapshot(0)["digital"]["quality"] == 0.75
