import json
import logging
import socket
import time
import types
import unittest
from unittest import mock

from tests.loader import HERE, free_port, load_backend, submodule
MODULE = json.loads((HERE / "module.json").read_text())


def defaults():
    """Every setting's default, as the core would give them (sections flattened)."""
    fields = []
    for item in MODULE["settings"]:
        fields += item.get("items", [item])
    return {f["key"]: f["default"] for f in fields}


class InvalidArgument(ValueError):
    pass


class Unavailable(RuntimeError):
    pass


class FakeSettings:
    def __init__(self, values):
        self.values, self.cbs = values, []

    def get(self, key=None):
        return self.values if key is None else self.values[key]

    def on_change(self, cb):
        self.cbs.append(cb)

    def change(self, key, value):
        self.values[key] = value
        for cb in self.cbs:
            cb(key, value)


class FakeCtx:
    InvalidArgument = InvalidArgument
    Unavailable = Unavailable

    def __init__(self, port):
        self.settings = FakeSettings({**defaults(), "enabled": True, "port": port})
        self.log = logging.getLogger("test.noty")
        self.shown, self.bodies, self.icons = [], [], []
        self.game = types.SimpleNamespace(running=None)

    def notify(self, title, body="", icon=""):
        self.shown.append(title)
        self.bodies.append(body)
        self.icons.append(icon)


class Backend(unittest.TestCase):
    def test_lifecycle(self):
        b = load_backend()
        ctx = FakeCtx(free_port())
        b.setup(ctx)
        self.addCleanup(b.teardown)
        self.assertTrue(b.status()["running"])
        self.assertTrue(b.test())
        self.assertEqual(ctx.shown, ["Noty"])
        self.assertEqual(b.recent()[0]["title"], "Noty")
        ctx.settings.change("enabled", False)
        self.assertEqual((b.status()["running"], b.status()["error"]), (False, None))
        self.assertTrue(b.test())  # the test button works with the service off too

    def test_silences_are_read_live(self):
        b = load_backend()
        ctx = FakeCtx(free_port())
        b.setup(ctx)
        self.addCleanup(b.teardown)
        server = b.server
        ctx.settings.change("dnd", True)
        ctx.settings.change("endpoint_comet", False)
        self.assertIs(b.server, server)  # no restart
        st = b.status()
        self.assertTrue(st["dnd"])
        self.assertEqual([(e["path"], e["on"]) for e in st["endpoints"]],
                         [("/notify", True), ("/notify/comet", False)])
        self.assertEqual(st["endpoints"][1]["url"], f"http://127.0.0.1:{st['port']}/notify/comet")
        self.assertEqual(server.receive("general", "x", "", ""), "do_not_disturb")
        self.assertEqual(server.receive("comet", "x", "", ""), "endpoint_off")
        self.assertTrue(b.test())  # the test button ignores the silences
        self.assertEqual(ctx.shown, ["Noty"])
        ctx.settings.change("dnd", False)
        self.assertIsNone(server.receive("general", "x", "", ""))
        self.assertEqual(ctx.shown, ["Noty", "x"])
        ctx.settings.change("port", free_port())
        self.assertIsNot(b.server, server)  # the port does restart it

    def started(self, running, dbs=("/a/gameplay.db", "/b/gameplay.db")):
        """A backend with a game running, a (mocked) comety, and the databases that were found."""
        b = load_backend()
        comet = submodule(b, "endpoint_comet")
        comet.GAME_WAITS, comet.GAME_POLL = (0.01, 0.01, 0.01), 0.01
        comet.games = mock.Mock(gameplay_dbs=mock.Mock(return_value=list(dbs)), logo_files=mock.Mock(return_value=[]))
        popen = mock.patch.object(comet.subprocess, "Popen").start()
        popen.return_value.poll.return_value = None
        chmod = mock.patch.object(comet.os, "chmod").start()
        self.addCleanup(mock.patch.stopall)
        ctx = FakeCtx(free_port())
        ctx.game.running = running
        return b, comet, ctx, popen, chmod

    def wait_for(self, cond):
        for _ in range(300):
            if cond():
                return
            time.sleep(0.01)
        self.fail("timed out")

    def details(self, b):
        return b.status()["endpoints"][1]["details"]

    def test_comety_runs_for_each_database_of_the_game_being_played(self):
        b, comet, ctx, popen, chmod = self.started({"appid": "123", "name": "Lil Gator Game"})
        b.setup(ctx)
        self.addCleanup(b.teardown)
        self.wait_for(lambda: popen.call_count == 2)
        url = f"http://127.0.0.1:{b.status()['port']}/notify/comet"
        self.assertEqual([c.args[0][1:] for c in popen.call_args_list],
                         [["--url", url, "--quiet", "/a/gameplay.db"], ["--url", url, "--quiet", "/b/gameplay.db"]])
        chmod.assert_called_with(comet.COMETY_BIN, 0o755)
        comet.games.gameplay_dbs.assert_called_with("123")
        self.assertEqual(self.details(b), ["● Comety: /a/gameplay.db", "● Comety: /b/gameplay.db"])
        self.assertEqual((ctx.shown, ctx.bodies), (["Comet - Lil Gator Game"], ["Noty is connecting to Lil Gator Game"]))
        ctx.game.running = None  # the game ends: comety stops, and it's said
        self.wait_for(lambda: self.details(b) == ["Comety: waiting for a game with Comet"])
        self.assertEqual(popen.return_value.terminate.call_count, 2)
        self.assertEqual((ctx.shown[1], ctx.bodies[1]), ("Comet - Lil Gator Game", "Noty disconnected from Lil Gator Game"))

    def test_the_game_message_has_its_logo_or_comets_image(self):
        b, comet, ctx, popen, _ = self.started({"appid": "123", "name": "TOEM"})
        b.setup(ctx)
        self.addCleanup(b.teardown)
        self.wait_for(lambda: ctx.icons)
        self.assertTrue(ctx.icons[0].startswith("data:image/png;base64,"))  # no logo: comety.png
        self.assertEqual(ctx.icons[0], b.eps["comet"].icon_data())
        self.assertNotEqual(ctx.icons[0], "")
        logo = HERE / "assets" / "comety.png"  # any real image will do as the game's logo
        comet.games.logo_files.return_value = [str(logo)]
        ctx.game.running = None
        self.wait_for(lambda: len(ctx.icons) == 2)
        self.assertTrue(ctx.icons[1].startswith("data:image/png;base64,"))

    def test_databases_that_appear_later_are_found(self):
        b, comet, ctx, popen, _ = self.started({"appid": "123", "name": "TOEM"})
        comet.games.gameplay_dbs.return_value = []
        b.setup(ctx)
        self.addCleanup(b.teardown)
        self.wait_for(lambda: comet.games.gameplay_dbs.call_count >= 2)
        popen.assert_not_called()
        comet.games.gameplay_dbs.return_value = ["/a/gameplay.db"]
        self.wait_for(lambda: ctx.shown)
        self.assertEqual((popen.call_count, ctx.shown), (1, ["Comet - TOEM"]))

    def test_it_gives_up_after_the_retries(self):
        b, comet, ctx, popen, _ = self.started({"appid": "123", "name": "TOEM"}, dbs=())
        b.setup(ctx)
        self.addCleanup(b.teardown)
        self.wait_for(lambda: comet.games.gameplay_dbs.call_count == len(comet.GAME_WAITS))
        time.sleep(0.2)
        self.assertEqual(comet.games.gameplay_dbs.call_count, len(comet.GAME_WAITS))
        popen.assert_not_called()

    def test_endpoint_off_no_comety_and_back_on_it_starts(self):
        b, comet, ctx, popen, _ = self.started({"appid": "123", "name": "TOEM"})
        ctx.settings.values["endpoint_comet"] = False
        b.setup(ctx)
        self.addCleanup(b.teardown)
        time.sleep(0.2)
        popen.assert_not_called()
        comet.games.gameplay_dbs.assert_not_called()
        self.assertEqual((self.details(b), ctx.shown), ([], []))
        self.assertTrue(b.test())  # the rest of Noty works
        ctx.shown.clear()
        ctx.settings.change("endpoint_comet", True)  # the game is still running: connects to it, saying nothing else
        self.wait_for(lambda: popen.call_count == 2 and ctx.shown)
        self.assertEqual(ctx.shown, ["Comet - TOEM"])
        ctx.settings.change("endpoint_comet", False)  # and off again: they stop, and it says so
        self.wait_for(lambda: self.details(b) == [])
        self.assertEqual(popen.return_value.terminate.call_count, 2)
        self.assertEqual(ctx.bodies[-1], "Noty disconnected from TOEM")

    def test_no_game_no_comety(self):
        b, comet, ctx, popen, _ = self.started(None)
        b.setup(ctx)
        self.addCleanup(b.teardown)
        time.sleep(0.1)
        popen.assert_not_called()
        self.assertEqual((self.details(b), ctx.shown), (["Comety: waiting for a game with Comet"], []))

    def test_a_game_without_databases_shows_nothing(self):
        b, comet, ctx, popen, _ = self.started({"appid": "5", "name": "x"}, dbs=())
        b.setup(ctx)
        self.addCleanup(b.teardown)
        self.wait_for(lambda: comet.games.gameplay_dbs.call_count >= 2)
        popen.assert_not_called()
        self.assertEqual(ctx.shown, [])

    def test_comety_failing_doesnt_stop_the_service(self):
        b, comet, ctx, popen, _ = self.started({"appid": "5", "name": "x"})
        popen.side_effect = FileNotFoundError(2, "No such file")
        b.setup(ctx)
        self.addCleanup(b.teardown)
        self.wait_for(lambda: popen.called)
        self.assertTrue(b.status()["running"])
        self.assertFalse(any(line.startswith("●") for line in self.details(b)))

    def test_turning_an_endpoint_on_or_off_says_so(self):
        b = load_backend()
        ctx = FakeCtx(free_port())
        b.setup(ctx)
        self.addCleanup(b.teardown)
        self.assertEqual(ctx.shown, [])  # starting the service says nothing
        ctx.settings.change("endpoint_general", False)
        ctx.settings.change("endpoint_general", True)
        self.assertEqual(ctx.shown, ["General disconnected", "General connected"])
        ctx.settings.change("endpoint_comet", False)  # Comet speaks about games, not about itself
        ctx.settings.change("endpoint_comet", True)
        self.assertEqual(ctx.shown, ["General disconnected", "General connected"])
        ctx.settings.change("dnd", True)  # and "do not disturb" silences them
        ctx.settings.change("endpoint_general", False)
        self.assertEqual(len(ctx.shown), 2)

    def test_every_endpoint_has_its_setting(self):
        b = load_backend()
        settings = defaults()
        for name in b.endpoints_mod.build():
            with self.subTest(endpoint=name):
                self.assertIs(settings.get(f"endpoint_{name}"), True)

    def test_port_taken_is_reported_not_raised(self):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            b = load_backend()
            b.setup(FakeCtx(busy.getsockname()[1]))
            self.addCleanup(b.teardown)
            st = b.status()
            self.assertFalse(st["running"])
            self.assertIn("can't listen", st["error"])


if __name__ == "__main__":
    unittest.main()
