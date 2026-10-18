"""Finding Comet's databases and a game's logo on disk. Pure stdlib, no Invasor imports."""
import os
import re
from pathlib import Path

STEAM = Path.home() / ".local/share/Steam"
LOGO_TYPES = (".png", ".jpg", ".jpeg", ".webp")


def gameplay_dbs(appid, steam=STEAM):
    """Every gameplay.db Comet keeps for the game, in the subfolders of its gameplay folder."""
    base = (Path(steam) / "steamapps" / "compatdata" / str(appid) / "pfx" / "drive_c" / "users" / "steamuser"
            / "AppData" / "Local" / "comet" / "gameplay")
    return sorted(str(p) for p in base.glob("*/**/gameplay.db") if p.is_file())


def logo_files(appid, steam=STEAM):
    """The game's logo images, best first: the one set by the user, then Steam's own."""
    steam, found = Path(steam), []
    for ext in LOGO_TYPES:
        found += sorted(steam.glob(f"userdata/*/config/grid/{appid}_logo{ext}"))
    found.append(steam / "appcache" / "librarycache" / str(appid) / "logo.png")
    return [str(p) for p in found if p.is_file()]


COMET_PORT = 9977  # where Comet listens for the game (fixed in Comet, so there is one Comet at a time)
STORE_GOG = re.compile(r'^\s*set\s+"?STORE=gog"?\s*$', re.IGNORECASE | re.MULTILINE)
MAX_LAUNCHER = 64 * 1024


def comet_config(appid, steam=STEAM):
    """Comet's config.toml in the game's prefix. Comet reads it once, when it starts."""
    return (Path(steam) / "steamapps" / "compatdata" / str(appid) / "pfx" / "drive_c" / "users" / "steamuser"
            / "AppData" / "Roaming" / "comet" / "config.toml")


def is_gog_launcher(path):
    """Whether the .bat a shortcut launches says it is a GOG game (set "STORE=gog"), so Comet will run."""
    if not isinstance(path, str) or not path.lower().endswith(".bat"):
        return False
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return bool(STORE_GOG.search(f.read(MAX_LAUNCHER)))
    except OSError:
        return False


def _comet_args(proc, entry):
    try:
        with open(os.path.join(proc, entry, "cmdline"), "rb") as f:
            return f.read().split(b"\0")
    except OSError:
        return []  # gone, or not ours


def _prefix_id(proc, entry):
    """The id (the folder name in compatdata, the shortcut's AppId) of the prefix a process runs in, or None."""
    try:
        with open(os.path.join(proc, entry, "environ"), "rb") as f:
            env = dict(item.split(b"=", 1) for item in f.read().split(b"\0") if b"=" in item)
    except OSError:
        return None
    path = env.get(b"STEAM_COMPAT_DATA_PATH") or env.get(b"WINEPREFIX")
    if not path:
        return None
    path = path.decode("utf-8", "replace").rstrip("/")
    if path.endswith("/pfx"):
        path = path[:-4]
    return path.rsplit("/", 1)[-1] or None


def comet_processes(proc="/proc"):
    """{pid: prefix id} of every running comet.exe (a Windows program under Wine/Proton). The prefix id is None when
    its environment can't be read."""
    try:
        entries = os.listdir(proc)
    except OSError:
        return {}
    found = {}
    for entry in entries:
        if not entry.isdigit():
            continue
        for arg in _comet_args(proc, entry):
            if arg.decode("utf-8", "replace").replace("\\", "/").rsplit("/", 1)[-1].lower() == "comet.exe":
                found[int(entry)] = _prefix_id(proc, entry)
                break
    return found


def comet_pid(appid, proc="/proc"):
    """The pid of the comet.exe running in the prefix of the game (its shortcut's AppId), or None."""
    return next((pid for pid, prefix in sorted(comet_processes(proc).items()) if prefix == str(appid)), None)


def comet_others(appid, proc="/proc"):
    """The prefix ids of the comet.exe that run for other games (None for a prefix that can't be told), sorted."""
    return sorted({str(prefix) for prefix in comet_processes(proc).values() if prefix != str(appid)})


def _listening_inodes(port, net):
    """The inodes of the sockets listening on 127.0.0.1:port, from the kernel's table (no connection to the port, which
    Comet would take for a client and log)."""
    wanted = f"0100007F:{port:04X}"
    inodes = set()
    try:
        with open(net) as f:
            for line in list(f)[1:]:
                cols = line.split()
                if len(cols) > 9 and cols[1] == wanted and cols[3] == "0A":  # 0A: LISTEN
                    inodes.add(cols[9])
    except OSError:
        pass
    return inodes


def port_listening(port=COMET_PORT, net="/proc/net/tcp"):
    """Whether anything listens on 127.0.0.1:port."""
    return bool(_listening_inodes(port, net))


def port_owned_by(pid, port=COMET_PORT, proc="/proc", net="/proc/net/tcp"):
    """Whether the process listens on 127.0.0.1:port."""
    inodes = _listening_inodes(port, net)
    if not inodes:
        return False
    fds = os.path.join(proc, str(pid), "fd")
    try:
        names = os.listdir(fds)
    except OSError:
        return False
    for name in names:
        try:
            link = os.readlink(os.path.join(fds, name))
        except OSError:
            continue
        if link.startswith("socket:[") and link[8:-1] in inodes:
            return True
    return False
