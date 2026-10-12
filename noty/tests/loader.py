import importlib
import importlib.util
import itertools
import socket
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
_count = itertools.count()


def load_backend():
    """backend.py as the core loads it: a package of its own, with a name nobody else has (so
    each call is a fresh copy of every file, and its files import each other consistently)."""
    name = f"noty_under_test_{next(_count)}"
    spec = importlib.util.spec_from_file_location(name, HERE / "backend.py", submodule_search_locations=[str(HERE)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def submodule(backend, name):
    """One of the module's files, as loaded with that backend."""
    return importlib.import_module(f"{backend.__name__}.{name}")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
