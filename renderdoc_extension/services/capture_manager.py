"""Open and inspect local RenderDoc captures."""

from datetime import datetime
from pathlib import Path

import renderdoc as rd


def capture_api(controller):
    return str(controller.GetAPIProperties().pipelineType)


def get_capture_status(ctx, replay):
    if not ctx.IsCaptureLoaded():
        return {"loaded": False}
    return {
        "loaded": True,
        "filename": ctx.GetCaptureFilename(),
        "api": replay(ctx, capture_api),
    }


def list_captures(directory):
    folder = Path(directory)
    if not folder.is_dir():
        raise ValueError("Directory not found: {}".format(folder))
    files = []
    for path in folder.iterdir():
        if path.suffix.casefold() != ".rdc" or not path.is_file():
            continue
        stat = path.stat()
        files.append(
            {
                "filename": path.name,
                "path": str(path),
                "size_bytes": stat.st_size,
                "modified_time": datetime.fromtimestamp(
                    stat.st_mtime
                ).isoformat(),
            }
        )
    files.sort(key=lambda row: row["modified_time"], reverse=True)
    return {"directory": directory, "count": len(files), "captures": files}


def open_capture(ctx, capture_path, replay):
    path = Path(capture_path).resolve()
    if path.suffix.casefold() != ".rdc" or not path.is_file():
        raise ValueError("Expected an existing .rdc file: {}".format(path))
    ctx.LoadCapture(str(path), rd.ReplayOptions(), str(path), False, True)
    if (
        not ctx.IsCaptureLoaded()
        or Path(ctx.GetCaptureFilename()).resolve() != path
    ):
        raise ValueError("RenderDoc could not load {}".format(path))
    return {
        "success": True,
        "capture_path": capture_path,
        "filename": path.name,
        "api": replay(ctx, capture_api),
    }
