import importlib.util
import os
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

    def fake_proc(self, d, processes, listeners={}):
        """processes: {pid: (arguments, prefix id or None)}; listeners: {pid: inode} that listen on Comet's port."""
        proc = Path(d) / "proc"
        proc.mkdir()
        for pid, (args, prefix) in processes.items():
            p = proc / str(pid)
            (p / "fd").mkdir(parents=True)
            (p / "cmdline").write_bytes(b"\0".join(a.encode() for a in args) + b"\0")
            env = [b"HOME=/home/deck"]
            if prefix:
                env.append(f"STEAM_COMPAT_DATA_PATH=/steam/compatdata/{prefix}".encode())
                env.append(f"WINEPREFIX=/steam/compatdata/{prefix}/pfx/".encode())
            (p / "environ").write_bytes(b"\0".join(env) + b"\0")
        (proc / "self").mkdir()
        net = Path(d) / "tcp"
        rows = ["  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode"]
        for n, (pid, inode) in enumerate(listeners.items()):
            rows.append(f"   {n}: 0100007F:26F9 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 {inode} 1")
            os.symlink(f"socket:[{inode}]", proc / str(pid) / "fd" / "91")
        rows.append("   9: 0100007F:96A1 0100007F:26F9 01 00000000:00000000 00:00000000 00000000  1000        0 777 1")
        net.write_text("\n".join(rows) + "\n")
        return str(proc), str(net)

    def test_comet_exe_is_looked_for_by_prefix(self):
        with tempfile.TemporaryDirectory() as d:
            proc, _ = self.fake_proc(d, {
                10: (["/usr/bin/steam"], None),
                20: (["C:\\Launchers\\bin\\Comet.exe", "--username", "x"], "42"),
                30: (["/usr/bin/wine", "Z:/home/deck/comet.exe.bak"], "42"),  # not comet.exe
                40: (["C:\\Launchers\\bin\\comet.exe"], "99"),
                50: (["/bin/bash", "-c", "pgrep comet.exe"], None),
            })
            self.assertEqual(games.comet_processes(proc), {20: "42", 40: "99"})
            self.assertEqual(games.comet_pid(42, proc), 20)
            self.assertEqual(games.comet_pid("99", proc), 40)
            self.assertIsNone(games.comet_pid(7, proc))
            self.assertEqual(games.comet_others(42, proc), ["99"])
            self.assertEqual(games.comet_others(7, proc), ["42", "99"])
        self.assertEqual(games.comet_processes("/nonexistent"), {})

    def test_the_prefix_comes_from_wineprefix_when_there_is_no_compat_data_path(self):
        with tempfile.TemporaryDirectory() as d:
            proc, _ = self.fake_proc(d, {20: (["comet.exe"], None)})
            (Path(proc) / "20" / "environ").write_bytes(b"WINEPREFIX=/steam/compatdata/42/pfx/\0")
            self.assertEqual(games.comet_pid(42, proc), 20)

    def test_port_owned_by_reads_the_kernel_tables(self):
        with tempfile.TemporaryDirectory() as d:
            proc, net = self.fake_proc(d, {20: (["comet.exe"], "42"), 40: (["comet.exe"], "99")}, listeners={20: 5551})
            self.assertTrue(games.port_owned_by(20, 9977, proc, net))
            self.assertFalse(games.port_owned_by(40, 9977, proc, net))  # not the one listening
            self.assertFalse(games.port_owned_by(20, 9978, proc, net))  # another port
            self.assertFalse(games.port_owned_by(77, 9977, proc, net))  # no such process
            self.assertFalse(games.port_owned_by(20, 9977, proc, str(Path(d) / "nothing")))
            self.assertTrue(games.port_listening(9977, net))
            self.assertFalse(games.port_listening(9978, net))
            self.assertFalse(games.port_listening(9977, str(Path(d) / "nothing")))
