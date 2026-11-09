"""Shared file transport primitives for the GUI and the stdio server."""

import json
import os
from pathlib import Path


def default_ipc_dir():
    if os.name == "nt":
        base = Path(os.environ["LOCALAPPDATA"])
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "omni-renderdoc-mcp" / "ipc"


def read_json(path):
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def publish(path, value):
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=True, allow_nan=False)
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()
