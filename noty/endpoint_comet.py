"""/notify/comet: Comet's (GOG) unlocked achievements.

Comet keeps each game's achievements in a SQLite database (gameplay.db). While this endpoint is on,
Noty follows the game being played and, for each database Comet keeps for it, runs Comety
(bin/comety), which POSTs every new unlock to this endpoint. Its messages are about the game:
"connecting to" it when the databases are found, "disconnected" when it ends.
"""
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import games, service
from .endpoints import Endpoint

COMETY_BIN = Path(__file__).resolve().parent / "bin" / "comety"
# After a game starts, look for its databases (Comet creates them late): the first time after
# 5 s, and while there are none, again after 10 s and after 15 s more. Then it gives up.
GAME_WAITS = (5.0, 10.0, 15.0)
GAME_POLL = 2.0
MAX_UNLOCK_AGE = 300  # seconds: an older unlock is Comet syncing what you had, not something new


def just_unlocked(unlock_time):
    """Whether Comet's unlock_time ("2026-07-31T01:48:51+0000") is recent. Comety sends every
    update of an achievement, including a new database being filled with what you already had."""
    try:
        when = datetime.strptime(unlock_time, "%Y-%m-%dT%H:%M:%S%z")
    except (TypeError, ValueError):
        return False
    return (datetime.now(timezone.utc) - when).total_seconds() <= MAX_UNLOCK_AGE


def logo(appid):
    """The game's logo as a data: URI, or "" if it has none (that fits)."""
    for path in games.logo_files(appid):
        try:
            return service.icon_from_file(path)
        except service.Rejected:
            continue  # too big, or not an image: the next one
    return ""


class Comet(Endpoint):
    name = "comet"
    path = "/notify/comet"
    label = "Comet"
    icon = "comety.png"

    def __init__(self):
        self.host = None
        self.comety = {}  # database -> subprocess, for the game being played
        self.game = None  # (appid, name) once connected to it
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def adapt(self, data):
        """Lenient: Comet may add fields at any time. Only a recent unlock is shown; any other
        update (not unlocked, unlocked long ago) is ignored (None)."""
        if not isinstance(data.get("name"), str):
            raise service.Rejected(400, "name is required")
        if not just_unlocked(data.get("unlock_time")):
            return None
        body = data.get("description")
        body = body if isinstance(body, str) else ""
        rarity = data.get("rarity")
        if isinstance(rarity, (int, float)) and not isinstance(rarity, bool):
            body = f"{body} · {rarity:.1f}%" if body else f"{rarity:.1f}%"
        url = data.get("image_url_unlocked")
        return data["name"], body, url if isinstance(url, str) and url.startswith("https://") else ""

    def start(self, host):
        if self._thread:
            return
        self.host = host
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch, name="noty-comet", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(5)
            self._thread = None
        with self._lock:
            self._stop_comety()
            self.game = None

    # Its messages are about the game, not about turning the endpoint on.
    def connected(self, host):
        pass

    def disconnected(self, host):
        self._leave()

    def details(self):
        with self._lock:
            lines = [f"{'●' if p and p.poll() is None else '○ not running —'} Comety: {db}"
                     for db, p in self.comety.items()]
        return lines or ["Comety: waiting for a game with Comet"]

    # -- Comety --

    def _stop_comety(self):
        """Stop every comety (their databases stay in self.comety)."""
        for p in self.comety.values():
            if p:
                p.terminate()
        for p in self.comety.values():
            if p:
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
        self.comety.update({db: None for db in self.comety})

    def _run(self, dbs):
        """Run a comety per database, against the same URL as this endpoint. Never raises."""
        for db in dbs:
            try:
                os.chmod(COMETY_BIN, 0o755)  # the installer doesn't keep the executable bit
                self.comety[db] = subprocess.Popen(
                    [str(COMETY_BIN), "--url", f"http://127.0.0.1:{self.host.port}{self.path}", "--quiet", db],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError as e:
                self.host.log.warning("can't run comety: %s", e.strerror or e)
                return
            self.host.log.info("comety watching %s", db)

    def _connect(self, appid, name, dbs):
        """Databases of the game found: watch them and, the first time, say so."""
        with self._lock:
            if self._stop.is_set() or self.host.port is None:
                return
            first = self.game is None
            for db in dbs:
                self.comety[db] = None
            self._run(dbs)
            if first:
                self.game = (appid, name)
                self.host.announce(self, f"Comet - {name}", f"Noty is connecting to {name}", logo(appid))

    def _leave(self):
        """The game is over (or the endpoint is turned off): stop its comety and say so."""
        with self._lock:
            game, self.game = self.game, None
            self._stop_comety()
            self.comety.clear()
            if game:
                self.host.announce(self, f"Comet - {game[1]}", f"Noty disconnected from {game[1]}", logo(game[0]))

    def _watch(self):
        """Follow the running game and look for its databases (see GAME_WAITS). Never raises."""
        current, due, attempt = None, 0.0, 0
        while not self._stop.wait(GAME_POLL):
            try:
                game = self.host.ctx.game.running
                appid = game["appid"] if game else None
                if appid != current:
                    current, due, attempt = appid, time.monotonic() + GAME_WAITS[0], 0
                    self._leave()
                if not appid or time.monotonic() < due or attempt >= len(GAME_WAITS):
                    continue
                dbs = games.gameplay_dbs(appid)
                if dbs:
                    self._connect(appid, game.get("name") or appid, dbs)
                    attempt = len(GAME_WAITS)  # found: done with this game
                else:
                    attempt += 1
                    self.host.log.info("no Comet database yet for %s (%s tries left)", appid, len(GAME_WAITS) - attempt)
                    if attempt < len(GAME_WAITS):
                        due = time.monotonic() + GAME_WAITS[attempt]
            except Exception:
                self.host.log.exception("watching games failed")
