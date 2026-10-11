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
from pathlib import Path

from . import service

# Comety (bin/comety) watches Comet's database and POSTs each new achievement to /notify/comet.
COMETY_BIN = Path(__file__).resolve().parent / "bin" / "comety"
COMETY_DB = "/home/deck/Proyectos/comety/gameplay.db"  # example, for now

ctx = None
server = None
error = None
comety = None
_lock = threading.Lock()


def _stop_comety():
    global comety
    p, comety = comety, None
    if not p:
        return
    p.terminate()
    try:
        p.wait(5)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()


def _start_comety(port):
    """Run comety against the same URL as /notify/comet. Never raises."""
    global comety
    try:
        os.chmod(COMETY_BIN, 0o755)  # the installer doesn't keep the executable bit
        comety = subprocess.Popen(
            [str(COMETY_BIN), "--url", f"http://127.0.0.1:{port}/notify/comet", "--quiet", COMETY_DB],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        ctx.log.warning("can't run comety: %s", e.strerror or e)
        return
    ctx.log.info("comety watching %s", COMETY_DB)


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
    global ctx
    ctx = context
    # Only these need a new server: endpoints and "do not disturb" are read live.
    context.settings.on_change(lambda key, value: _start() if key in ("enabled", "port") else None)
    _start()


def teardown():
    global server
    with _lock:
        _stop_comety()
        if server:
            server.stop()
            server = None


def status():
    port = ctx.settings.get("port")
    base = f"http://127.0.0.1:{port}"
    endpoints = [{"name": name, "path": path, "url": base + path, "on": ctx.settings.get(f"endpoint_{name}")}
                 for name, (path, _) in service.ENDPOINTS.items()]
    return {"enabled": ctx.settings.get("enabled"), "running": server is not None, "port": port,
            "url": base + "/notify", "error": error,
            "comety": {"running": comety is not None and comety.poll() is None, "db": COMETY_DB}, "dnd": ctx.settings.get("dnd"), "endpoints": endpoints}


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
