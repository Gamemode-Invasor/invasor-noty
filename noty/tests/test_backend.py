import importlib.util
import json
import logging
import socket
import sys
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
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
        self.shown, self.bodies = [], []
        self.game = types.SimpleNamespace(running=None)

    def notify(self, title, body="", icon=""):
        self.shown.append(title)
        self.bodies.append(body)


def load_backend():
    name = "noty_backend_under_test"
    for n in [n for n in sys.modules if n == name or n.startswith(name + ".")]:
        del sys.modules[n]
    spec = importlib.util.spec_from_file_location(name, HERE / "backend.py", submodule_search_locations=[str(HERE)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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

    def started(self, running):
        """A backend with a game running, a (mocked) comety, and the databases that were found."""
        from unittest import mock
        b = load_backend()
        b.GAME_DELAY = b.GAME_RETRY = b.GAME_POLL = 0.01
        ctx = FakeCtx(free_port())
        ctx.game.running = running
        b.games = mock.Mock(gameplay_dbs=mock.Mock(return_value=["/a/gameplay.db", "/b/gameplay.db"]),
                            logo_files=mock.Mock(return_value=[]))
        patch = mock.patch.object(b.subprocess, "Popen")
        popen = patch.start()
        self.addCleanup(patch.stop)
        popen.return_value.poll.return_value = None
        chmod = mock.patch.object(b.os, "chmod").start()
        self.addCleanup(mock.patch.stopall)
        return b, ctx, popen, chmod

    def wait_for(self, cond):
        import time
        for _ in range(300):
            if cond():
                return
            time.sleep(0.01)
        self.fail("timed out")

    def test_comety_runs_for_each_database_of_the_game_being_played(self):
        b, ctx, popen, chmod = self.started({"appid": "123", "name": "Lil Gator Game"})
        b.setup(ctx)
        self.addCleanup(b.teardown)
        self.wait_for(lambda: popen.call_count == 2)
        port = b.status()["port"]
        url = f"http://127.0.0.1:{port}/notify/comet"
        self.assertEqual([c.args[0][1:] for c in popen.call_args_list],
                         [["--url", url, "--quiet", "/a/gameplay.db"], ["--url", url, "--quiet", "/b/gameplay.db"]])
        chmod.assert_called_with(b.COMETY_BIN, 0o755)
        b.games.gameplay_dbs.assert_called_with("123")
        self.assertEqual(b.status()["comety"],
                         [{"db": "/a/gameplay.db", "running": True}, {"db": "/b/gameplay.db", "running": True}])
        self.assertEqual(ctx.shown, ["Comet - Lil Gator Game"])
        self.assertEqual(ctx.bodies, ["Noty is connecting to Lil Gator Game"])
        ctx.game.running = None  # the game ends: comety stops
        self.wait_for(lambda: b.status()["comety"] == [])
        self.assertEqual(popen.return_value.terminate.call_count, 2)

    def test_databases_that_appear_later_are_found(self):
        b, ctx, popen, _ = self.started({"appid": "123", "name": "TOEM"})
        b.games.gameplay_dbs.return_value = []
        b.setup(ctx)
        self.addCleanup(b.teardown)
        self.wait_for(lambda: b.games.gameplay_dbs.call_count >= 2)
        popen.assert_not_called()
        b.games.gameplay_dbs.return_value = ["/a/gameplay.db"]
        self.wait_for(lambda: popen.call_count == 1)
        self.assertEqual(ctx.shown, ["Comet - TOEM"])

    def test_it_gives_up_after_the_retries(self):
        b, ctx, popen, _ = self.started({"appid": "123", "name": "TOEM"})
        b.games.gameplay_dbs.return_value = []
        b.setup(ctx)
        self.addCleanup(b.teardown)
        self.wait_for(lambda: b.games.gameplay_dbs.call_count == 1 + b.RETRIES)
        import time
        time.sleep(0.2)
        self.assertEqual(b.games.gameplay_dbs.call_count, 1 + b.RETRIES)
        popen.assert_not_called()

    def test_endpoint_off_no_comety_and_back_on_it_starts(self):
        b, ctx, popen, _ = self.started({"appid": "123", "name": "TOEM"})
        ctx.settings.change("endpoint_comet", False)
        b.setup(ctx)
        self.addCleanup(b.teardown)
        import time
        time.sleep(0.2)
        popen.assert_not_called()
        b.games.gameplay_dbs.assert_not_called()
        self.assertEqual((b.status()["comety"], ctx.shown), ([], []))
        self.assertTrue(b.test())  # the rest of Noty works
        ctx.settings.change("endpoint_comet", True)  # the game is still running
        self.wait_for(lambda: popen.call_count == 2)
        ctx.settings.change("endpoint_comet", False)  # and off again: they stop
        self.wait_for(lambda: b.status()["comety"] == [])
        self.assertEqual(popen.return_value.terminate.call_count, 2)

    def test_no_game_no_comety(self):
        b, ctx, popen, _ = self.started(None)
        b.setup(ctx)
        self.addCleanup(b.teardown)
        import time
        time.sleep(0.1)
        popen.assert_not_called()
        self.assertEqual((b.status()["comety"], ctx.shown), ([], []))

    def test_a_game_without_databases_shows_nothing(self):
        b, ctx, popen, _ = self.started({"appid": "5", "name": "x"})
        b.games.gameplay_dbs.return_value = []
        b.setup(ctx)
        self.addCleanup(b.teardown)
        self.wait_for(lambda: b.games.gameplay_dbs.call_count >= 2)
        popen.assert_not_called()
        self.assertEqual(ctx.shown, [])

    def test_comety_failing_doesnt_stop_the_service(self):
        b, ctx, popen, _ = self.started({"appid": "5", "name": "x"})
        popen.side_effect = FileNotFoundError(2, "No such file")
        b.setup(ctx)
        self.addCleanup(b.teardown)
        self.wait_for(lambda: popen.called)
        st = b.status()
        self.assertTrue(st["running"])
        self.assertFalse(any(c["running"] for c in st["comety"]))

    def test_every_endpoint_has_its_setting(self):
        b = load_backend()
        settings = defaults()
        for name in b.service.ENDPOINTS:
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
