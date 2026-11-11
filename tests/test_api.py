import xml.etree.ElementTree as ET

import pytest
from fakes import FakeRadio, fake_encoders

from freqon.api import make_app
from freqon.pool import TunerPool
from freqon.service import Controller
from freqon.stations import Station, StationStore


@pytest.fixture
def store(tmp_path):
    s = StationStore(str(tmp_path / "stations.json"))
    s.upsert(Station(97.1, name="WXYT", hd=True,
                     programs=[{"program": 0, "name": "HD1"}, {"program": 1, "name": "HD2", "type": "News"}]))
    s.upsert(Station(101.1, name=None, hd=False))
    return s


@pytest.fixture
async def setup(aiohttp_client, store):
    radio = FakeRadio()
    ctl = Controller(TunerPool([radio]), store, make_encoders=fake_encoders())
    client = await aiohttp_client(make_app(ctl))
    return client, ctl, radio


async def test_tune_and_status(setup):
    client, _ctl, radio = setup
    r = await client.post("/api/tune", json={"frequency": 97.1, "program": 1, "client_id": "a"})
    assert r.status == 200
    body = await r.json()
    assert body["frequency"] == 97.1 and body["program"] == 1
    assert body["channel_id"] == "971.2"
    assert radio.tunes == [97.1]


async def test_tune_by_channel_id(setup):
    client, _ctl, radio = setup
    r = await client.post("/api/tune", json={"channel": "971.2"})
    assert r.status == 200
    assert radio.tunes == [97.1]


async def test_rejects_out_of_band_frequency(setup):
    client, _ctl, _radio = setup
    r = await client.post("/api/tune", json={"frequency": 120.5})
    assert r.status == 400


async def test_busy_when_someone_else_is_listening(setup):
    client, ctl, radio = setup
    await client.post("/api/tune", json={"frequency": 97.1, "client_id": "plex"})
    ctl.add_listener("plex", "Plex", "plex")
    r = await client.post("/api/tune", json={"frequency": 99.5, "client_id": "web1"})
    assert r.status == 409
    body = await r.json()
    assert body["error"] == "busy"
    assert "Plex" in body["message"]
    assert radio.tunes == [97.1]


async def test_same_station_is_not_a_conflict(setup):
    client, ctl, _radio = setup
    await client.post("/api/tune", json={"frequency": 97.1, "client_id": "plex"})
    ctl.add_listener("plex", "Plex", "plex")
    r = await client.post("/api/tune", json={"frequency": 97.1, "client_id": "web1"})
    assert r.status == 200


async def test_different_hd_program_is_not_a_conflict(setup):
    client, ctl, radio = setup
    await client.post("/api/tune", json={"frequency": 97.1, "client_id": "plex"})
    ctl.add_listener("plex", "Plex", "plex")
    r = await client.post("/api/tune", json={"frequency": 97.1, "program": 1, "client_id": "web1"})
    assert r.status == 200
    assert (await r.json())["channel_id"] == "971.2"
    assert radio.tunes == [97.1] and set(radio.state.mixers) == {0, 1}


async def test_override_takes_the_tuner(setup):
    client, ctl, radio = setup
    await client.post("/api/tune", json={"frequency": 97.1, "client_id": "plex"})
    ctl.add_listener("plex", "Plex", "plex")
    r = await client.post("/api/tune", json={"frequency": 99.5, "client_id": "web1", "override": True})
    assert r.status == 200
    assert radio.state.frequency == 99.5


async def test_own_listening_does_not_block_yourself(setup):
    client, ctl, _radio = setup
    await client.post("/api/tune", json={"frequency": 97.1, "client_id": "web1"})
    ctl.add_listener("web1", "Web", "web")
    r = await client.post("/api/tune", json={"frequency": 99.5, "client_id": "web1"})
    assert r.status == 200


async def test_free_once_listener_leaves(setup):
    client, ctl, _radio = setup
    await client.post("/api/tune", json={"frequency": 97.1, "client_id": "plex"})
    lst = ctl.add_listener("plex", "Plex", "plex")
    ctl.remove_listener(lst)
    r = await client.post("/api/tune", json={"frequency": 99.5, "client_id": "web1"})
    assert r.status == 200


async def test_mode_change_never_needs_override(setup):
    client, ctl, _radio = setup
    await client.post("/api/tune", json={"frequency": 97.1, "client_id": "plex"})
    ctl.add_listener("plex", "Plex", "plex")
    r = await client.post("/api/mode", json={"mode": "analog", "client_id": "web1"})
    assert r.status == 200
    assert (await r.json())["mode"] == "analog"
    r = await client.post("/api/mode", json={"mode": "loud"})
    assert r.status == 400


async def test_scan_is_busy_when_others_listen(setup):
    client, ctl, _radio = setup
    await client.post("/api/tune", json={"frequency": 97.1, "client_id": "plex"})
    ctl.add_listener("plex", "Plex", "plex")
    r = await client.post("/api/scan", json={"client_id": "web1"})
    assert r.status == 409


async def test_jellyfin_channel_change_takes_the_tuner_and_web_follows(setup):
    # Listening on the web; Jellyfin on another device opens a different station: the tuner
    # switches to it, Jellyfin plays, and the web player moves to the new station too.
    client, ctl, radio = setup
    await client.post("/api/tune", json={"frequency": 97.1, "client_id": "web1"})
    ctl.add_listener("web1", "Web", "web")
    r = await client.get("/stream/101.1/0.ts?client_id=jellyfin&client_name=Jellyfin&kind=jellyfin")
    assert r.status == 200
    assert radio.state.frequency == 101.1
    assert ctl.sessions["web1"].frequency == 101.1
    r.close()


async def test_a_browser_cannot_take_the_tuner_through_a_video_url(setup):
    # Every current browser sends Sec-Fetch-Site; Plex and Jellyfin (server-side) don't. Even a
    # same-site page loading the video URL in a <video> tag gets "busy", not the tuner.
    client, ctl, radio = setup
    await client.post("/api/tune", json={"frequency": 97.1, "client_id": "web1"})
    ctl.add_listener("web1", "Web", "web")
    for site in ("same-origin", "same-site", "none"):
        r = await client.get("/stream/99.5/0.ts", headers={"Sec-Fetch-Site": site})
        assert r.status == 503, site
    assert radio.state.frequency == 97.1


async def test_plex_channel_change_takes_the_tuner_too(setup):
    client, ctl, radio = setup
    await client.post("/api/tune", json={"frequency": 97.1, "client_id": "web1"})
    ctl.add_listener("web1", "Web", "web")
    r = await client.get("/stream/99.5/0.ts")
    assert r.status == 200 and radio.state.frequency == 99.5
    r.close()


async def test_stream_current_requires_tuning(setup):
    client, _ctl, _radio = setup
    r = await client.get("/stream.mp3")
    assert r.status == 409


async def test_stations_and_rename(setup):
    client, _ctl, _radio = setup
    r = await client.get("/api/stations")
    stations = (await r.json())["stations"]
    assert [s["frequency"] for s in stations] == [97.1, 101.1]
    assert [c["number"] for c in stations[0]["channels"]] == ["971.1", "971.2"]
    assert stations[1]["display_name"] == "101.1 FM"
    r = await client.patch("/api/stations/101.1", json={"custom_name": "WRIF"})
    assert r.status == 200 and (await r.json())["display_name"] == "WRIF"
    r = await client.patch("/api/stations/101.1", json={"frequency": 5})
    assert r.status == 400
    r = await client.patch("/api/stations/88.1", json={"custom_name": "x"})
    assert r.status == 404


async def test_plex_integration_files(setup):
    client, _ctl, _radio = setup
    m3u = await (await client.get("/playlist.m3u")).text()
    assert m3u.startswith("#EXTM3U")
    assert 'tvg-chno="971.2"' in m3u and "/stream/97.1/1.ts" in m3u
    lineup = await (await client.get("/lineup.json")).json()
    assert [c["GuideNumber"] for c in lineup] == ["971.1", "971.2", "1011.1"]
    assert [c.get("HD") for c in lineup] == [1, 1, None]
    disc = await (await client.get("/discover.json")).json()
    assert disc["TunerCount"] == 1 and disc["LineupURL"].endswith("/lineup.json")
    xml = await (await client.get("/xmltv.xml")).text()
    assert 'channel id="hdradio.971.2"' in xml
    logo = await client.get("/logo/971.2.png")
    assert logo.status == 200 and logo.content_type == "image/png"


async def test_web_client_served(setup):
    client, _ctl, _radio = setup
    r = await client.get("/")
    assert r.status == 200 and "Tune to station" in await r.text()
    assert (await client.get("/static/app.js")).status == 200
    assert (await client.get("/static/../api.py")).status == 404


def test_scan_merge_keeps_user_edits(store):
    store.update(101.1, custom_name="WRIF")
    store.update(97.1, hidden=True)
    store.merge_scan([Station(97.1, name="WXYT", hd=True), Station(99.5, name="WYCD", hd=True)])
    by_f = {s.frequency: s for s in store.all(include_hidden=True)}
    assert by_f[97.1].hidden is True
    assert by_f[101.1].custom_name == "WRIF"  # missed by the scan but customised, so kept
    assert 99.5 in by_f
    reloaded = StationStore(store.path)
    assert {s.frequency for s in reloaded.all(include_hidden=True)} == {97.1, 99.5, 101.1}


async def test_rejects_non_json_control_requests(setup):
    client, _ctl, radio = setup
    # A cross-site <form> or text/plain fetch skips CORS preflight; it must not change the station.
    r = await client.post("/api/tune", data='{"frequency": 99.5}', headers={"Content-Type": "text/plain"})
    assert r.status == 415
    assert radio.tunes == []


async def test_rejects_cross_site_origin(setup):
    client, _ctl, radio = setup
    r = await client.post("/api/tune", json={"frequency": 99.5}, headers={"Origin": "https://evil.example"})
    assert r.status == 403
    assert radio.tunes == []
    r = await client.get("/api/events", headers={"Origin": "https://evil.example"})
    assert r.status == 403


async def test_same_origin_and_non_browser_clients_allowed(setup):
    client, _ctl, _radio = setup
    host = f"{client.host}:{client.port}"
    r = await client.post("/api/tune", json={"frequency": 99.5}, headers={"Origin": f"http://{host}"})
    assert r.status == 200
    r = await client.post("/api/tune", json={"frequency": 97.1})  # no Origin: Plex, curl
    assert r.status == 200


async def test_station_edits_are_validated(setup):
    client, _ctl, _radio = setup
    assert (await client.patch("/api/stations/101.1", json={"custom_name": {"x": 1}})).status == 400
    assert (await client.patch("/api/stations/101.1", json={"hidden": "yes"})).status == 400
    r = await client.patch("/api/stations/101.1", json={"custom_name": "A" * 500})
    assert r.status == 200 and len((await r.json())["display_name"]) == 64


async def test_names_cannot_inject_playlist_or_guide_entries(setup):
    client, ctl, _radio = setup
    evil = 'Bad",x\n#EXTINF:-1,Injected\nhttp://evil/</display-name><x a="'
    r = await client.patch("/api/stations/101.1", json={"custom_name": evil})
    assert r.status == 200
    ctl.stations.get(97.1).name = 'Air"\r\nname'  # names broadcast over the air are untrusted too
    m3u = await (await client.get("/playlist.m3u")).text()
    entries = [line for line in m3u.splitlines() if line.startswith("#EXTINF")]
    assert len(entries) == 3
    assert all(line.startswith(("#EXTINF", "http")) for line in m3u.splitlines()[1:])
    guide = await (await client.get("/xmltv.xml")).text()
    ET.fromstring(guide.encode())  # still well-formed


async def test_cross_site_page_cannot_tune_through_a_stream_url(setup):
    client, _ctl, radio = setup
    # e.g. <audio src="http://pi:8080/stream/99.5/0.mp3?override=1"> on another website
    for headers in ({"Sec-Fetch-Site": "cross-site"}, {"Referer": "https://evil.example/page"}):
        r = await client.get("/stream/99.5/0.mp3?override=1", headers=headers)
        assert r.status == 403
    assert radio.tunes == []


async def test_dns_rebinding_host_is_refused(setup):
    client, _ctl, radio = setup
    # After rebinding, the browser's Origin and Host both name the attacker's domain.
    h = {"Host": "evil.example:8080", "Origin": "http://evil.example:8080"}
    assert (await client.post("/api/tune", json={"frequency": 99.5}, headers=h)).status == 421
    assert (await client.get("/api/status", headers={"Host": "evil.example:8080"})).status == 421
    assert radio.tunes == []


async def test_configured_and_local_names_are_accepted(aiohttp_client, store):
    import socket
    ctl = Controller(TunerPool([FakeRadio()]), store)
    client = await aiohttp_client(make_app(ctl, {"allowed_hosts": ["radio.home.arpa"]}))
    me = socket.gethostname().split(".")[0]
    for host in ("radio.home.arpa:8080", f"{me}.local:8080", "localhost", "192.168.1.20:8080", "[::1]:8080"):
        r = await client.get("/api/status", headers={"Host": host})
        assert r.status == 200, host


async def test_stream_urls_never_take_over(setup):
    client, ctl, radio = setup
    await client.post("/api/tune", json={"frequency": 97.1, "client_id": "plex"})
    ctl.add_listener("plex", "Plex", "plex")
    # No browser headers at all (old browser + no-referrer page) still can't take the tuner.
    r = await client.get("/stream/99.5/0.mp3?override=1")
    assert r.status == 503
    assert radio.state.frequency == 97.1


@pytest.fixture
async def two_tuners(aiohttp_client, store):
    radios = [FakeRadio(0), FakeRadio(1)]
    ctl = Controller(TunerPool(radios), store, make_encoders=fake_encoders())
    client = await aiohttp_client(make_app(ctl))
    return client, ctl, radios


async def test_status_is_per_client(two_tuners):
    client, _ctl, _radios = two_tuners
    await client.post("/api/tune", json={"frequency": 97.1, "program": 1, "client_id": "a"})
    await client.post("/api/tune", json={"frequency": 101.1, "client_id": "b"})
    a = await (await client.get("/api/status?client_id=a")).json()
    b = await (await client.get("/api/status?client_id=b")).json()
    assert (a["channel_id"], a["tuner"]) == ("971.2", 0)
    assert (b["channel_id"], b["tuner"]) == ("1011.1", 1)
    assert [t["frequency"] for t in a["tuners"]] == [97.1, 101.1]


async def test_events_are_per_client(two_tuners):
    client, _ctl, _radios = two_tuners
    await client.post("/api/tune", json={"frequency": 97.1, "client_id": "a"})
    await client.post("/api/tune", json={"frequency": 101.1, "client_id": "b"})
    ws = await client.ws_connect("/api/events?client_id=b")
    msg = await ws.receive_json(timeout=2)
    assert msg["status"]["frequency"] == 101.1
    await ws.close()


async def read_until(resp, needle: bytes, feed, timeout=2.0):
    import asyncio
    got = b""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while needle not in got:
        assert loop.time() < end, got
        feed()
        try:
            got += await asyncio.wait_for(resp.content.readany(), 0.05)
        except TimeoutError:
            pass
    return got


async def test_stream_follows_retune(setup):
    client, ctl, _radio = setup
    await client.post("/api/tune", json={"frequency": 97.1, "client_id": "a"})
    resp = await client.get("/stream.mp3?client_id=a")
    assert resp.status == 200
    await read_until(resp, b"mp3-97.1-0:one", lambda: ctl._feed(0, 0, b"one"))
    r = await client.post("/api/tune", json={"frequency": 99.5, "client_id": "a"})
    assert r.status == 200
    await read_until(resp, b"mp3-99.5-0:two", lambda: ctl._feed(0, 0, b"two"))
    assert not resp.content.at_eof()
    assert [l["channel_id"] for l in (await (await client.get("/api/status?client_id=a")).json())["listeners"]] == ["995.1"]
    resp.close()


async def test_stream_follows_program_change_on_same_station(setup):
    client, ctl, _radio = setup
    await client.post("/api/tune", json={"frequency": 97.1, "client_id": "a"})
    resp = await client.get("/stream.mp3?client_id=a")
    await read_until(resp, b"mp3-97.1-0:one", lambda: ctl._feed(0, 0, b"one"))
    await client.post("/api/tune", json={"frequency": 97.1, "program": 1, "client_id": "a"})
    await read_until(resp, b"mp3-97.1-1:two", lambda: ctl._feed(0, 1, b"two"))
    resp.close()


async def test_bumped_plex_stream_ends(two_tuners):
    client, ctl, _radios = two_tuners
    plex = await client.get("/stream/97.1/0.ts")
    assert plex.status == 200
    await client.post("/api/tune", json={"frequency": 101.1, "client_id": "b"})
    ctl.add_listener("b", "B", "web")
    r = await client.post("/api/tune", json={"frequency": 99.5, "client_id": "c", "override": True})
    assert r.status == 200
    import asyncio
    await asyncio.wait_for(plex.read(), 2)  # response finished


async def test_discover_tuner_count(two_tuners):
    client, _ctl, _radios = two_tuners
    assert (await (await client.get("/discover.json")).json())["TunerCount"] == 2


def test_config_tuners_list(tmp_path):
    from freqon.__main__ import load_config, tuner_sources
    path = tmp_path / "c.toml"
    path.write_text('gain = 10.0\n[[tuners]]\ndevice_index = 0\n[[tuners]]\nrtltcp = "10.0.0.5:1234"\ngain = 20.0\n')
    sources = tuner_sources(load_config(["--config", str(path)]))
    assert [(s.device_index, s.rtltcp, s.gain_db) for s in sources] == [(0, None, 10.0), (0, "10.0.0.5:1234", 20.0)]
    path.write_text("device_index = 2\nppm = 3\n")
    sources = tuner_sources(load_config(["--config", str(path)]))
    assert [(s.device_index, s.ppm) for s in sources] == [(2, 3)]
    path.write_text('[[tuners]]\nantenna = "roof"\n')
    with pytest.raises(SystemExit):
        load_config(["--config", str(path)])


def test_config_video(tmp_path):
    from freqon.__main__ import load_config, video_options
    path = tmp_path / "c.toml"
    path.write_text('video_source = "rtsp://go2rtc.local:8554/camera"\nvideo_size = "640x360"\nvideo_fps = 10\n')
    v = video_options(load_config(["--config", str(path)]))
    assert v == {"source": "rtsp://go2rtc.local:8554/camera", "size": (640, 360), "fps": 10,
                 "encoder": "libx264", "bitrate": "2500k"}
    path.write_text("")
    assert video_options(load_config(["--config", str(path)]))["source"] is None
    path.write_text('video_source = "rtsp://x"\nvideo_size = "huge"\n')
    with pytest.raises(SystemExit):
        video_options(load_config(["--config", str(path)]))


async def test_stream_survives_a_there_and_back_retune(setup):
    client, ctl, _radio = setup
    await client.post("/api/tune", json={"frequency": 97.1, "client_id": "a"})
    resp = await client.get("/stream.mp3?client_id=a")
    await read_until(resp, b"mp3-97.1-0:one", lambda: ctl._feed(0, 0, b"one"))
    key = ctl.sessions["a"]
    ctl._set_session("a", key._replace(program=1))  # A -> B -> A before the stream wakes up
    ctl._set_session("a", key)
    import asyncio
    await asyncio.sleep(0.05)  # let the stream notice the change before more audio arrives
    await read_until(resp, b"mp3-97.1-0:two", lambda: ctl._feed(0, 0, b"two"))
    resp.close()


async def test_two_plex_viewers_use_two_tuners(two_tuners):
    client, _ctl, radios = two_tuners
    first = await client.get("/stream/97.1/0.ts")
    second = await client.get("/stream/101.9/0.ts")
    assert first.status == 200 and second.status == 200
    assert [r.state.frequency for r in radios] == [97.1, 101.9]
    first.close()
    second.close()


async def test_head_on_a_stream_url_does_not_tune(setup):
    client, ctl, radio = setup
    for url, ctype in (("/stream/99.5/0.ts", "video/mp2t"), ("/stream/99.5/0.mp3", "audio/mpeg")):
        r = await client.head(url)
        assert r.status == 200 and r.content_type == ctype
    assert radio.tunes == [] and not ctl.channels


class BrokenRadio(FakeRadio):
    def tune(self, frequency):
        raise RuntimeError("Failed to open RTL-SDR.")


async def test_missing_dongle_is_a_clear_503(aiohttp_client, store):
    ctl = Controller(TunerPool([BrokenRadio()]), store, make_encoders=fake_encoders())
    client = await aiohttp_client(make_app(ctl))
    r = await client.get("/stream/97.1/0.ts")
    assert r.status == 503
    body = await r.json()
    assert body["error"] == "tuner unavailable" and "RTL-SDR" in body["message"]
    r = await client.post("/api/tune", json={"frequency": 97.1})
    assert r.status == 503 and (await r.json())["error"] == "tuner unavailable"


def test_access_log_option(tmp_path):
    from freqon.__main__ import load_config
    path = tmp_path / "c.toml"
    path.write_text("")
    assert load_config(["--config", str(path)])["access_log"] is False
    assert load_config(["--config", str(path), "--access-log"])["access_log"] is True


async def test_add_the_station_you_are_tuned_to(setup):
    client, ctl, radio = setup
    await client.post("/api/tune", json={"frequency": 101.1, "client_id": "web1"})
    s = radio.state  # what the tuner has decoded from the air
    s.synced, s.station_name = True, "WRIF"
    s.programs = {0: {"name": "HD-1 ", "type": "Rock"}, 1: {"name": "HD2", "type": "Rock"}, 2: {"name": "HD3", "type": "Talk"}}
    ctl.stations.update(101.1, custom_name="Riff")  # existing user rename survives
    r = await client.post("/api/stations", json={"frequency": 101.1})
    assert r.status == 201
    st = await r.json()
    assert (st["name"], st["hd"], st["display_name"]) == ("WRIF", True, "Riff")
    assert [c["number"] for c in st["channels"]] == ["1011.1", "1011.2", "1011.3"]
    assert st["channels"][0]["program_name"] == "HD-1"  # trimmed
    lineup = await (await client.get("/lineup.json")).json()
    assert "1011.3" in [c["GuideNumber"] for c in lineup]


async def test_add_an_untuned_station_as_plain_fm(setup):
    client, _ctl, _radio = setup
    r = await client.post("/api/stations", json={"frequency": 88.7})
    assert r.status == 201
    st = await r.json()
    assert (st["frequency"], st["hd"], st["display_name"]) == (88.7, False, "88.7 FM")
    assert (await client.post("/api/stations", json={"frequency": 150})).status == 400
    assert (await client.post("/api/stations", json={})).status == 400


async def test_stream_ends_when_no_data_arrives(aiohttp_client, store):
    ctl = Controller(TunerPool([FakeRadio()]), store, make_encoders=fake_encoders())
    client = await aiohttp_client(make_app(ctl, {"stream_idle_timeout": 0.2}))
    import asyncio
    for url in ("/stream/97.1/0.ts", "/stream.mp3?client_id=plex"):
        r = await client.get(url)
        assert r.status == 200
        await asyncio.wait_for(r.read(), 3)  # response finishes instead of hanging forever
    await asyncio.sleep(0.05)
    assert (await (await client.get("/api/status")).json())["listeners"] == []


def test_stall_handler_exits_for_a_restart():
    from freqon.__main__ import stall_handler
    exits = []
    stall_handler({"exit_on_stall": True}, exit=exits.append)(FakeRadio(0))
    assert exits == [75]  # non-zero: systemd's Restart=on-failure brings the service back
    exits.clear()
    stall_handler({"exit_on_stall": False}, exit=exits.append)(FakeRadio(0))
    assert exits == []


def test_add_found_only_adds(store):
    store.update(97.1, custom_name="The Ticket")
    store.update(101.1, hidden=True)
    found = [Station(97.1, name="WXYT", hd=True, programs=[{"program": 0, "name": "HD1"}, {"program": 1, "name": "HD2"},
                                                        {"program": 2, "name": "HD3", "type": "Talk"}]),
             Station(98.7, name=None, hd=False)]
    added = store.add_found(found)
    assert added == ["971.3", "987.1"]
    by_f = {s.frequency: s for s in store.all(include_hidden=True)}
    assert set(by_f) == {97.1, 98.7, 101.1}  # nothing removed (101.1 wasn't found this time)
    assert by_f[97.1].custom_name == "The Ticket" and by_f[101.1].hidden
    assert [p["program"] for p in by_f[97.1].programs] == [0, 1, 2]
    assert by_f[97.1].programs[1].get("type") == "News"  # existing details kept
    assert store.add_found(found) == []  # nothing new the second time


def test_add_found_upgrades_fm_station_to_hd(store):
    added = store.add_found([Station(101.1, name="WRIF", hd=True, programs=[{"program": 0, "name": "HD1"},
                                                                           {"program": 1, "name": "HD2"}])])
    st = store.get(101.1)
    assert st.hd and st.name == "WRIF" and added == ["1011.2"]


def test_auto_scan_settings(tmp_path):
    from freqon.__main__ import load_config
    path = tmp_path / "c.toml"
    path.write_text("")
    cfg = load_config(["--config", str(path)])
    assert (cfg["auto_scan_idle_days"], cfg["auto_scan_active_hours"]) == (30, 1)
    path.write_text("auto_scan_idle_days = 0\nauto_scan_active_hours = 2\n")
    cfg = load_config(["--config", str(path)])
    assert (cfg["auto_scan_idle_days"], cfg["auto_scan_active_hours"]) == (0, 2)


async def test_playlist_can_carry_a_client_identity(setup):
    client, ctl, _radio = setup
    m3u = await (await client.get("/playlist.m3u?client=jellyfin")).text()
    urls = [line for line in m3u.splitlines() if line.startswith("http")]
    assert urls and all(u.endswith("?client_id=jellyfin&client_name=Jellyfin&kind=jellyfin") for u in urls)
    plain = await (await client.get("/playlist.m3u")).text()
    assert "client_id" not in plain  # Plex/xTeVe keep the shared Plex identity
    bad = await (await client.get("/playlist.m3u?client=a%22b c&x")).text()
    assert '"' not in bad.split("\n", 1)[1].split("#EXTINF")[0]  # untrusted value can't break the file
    # Jellyfin listening is not "Plex": they are told apart in the busy list and takeovers.
    await client.get("/api/status")
    r = await client.post("/api/tune", json={"frequency": 97.1, "client_id": "jellyfin"})
    assert r.status == 200
    ctl.add_listener("jellyfin", "Jellyfin", "jellyfin")
    r = await client.post("/api/tune", json={"frequency": 99.5, "client_id": "web1"})
    assert "Jellyfin" in (await r.json())["message"]


def test_idle_timeout_default_is_30_seconds(tmp_path):
    from freqon.__main__ import load_config
    path = tmp_path / "c.toml"
    path.write_text("")
    assert load_config(["--config", str(path)])["idle_timeout"] == 30


def test_tuner_serial_and_ppm_auto(tmp_path):
    from freqon.__main__ import load_config, tuner_sources
    path = tmp_path / "c.toml"
    path.write_text('[[tuners]]\nserial = "KITCHEN"\n[[tuners]]\ndevice_index = 1\nppm = 12\n')
    a, b = tuner_sources(load_config(["--config", str(path)]))
    assert (a.serial, a.ppm_auto, a.key) == ("KITCHEN", True, "serial:KITCHEN")  # ppm defaults to "auto"
    assert (b.serial, b.ppm, b.ppm_auto, b.key) == (None, 12, False, "index:1")
    path.write_text('ppm = "sometimes"\n')
    with pytest.raises(SystemExit):
        tuner_sources(load_config(["--config", str(path)]))


def test_stall_handler_resets_the_dongle_first():
    from freqon.__main__ import stall_handler
    from freqon.radio import SourceConfig
    calls = []
    radio = FakeRadio(0)
    radio.source_cfg = SourceConfig(serial="KITCHEN")
    stall_handler({"exit_on_stall": True}, exit=lambda c: calls.append(("exit", c)),
                  reset=lambda **kw: calls.append(("reset", kw)))(radio)
    assert calls == [("reset", {"serial": "KITCHEN", "index": None}), ("exit", 75)]
    calls.clear()
    radio.source_cfg = SourceConfig(rtltcp="10.0.0.5:1234")  # a remote dongle can't be reset from here
    stall_handler({"exit_on_stall": True}, exit=lambda c: calls.append(("exit", c)),
                  reset=lambda **kw: calls.append(("reset", kw)))(radio)
    assert calls == [("exit", 75)]


def test_list_dongles_cli(monkeypatch, capsys):
    from freqon import __main__ as m
    monkeypatch.setattr("freqon.rtlsdr.list_dongles", lambda: [
        {"index": 0, "name": "Generic RTL2832U OEM", "manufacturer": "Realtek", "product": "RTL2838UHIDIR",
         "serial": "00000001"}])
    with pytest.raises(SystemExit) as e:
        m.main(["--list-dongles"])
    assert e.value.code == 0
    assert "00000001" in capsys.readouterr().out


def _jpeg(color=(255, 0, 0)):
    import io as _io

    from PIL import Image as _Image
    buf = _io.BytesIO()
    _Image.new("RGB", (120, 120), color).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
async def extras_setup(aiohttp_client, store, tmp_path):
    radio = FakeRadio()
    ctl = Controller(TunerPool([radio]), store, make_encoders=fake_encoders(), logos_dir=str(tmp_path / "logos"))
    client = await aiohttp_client(make_app(ctl))
    await client.post("/api/tune", json={"frequency": 97.1, "program": 1, "client_id": "a"})
    return client, ctl, radio, tmp_path


async def test_album_art_endpoint(extras_setup):
    client, _ctl, radio, _ = extras_setup
    assert (await client.get("/art/971.2.jpg")).status == 404  # nothing yet: clients fall back
    radio.extras.lot(1, "art", 5, _jpeg())
    r = await client.get("/art/971.2.jpg")
    assert r.status == 200 and r.content_type == "image/jpeg"
    st = await (await client.get("/api/status?client_id=a")).json()
    assert st["art"] is True and len(st["art_key"]) == 8
    assert (await client.get("/art/1019.1.jpg")).status == 404  # not tuned
    assert (await client.get("/art/nonsense.jpg")).status == 404


async def test_broadcast_logo_is_preferred_and_kept(extras_setup):
    client, _ctl, radio, tmp_path = extras_setup
    generated = await (await client.get("/logo/971.2.png")).read()
    radio.extras.lot(1, "logo", 9, _jpeg((0, 0, 255)))
    r = await client.get("/logo/971.2.png")
    broadcast = await r.read()
    assert r.status == 200 and broadcast != generated
    _ctl.check_tuners()  # logos are saved as they arrive (every 5 s)
    assert (tmp_path / "logos" / "971.2.png").read_bytes() == broadcast
    radio.state.extras.reset()  # retuned away: the saved logo is still used
    assert await (await client.get("/logo/971.2.png")).read() == broadcast


async def test_logos_are_saved_when_they_arrive_and_disk_errors_are_harmless(extras_setup):
    client, ctl, radio, tmp_path = extras_setup
    radio.extras.lot(0, "logo", 3, _jpeg((0, 255, 0)))
    ctl.check_tuners()  # no client asked for it: still kept
    assert (tmp_path / "logos" / "971.1.png").exists()
    ctl.logos_dir = "/nonexistent/readonly/dir"
    radio.extras.lot(1, "logo", 4, _jpeg((9, 9, 9)))
    ctl.check_tuners()  # must not raise
    assert (await client.get("/logo/971.2.png")).status == 200


async def test_maps_endpoint(extras_setup):
    import io as _io

    from PIL import Image as _Image
    client, _ctl, radio, _ = extras_setup
    assert (await client.get("/maps/weather.png")).status == 404
    assert (await client.get("/maps/../etc.png")).status == 404
    buf = _io.BytesIO()
    _Image.new("RGB", (64, 64)).save(buf, "PNG")
    radio.extras.here_image("weather", 1, 5, 6, None, buf.getvalue())
    r = await client.get("/maps/weather.png")
    assert r.status == 200 and r.content_type == "image/png"



def test_stall_handler_exits_even_if_the_reset_fails():
    from freqon.__main__ import stall_handler
    exits = []
    def bad_reset(**kw):
        raise ValueError("sysfs weirdness")
    stall_handler({"exit_on_stall": True}, exit=exits.append, reset=bad_reset)(FakeRadio(0))
    assert exits == [75]


def test_config_from_before_the_freqon_name_is_still_found(tmp_path, monkeypatch):
    from freqon import __main__ as m
    # data_dir is set so the loader never touches the real home folder (no data migration).
    old = tmp_path / "hdradio.toml"
    old.write_text(f'port = 9001\ndata_dir = "{tmp_path / "data"}"\n')
    monkeypatch.setattr(m, "CONFIG_LOCATIONS", [str(tmp_path / "missing.toml")])
    monkeypatch.setattr(m, "LEGACY_CONFIG_LOCATIONS", [str(old)])
    assert m.load_config([])["port"] == 9001
    new = tmp_path / "freqon.toml"
    new.write_text(f'port = 9002\ndata_dir = "{tmp_path / "data"}"\n')
    monkeypatch.setattr(m, "CONFIG_LOCATIONS", [str(new)])
    assert m.load_config([])["port"] == 9002  # the new location wins


def test_data_from_before_the_freqon_name_is_moved(tmp_path):
    from freqon.__main__ import migrate_legacy_data
    old, new = tmp_path / "hdradio", tmp_path / "freqon"
    (old / "logos").mkdir(parents=True)
    (old / "stations.json").write_text("{}")
    assert migrate_legacy_data(str(new), str(old)) is True
    assert (new / "stations.json").exists() and (new / "logos").is_dir() and not old.exists()
    assert migrate_legacy_data(str(new), str(old)) is False  # nothing left to move
    (old).mkdir()
    assert migrate_legacy_data(str(new), str(old)) is False  # never overwrites an existing new folder


def test_log_file_gets_logs_and_tracebacks(tmp_path, monkeypatch):
    # The Windows task runs hidden, so logs and any crash traceback go to a file.
    import logging
    import sys

    import freqon.__main__ as m

    monkeypatch.setattr(sys, "stderr", sys.stderr)  # restored after the test
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    path = tmp_path / "logs" / "freqon.log"
    cfg = m.load_config(["--log-file", str(path), "--data-dir", str(tmp_path / "d")])
    m.setup_logging(cfg)
    logging.getLogger("freqon").warning("hello from the test")
    print("traceback text", file=sys.stderr)
    sys.stderr.flush()
    text = path.read_text()
    assert "hello from the test" in text and "traceback text" in text


async def test_takeovers_can_be_turned_off(aiohttp_client, store):
    radio = FakeRadio()
    ctl = Controller(TunerPool([radio]), store, make_encoders=fake_encoders(), allow_takeovers=False)
    client = await aiohttp_client(make_app(ctl))
    await client.post("/api/tune", json={"frequency": 97.1, "client_id": "web1"})
    ctl.add_listener("web1", "Web", "web")
    r = await client.post("/api/tune", json={"frequency": 99.5, "client_id": "web2", "override": True})
    assert r.status == 409
    body = await r.json()
    assert body["can_take_over"] is False and "turned off" in body["message"]
    assert (await client.get("/stream/99.5/0.ts?client_id=jellyfin&kind=jellyfin")).status == 503
    assert (await client.post("/api/scan", json={"client_id": "web2", "override": True})).status == 409
    assert radio.state.frequency == 97.1
    assert (await (await client.get("/api/status")).json())["takeovers"] is False


async def test_takeovers_are_on_by_default(setup):
    client, ctl, _radio = setup
    await client.post("/api/tune", json={"frequency": 97.1, "client_id": "web1"})
    ctl.add_listener("web1", "Web", "web")
    body = await (await client.post("/api/tune", json={"frequency": 99.5, "client_id": "web2"})).json()
    assert body["can_take_over"] is True
    assert (await (await client.get("/api/status")).json())["takeovers"] is True


def test_weak_hd_and_takeover_settings(tmp_path):
    from freqon.__main__ import hd_policy, load_config
    path = tmp_path / "c.toml"
    path.write_text("allow_takeovers = false\nhd_fallback_seconds = 2\nhd_return_seconds = 8\n"
                    "hd_max_fallbacks = 4\nhd_fallback_window_seconds = 600\n")
    cfg = load_config(["--config", str(path), "--data-dir", str(tmp_path)])
    assert cfg["allow_takeovers"] is False
    p = hd_policy(cfg)
    assert (p.fallback_seconds, p.return_seconds, p.max_fallbacks, p.window_seconds) == (2, 8, 4, 600)
    path.write_text("hd_fallback_seconds = 0\n")
    assert hd_policy(load_config(["--config", str(path), "--data-dir", str(tmp_path)])) is None
    path.write_text("hd_max_fallbacks = 0\n")
    with pytest.raises(SystemExit):
        load_config(["--config", str(path), "--data-dir", str(tmp_path)])


async def test_status_json_hdhomerun_format(setup):
    client, ctl, radio = setup
    idle = await (await client.get("/status.json")).json()
    assert idle == [{"Resource": "tuner0"}]

    await client.post("/api/tune", json={"frequency": 97.1, "program": 1, "client_id": "a"})
    radio.state.synced = True
    radio.state.ber = 0.02
    (tuned,) = await (await client.get("/status.json")).json()
    # The widget counts tuners in use by VctNumber and shows VctName for the tuner it watches.
    assert tuned["Resource"] == "tuner0"
    assert tuned["VctNumber"] == "971.2" and tuned["VctName"] == "WXYT HD2"
    assert tuned["Frequency"] == 97_100_000
    assert tuned["SymbolQualityPercent"] == 98

    ctl._scan_tuner = 0
    (scanning,) = await (await client.get("/status.json")).json()
    assert scanning == {"Resource": "tuner0", "VctName": "Scanning"}


async def test_status_json_is_open_without_web_login(aiohttp_client, store):
    ctl = Controller(TunerPool([FakeRadio()]), store, make_encoders=fake_encoders())
    client = await aiohttp_client(make_app(ctl, {"web_username": "radio", "web_password": "pw"}))
    assert (await client.get("/api/status")).status == 401  # sign-in is on
    assert (await client.get("/status.json")).status == 200
