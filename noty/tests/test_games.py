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
