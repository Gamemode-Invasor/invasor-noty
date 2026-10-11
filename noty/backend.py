"""Noty: show Steam notifications (as Steam shows an achievement) from a local web service.

Programs on this machine POST JSON to http://127.0.0.1:<port>/notify, or to the endpoint
of their own format (/notify/comet…, service.py), and Invasor's core shows it
(ctx.notify). If Steam can't show notifications, the request gets a 503 and nothing else
is affected; if the port is taken, the tab says so. Each endpoint can be turned off, and
"do not disturb" silences them all; both are read live, without restarting the service.
"""
import os
import subprocess
import threading
import time
from pathlib import Path

from . import games, service

# Comety (bin/comety) watches a Comet database and POSTs each new achievement to /notify/comet.
# One runs per database Comet keeps for the game being played: found a few seconds after it starts.
COMETY_BIN = Path(__file__).resolve().parent / "bin" / "comety"
# After a game starts, look for its databases after GAME_DELAY seconds (Comet creates them late),
# and, if there are none, RETRIES more times, GAME_RETRY seconds apart.
GAME_DELAY, GAME_RETRY, RETRIES = 10.0, 10.0, 2
GAME_POLL = 2.0

ctx = None
server = None
error = None
comety = {}  # database -> subprocess, for the running game
_lock = threading.Lock()
_stop = threading.Event()
_watcher = None


def _stop_comety():
    """Stop every comety (their databases are kept in `comety`, for _start_comety)."""
    for p in comety.values():
        if p:
            p.terminate()
    for p in comety.values():
        if p:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
    comety.update({db: None for db in comety})


def _start_comety(port, dbs=None):
    """Run a comety per database (the new ones, or all of them), against the same URL as
    /notify/comet. Never raises."""
    for db in comety if dbs is None else dbs:
        try:
            os.chmod(COMETY_BIN, 0o755)  # the installer doesn't keep the executable bit
            comety[db] = subprocess.Popen(
                [str(COMETY_BIN), "--url", f"http://127.0.0.1:{port}/notify/comet", "--quiet", db],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            ctx.log.warning("can't run comety: %s", e.strerror or e)
            return
        ctx.log.info("comety watching %s", db)


def _logo(appid):
    for path in games.logo_files(appid):
        try:
            return service.icon_from_file(path)
        except service.Rejected:
            continue  # too big, or not an image: the next one
    return ""


def _connect(appid, name, dbs):
    """Databases of the game found: watch them and, the first time, tell the user."""
    with _lock:
        if not server or _stop.is_set():
            return False
        first = not comety
        for db in dbs:
            comety[db] = None
        _start_comety(server.port, dbs)
        if first:
            try:
                server.receive("comet", f"Comet - {name}"[:64], f"Noty is connecting to {name}"[:256], _logo(appid))
            except service.Rejected as e:
                ctx.log.warning("notification not shown: %s", e)
    return True


def _watch():
    """Follow the running game and look for its databases (see GAME_DELAY), while the Comet
    endpoint is on. Never raises."""
    current, due, retries = None, 0.0, 0
    while not _stop.wait(GAME_POLL):
        try:
            if not ctx.settings.get("endpoint_comet"):
                # Comet's endpoint is off: no comety, no looking for databases. Turned on again
                # with a game running, it counts as a game that has just started.
                if current is not None or comety:
                    current = None
                    with _lock:
                        _stop_comety()
                        comety.clear()
                continue
            game = ctx.game.running
            appid = game["appid"] if game else None
            if appid != current:
                current, due, retries = appid, time.monotonic() + GAME_DELAY, RETRIES
                with _lock:
                    _stop_comety()
                    comety.clear()
            if not appid or not server or time.monotonic() < due or retries < 0:
                continue
            dbs = games.gameplay_dbs(appid)
            if dbs:
                _connect(appid, game.get("name") or appid, dbs)
                retries = -1  # found: done with this game
            else:
                ctx.log.info("no Comet database yet for %s (%s retries left)", appid, retries)
                due, retries = time.monotonic() + GAME_RETRY, retries - 1
        except Exception:
            ctx.log.exception("watching games failed")


def _start():
    """(Re)start the service with the current settings. Never raises."""
    global server, error
    with _lock:
        _stop_comety()
        if server:
            server.stop()
            server = None
        error = None
        if not ctx.settings.get("enabled"):
            return
        port = ctx.settings.get("port")
        s = service.NotyServer(port, ctx.notify, ctx.log, unavailable=(ctx.Unavailable,),
                               endpoint_on=lambda name: ctx.settings.get(f"endpoint_{name}"),
                               quiet=lambda: ctx.settings.get("dnd"))
        try:
            s.start()
        except OSError as e:
            error = f"can't listen on 127.0.0.1:{port} ({e.strerror or e})"
            ctx.log.warning("%s", error)
            return
        server = s
        ctx.log.info("listening on http://127.0.0.1:%s/notify", port)
        _start_comety(s.port)


def setup(context):
    global ctx, _watcher
    ctx = context
    # Only these need a new server: endpoints and "do not disturb" are read live.
    context.settings.on_change(lambda key, value: _start() if key in ("enabled", "port") else None)
    _start()
    _stop.clear()
    _watcher = threading.Thread(target=_watch, name="noty-games", daemon=True)
    _watcher.start()


def teardown():
    global server, _watcher
    _stop.set()
    if _watcher:
        _watcher.join(5)
        _watcher = None
    with _lock:
        _stop_comety()
        comety.clear()
        if server:
            server.stop()
            server = None


def status():
    port = ctx.settings.get("port")
    base = f"http://127.0.0.1:{port}"
    endpoints = [{"name": name, "path": path, "url": base + path, "on": ctx.settings.get(f"endpoint_{name}")}
                 for name, (path, _) in service.ENDPOINTS.items()]
    with _lock:
        watching = [{"db": db, "running": p is not None and p.poll() is None} for db, p in comety.items()]
    return {"enabled": ctx.settings.get("enabled"), "running": server is not None, "port": port,
            "url": base + "/notify", "error": error, "comety": watching,
            "dnd": ctx.settings.get("dnd"), "endpoints": endpoints}


def recent():
    return list(server.recent) if server else []


def test():
    """Show a test notification (through the service's rate limit, as a POST would). It
    ignores "do not disturb" and the endpoints: the user asked for it."""
    s = server or service.NotyServer(0, ctx.notify, ctx.log, unavailable=(ctx.Unavailable,))
    try:
        s.notify("Noty", "This is a test notification from Invasor.", "")
    except service.Rejected as e:
        raise (ctx.Unavailable if e.status == 503 else ctx.InvalidArgument)(str(e)) from None
    return True


METHODS = {"status": status, "recent": recent, "test": test}
