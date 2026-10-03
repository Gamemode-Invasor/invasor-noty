import http.client
import json
import logging
import socket
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

import service


class Unavailable(RuntimeError):
    pass


def png_bytes():
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00")) + chunk(b"IEND", b""))


class Server(unittest.TestCase):
    """A running service, with its endpoints and "do not disturb" in self.on / self.dnd."""

    def setUp(self):
        self.sent, self.fail = [], None
        self.on, self.dnd = {name: True for name in service.ENDPOINTS}, False

        def send(title, body, icon):
            if self.fail:
                raise self.fail
            if not title.strip():
                raise ValueError("a notification needs a title")
            self.sent.append((title, body, icon))

        self.s = service.NotyServer(0, send, logging.getLogger("test.noty"), unavailable=(Unavailable,),
                                    endpoint_on=lambda name: self.on[name], quiet=lambda: self.dnd)
        self.s.start()
        self.addCleanup(self.s.stop)

    def request(self, method="POST", path="/notify", body=None, ctype="application/json", host=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.s.port, timeout=5)
        headers = {"Content-Type": ctype} if ctype else {}
        if host:
            headers["Host"] = host
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        c.request(method, path, body=data, headers=headers)
        r = c.getresponse()
        out = (r.status, json.loads(r.read() or b"{}"))
        c.close()
        return out


class Service(Server):
    def test_shows_a_notification(self):
        self.assertEqual(self.request(body={"title": "Hi", "body": "There"}), (200, {"ok": True, "shown": True}))
        self.assertEqual(self.sent, [("Hi", "There", "")])
        self.assertEqual({k: self.s.recent[0][k] for k in ("source", "ok", "muted")},
                         {"source": "general", "ok": True, "muted": None})
        self.assertEqual(self.request("GET", "/"),
                         (200, {"ok": True, "service": "noty", "endpoints": ["/notify", "/notify/comet"]}))

    def test_bad_requests(self):
        cases = [
            (dict(raw=b"{nope"), 400),
            (dict(body=["x"]), 400),
            (dict(body={"title": "x", "extra": 1}), 400),
            (dict(body={"title": 5}), 400),
            (dict(body={"title": " "}), 400),  # refused by send (ValueError)
            (dict(body={"title": "x"}, ctype="text/plain"), 415),
            (dict(body={"title": "x"}, path="/other"), 404),
            (dict(body={"title": "x"}, path="/notify/other"), 404),
            (dict(body={"title": "x"}, host="evil.example"), 403),
        ]
        for kw, status in cases:
            with self.subTest(kw=str(kw)[:60]):
                self.assertEqual(self.request(**kw)[0], status)
        self.assertEqual(self.sent, [])

    def test_too_large_is_refused_unread(self):
        with socket.create_connection(("127.0.0.1", self.s.port), timeout=5) as sock:
            sock.sendall(b"POST /notify HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                         + f"Content-Length: {service.MAX_BODY + 1}\r\n\r\n".encode())
            self.assertIn(b" 413 ", sock.recv(200))

    def test_steam_unavailable_is_503(self):
        self.fail = Unavailable("Steam's notifications aren't available")
        status, body = self.request(body={"title": "x"})
        self.assertEqual((status, body["error"]), (503, "Steam's notifications aren't available"))
        self.assertFalse(self.s.recent[0]["ok"])

    def test_rate_limit(self):
        statuses = [self.request(body={"title": f"n{i}"})[0] for i in range(service.RATE + 1)]
        self.assertEqual(statuses, [200] * service.RATE + [429])

    def test_icon_from_a_local_file(self):
        with tempfile.TemporaryDirectory() as d:
            ok = Path(d) / "i.png"
            ok.write_bytes(png_bytes())
            fake = Path(d) / "fake.png"
            fake.write_text("not a png")
            other = Path(d) / "secret.txt"
            other.write_text("x")
            self.assertEqual(self.request(body={"title": "x", "icon": str(ok)})[0], 200)
            self.assertTrue(self.sent[0][2].startswith("data:image/png;base64,"))
            for bad in (str(fake), str(other), str(Path(d) / "missing.png"), "relative.png"):
                with self.subTest(icon=bad):
                    self.assertEqual(self.request(body={"title": "x", "icon": bad})[0], 400)

    def test_port_in_use_and_clean_stop(self):
        busy = service.NotyServer(self.s.port, lambda *a: None, logging.getLogger("test.noty"))
        with self.assertRaises(OSError):
            busy.start()
        port = self.s.port
        self.s.stop()
        self.assertFalse(self.s.running)
        with socket.socket() as sock:  # the port is free again
            sock.bind(("127.0.0.1", port))


    def test_endpoint_off_answers_but_shows_nothing(self):
        self.on["comet"] = False
        self.assertEqual(self.request(path="/notify/comet", body=comet_payload()),
                         (200, {"ok": True, "shown": False, "reason": "endpoint_off"}))
        self.assertEqual(self.sent, [])
        self.assertEqual({k: self.s.recent[0][k] for k in ("source", "title", "muted")},
                         {"source": "comet", "title": "Un salto brillante", "muted": "endpoint_off"})
        self.assertEqual(self.request(body={"title": "Still here"})[1]["shown"], True)  # the others go on
        self.assertEqual(self.sent, [("Still here", "", "")])

    def test_a_bad_request_is_still_400_when_silenced(self):
        self.on["general"], self.dnd = False, True
        self.assertEqual(self.request(body={"title": "x", "extra": 1})[0], 400)
        self.assertEqual(self.request(path="/notify/comet", body={"event": "achievement_unlocked"})[0], 400)

    def test_do_not_disturb(self):
        self.dnd = True
        for i in range(service.RATE + 1):  # silenced ones don't count for the rate limit
            self.assertEqual(self.request(body={"title": f"n{i}"}),
                             (200, {"ok": True, "shown": False, "reason": "do_not_disturb"}))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.s.recent[0]["muted"], "do_not_disturb")
        self.dnd = False  # read live: no restart
        self.assertEqual(self.request(body={"title": "back"})[1]["shown"], True)
        self.s.notify("test", "", "")  # the test button ignores the silences
        self.dnd = True
        self.s.notify("test", "", "")
        self.assertEqual([t for t, _, _ in self.sent], ["back", "test", "test"])


def comet_payload(**achievement):
    a = {"key": "CLOSE_UP_DANCER", "name": "Un salto brillante", "description": "Haz un primer plano de una bailarina.",
         "rarity": 22.600000381469727, "image_url": "https://images.gog.com/e8a0_gac_60.jpg"}
    a.update(achievement)
    return {"event": "achievement_unlocked", "version": 1, "timestamp": "2026-10-02T10:00:00Z",
            "game": {"id": "1234567890", "name": "Lil Gator Game"}, "achievement": a}


class Comet(Server):
    def post(self, body):
        return self.request(path="/notify/comet", body=body)

    def test_an_unlocked_achievement_is_shown(self):
        self.assertEqual(self.post(comet_payload()), (200, {"ok": True, "shown": True}))
        self.assertEqual(self.sent, [("Un salto brillante", "Haz un primer plano de una bailarina. · 22.6%",
                                      "https://images.gog.com/e8a0_gac_60.jpg")])
        self.assertEqual(self.s.recent[0]["source"], "comet")

    def test_extra_fields_are_fine(self):
        payload = comet_payload(new_field=[1, 2], rarity=None, description=None)
        payload["something_new"] = {"x": 1}
        self.assertEqual(self.post(payload)[0], 200)
        self.assertEqual(self.sent[0][:2], ("Un salto brillante", ""))

    def test_the_cached_image_wins_over_the_url(self):
        with tempfile.TemporaryDirectory() as d:
            img = Path(d) / "a.png"
            img.write_bytes(png_bytes())
            self.assertEqual(self.post(comet_payload(image_path=str(img)))[0], 200)
            self.assertTrue(self.sent[0][2].startswith("data:image/png;base64,"))
            # missing or relative: back to the URL; a plain http:// URL isn't used
            self.post(comet_payload(image_path=str(Path(d) / "gone.png")))
            self.post(comet_payload(image_path="a.png", image_url="http://insecure.example/a.png"))
        self.assertEqual([s[2] for s in self.sent[1:]], ["https://images.gog.com/e8a0_gac_60.jpg", ""])

    def test_other_events_are_ignored(self):
        self.assertEqual(self.post({"event": "leaderboard_updated", "leaderboard": {}}),
                         (200, {"ok": True, "shown": False, "reason": "ignored"}))
        self.assertEqual(self.post({}), (200, {"ok": True, "shown": False, "reason": "ignored"}))
        self.assertEqual((self.sent, list(self.s.recent)), ([], []))

    def test_an_achievement_needs_a_name(self):
        for bad in ({"event": "achievement_unlocked"}, {"event": "achievement_unlocked", "achievement": "x"},
                    comet_payload(name=5)):
            with self.subTest(bad=bad):
                self.assertEqual(self.post(bad)[0], 400)
        self.assertEqual(self.post(comet_payload(name=" "))[0], 400)  # refused by send
        self.assertEqual(self.sent, [])


class Limits(unittest.TestCase):
    def test_window_slides(self):
        now = [0.0]
        limit = service.RateLimit(2, 10, clock=lambda: now[0])
        self.assertEqual([limit.allow(), limit.allow(), limit.allow()], [True, True, False])
        now[0] = 10.0
        self.assertTrue(limit.allow())


if __name__ == "__main__":
    unittest.main()
