import pytest
from fakes import FakeRadio, fake_encoders

from freqon.api import make_app
from freqon.pool import TunerPool
from freqon.service import Controller
from freqon.stations import Station, StationStore

LOGIN = {"web_username": "radio", "web_password": "s3cret pass"}


@pytest.fixture
def store(tmp_path):
    s = StationStore(str(tmp_path / "stations.json"))
    s.upsert(Station(97.1, name="WXYT", hd=True))
    return s


def _app(store, tmp_path, **cfg):
    ctl = Controller(TunerPool([FakeRadio()]), store, make_encoders=fake_encoders())
    return make_app(ctl, {"data_dir": str(tmp_path), "login_fail_delay": 0, **cfg})


async def _login(client, username="radio", password="s3cret pass"):
    return await client.post("/login", data={"username": username, "password": password},
                             allow_redirects=False)


async def test_without_a_login_everything_is_open(aiohttp_client, store, tmp_path):
    client = await aiohttp_client(_app(store, tmp_path))
    assert (await client.get("/")).status == 200
    assert (await client.get("/api/status")).status == 200
    assert (await client.get("/login", allow_redirects=False)).status == 302  # nothing to log in to


async def test_web_player_and_api_need_a_login(aiohttp_client, store, tmp_path):
    client = await aiohttp_client(_app(store, tmp_path, **LOGIN))
    r = await client.get("/", allow_redirects=False)
    assert r.status == 302 and r.headers["Location"] == "/login"
    assert (await client.get("/api/status")).status == 401
    assert (await client.post("/api/tune", json={"frequency": 97.1})).status == 401
    assert (await client.get("/stream.mp3")).status == 401
    assert (await client.get("/stream/97.1/0.mp3")).status == 401
    login_page = await client.get("/login")
    assert login_page.status == 200 and "password" in (await login_page.text())
    assert (await client.get("/static/style.css")).status == 200  # the login page needs it


async def test_plex_and_jellyfin_need_no_login(aiohttp_client, store, tmp_path):
    client = await aiohttp_client(_app(store, tmp_path, **LOGIN))
    for url in ("/discover.json", "/lineup.json", "/lineup_status.json", "/device.xml", "/playlist.m3u",
                "/xmltv.xml", "/card.png"):
        assert (await client.get(url)).status == 200, url
    r = await client.get("/stream/97.1/0.ts")
    assert r.status == 200
    r.close()


async def test_wrong_password_is_refused(aiohttp_client, store, tmp_path):
    client = await aiohttp_client(_app(store, tmp_path, **LOGIN))
    for user, pw in (("radio", "wrong"), ("someone", "s3cret pass"), ("", "")):
        r = await _login(client, user, pw)
        assert r.status == 401 and "freqon_session" not in r.cookies
        assert "Wrong username or password" in await r.text()
    assert (await client.get("/api/status")).status == 401


async def test_login_then_logout(aiohttp_client, store, tmp_path):
    client = await aiohttp_client(_app(store, tmp_path, **LOGIN))
    r = await _login(client)
    assert r.status == 302 and r.headers["Location"] == "/"
    cookie = r.cookies["freqon_session"]
    assert cookie["httponly"] and cookie["samesite"] == "Lax"
    assert (await client.get("/")).status == 200
    assert (await client.get("/api/status")).status == 200
    session = await (await client.get("/api/session")).json()
    assert session == {"login": True, "username": "radio"}
    r = await client.post("/logout", allow_redirects=False)
    assert r.status == 302 and r.headers["Location"] == "/login"
    assert (await client.get("/api/status")).status == 401


async def test_forged_or_tampered_sessions_are_refused(aiohttp_client, store, tmp_path):
    client = await aiohttp_client(_app(store, tmp_path, **LOGIN))
    token = (await _login(client)).cookies["freqon_session"].value
    expires, sig = token.split(".", 1)
    for bad in (f"{int(expires) + 999}.{sig}", f"{expires}.{'0' * len(sig)}", "nonsense", ""):
        client.session.cookie_jar.clear()
        client.session.cookie_jar.update_cookies({"freqon_session": bad})
        assert (await client.get("/api/status")).status == 401, bad


async def test_session_survives_a_restart_but_not_a_password_change(aiohttp_client, store, tmp_path):
    first = await aiohttp_client(_app(store, tmp_path, **LOGIN))
    token = (await _login(first)).cookies["freqon_session"].value
    restarted = await aiohttp_client(_app(store, tmp_path, **LOGIN))
    restarted.session.cookie_jar.update_cookies({"freqon_session": token})
    assert (await restarted.get("/api/status")).status == 200
    changed = await aiohttp_client(_app(store, tmp_path, web_username="radio", web_password="new one"))
    changed.session.cookie_jar.update_cookies({"freqon_session": token})
    assert (await changed.get("/api/status")).status == 401


async def test_session_key_file_is_private(aiohttp_client, store, tmp_path):
    import os
    import stat
    await aiohttp_client(_app(store, tmp_path, **LOGIN))
    mode = stat.S_IMODE(os.stat(tmp_path / "session.key").st_mode)
    assert mode & 0o077 == 0


def test_login_settings(tmp_path, monkeypatch):
    from freqon.__main__ import load_config
    monkeypatch.delenv("FREQON_WEB_PASSWORD", raising=False)
    args = ["--data-dir", str(tmp_path)]
    cfg = load_config([*args, "--web-username", "radio", "--web-password", "x"])
    assert (cfg["web_username"], cfg["web_password"]) == ("radio", "x")
    # The password can come from the environment, so it doesn't show in the process list.
    monkeypatch.setenv("FREQON_WEB_PASSWORD", "from env")
    assert load_config([*args, "--web-username", "radio"])["web_password"] == "from env"
    monkeypatch.delenv("FREQON_WEB_PASSWORD")
    with pytest.raises(SystemExit):  # only half of a login is a mistake, not "no login"
        load_config([*args, "--web-username", "radio"])


async def test_signing_out_revokes_that_session_even_if_copied(aiohttp_client, store, tmp_path):
    client = await aiohttp_client(_app(store, tmp_path, **LOGIN))
    token = (await _login(client)).cookies["freqon_session"].value
    await client.post("/logout", allow_redirects=False)
    client.session.cookie_jar.update_cookies({"freqon_session": token})
    assert (await client.get("/api/status")).status == 401
    # ...also after a restart, while other sessions keep working.
    other = await aiohttp_client(_app(store, tmp_path, **LOGIN))
    other_token = (await _login(other)).cookies["freqon_session"].value
    restarted = await aiohttp_client(_app(store, tmp_path, **LOGIN))
    restarted.session.cookie_jar.update_cookies({"freqon_session": token})
    assert (await restarted.get("/api/status")).status == 401
    restarted.session.cookie_jar.update_cookies({"freqon_session": other_token})
    assert (await restarted.get("/api/status")).status == 200


def test_lockout_groups_ipv6_by_network_and_caps_the_account():
    from freqon import auth
    a = auth.Auth("radio", "pw", b"k" * 32, fail_delay=0)
    for i in range(auth.MAX_FAILURES):
        a.failed(f"2001:db8:1:2::{i + 1}")  # a new address each time, same /64
    assert a.locked_out("2001:db8:1:2::ffff")
    assert not a.locked_out("2001:db8:1:3::1") and not a.locked_out("192.168.1.9")
    for i in range(auth.MAX_ACCOUNT_FAILURES):
        a.failed(f"10.0.{i // 250}.{i % 250}")
    # Too many wrong tries overall: any address that has also failed waits out the window...
    a.failed("192.168.1.50")
    assert a.locked_out("192.168.1.50")
    # ...but someone who hasn't failed (the owner, typing it right) can still sign in.
    assert not a.locked_out("192.168.1.9")
    assert not a._failures.get("unused")
