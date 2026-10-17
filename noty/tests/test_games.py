import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("noty_games_under_test", HERE / "games.py")
games = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = games
spec.loader.exec_module(games)


def touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return str(path)


class Games(unittest.TestCase):
    def test_finds_every_gameplay_db_of_the_game(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            gp = "AppData/Local/comet/gameplay"
            one = touch(root / "steamapps/compatdata/42/pfx/drive_c/users/steamuser" / gp / "111/gameplay.db")
            two = touch(root / "steamapps/compatdata/42/pfx/drive_c/users/steamuser" / gp / "222/gameplay.db")
            touch(root / "steamapps/compatdata/42/pfx/drive_c/users/steamuser" / gp / "111/other.db")
            touch(root / "steamapps/compatdata/42/pfx/drive_c/users/other" / gp / "333/gameplay.db")
            touch(root / "steamapps/compatdata/43/pfx/drive_c/users/steamuser" / gp / "1/gameplay.db")
            self.assertEqual(games.gameplay_dbs(42, root), sorted([one, two]))
            self.assertEqual(games.gameplay_dbs(44, root), [])

    def test_logo_the_users_first_then_steams(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.assertEqual(games.logo_files(7, root), [])
            steam = touch(root / "appcache/librarycache/7/logo.png")
            self.assertEqual(games.logo_files(7, root), [steam])
            mine = touch(root / "userdata/1/config/grid/7_logo.png")
            self.assertEqual(games.logo_files(7, root), [mine, steam])


if __name__ == "__main__":
    unittest.main()


class Comet(unittest.TestCase):
    def test_config_path_is_in_the_roaming_folder_of_the_prefix(self):
        path = games.comet_config(42, "/steam")
        self.assertEqual(str(path), "/steam/steamapps/compatdata/42/pfx/drive_c/users/steamuser/AppData/Roaming/comet/config.toml")

    def test_gog_launcher(self):
        with tempfile.TemporaryDirectory() as d:
            def bat(name, text):
                p = Path(d) / name
                p.write_bytes(text.encode())
                return str(p)
            gog = bat("a.bat", '@echo off\r\nrem x\r\nset "STORE=gog"\r\nset "IDGAME=1"\r\n')
            self.assertTrue(games.is_gog_launcher(gog))
            self.assertTrue(games.is_gog_launcher(bat("b.bat", "  SET STORE=GOG\n")))
            self.assertFalse(games.is_gog_launcher(bat("c.bat", 'set "STORE=epic"\n')))
            self.assertFalse(games.is_gog_launcher(bat("d.bat", 'rem set "STORE=gog" is gog\n')))
            self.assertFalse(games.is_gog_launcher(bat("e.sh", 'set "STORE=gog"\n')))  # not a .bat
            self.assertFalse(games.is_gog_launcher(str(Path(d) / "missing.bat")))
            self.assertFalse(games.is_gog_launcher(None))

    def test_comet_running_looks_for_comet_exe_in_the_processes(self):
        with tempfile.TemporaryDirectory() as d:
            def proc(pid, *args):
                p = Path(d) / str(pid)
                p.mkdir()
                (p / "cmdline").write_bytes(b"\0".join(a.encode() for a in args) + b"\0")
            proc(10, "/usr/bin/steam")
            proc(20, "C:\\Launchers\\bin\\Comet.exe", "--username", "x")
            proc(30, "/usr/bin/wine", "Z:/home/deck/comet.exe.bak")
            (Path(d) / "self").mkdir()
            self.assertEqual(games.comet_running(d), 20)
            self.assertIsNone(games.comet_running(str(Path(d) / "none")))
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(games.comet_running(d))

    def test_comet_listening(self):
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()
            port = s.getsockname()[1]
            self.assertTrue(games.comet_listening(port))
        self.assertFalse(games.comet_listening(port))  # closed again
