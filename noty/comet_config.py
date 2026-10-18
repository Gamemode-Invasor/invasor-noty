"""Putting Noty's [[custom_event]] in Comet's config.toml, without touching the rest of the file."""
import os
import re
import tempfile

try:
    import tomllib
except ImportError:  # Python < 3.11: written anyway, just not checked
    tomllib = None

BEGIN = "# noty:begin (written by Noty, replaced every time)"
END = "# noty:end"
BLOCK = re.compile(r"^# noty:begin.*?^# noty:end[ \t]*\r?\n?", re.DOTALL | re.MULTILINE)


def _quote(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def block(url, eol="\n"):
    return eol.join([BEGIN, "[[custom_event]]", "enabled = true", f"url = {_quote(url)}", END, ""])


def _events(text):
    """The custom_event entries of a config text ({} if this Python can't read TOML). ValueError if it isn't TOML."""
    if tomllib is None:
        return []
    try:
        events = tomllib.loads(text).get("custom_event", [])
    except tomllib.TOMLDecodeError as e:
        raise ValueError(str(e)) from e
    return [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []


def _has(events, url):
    return any(e.get("url") == url and e.get("enabled") is True for e in events)


def has_url(path, url):
    """Whether the config at path sends custom events to url."""
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return _has(_events(f.read()), url)
    except (OSError, ValueError):
        return False


def ensure_block(path, url):
    """Make the config at path send Comet's custom events to url. Only Noty's own block (between its markers) is ever
    replaced; the rest is kept. Returns whether the file changed. ValueError if the result wouldn't be valid TOML
    (nothing is written then), OSError if it can't be written."""
    try:
        with open(path, encoding="utf-8", newline="") as f:
            old = f.read()
    except FileNotFoundError:
        old = ""
    eol = "\r\n" if "\r\n" in old else "\n"
    mine = block(url, eol)
    if BLOCK.search(old):
        new = BLOCK.sub(lambda _: mine, old, count=1)
    else:
        if _has(_events(old), url):
            return False  # the user already has it
        new = old + ("" if not old or old.endswith("\n") else eol) + (eol if old else "") + mine
    if new == old:
        return False
    _events(new)  # the result must be TOML
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(new)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True
