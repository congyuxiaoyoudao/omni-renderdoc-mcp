"""Install the extension in the selected RenderDoc profile."""

import shutil
import tempfile
from pathlib import Path

import renderdoc_extension

EXTENSION = "omni_renderdoc_bridge"


def install(extensions_dir: Path) -> Path:
    extensions_dir = extensions_dir.expanduser().resolve()
    extensions_dir.mkdir(parents=True, exist_ok=True)
    destination = extensions_dir / EXTENSION
    source = Path(renderdoc_extension.__file__).parent
    if destination.is_symlink() or (
        destination.exists()
        and not (destination / ".omni-installed").is_file()
    ):
        raise ValueError(
            f"Destination is not managed by this installer: {destination}"
        )

    with tempfile.TemporaryDirectory(dir=extensions_dir) as temporary:
        stage = Path(temporary) / "new"
        previous = Path(temporary) / "previous"
        shutil.copytree(
            source,
            stage,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        (stage / ".omni-installed").touch()
        if destination.exists():
            destination.rename(previous)
        try:
            stage.rename(destination)
        except OSError:
            if previous.exists():
                previous.rename(destination)
            raise
    return destination
