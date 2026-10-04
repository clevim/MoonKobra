"""
auth.py - optional login barrier in front of the bridge.

Off for anyone who configures nothing: without `auth_user`/`auth_password` in
config.ini the bridge runs exactly as before (no login, no cookies).

Three ways through the barrier:
  1. Session cookie – the browser, after logging in at /login
  2. X-Api-Key      – OrcaSlicer, moonraker-obico (Moonraker convention)
  3. none           – 401 or a redirect to /login

A cookie is needed (instead of HTTP Basic Auth) because the browser does not send
the Authorization header on the WebSocket upgrade, but it does send cookies -
otherwise the UI's live updates would die.
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import pathlib
import secrets
import time
import urllib.parse

from aiohttp import web

log = logging.getLogger("kobrax.auth")

COOKIE_NAME = "kx_session"
SESSION_MAX_AGE = 30 * 24 * 3600          # 30 dias
LOGIN_PATH = "/login"

# Reachable without login: the login page itself, its assets and the favicon.
# Everything else - including /api/*, /kx/*, Moonraker and WebSocket - is closed.
_PUBLIC_PATHS = frozenset({LOGIN_PATH, "/api/login", "/favicon.ico"})
# /kx/ui/ only serves static theme files (CSS, app.js, translations) from a
# public GPL repository - without them the login page cannot render, and
# there is nothing secret in them.
_PUBLIC_PREFIXES = ("/kx/ui/",)

# Factory default login: created in config.ini on the first start, with a
# mandatory password change on first access (must_change). Until the password
# is changed, the session only reaches the login page, the change and logout.
DEFAULT_USER = "kx"
DEFAULT_PASSWORD = "kx123"
CHANGE_PATH = "/api/auth/change"
_MUST_CHANGE_OK = frozenset({CHANGE_PATH, "/api/logout"})

# Camera stream and snapshot with a camera-ONLY token (?token=...), for OBS and
# the like, which cannot log in. It opens no other endpoint.
CAMERA_PATHS = frozenset({"/api/camera/stream", "/api/camera/snapshot", "/webcam/", "/webcam"})


# ── Password hash ────────────────────────────────────────────────────────────
# scrypt comes from the stdlib. The password is never stored in plain text, so a
# shared config.ini (screenshot, support thread, backup) does not reveal it.
# ponytail: fixed parameters in the code - enough for a single-user login;
# with more users they should travel inside the hash string.
_SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32)
_HASH_PREFIX = "scrypt$"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return _HASH_PREFIX + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password: str, stored: str) -> bool:
    """Checks against a scrypt hash or - for quick tests - against a
    plain-text password in config.ini."""
    stored = (stored or "").strip()
    if not stored:
        return False
    if not stored.startswith(_HASH_PREFIX):
        return hmac.compare_digest(password, stored)
    try:
        _, salt_b64, dk_b64 = stored.split("$", 2)
        salt = base64.b64decode(salt_b64)
        expect = base64.b64decode(dk_b64)
    except Exception:
        log.warning("auth: malformed password hash in the config - login disabled")
        return False
    dk = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return hmac.compare_digest(dk, expect)


# ── Session secret ───────────────────────────────────────────────────────────
def load_or_create_secret(data_dir: str) -> bytes:
    """Signing key of the session cookies, persisted in data_dir so that
    restarting the bridge does not log every session out."""
    path = pathlib.Path(data_dir) / "session.key"
    try:
        raw = path.read_bytes().strip()
        if len(raw) >= 32:
            return raw
    except OSError:
        pass
    raw = secrets.token_bytes(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        os.chmod(path, 0o600)
    except OSError as e:
        log.warning("auth: could not persist the session key (%s) - sessions reset on restart", e)
    return raw


# ── Sign / verify cookie ──────────────────────────────────────────────────────
def _sign(secret: bytes, msg: bytes) -> str:
    return base64.urlsafe_b64encode(hmac.new(secret, msg, hashlib.sha256).digest()).decode().rstrip("=")


def make_session(secret: bytes, user: str, now: float | None = None) -> str:
    payload = json.dumps({"u": user, "exp": int((now or time.time()) + SESSION_MAX_AGE)},
                         separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return body + "." + _sign(secret, body.encode())


def check_session(secret: bytes, token: str, now: float | None = None) -> str:
    """Returns the user name, or "" if the token is invalid/expired."""
    if not token or "." not in token:
        return ""
    body, _, sig = token.rpartition(".")
    if not hmac.compare_digest(sig, _sign(secret, body.encode())):
        return ""
    try:
        pad = "=" * (-len(body) % 4)
        data = json.loads(base64.urlsafe_b64decode(body + pad))
    except Exception:
        return ""
    if float(data.get("exp", 0)) < (now or time.time()):
        return ""
    return str(data.get("u") or "")


# ── Brute-force lockout ──────────────────────────────────────────────────────
# With the bridge published to the internet (tunnel/proxy), the fixed delay is
# not enough: 5 wrong passwords in a row from the same IP lock it out for 15 minutes.
LOCK_FAILS = 5
LOCK_SECONDS = 15 * 60
_fails: dict = {}          # ip -> [consecutive failures, locked until]
# Only the real local network (RFC 1918 + ULA): where a tunnel/proxy talks to the bridge from.
_LOCAL_NETS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")]


def client_ip(request) -> str:
    """IP of whoever is on the other side. Behind a tunnel/proxy on the local network
    (Cloudflare, nginx) the remote is the proxy itself: then the header it sends
    counts. Coming straight from outside, the header is ignored (it could be forged)."""
    remote = request.remote or ""
    try:
        addr = ipaddress.ip_address(remote)
        trusted = addr.is_loopback or any(addr in n for n in _LOCAL_NETS)
    except ValueError:
        trusted = False
    if trusted:
        fwd = request.headers.get("CF-Connecting-IP") or \
            (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        if fwd:
            return fwd
    return remote


def login_locked(ip: str, now: float | None = None) -> float:
    """Seconds of lockout still left for this IP (0 = free)."""
    rec = _fails.get(ip)
    left = (rec[1] - (now or time.time())) if rec else 0
    return max(left, 0)


def login_failed(ip: str, now: float | None = None) -> None:
    now = now or time.time()
    rec = _fails.setdefault(ip, [0, 0.0])
    rec[0] += 1
    if rec[0] >= LOCK_FAILS:
        rec[0], rec[1] = 0, now + LOCK_SECONDS
    if len(_fails) > 5000:            # keeps the table from growing forever
        for k in [k for k, v in _fails.items() if v[1] < now][:2500]:
            _fails.pop(k, None)


def login_ok(ip: str) -> None:
    _fails.pop(ip, None)


# ── Middleware ───────────────────────────────────────────────────────────────
def _is_public(path: str) -> bool:
    return path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES)


def _wants_html(request) -> bool:
    return "text/html" in request.headers.get("Accept", "")


def make_auth_middleware(user: str, password: str, api_key: str, secret: bytes,
                         must_change: bool = False, camera_token: str = ""):
    """None when no login is configured - then everything works as before.
    With must_change, a valid session only gets through to the password change and logout."""
    if not user or not password:
        return None

    @web.middleware
    async def auth_middleware(request, handler):
        # The CORS preflight must pass without credentials, otherwise every
        # cross-origin call fails before the real request is even sent.
        if request.method == "OPTIONS" or _is_public(request.path):
            return await handler(request)

        if camera_token and request.path in CAMERA_PATHS and \
                hmac.compare_digest(request.query.get("token", ""), camera_token):
            return await handler(request)

        if api_key:
            sent = request.headers.get("X-Api-Key") or request.query.get("api_key", "")
            if sent and hmac.compare_digest(sent, api_key):
                return await handler(request)

        if check_session(secret, request.cookies.get(COOKIE_NAME, "")):
            if not must_change or request.path in _MUST_CHANGE_OK:
                return await handler(request)
            if _wants_html(request):
                raise web.HTTPFound(f"{LOGIN_PATH}?trocar=1")
            return web.json_response({"error": "change the default password", "must_change": True}, status=403)

        if _wants_html(request):
            nxt = urllib.parse.quote(request.path_qs, safe="")
            raise web.HTTPFound(f"{LOGIN_PATH}?next={nxt}")
        return web.json_response({"error": "unauthorized"}, status=401)

    return auth_middleware


# ── Handlers ─────────────────────────────────────────────────────────────────
def make_handlers(user: str, password: str, secret: bytes, login_html_path: str,
                  must_change: bool = False):
    """(handle_login_page, handle_login_post, handle_logout) for build_app()."""

    async def handle_login_page(request):
        try:
            html = pathlib.Path(login_html_path).read_text(encoding="utf-8")
        except OSError:
            raise web.HTTPNotFound()
        return web.Response(text=html, content_type="text/html",
                            headers={"Cache-Control": "no-store"})

    async def handle_login_post(request):
        ip = client_ip(request)
        if login_locked(ip):
            log.warning("auth: login locked for %s (too many wrong passwords)", ip)
            return web.json_response({"error": "locked", "retry_after": int(login_locked(ip))}, status=429)
        try:
            body = await request.json()
        except Exception:
            body = dict(await request.post())
        ok = (hmac.compare_digest(str(body.get("user", "")), user)
              and verify_password(str(body.get("password", "")), password))
        if not ok:
            log.warning("auth: failed login from %s", ip)
            login_failed(ip)
            # Slows down password guessing without keeping a counter.
            # ponytail: fixed delay; under real brute force, a per-IP lockout
            # goes here.
            import asyncio
            await asyncio.sleep(1.0)
            return web.json_response({"error": "invalid"}, status=401)
        login_ok(ip)
        resp = web.json_response({"ok": True, "must_change": must_change})
        resp.set_cookie(
            COOKIE_NAME, make_session(secret, user),
            max_age=SESSION_MAX_AGE, httponly=True, samesite="Lax",
            secure=request.headers.get("X-Forwarded-Proto") == "https",
            path="/",
        )
        return resp

    async def handle_logout(request):
        resp = web.json_response({"ok": True})
        resp.del_cookie(COOKIE_NAME, path="/")
        return resp

    return handle_login_page, handle_login_post, handle_logout


# ── CLI + self-test ──────────────────────────────────────────────────────────
def _selftest():
    h = hash_password("hunter2")
    assert h.startswith(_HASH_PREFIX) and verify_password("hunter2", h)
    assert not verify_password("hunter3", h)
    assert verify_password("plain", "plain") and not verify_password("plain", "other")
    assert not verify_password("x", "") and not verify_password("x", _HASH_PREFIX + "garbage")

    sec = secrets.token_bytes(32)
    tok = make_session(sec, "admin")
    assert check_session(sec, tok) == "admin"
    assert check_session(secrets.token_bytes(32), tok) == ""      # someone else's key
    assert check_session(sec, tok[:-1] + ("a" if tok[-1] != "a" else "b")) == ""  # tampered signature
    assert check_session(sec, "") == "" and check_session(sec, "nodot") == ""
    # expired: issued before SESSION_MAX_AGE + 1s
    old = make_session(sec, "admin", now=time.time() - SESSION_MAX_AGE - 1)
    assert check_session(sec, old) == ""

    assert _is_public("/login") and _is_public("/kx/ui/style.css")
    assert not _is_public("/api/state") and not _is_public("/websocket")
    print("auth: autoteste ok")


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 2 and sys.argv[1] != "--selftest":
        print(hash_password(sys.argv[1]))
    elif len(sys.argv) == 2:
        _selftest()
    else:
        print("usage: python3 auth.py <password>   # hash for config.ini [auth] password\n"
              "       python3 auth.py --selftest")
        sys.exit(2)
