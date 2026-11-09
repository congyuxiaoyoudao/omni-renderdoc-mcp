"""Default RenderDoc profile used by the extension installer."""

import os
import sys
from pathlib import Path


def default_extensions_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ["APPDATA"])
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(
            os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
        )
    return base / "qrenderdoc" / "extensions"
