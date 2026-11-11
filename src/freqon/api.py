"""HTTP API: control (REST + WebSocket), audio/video streams, and Plex/xTeVe integration.

Control
    GET    /api/status                      current tuner status
    GET    /api/stations                    station list (with channels)
    POST   /api/stations                    {"frequency": 101.1}: add a station (live HD info if tuned)
    PATCH  /api/stations/{freq}             {"custom_name": str|null, "hidden": bool}
    DELETE /api/stations/{freq}
    POST   /api/tune                        {"frequency": 101.1, "program": 0, "override": false}
    POST   /api/mode                        {"mode": "auto"|"digital"|"analog"}
    POST   /api/scan                        {"override": false}     (DELETE cancels)
    GET    /api/events                      WebSocket; pushes {"type": "status", "status": {...}}
    Clients identify themselves with ?client_id=...&client_name=...&kind=... (or the JSON body).
    A tune/scan that would take the tuner from someone else returns 409 {"error": "busy", ...}.

Streams
    GET /stream.mp3                         whatever is playing now
    GET /stream/{freq}/{program}.mp3        tune and listen; 503 if busy (take over via POST /api/tune)
    GET /stream/{freq}/{program}.ts         same, MPEG-TS with a 'now playing' video card (Plex)
    GET /card.png                           the current 'now playing' card

Plex / xTeVe
    GET /playlist.m3u, /xmltv.xml           channel list + guide for xTeVe (or Plex's XMLTV option)
    GET /discover.json, /lineup.json, /lineup_status.json, /status.json, /device.xml
                                            HDHomeRun emulation: add this server in Plex directly
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import socket
from datetime import UTC, datetime, timedelta
from importlib import resources
from urllib.parse import urlencode, urlsplit
from xml.sax.saxutils import escape, quoteattr

from aiohttp import WSCloseCode, WSMsgType, web

from . import card
from .nrsc5 import NRSC5Error
from .service import Busy, Controller
from .stations import parse_channel_id

log = logging.getLogger(__name__)

# What the radio hardware raises when it can't be used (unplugged, busy elsewhere, ...).
DEVICE_ERRORS = (NRSC5Error, OSError, RuntimeError)

CTL = web.AppKey("controller", Controller)
CFG = web.AppKey("config", dict)
SOCKETS = web.AppKey("sockets", set)
HOSTS = web.AppKey("hosts", set)
# Guide channel ids keep the project's original name: Plex and Jellyfin store their channel
# mappings by these ids, so changing them would unmap every channel. Don't rename.
GUIDE_ID_PREFIX = "hdradio."
STREAM_IDLE_TIMEOUT = 15.0  # end a stream that has produced nothing for this long (dead tuner, gone client)


def json_response(data, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, default=str))


def error(status: int, message: str, **extra) -> web.Response:
    return json_response({"error": message, **extra}, status=status)


async def _body(request: web.Request) -> dict:
    if request.can_read_body:
        if request.content_type != "application/json":
            # Also blocks cross-site "simple" form/text posts, which skip CORS preflight.
            raise web.HTTPUnsupportedMediaType(text="send JSON with Content-Type: application/json")
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            raise web.HTTPBadRequest(text="body must be JSON")
    return {}


def _client(request: web.Request, body: dict | None = None) -> tuple[str, str, str]:
    body = body or {}
    q = request.query
    peer = request.remote or "unknown"
    client_id = body.get("client_id") or q.get("client_id") or peer
    name = body.get("client_name") or q.get("client_name") or client_id
    kind = body.get("kind") or q.get("kind") or "other"
    return str(client_id), str(name), str(kind)


def _hostname(netloc: str) -> str:
    """'Host'/'Origin' netloc -> lower-case host name without port or IPv6 brackets."""
    host = urlsplit("//" + netloc).hostname or ""
    return host.rstrip(".").lower()


def allowed_hosts(config: dict) -> set[str]:
    """Names this server answers to. IP literals are always accepted (see _host_ok)."""
    me = socket.gethostname().split(".")[0].lower()
    names = {"localhost", me, f"{me}.local", socket.getfqdn().lower()}
    if config.get("public_url"):
        names.add(_hostname(urlsplit(config["public_url"]).netloc))
    names.update(h.lower() for h in config.get("allowed_hosts") or [])
    return {n for n in names if n}


def _host_ok(name: str, allowed: set[str]) -> bool:
    # DNS rebinding needs an attacker-controlled *name*, so IP addresses are safe to accept.
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return name in allowed


def _cross_site(request: web.Request, allowed: set[str]) -> bool:
    """True for a request a browser made on behalf of some other website."""
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        return True
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if not origin or origin == "null":
        return origin == "null"  # no header: not a browser (Plex, Jellyfin, curl)
    netloc = urlsplit(origin).netloc
    return netloc != request.host or not _host_ok(_hostname(netloc), allowed)


@web.middleware
async def origin_guard(request: web.Request, handler):
    allowed = request.app[HOSTS]
    if not _host_ok(_hostname(request.host), allowed):
        # Unknown host name: likely DNS rebinding. Add real names via `allowed_hosts`.
        return error(421, f"unknown host {_hostname(request.host)!r}; add it to allowed_hosts in the config")
    # Anything that changes or holds the tuner: control calls, live events, tune-and-listen URLs.
    control = (request.method in ("POST", "PATCH", "DELETE", "PUT")
               or request.path == "/api/events" or request.path.startswith("/stream"))
    if control and _cross_site(request, allowed):
        return error(403, "cross-site requests are not allowed")
    return await handler(request)


def tuner_unavailable(e: Exception) -> web.Response:
    """The radio hardware failed (dongle unplugged, held by another program, ...)."""
    log.error("tuner unavailable: %s", e)
    return json_response({"error": "tuner unavailable", "message": f"The radio tuner could not be used: {e}"},
                         status=503)


def head_only(content_type: str) -> web.Response:
    """Answer a HEAD probe of a stream URL (Plex and others do this) without tuning anything."""
    return web.Response(headers={"Content-Type": content_type, "Cache-Control": "no-cache, no-store"})


def m3u_safe(text: str) -> str:
    """Single line, no quotes or commas that would break an #EXTINF attribute or title."""
    return " ".join(str(text).replace('"', "'").replace(",", " ").split())


def base_url(request: web.Request) -> str:
    return request.app[CFG].get("public_url") or f"{request.scheme}://{request.host}"


# -- control ---------------------------------------------------------------------------------------

async def get_status(request):
    client_id, _, _ = _client(request)
    return json_response(request.app[CTL].status(client_id))


async def get_stations(request):
    ctl = request.app[CTL]
    include_hidden = request.query.get("all") in ("1", "true")
    return json_response({"stations": [s.to_dict() for s in ctl.stations.all(include_hidden)]})


async def add_station(request):
    body = await _body(request)
    try:
        st = request.app[CTL].add_station(float(body["frequency"]))
    except (KeyError, ValueError, TypeError) as e:
        return error(400, f"bad station: {e}")
    return json_response(st.to_dict(), status=201)


async def patch_station(request):
    ctl = request.app[CTL]
    body = await _body(request)
    try:
        st = ctl.stations.update(float(request.match_info["freq"]), **body)
    except KeyError:
        return error(404, "no such station")
    except (ValueError, TypeError) as e:
        return error(400, str(e))
    ctl.notify()
    return json_response(st.to_dict())


async def delete_station(request):
    request.app[CTL].stations.delete(float(request.match_info["freq"]))
    return web.Response(status=204)


async def post_tune(request):
    ctl = request.app[CTL]
    body = await _body(request)
    client_id, _, _ = _client(request, body)
    try:
        if "channel" in body:
            freq, program = parse_channel_id(str(body["channel"]))
        else:
            freq, program = float(body["frequency"]), int(body.get("program", 0))
        status = await ctl.tune(freq, program, client_id=client_id, override=bool(body.get("override")))
    except Busy as b:
        return json_response(b.to_dict(), status=409)
    except (KeyError, ValueError, TypeError) as e:
        return error(400, f"bad tune request: {e}")
    except DEVICE_ERRORS as e:
        return tuner_unavailable(e)
    return json_response(status)


async def post_mode(request):
    body = await _body(request)
    client_id, _, _ = _client(request, body)
    try:
        return json_response(await request.app[CTL].set_mode(str(body.get("mode")), client_id=client_id))
    except ValueError as e:
        return error(400, str(e))


async def post_scan(request):
    ctl = request.app[CTL]
    body = await _body(request)
    client_id, _, _ = _client(request, body)
    try:
        return json_response(await ctl.start_scan(client_id=client_id, override=bool(body.get("override"))))
    except Busy as b:
        return json_response(b.to_dict(), status=409)


async def delete_scan(request):
    request.app[CTL].cancel_scan()
    return json_response(request.app[CTL].status())


async def events(request):
    ctl = request.app[CTL]
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    request.app[SOCKETS].add(ws)
    client_id, _, _ = _client(request)
    q = ctl.subscribe(client_id)
    await ws.send_json({"type": "status", "status": ctl.status(client_id)}, dumps=lambda o: json.dumps(o, default=str))

    async def pump():
        while True:
            msg = await q.get()
            await ws.send_json(msg, dumps=lambda o: json.dumps(o, default=str))

    task = asyncio.create_task(pump())
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT and msg.data == "ping":
                await ws.send_str("pong")
    finally:
        task.cancel()
        ctl.unsubscribe(q)
        request.app[SOCKETS].discard(ws)
    return ws


# -- streams ---------------------------------------------------------------------------------------

async def _serve_stream(request: web.Request, which: str, kind_default: str,
                        identity: tuple[str, str, str] | None = None) -> web.StreamResponse:
    ctl = request.app[CTL]
    client_id, name, kind = identity or _client(request)
    if kind == "other":
        kind = kind_default
    listener = ctl.add_listener(client_id, name, kind)
    encoder = getattr(ctl.channel(listener.key), which)
    q = None
    resp = web.StreamResponse(headers={
        "Content-Type": encoder.content_type,
        "Cache-Control": "no-cache, no-store",
        "icy-name": "freqon",
    })
    try:
        q = await encoder.subscribe()
        await resp.prepare(request)
        idle = _idle_timeout(request)
        while True:
            try:
                chunk = await asyncio.wait_for(q.get(), idle)
            except TimeoutError:
                log.warning("stream for %s produced nothing for %ss; ending it", client_id, idle)
                break
            if not chunk:
                break
            await resp.write(chunk)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        if q is not None:
            encoder.unsubscribe(q)
        ctl.remove_listener(listener)
    return resp


async def stream_current(request):
    """Your channel's audio. Follows your session: retune and the same response carries on
    with the new channel (MP3 is a sequence of independent frames, so players don't notice)."""
    ctl = request.app[CTL]
    if request.method == "HEAD":
        return head_only("audio/mpeg")
    client_id, name, kind = _client(request)
    key = ctl.key_for(client_id)
    if key is None:
        return error(409, "nothing is tuned; POST /api/tune first or use /stream/{freq}/{program}.mp3")
    if not ctl.radio(key.tuner).state.running or not ctl.is_current(key):
        # Released while idle (or taken for a scan): bring your station back if a tuner is free.
        try:
            await ctl.tune(key.frequency, key.program, client_id=client_id, override=False)
        except Busy as b:
            return json_response(b.to_dict(), status=409)
        except DEVICE_ERRORS as e:
            return tuner_unavailable(e)
        key = ctl.key_for(client_id)
    ctl.join(client_id, key)
    listener = ctl.add_listener(client_id, name, "web" if kind == "other" else kind, key)
    resp = web.StreamResponse(headers={
        "Content-Type": "audio/mpeg",
        "Cache-Control": "no-cache, no-store",
        "icy-name": "freqon",
    })
    try:
        await resp.prepare(request)
        while True:
            changed = ctl.session_changed(client_id)
            want = ctl.sessions.get(client_id, listener.key)
            if want != listener.key:
                ctl.move_listener(listener, want)
            if not await _pump(resp, ctl.channel(listener.key).mp3, changed, _idle_timeout(request)):
                break  # the channel ended without the session moving (shutdown)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        ctl.remove_listener(listener)
    return resp


def _idle_timeout(request: web.Request) -> float:
    return float(request.app[CFG].get("stream_idle_timeout") or STREAM_IDLE_TIMEOUT)


async def _pump(resp: web.StreamResponse, encoder, changed: asyncio.Event, idle: float) -> bool:
    """Copy encoder output to the response until it ends or goes quiet for `idle` seconds (False)
    or `changed` is set (True). An end that coincides with a session change counts as a change."""
    q = await encoder.subscribe()
    waiter = asyncio.ensure_future(changed.wait())
    try:
        while True:
            getter = asyncio.ensure_future(q.get())
            done, _ = await asyncio.wait({getter, waiter}, timeout=idle, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                getter.cancel()
                return False
            if getter not in done:
                getter.cancel()
                return True
            chunk = getter.result()
            if not chunk:
                await asyncio.sleep(0)  # let a session change that ended this channel land
                return changed.is_set()
            await resp.write(chunk)
    finally:
        waiter.cancel()
        encoder.unsubscribe(q)


def _media_server_pick(request: web.Request, which: str) -> bool:
    """Plex or Jellyfin opening a channel's video (.ts) URL: someone chose that channel at the
    TV, so it takes the tuner (players following it, like the web player, move along).
    Browsers never take over this way: every current one sends Sec-Fetch-Site, and Plex and
    Jellyfin fetch streams server-side without it. The .mp3 URLs never take over either."""
    return which == "ts" and "Sec-Fetch-Site" not in request.headers


async def _tune_then_stream(request, which: str, kind: str, shared_identity: str | None = None):
    ctl = request.app[CTL]
    if request.method == "HEAD":
        return head_only("video/mp2t" if which == "ts" else "audio/mpeg")
    client_id, name, req_kind = _client(request)
    if shared_identity and "client_id" not in request.query:
        # All Plex sessions share one identity, so Plex changing channel never blocks itself.
        client_id, name, req_kind = shared_identity, shared_identity.title(), kind
    try:
        freq = float(request.match_info["freq"])
        program = int(request.match_info["program"])
        await ctl.tune(freq, program, client_id=client_id, override=_media_server_pick(request, which),
                       prefer_idle=client_id == shared_identity)
    except Busy as b:
        return json_response(b.to_dict(), status=503)
    except ValueError as e:
        return error(400, str(e))
    except DEVICE_ERRORS as e:
        return tuner_unavailable(e)
    return await _serve_stream(request, which, kind, (client_id, name, req_kind))


async def stream_tune_mp3(request):
    return await _tune_then_stream(request, "mp3", "other")


async def stream_tune_ts(request):
    return await _tune_then_stream(request, "ts", "plex", shared_identity="plex")


async def card_png(request):
    ctl = request.app[CTL]
    st = ctl.status()
    st["station"]["name"] = st["station"]["display_name"]
    return web.Response(body=card.png(card.render_card(st)), content_type="image/png",
                        headers={"Cache-Control": "no-cache"})


async def channel_logo(request):
    cid = request.match_info["cid"]
    try:
        freq, program = parse_channel_id(cid)
    except ValueError:
        return error(404, "bad channel")
    broadcast = request.app[CTL].broadcast_logo(freq, program)
    if broadcast is not None:
        return web.Response(body=broadcast, content_type="image/png", headers={"Cache-Control": "max-age=3600"})
    st = request.app[CTL].stations.get(freq)
    title = st.display_name if st else f"{freq:.1f}"
    sub = f"HD{program + 1}" if (st and st.hd) else "FM"
    return web.Response(body=card.render_logo_png(title, f"{freq:.1f} {sub}"), content_type="image/png")


async def channel_art(request):
    """The album art the station is sending now for this channel; 404 when there is none."""
    try:
        freq, program = parse_channel_id(request.match_info["cid"])
    except ValueError:
        return error(404, "bad channel")
    art = request.app[CTL].art(freq, program)
    if art is None:
        return error(404, "no album art right now")
    return web.Response(body=art, content_type="image/jpeg", headers={"Cache-Control": "no-cache"})


async def map_image(request):
    kind = request.match_info["kind"]
    data = request.app[CTL].map_png(kind) if kind in ("traffic", "weather") else None
    if data is None:
        return error(404, f"no {kind} map")
    return web.Response(body=data, content_type="image/png", headers={"Cache-Control": "no-cache"})


# -- Plex / xTeVe ----------------------------------------------------------------------------------

def _client_suffix(request) -> str:
    """`?client=jellyfin` on the playlist: stream URLs then identify that app (its own listener
    name and busy rules) instead of the shared Plex identity."""
    name = "".join(ch for ch in request.query.get("client", "").lower() if ch.isalnum() or ch in "-_")[:32]
    if not name:
        return ""
    return "?" + urlencode({"client_id": name, "client_name": name.title(), "kind": name})


def _channels(request):
    base = base_url(request)
    suffix = _client_suffix(request)
    for c in request.app[CTL].stations.channels():
        yield c, f"{base}/stream/{c['frequency']:.1f}/{c['program']}.ts{suffix}", f"{base}/logo/{c['id']}.png"


async def playlist_m3u(request):
    lines = [f'#EXTM3U url-tvg="{m3u_safe(base_url(request))}/xmltv.xml"']
    for c, url, logo in _channels(request):
        name = m3u_safe(c["name"])
        lines.append(
            f'#EXTINF:-1 tvg-id="{GUIDE_ID_PREFIX}{c["id"]}" tvg-chno="{c["number"]}" tvg-name="{name}" '
            f'tvg-logo="{m3u_safe(logo)}" group-title="Radio",{name}')
        lines.append(m3u_safe(url))
    return web.Response(text="\n".join(lines) + "\n", content_type="audio/x-mpegurl")


async def xmltv(request):
    ctl = request.app[CTL]
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    fmt = "%Y%m%d%H%M%S +0000"
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="freqon">']
    chans = list(_channels(request))
    for c, _, logo in chans:
        out.append(f'  <channel id={quoteattr(GUIDE_ID_PREFIX + c["id"])}><display-name>{escape(c["name"])}</display-name>'
                   f'<display-name>{escape(c["number"])}</display-name><icon src={quoteattr(logo)}/></channel>')
    for c, _, _ in chans:
        st = ctl.stations.get(c["frequency"])
        desc = f'{c["frequency"]:.1f} MHz {"HD" + str(c["program"] + 1) if c["hd"] else "FM"}'
        if c.get("program_type"):
            desc += f' · {c["program_type"]}'
        for h in range(-1, 47):
            start, stop = now + timedelta(hours=h), now + timedelta(hours=h + 1)
            out.append(f'  <programme start="{start.strftime(fmt)}" stop="{stop.strftime(fmt)}" '
                       f'channel={quoteattr(GUIDE_ID_PREFIX + c["id"])}><title>{escape(st.display_name if st else c["name"])}</title>'
                       f'<desc>{escape(desc)}</desc><category>Music</category></programme>')
    out.append("</tv>")
    return web.Response(text="\n".join(out) + "\n", content_type="application/xml")


def _device_id() -> str:
    return hashlib.sha1(socket.gethostname().encode()).hexdigest()[:8].upper()


async def discover_json(request):
    base = base_url(request)
    return json_response({
        "FriendlyName": request.app[CFG].get("friendly_name", "Freqon"),
        "Manufacturer": "Freqon",
        "ModelNumber": "HDTC-2US",
        "FirmwareName": "hdhomeruntc_atsc",
        "FirmwareVersion": "20240101",
        "DeviceID": _device_id(),
        "DeviceAuth": "hdradio",  # unchanged since before the Freqon name: Plex has it on record
        "BaseURL": base,
        "LineupURL": f"{base}/lineup.json",
        "TunerCount": len(request.app[CTL].pool),
    })


async def lineup_json(request):
    # "HD": 1 marks HD Radio channels (dashboards count them like an HDHomeRun's HD channels).
    return json_response([{"GuideNumber": c["number"], "GuideName": c["name"], "URL": url,
                           **({"HD": 1} if c["hd"] else {})}
                          for c, url, _ in _channels(request)])


async def status_json(request):
    """Per-tuner status in the HDHomeRun's format (what dashboards such as HomePage read).
    A tuner on a station reports its channel; signal fields appear only when they're known."""
    ctl = request.app[CTL]
    out = []
    for r in ctl.pool:
        entry = {"Resource": f"tuner{r.index}"}
        info = ctl.tuner_info(r)
        if info["scanning"]:
            entry["VctName"] = "Scanning"
        elif r.state.running and r.state.frequency is not None:
            program = min(r.state.mixers, default=0)
            st = ctl.stations.get(r.state.frequency)
            chan = next((c for c in st.channels() if c["program"] == program), None) if st else None
            entry["VctNumber"] = chan["number"] if chan else f"{round(r.state.frequency * 10)}.{program + 1}"
            entry["VctName"] = chan["name"] if chan else (r.state.station_name or f"{r.state.frequency:.1f} FM")
            entry["Frequency"] = round(r.state.frequency * 1_000_000)
            digital = r.state.snapshot(program)["digital"]
            if digital["synced"] and digital["quality"] is not None:
                entry["SignalQualityPercent"] = round(digital["quality"] * 100)
            if digital["synced"] and digital["ber"] is not None:
                entry["SymbolQualityPercent"] = max(0, round((1 - digital["ber"]) * 100))
        out.append(entry)
    return json_response(out)


async def lineup_status_json(request):
    scanning = request.app[CTL].scan.get("running", False)
    return json_response({"ScanInProgress": int(scanning), "ScanPossible": 1,
                          "Source": "Antenna", "SourceList": ["Antenna"]})


async def lineup_post(request):
    return web.Response(status=200)


async def device_xml(request):
    base = base_url(request)
    xml = f"""<root xmlns="urn:schemas-upnp-org:device-1-0">
<specVersion><major>1</major><minor>0</minor></specVersion><URLBase>{escape(base)}</URLBase>
<device><deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
<friendlyName>{escape(request.app[CFG].get("friendly_name", "Freqon"))}</friendlyName>
<manufacturer>Silicondust</manufacturer><modelName>HDTC-2US</modelName><modelNumber>HDTC-2US</modelNumber>
<serialNumber/><UDN>uuid:{_device_id()}</UDN></device></root>"""
    return web.Response(text=xml, content_type="application/xml")


# -- web client ------------------------------------------------------------------------------------

def _web_file(name: str) -> bytes:
    return resources.files("freqon").joinpath("web", name).read_bytes()


async def index(request):
    return web.Response(body=_web_file("index.html"), content_type="text/html")


async def static(request):
    name = request.match_info["name"]
    if name not in ("app.js", "style.css", "icon.svg"):
        raise web.HTTPNotFound()
    ctype = {"js": "application/javascript", "css": "text/css", "svg": "image/svg+xml"}[name.rsplit(".", 1)[1]]
    return web.Response(body=_web_file(name), content_type=ctype)


# -- app ----------------------------------------------------------------------------------------------

def make_app(controller: Controller, config: dict | None = None) -> web.Application:
    from . import auth

    app = web.Application(middlewares=[origin_guard, auth.login_guard])
    app[CTL] = controller
    app[CFG] = config or {}
    auth.setup(app, app[CFG])

    async def on_startup(app):
        await controller.start()

    async def on_shutdown(app):
        for ws in list(app[SOCKETS]):
            await ws.close(code=WSCloseCode.GOING_AWAY, message=b"server shutting down")
        controller.end_streams()

    async def on_cleanup(app):
        await controller.close()

    app[SOCKETS] = set()
    app[HOSTS] = allowed_hosts(app[CFG])
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    app.on_cleanup.append(on_cleanup)
    r = app.router
    r.add_get("/api/status", get_status)
    r.add_get("/api/stations", get_stations)
    r.add_post("/api/stations", add_station)
    r.add_patch("/api/stations/{freq}", patch_station)
    r.add_delete("/api/stations/{freq}", delete_station)
    r.add_post("/api/tune", post_tune)
    r.add_post("/api/mode", post_mode)
    r.add_post("/api/scan", post_scan)
    r.add_delete("/api/scan", delete_scan)
    r.add_get("/api/events", events)
    r.add_get("/stream.mp3", stream_current)
    r.add_get("/stream/{freq}/{program}.mp3", stream_tune_mp3)
    r.add_get("/stream/{freq}/{program}.ts", stream_tune_ts)
    r.add_get("/card.png", card_png)
    r.add_get("/logo/{cid}.png", channel_logo)
    r.add_get("/art/{cid}.jpg", channel_art)
    r.add_get("/maps/{kind}.png", map_image)
    r.add_get("/playlist.m3u", playlist_m3u)
    r.add_get("/xmltv.xml", xmltv)
    r.add_get("/discover.json", discover_json)
    r.add_get("/lineup.json", lineup_json)
    r.add_get("/lineup_status.json", lineup_status_json)
    r.add_get("/status.json", status_json)
    r.add_post("/lineup.post", lineup_post)
    r.add_get("/device.xml", device_xml)
    r.add_get("/", index)
    r.add_get("/static/{name}", static)
    return app

