"""Optional login for the web player.

Off unless both web_username and web_password are set. When on, the web player, its API and
its MP3 streams need a session cookie; the endpoints Plex and Jellyfin use (HDHomeRun
emulation, playlist, guide, video streams, images) stay open, since those servers can't log in.

The session cookie is "<expiry>.<session id>.<HMAC>", signed with a key kept in
data_dir/session.key, so it survives restarts (Freqon restarts itself after a dongle stall).
The password is part of the signed text, so changing it signs everyone out. Signing out
revokes that session id (kept in data_dir/revoked.json until it would have expired).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import time
from html import escape

from aiohttp import web

log = logging.getLogger(__name__)

COOKIE = "freqon_session"
SESSION_DAYS = 30
MAX_FAILURES = 10  # per address (IPv6: per /64), within FAILURE_WINDOW seconds
MAX_ACCOUNT_FAILURES = 100  # from anywhere: then every address that has failed waits it out
FAILURE_WINDOW = 600

AUTH = web.AppKey("auth", object)


class Auth:
    def __init__(self, username: str, password: str, key: bytes, fail_delay: float = 1.0,
                 revoked_path: str | None = None):
        self.username = username
        self._password = password
        self._key = key
        self.fail_delay = fail_delay
        self._failures: dict[str, list[float]] = {}
        self._all_failures: list[float] = []
        self._revoked_path = revoked_path
        self._revoked: dict[str, int] = self._load_revoked()

    def _sign(self, expires: int, sid: str) -> str:
        pw = hashlib.sha256(self._password.encode()).hexdigest()
        msg = f"{self.username}\n{expires}\n{sid}\n{pw}".encode()
        return hmac.new(self._key, msg, hashlib.sha256).hexdigest()

    def make_token(self, now: float | None = None) -> str:
        expires = int((now or time.time()) + SESSION_DAYS * 86400)
        sid = secrets.token_hex(12)
        return f"{expires}.{sid}.{self._sign(expires, sid)}"

    def _parse(self, token: str | None, now: float | None = None) -> tuple[int, str] | None:
        """(expiry, session id) of a valid, unexpired, unrevoked token; else None."""
        try:
            expires_text, sid, sig = (token or "").split(".")
            expires = int(expires_text)
        except ValueError:
            return None
        if expires < (now or time.time()) or sid in self._revoked:
            return None
        if not hmac.compare_digest(sig, self._sign(expires, sid)):
            return None
        return expires, sid

    def check_token(self, token: str | None, now: float | None = None) -> bool:
        return self._parse(token, now) is not None

    def revoke(self, token: str | None) -> None:
        parsed = self._parse(token)
        if parsed is None:
            return
        expires, sid = parsed
        now = time.time()
        self._revoked = {s: e for s, e in self._revoked.items() if e > now}
        self._revoked[sid] = expires
        self._save_revoked()

    def _load_revoked(self) -> dict[str, int]:
        if not self._revoked_path:
            return {}
        try:
            with open(self._revoked_path) as f:
                data = json.load(f)
            now = time.time()
            return {str(s): int(e) for s, e in data.items() if int(e) > now}
        except (OSError, ValueError, AttributeError):
            return {}

    def _save_revoked(self) -> None:
        if not self._revoked_path:
            return
        tmp = f"{self._revoked_path}.{os.getpid()}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self._revoked, f)
            os.replace(tmp, self._revoked_path)
        except OSError as e:
            log.warning("couldn't save signed-out sessions: %s", e)

    def check_password(self, username: str, password: str) -> bool:
        # Both compared every time, in constant time, so the answer doesn't leak which was wrong.
        ok_user = hmac.compare_digest(username.encode(), self.username.encode())
        ok_pass = hmac.compare_digest(password.encode(), self._password.encode())
        return ok_user and ok_pass

    @staticmethod
    def _source(address: str) -> str:
        """Who is trying: the address, or for IPv6 its /64 (one home or device can use many)."""
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return address
        if ip.version == 6:
            if ip.ipv4_mapped:
                return str(ip.ipv4_mapped)
            return str(ipaddress.ip_network(f"{ip}/64", strict=False))
        return str(ip)

    def locked_out(self, address: str) -> bool:
        now = time.monotonic()
        self._all_failures = [t for t in self._all_failures if now - t < FAILURE_WINDOW]
        for src in list(self._failures):
            recent = [t for t in self._failures[src] if now - t < FAILURE_WINDOW]
            if recent:
                self._failures[src] = recent
            else:
                del self._failures[src]
        mine = len(self._failures.get(self._source(address), []))
        # The account-wide cap never blocks an address with no wrong tries of its own, so a
        # flood of guesses from elsewhere can't lock the owner out.
        return mine >= MAX_FAILURES or (mine > 0 and len(self._all_failures) >= MAX_ACCOUNT_FAILURES)

    def failed(self, address: str) -> None:
        now = time.monotonic()
        self._failures.setdefault(self._source(address), []).append(now)
        self._all_failures.append(now)


def session_key(data_dir: str | None) -> bytes:
    """The signing key, made once and kept private in data_dir (in memory if there is none)."""
    if not data_dir:
        return secrets.token_bytes(32)
    path = os.path.join(os.path.expanduser(data_dir), "session.key")
    try:
        with open(path, "rb") as f:
            key = f.read()
        if len(key) >= 32:
            return key
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    key = secrets.token_bytes(32)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    os.replace(tmp, path)
    return key


def from_config(config: dict) -> Auth | None:
    user, password = config.get("web_username"), config.get("web_password")
    if not (user and password):
        return None
    data_dir = config.get("data_dir")
    return Auth(user, password, session_key(data_dir), fail_delay=float(config.get("login_fail_delay", 1.0)),
                revoked_path=os.path.join(os.path.expanduser(data_dir), "revoked.json") if data_dir else None)


def needs_login(path: str) -> bool:
    """The web player and what only it uses."""
    return (path == "/" or path.startswith("/api/") or path == "/stream.mp3"
            or (path.startswith("/stream/") and path.endswith(".mp3")))


@web.middleware
async def login_guard(request: web.Request, handler):
    auth: Auth | None = request.app.get(AUTH)
    if auth is None or not needs_login(request.path) or auth.check_token(request.cookies.get(COOKIE)):
        return await handler(request)
    if request.path == "/":
        raise web.HTTPFound("/login")
    return web.json_response({"error": "login required", "message": "Sign in to use Freqon."}, status=401)


# -- pages ------------------------------------------------------------------------------------------

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in · Freqon</title>
<link rel="icon" href="/static/icon.svg" type="image/svg+xml">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;600&family=Barlow:wght@400;500;600&display=swap">
<link rel="stylesheet" href="/static/style.css">
</head>
<body>
<main class="login">
  <header class="brand"><img src="/static/icon.svg" alt="" width="28" height="28"><span>Freqon</span></header>
  <form method="post" action="/login" class="login-form">
    <label>Username <input name="username" autocomplete="username" required {focus_user} value="{username}"></label>
    <label>Password <input name="password" type="password" autocomplete="current-password" required {focus_pass}></label>
    {error}
    <button type="submit">Sign in</button>
  </form>
</main>
</body>
</html>
"""


def _page(username: str = "", error: str = "", status: int = 200) -> web.Response:
    err = f'<p class="login-error" role="alert">{escape(error)}</p>' if error else ""
    focus = ("", "autofocus") if username else ("autofocus", "")  # after a wrong try: the password
    return web.Response(text=_PAGE.format(username=escape(username, quote=True), error=err,
                                          focus_user=focus[0], focus_pass=focus[1]),
                        content_type="text/html", status=status,
                        headers={"Cache-Control": "no-store"})


async def login_page(request: web.Request) -> web.StreamResponse:
    auth: Auth | None = request.app.get(AUTH)
    if auth is None or auth.check_token(request.cookies.get(COOKIE)):
        raise web.HTTPFound("/")
    return _page()


async def login_post(request: web.Request) -> web.StreamResponse:
    auth: Auth | None = request.app.get(AUTH)
    if auth is None:
        raise web.HTTPFound("/")
    address = request.remote or "?"
    form = await request.post()
    username = str(form.get("username", ""))[:200]
    password = str(form.get("password", ""))[:1000]
    if auth.locked_out(address):
        return _page(username, "Too many wrong tries. Wait ten minutes, then try again.", status=429)
    if not auth.check_password(username, password):
        auth.failed(address)
        log.warning("failed sign-in from %s", address)
        await asyncio.sleep(auth.fail_delay)  # slows down guessing
        return _page(username, "Wrong username or password.", status=401)
    resp = web.HTTPFound("/")
    resp.set_cookie(COOKIE, auth.make_token(), max_age=SESSION_DAYS * 86400, httponly=True,
                    samesite="Lax", path="/", secure=request.secure)
    raise resp


async def logout(request: web.Request) -> web.StreamResponse:
    auth: Auth | None = request.app.get(AUTH)
    if auth is not None:
        auth.revoke(request.cookies.get(COOKIE))  # a copy of the cookie stops working too
    resp = web.HTTPFound("/login")
    resp.del_cookie(COOKIE, path="/")
    raise resp


async def session_info(request: web.Request) -> web.Response:
    auth: Auth | None = request.app.get(AUTH)
    if auth is None:
        return web.json_response({"login": False})
    return web.json_response({"login": True, "username": auth.username})


def setup(app: web.Application, config: dict) -> None:
    app[AUTH] = from_config(config)
    app.router.add_get("/login", login_page)
    app.router.add_post("/login", login_post)
    app.router.add_post("/logout", logout)
    app.router.add_get("/api/session", session_info)
