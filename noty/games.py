"""Finding Comet's databases and a game's logo on disk. Pure stdlib, no Invasor imports."""
import os
import re
import socket
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


def comet_running(proc="/proc"):
    """The pid of a running comet.exe (a Windows program under Wine/Proton), or None."""
    try:
        entries = os.listdir(proc)
    except OSError:
        return None
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(os.path.join(proc, entry, "cmdline"), "rb") as f:
                args = f.read().split(b"\0")
        except OSError:
            continue  # gone, or not ours
        for arg in args:
            if arg.decode("utf-8", "replace").replace("\\", "/").rsplit("/", 1)[-1].lower() == "comet.exe":
                return int(entry)
    return None


def comet_listening(port=COMET_PORT):
    """Whether something accepts connections on Comet's port."""
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    except OSError:
        return False
    return True
