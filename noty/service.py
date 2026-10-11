"""Noty's local web service: POST JSON to an endpoint and it shows a Steam notification.

Pure stdlib, no Invasor imports: tested on its own (tests/test_service.py). It listens
on 127.0.0.1 only, so only programs on this machine can use it.

    POST /notify         {"title": "…", "body": "…", "icon": "https://…" | "data:image/…" | "/path/to/image.png"}
    POST /notify/comet   Comet's (GOG) unlocked achievement: {"name": "…", "description": "…", …}
    GET  /               {"ok": true, "service": "noty", "endpoints": [...]}

Each endpoint has an adapter (ENDPOINTS) that turns its payload into (title, body, icon),
or None for an event that isn't shown. An endpoint can be turned off, and "do not
disturb" silences them all: the request is still checked and answered (200, shown:
false), but nothing is shown.

Answers: 200 {"ok": true, "shown": true|false, "reason": …}; 400 bad request; 404;
413 too large; 415 not JSON; 429 too many (more than RATE notifications in RATE_WINDOW
seconds); 503 Steam can't show it. A web page in a browser can't use it: it requires
application/json (so a browser asks first, and gets no permission) and a Host of
127.0.0.1/localhost.
"""
import base64
import collections
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
MAX_BODY = 768 << 10  # room for an embedded image (data:…) of up to 512 KB
MAX_ICON_FILE = 384 << 10
RATE, RATE_WINDOW = 5, 10.0
IMAGE_TYPES = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".gif": "gif", ".webp": "webp"}
MAGIC = {"png": b"\x89PNG", "jpeg": b"\xff\xd8\xff", "gif": b"GIF8", "webp": b"RIFF"}


class Rejected(Exception):
    """A request we answer with an error: (status, message)."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def icon_from_file(path):
    """A local image (absolute path) as a data: URI, so Steam can show it."""
    p = Path(path)
    if not p.is_absolute():
        raise Rejected(400, "icon: an absolute path, an https:// URL or data:image/…")
    kind = IMAGE_TYPES.get(p.suffix.lower())
    if not kind:
        raise Rejected(400, f"icon: only {', '.join(sorted(IMAGE_TYPES))} files")
    try:
        if p.stat().st_size > MAX_ICON_FILE:
            raise Rejected(413, f"icon: files up to {MAX_ICON_FILE >> 10} KB")
        data = p.read_bytes()
    except OSError as e:
        raise Rejected(400, f"icon: can't read {path} ({e.strerror or e})") from None
    if not data.startswith(MAGIC[kind]):
        raise Rejected(400, f"icon: {p.name} isn't a {kind} image")
    return f"data:image/{kind};base64,{base64.b64encode(data).decode()}"


def load_json(raw):
    """The request body as a JSON object, or Rejected."""
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise Rejected(400, "the body isn't valid JSON") from None
    if not isinstance(data, dict):
        raise Rejected(400, 'send a JSON object: {"title": "…", "body": "…"}')
    return data


def validate(title, body, icon):
    """(title, body, icon) checked, with a local icon file read as a data: URI."""
    if not isinstance(title, str) or not isinstance(body, str) or not isinstance(icon, str):
        raise Rejected(400, "title, body and icon must be text")
    if icon and not icon.startswith(("https://", "data:")):
        icon = icon_from_file(icon)  # a local file
    return title, body, icon


def native(data):
    """/notify: Noty's own format, strict (a typo in a field name is an error)."""
    unknown = set(data) - {"title", "body", "icon"}
    if unknown:
        raise Rejected(400, f"unknown field(s): {', '.join(sorted(unknown))}")
    return data.get("title"), data.get("body", ""), data.get("icon", "")


def comet(data):
    """/notify/comet: an unlocked achievement, as Comet sends it (the achievement itself, flat).
    Lenient: Comet may add fields at any time."""
    if not isinstance(data.get("name"), str):
        raise Rejected(400, "name is required")
    body = data.get("description")
    body = body if isinstance(body, str) else ""
    rarity = data.get("rarity")
    if isinstance(rarity, (int, float)) and not isinstance(rarity, bool):
        body = f"{body} · {rarity:.1f}%" if body else f"{rarity:.1f}%"
    url = data.get("image_url_unlocked")
    icon = url if isinstance(url, str) and url.startswith("https://") else ""
    return data["name"], body, icon


# name -> (path, adapter). The name is the endpoint's setting: endpoint_<name>.
ENDPOINTS = {
    "general": ("/notify", native),
    "comet": ("/notify/comet", comet),
}
ROUTES = {path: (name, adapter) for name, (path, adapter) in ENDPOINTS.items()}


class RateLimit:
    def __init__(self, count=RATE, window=RATE_WINDOW, clock=time.monotonic):
        self.count, self.window, self.clock = count, window, clock
        self.times = collections.deque()
        self.lock = threading.Lock()

    def allow(self):
        with self.lock:
            now = self.clock()
            while self.times and now - self.times[0] >= self.window:
                self.times.popleft()
            if len(self.times) >= self.count:
                return False
            self.times.append(now)
            return True


class NotyServer:
    """The service. send(title, body, icon) shows the notification and raises
    ValueError (bad input -> 400) or another exception (Steam can't -> 503).
    endpoint_on(name) and quiet() are read on every request, so turning an endpoint
    off or "do not disturb" on takes effect at once, without a restart."""

    def __init__(self, port, send, log, unavailable=(), host=HOST,
                 endpoint_on=lambda name: True, quiet=lambda: False):
        self.port, self.send, self.log, self.host = port, send, log, host
        self.endpoint_on, self.quiet = endpoint_on, quiet
        self.unavailable = tuple(unavailable)
        self.limit = RateLimit()
        self.recent = collections.deque(maxlen=10)
        self._server = None
        self._thread = None

    def start(self):
        """Listen in a thread of its own. OSError if the port can't be used."""
        server = ThreadingHTTPServer((self.host, self.port), self._handler())
        server.daemon_threads = True
        self._server = server
        self.port = server.server_address[1]  # port 0 (tests): the one picked
        self._thread = threading.Thread(target=server.serve_forever, name="noty-http", daemon=True)
        self._thread.start()

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread:
            self._thread.join(5)
            self._thread = None

    @property
    def running(self):
        return self._server is not None

    def _entry(self, source, title, muted=None):
        entry = {"time": time.strftime("%H:%M:%S"), "source": source, "title": title[:64],
                 "ok": muted is not None, "error": None, "muted": muted}
        self.recent.appendleft(entry)
        return entry

    def receive(self, source, title, body, icon):
        """A notification from an endpoint: shown, or silenced (endpoint off, do not
        disturb). Returns None when shown, or the reason it wasn't."""
        reason = ("endpoint_off" if not self.endpoint_on(source)
                  else "do_not_disturb" if self.quiet() else None)
        if reason:
            self._entry(source, title, muted=reason)
            return reason
        self.notify(title, body, icon, source)
        return None

    def notify(self, title, body, icon, source="test"):
        """Show one notification, whatever the silences (rate limit included)."""
        if not self.limit.allow():
            raise Rejected(429, f"too many notifications: {RATE} every {RATE_WINDOW:g} s at most")
        entry = self._entry(source, title)
        try:
            self.send(title, body, icon)
        except ValueError as e:
            entry["error"] = str(e)
            raise Rejected(400, str(e)) from None
        except self.unavailable as e:
            entry["error"] = str(e)
            raise Rejected(503, str(e)) from None
        entry["ok"] = True

    def _handler(self):
        service = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "Noty"
            sys_version = ""

            def log_message(self, fmt, *args):  # into the module's log, not stderr
                service.log.debug("http: " + fmt, *args)

            def _answer(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _host_ok(self):
                host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
                return host in ("127.0.0.1", "localhost")

            def do_GET(self):
                if not self._host_ok():
                    return self._answer(403, {"ok": False, "error": "use 127.0.0.1"})
                if self.path != "/":
                    return self._answer(404, {"ok": False, "error": "POST to " + ", ".join(ROUTES)})
                self._answer(200, {"ok": True, "service": "noty", "endpoints": list(ROUTES)})

            def do_POST(self):
                try:
                    if not self._host_ok():
                        raise Rejected(403, "use 127.0.0.1")
                    route = ROUTES.get(self.path)
                    if not route:
                        raise Rejected(404, "POST to " + ", ".join(ROUTES))
                    source, adapter = route
                    ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                    if ctype != "application/json":
                        raise Rejected(415, "Content-Type must be application/json")
                    try:
                        length = int(self.headers.get("Content-Length") or "")
                    except ValueError:
                        raise Rejected(411, "Content-Length is required") from None
                    if length < 0 or length > MAX_BODY:
                        raise Rejected(413, f"the body is limited to {MAX_BODY >> 10} KB")
                    found = adapter(load_json(self.rfile.read(length)))
                    if found is None:
                        service.log.debug("%s: event ignored", source)
                        return self._answer(200, {"ok": True, "shown": False, "reason": "ignored"})
                    reason = service.receive(source, *validate(*found))
                except Rejected as e:
                    if e.status >= 500:
                        service.log.warning("notification not shown: %s", e)
                    return self._answer(e.status, {"ok": False, "error": str(e)})
                except Exception:
                    service.log.exception("notification request failed")
                    return self._answer(500, {"ok": False, "error": "internal error (see the log)"})
                if reason:
                    return self._answer(200, {"ok": True, "shown": False, "reason": reason})
                self._answer(200, {"ok": True, "shown": True})

        return Handler
