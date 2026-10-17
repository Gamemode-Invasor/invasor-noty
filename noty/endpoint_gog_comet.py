"""/cometa: the achievements Comet itself sends (its custom_event setting).

Unlike /cometa/db, nothing is read from Comet's database: Comet POSTs each unlock here as
{"name": "achievement_unlocked", "client_id": "…", "data": {…the achievement…}}. Both can be on at once; an
achievement then shows twice.

While this endpoint is on, Noty follows the games of Invasor (game_selected / game_started / game_stopped). A game that is
a shortcut whose .bat says set "STORE=gog" will run Comet, so as soon as it is selected Noty puts the [[custom_event]]
pointing here in Comet's config.toml of the game's prefix (Comet reads it only when it starts). When the game starts it
checks every few seconds that the config has that URL, that comet.exe is running and that its port is open, and says so.
"""
import threading

from . import comet_config, games, service
from .endpoint_comet import just_unlocked, logo, trophy
from .endpoints import Endpoint

EVENT = "achievement_unlocked"
WATCH_WAIT = 5.0   # seconds between checks
WATCH_TRIES = 12   # and then it gives up


class GogComet(Endpoint):
    name = "gog_comet"
    path = "/cometa"
    label = "Comet"
    icon = "comety.png"

    def __init__(self):
        self.host = None
        self._current = None  # appid of the game being watched
        self._cancel = threading.Event()  # ends the watch of _current
        self._registered = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._state = ""

    def adapt(self, data):
        """Lenient, like /cometa/db. Any other event, and an unlock that is not recent, is ignored (None)."""
        if data.get("name") != EVENT:
            return None
        achievement = data.get("data")
        if not isinstance(achievement, dict):
            raise service.Rejected(400, "data is required")
        if not isinstance(achievement.get("name"), str):
            raise service.Rejected(400, "data.name is required")
        if not just_unlocked(achievement.get("date_unlocked")):
            return None
        return trophy(achievement["name"], achievement.get("description"), achievement.get("rarity"),
                      achievement.get("image_url_unlocked"))

    def start(self, host):
        if self._registered:
            return
        self.host = host
        self._stop.clear()
        events = host.ctx.events
        events.on("game_selected", self._game_selected)
        events.on("game_started", self._game_started)
        events.on("game_stopped", self._game_stopped)
        self._registered = True
        # Turned on in the middle of a session: the events already passed.
        selected, running = host.ctx.game.selected, host.ctx.game.running
        if selected:
            self._prepare(selected)
        if running:
            self._begin(running)

    def stop(self):
        if self._registered:
            self._registered = False
            for name, cb in (("game_selected", self._game_selected), ("game_started", self._game_started),
                             ("game_stopped", self._game_stopped)):
                try:
                    self.host.ctx.events.off(name, cb)
                except Exception:
                    self.host.log.exception("can't stop listening to %s", name)
        self._stop.set()
        self._cancel.set()
        with self._lock:
            self._current = None
            self._state = ""

    # Its messages are about the game, not about turning the endpoint on. Turning it off leaves Comet's config as it is.
    def connected(self, host):
        pass

    def disconnected(self, host):
        pass

    def details(self):
        with self._lock:
            return [f"Comet: {self._state or 'waiting for a game'}"]

    # -- the config of Comet --

    def _url(self):
        return f"http://127.0.0.1:{self.host.port}{self.path}"

    def _prepare(self, game):
        """If the game will run Comet, put Noty's custom event in its config. Returns the config's path, or None if the
        game isn't one of GOG (or it can't be done). Never raises."""
        try:
            appid = game.get("appid")
            if appid is None or not game.get("shortcut") or self.host.port is None:
                return None
            if not games.is_gog_launcher(self.host.ctx.game.shortcut_exe(appid)):
                return None
            path = str(games.comet_config(appid))
            if comet_config.ensure_block(path, self._url()):
                self.host.log.info("Comet config of %s set up: %s", game.get("name") or appid, path)
            return path
        except (OSError, ValueError) as e:
            self.host.log.warning("can't set up Comet's config for %s: %s", game.get("name") or game.get("appid"), e)
        except Exception:
            self.host.log.exception("setting up Comet's config failed")
        return None

    # -- the games --

    def _game_selected(self, game):
        self._prepare(game)

    def _game_started(self, game):
        self._begin(game)

    def _game_stopped(self, game):
        with self._lock:
            if game.get("appid") != self._current:
                return  # not the one watched (events of two games may arrive in any order)
            self._current = None
            self._state = ""
            self._cancel.set()

    def _begin(self, game):
        """The game started: make sure its config is set (selecting it should have done it) and watch Comet come up. A game
        already watched is ignored, another one replaces it."""
        appid = game.get("appid")
        with self._lock:
            if appid is None or appid == self._current or self._stop.is_set():
                return
            self._cancel.set()
            self._cancel = cancel = threading.Event()
            self._current = appid
        path = self._prepare(game)
        if path is None:
            with self._lock:
                if self._current == appid:
                    self._current, self._state = None, ""
            return
        with self._lock:
            self._state = "waiting for Comet"
        threading.Thread(target=self._watch, args=(game, path, cancel), name="noty-gog-comet", daemon=True).start()

    def _watch(self, game, path, cancel):
        """Check, every WATCH_WAIT seconds and up to WATCH_TRIES times, (a) that the config has Noty's URL, (b) that a comet.exe
        runs in the prefix of this game and (c) that this very process listens on Comet's port. Never raises."""
        appid, name = game["appid"], game.get("name") or str(game["appid"])
        warned = False
        try:
            for _ in range(WATCH_TRIES):
                url = self._url()
                config = comet_config.has_url(path, url)
                pid = games.comet_pid(appid)
                port = bool(pid) and games.port_owned_by(pid)
                others = [] if pid else games.comet_others(appid)
                if not config:
                    # Gone, or the port changed: put it back. A Comet that is already running won't read it until the game restarts.
                    self._prepare(game)
                    if pid and not warned:
                        warned = True
                        self._say(appid, name, f"Comet - {name}", "Restart the game: Comet started before Noty could set it up.", "mention")
                with self._lock:
                    if cancel.is_set():
                        return
                    self._state = self._describe(config, pid, port, others)
                if config and pid and port:
                    self._say(appid, name, f"Comet - {name}", "Comet connected", "online")
                    return
                if cancel.wait(WATCH_WAIT) or self._stop.is_set():
                    return
            if others:
                reason = f"the Comet of another game ({', '.join(others)}) is running and holds port {games.COMET_PORT}"
            else:
                reason = ", ".join(label for label, ok in (("config", config), ("comet.exe of this game", bool(pid)),
                                                           (f"port {games.COMET_PORT}", port)) if not ok)
            self._say(appid, name, f"Comet - {name}", f"Comet isn't ready: {reason}", "mention")
        except Exception:
            self.host.log.exception("watching Comet failed")

    @staticmethod
    def _describe(config, pid, port, others=()):
        mark = lambda ok: "●" if ok else "○"
        text = (f"{mark(config)} config  {mark(pid)} comet.exe{f' ({pid})' if pid else ''}  "
                f"{mark(port)} port {games.COMET_PORT}")
        return f"{text}  ⚠ Comet of another game: {', '.join(others)}" if others else text

    def _say(self, appid, name, title, body, sound):
        if self._stop.is_set() or self._current != appid:
            return
        self.host.announce(self, title, body, logo(appid), sound=sound)
