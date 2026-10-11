import importlib.util
import json
import logging
import socket
import sys
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
        self.shown = []

    def notify(self, title, body="", icon=""):
        self.shown.append(title)


def load_backend():
    name = "noty_backend_under_test"
    for n in [n for n in sys.modules if n == name or n.startswith(name + ".")]:
        del sys.modules[n]
    spec = importlib.util.spec_from_file_location(name, HERE / "backend.py", submodule_search_locations=[str(HERE)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    mod.COMETY_BIN = HERE / "bin" / "not-here"  # tests never run the real one
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

    def test_comety_runs_against_the_comet_endpoint(self):
        from unittest import mock
        b = load_backend()
        b.COMETY_BIN = HERE / "bin" / "comety"
        b.COMETY_DB = "/tmp/some.db"
        with mock.patch.object(b.os, "chmod") as chmod, mock.patch.object(b.subprocess, "Popen") as popen:
            popen.return_value.poll.return_value = None
            b.setup(FakeCtx(free_port()))
            port = b.status()["port"]
            self.assertEqual(popen.call_args.args[0],
                             [str(HERE / "bin" / "comety"), "--url", f"http://127.0.0.1:{port}/notify/comet",
                              "--quiet", "/tmp/some.db"])
            chmod.assert_called_once_with(HERE / "bin" / "comety", 0o755)
            self.assertEqual(b.status()["comety"], {"running": True, "db": "/tmp/some.db"})
            b.teardown()
            popen.return_value.terminate.assert_called_once()
            self.assertFalse(b.status()["comety"]["running"])

    def test_comety_failing_doesnt_stop_the_service(self):
        b = load_backend()  # the binary is missing: chmod fails
        b.setup(FakeCtx(free_port()))
        self.addCleanup(b.teardown)
        st = b.status()
        self.assertTrue(st["running"])
        self.assertFalse(st["comety"]["running"])

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
