"""Finding Comet's databases and a game's logo on disk. Pure stdlib, no Invasor imports."""
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
