"""/cometa/db: Comet's (GOG) unlocked achievements.

Comet keeps each game's achievements in a SQLite database (gameplay.db). While this endpoint is on,
Noty follows the game being played (Invasor's game_started / game_stopped events) and, for each database Comet keeps for it, runs Comety
(bin/comety), which POSTs every new unlock to this endpoint. Its messages are about the game:
"connecting to" it when the databases are found, "disconnected" when it ends.
"""
import os
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import games, service
from .endpoints import Endpoint

COMETY_BIN = Path(__file__).resolve().parent / "bin" / "comety"
# After a game starts, look for its databases (Comet creates them late): the first time after
# 5 s, and while there are none, again after 10 s and after 15 s more. Then it gives up.
GAME_WAITS = (5.0, 10.0, 15.0)
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


def trophy(name, description, rarity, image_url):
    """An unlocked achievement as (title, body, icon, sound): the description and the rarity as the body, the
    https:// image as the icon, always the trophy sound."""
    body = description if isinstance(description, str) else ""
    if isinstance(rarity, (int, float)) and not isinstance(rarity, bool):
        body = f"{body} · {rarity:.1f}%" if body else f"{rarity:.1f}%"
    return name, body, image_url if isinstance(image_url, str) and image_url.startswith("https://") else "", "trophy"


class Comet(Endpoint):
    name = "comet"
    path = "/cometa/db"
    label = "Comet (fallback by db)"
    icon = "comety.png"

    def __init__(self):
        self.host = None
        self.comety = {}  # database -> subprocess, for the game being played
        self.game = None  # (appid, name) once connected to it
        self._current = None  # appid of the game being followed (connected or still looking)
        self._cancel = threading.Event()  # ends the search for _current's databases
        self._registered = False
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def adapt(self, data):
        """Lenient: Comet may add fields at any time. Always the trophy sound. Only a recent unlock is shown; any other
        update (not unlocked, unlocked long ago) is ignored (None)."""
        if not isinstance(data.get("name"), str):
            raise service.Rejected(400, "name is required")
        if not just_unlocked(data.get("unlock_time")):
            return None
        return trophy(data["name"], data.get("description"), data.get("rarity"), data.get("image_url_unlocked"))

    def start(self, host):
        if self._registered:
            return
        self.host = host
        self._stop.clear()
        events = host.ctx.events
        events.on("game_started", self._game_started)
        events.on("game_stopped", self._game_stopped)
        self._registered = True
        running = host.ctx.game.running  # turned on in the middle of a game: the event already passed
        if running:
            self._begin(running)

    def stop(self):
        if self._registered:
            self._registered = False
            for name, cb in (("game_started", self._game_started), ("game_stopped", self._game_stopped)):
                try:
                    self.host.ctx.events.off(name, cb)
                except Exception:
                    self.host.log.exception("can't stop listening to %s", name)
        self._stop.set()
        self._cancel.set()
        with self._lock:
            self._current = None
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

    def _connect(self, appid, name, dbs, cancel):
        """Databases of the game found: watch them and, the first time, say so."""
        with self._lock:
            if self._stop.is_set() or cancel.is_set() or self.host.port is None:
                return
            first = self.game is None
            for db in dbs:
                self.comety[db] = None
            self._run(dbs)
            if first:
                self.game = (appid, name)
                self.host.announce(self, f"Comet - {name}", f"Noty is connecting to {name}", logo(appid), sound="online")

    def _leave(self):
        """The game is over (or the endpoint is turned off): stop its comety and say so."""
        with self._lock:
            game, self.game = self.game, None
            self._stop_comety()
            self.comety.clear()
            if game:
                self.host.announce(self, f"Comet - {game[1]}", f"Noty disconnected from {game[1]}", logo(game[0]), sound="mention")

    # -- the game being played --

    def _game_started(self, game):
        self._begin(game)

    def _game_stopped(self, game):
        """The game that was being followed ended: stop looking for its databases and its comety."""
        with self._lock:
            if game.get("appid") != self._current:
                return  # not the one followed (events of two games may arrive in any order)
            self._current = None
            self._cancel.set()
        self._leave()

    def _begin(self, game):
        """Follow a game: look for its databases in a thread of its own (see GAME_WAITS). A game already followed is ignored,
        another one replaces it."""
        appid = game.get("appid")
        with self._lock:
            if appid is None or appid == self._current or self._stop.is_set():
                return
            replaced = self._current is not None
            self._current = appid
            self._cancel.set()
            self._cancel = cancel = threading.Event()
        if replaced:
            self._leave()
        threading.Thread(target=self._search, args=(appid, game.get("name") or appid, cancel),
                         name="noty-comet", daemon=True).start()

    def _search(self, appid, name, cancel):
        """Look for the databases of the game, waiting between tries, until found, cancelled or out of tries. Never raises."""
        try:
            for attempt, wait in enumerate(GAME_WAITS, 1):
                if cancel.wait(wait) or self._stop.is_set():
                    return
                dbs = games.gameplay_dbs(appid)
                if dbs:
                    self._connect(appid, name, dbs, cancel)
                    return
                self.host.log.info("no Comet database yet for %s (%s tries left)", appid, len(GAME_WAITS) - attempt)
        except Exception:
            self.host.log.exception("looking for Comet databases failed")
