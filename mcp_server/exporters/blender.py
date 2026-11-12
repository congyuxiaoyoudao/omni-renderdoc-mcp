"""Build native Blender scenes from geometry staged by the replay bridge."""

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

from ..bridge.client import BridgeError


def find_blender(executable: str | None = None) -> Path:
    """Use an explicit executable, environment override, PATH, or install."""
    requested = executable or os.environ.get("BLENDER_EXECUTABLE")
    if requested:
        path = Path(requested).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise ValueError(
                "blender_executable must be an existing absolute file"
            )
        return path.resolve()
    located = shutil.which("blender")
    if located:
        return Path(located).resolve()
    candidates = []
    if os.name == "nt":
        base = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
        candidates = sorted(
            (base / "Blender Foundation").glob("Blender */blender.exe"),
            reverse=True,
        )
    elif os.name == "posix":
        candidates = [
            Path("/Applications/Blender.app/Contents/MacOS/Blender"),
        ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(
        "Blender was not found; supply blender_executable or set "
        "BLENDER_EXECUTABLE to its absolute path"
    )


def _event_id(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(name + " must be a non-negative integer")


def _validate(options):
    event_ids, pass_id = options.get("event_ids"), options.get("pass_event_id")
    if (event_ids is None) == (pass_id is None):
        raise ValueError("Specify exactly one of event_ids or pass_event_id")
    if event_ids is not None:
        if not isinstance(event_ids, list) or not event_ids:
            raise ValueError("event_ids must be a nonempty list")
        for eid in event_ids:
            _event_id(eid, "event_ids entries")
    if pass_id is not None:
        _event_id(pass_id, "pass_event_id")
    reference_id = options.get("reference_event_id")
    if (reference_id is None) != (
        options.get("reference_resource_id") is None
    ):
        raise ValueError(
            "reference_event_id and reference_resource_id must be supplied "
            "together"
        )
    if reference_id is not None:
        _event_id(reference_id, "reference_event_id")
    skeletons = options.get("skeletons")
    if skeletons is not None and not isinstance(skeletons, dict):
        raise ValueError("skeletons must map draw IDs to skeleton data")


async def _run_blender(command, folder, timeout_seconds):
    log_path = folder / "blender.log"
    process = None
    with log_path.open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(folder),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=(
                subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            ),
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
        except (asyncio.TimeoutError, asyncio.CancelledError) as error:
            if process.returncode is None:
                process.kill()
                await process.wait()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise RuntimeError(
                f"Blender exceeded {timeout_seconds:g}s; staged data and "
                f"log retained at {folder}"
            ) from error
    if process.returncode:
        raise RuntimeError(
            f"Blender exited with code {process.returncode}; see {log_path}"
        )


def _verify(folder, render_preview):
    blend = folder / "scene.blend"
    if not blend.is_file():
        raise RuntimeError(
            f"Blender produced no scene.blend; see {folder / 'blender.log'}"
        )
    with blend.open("rb") as stream:
        if not stream.read(7) == b"BLENDER":
            raise RuntimeError("Blender output has no native .blend header")
    report_path = blend.with_suffix(".report.json")
    if not report_path.is_file():
        raise RuntimeError("Blender produced no scene verification report")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not report.get("mesh_count") or not report.get("vertex_count"):
        raise RuntimeError("Blender scene contains no reconstructed geometry")
    if render_preview:
        for name in ("preview.png", "preview_editable.png"):
            path = folder / name
            if (
                not path.is_file()
                or path.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n"
            ):
                raise RuntimeError(f"Blender produced no valid {name}")
    return report


async def export_blender(
    client,
    output_directory: str,
    blender_executable: str | None = None,
    event_ids: list[int] | None = None,
    pass_event_id: int | None = None,
    camera: dict | None = None,
    material_bindings: dict | None = None,
    character_event_ids: list[int] | None = None,
    reference_event_id: int | None = None,
    reference_resource_id: str | None = None,
    uv_attributes: dict | None = None,
    appearance_mode: str = "shader_nodes",
    render_preview: bool = True,
    reference_background: bool = False,
    timeout_seconds: float = 600,
    node_recipes: str | None = None,
    unsupported_shaders: str = "report",
    shader_evidence_limits: dict | None = None,
    spirv_cross: Path | None = None,
    geometry_blend: str | None = None,
    shader_evidence_directory: str | None = None,
    render_scale: int = 100,
    skeletons: dict | None = None,
    position_attributes: dict[str, str] | None = None,
) -> dict:
    """Preserve the staging folder and logs even when Blender fails."""
    from .generic_shader import rebuild_generic, validate_options

    validate_options(
        render_preview, render_scale, timeout_seconds, unsupported_shaders
    )
    options = {
        "event_ids": event_ids,
        "pass_event_id": pass_event_id,
        "camera": camera,
        "material_bindings": material_bindings,
        "character_event_ids": character_event_ids,
        "reference_event_id": reference_event_id,
        "reference_resource_id": reference_resource_id,
        "uv_attributes": uv_attributes,
        "skeletons": skeletons,
        "position_attributes": position_attributes,
    }
    if appearance_mode not in ("shader_nodes", "captured", "editable"):
        raise ValueError(
            "appearance_mode must be shader_nodes, captured or editable"
        )
    offline = (
        geometry_blend is not None or shader_evidence_directory is not None
    )
    if offline:
        if geometry_blend is None or shader_evidence_directory is None:
            raise ValueError(
                "geometry_blend and shader_evidence_directory "
                "must be supplied together"
            )
        if appearance_mode != "shader_nodes":
            raise ValueError("Offline rebuilding requires shader_nodes mode")
        capture_options = [
            name for name, value in options.items() if value is not None
        ]
        if reference_background:
            capture_options.append("reference_background")
        if shader_evidence_limits is not None:
            capture_options.append("shader_evidence_limits")
        if capture_options:
            raise ValueError(
                "Offline rebuilding cannot combine capture options: "
                + ", ".join(capture_options)
            )
        return await rebuild_generic(
            output_directory,
            geometry_blend,
            shader_evidence_directory,
            blender_executable=blender_executable,
            node_recipes=node_recipes,
            unsupported_shaders=unsupported_shaders,
            render_preview=render_preview,
            render_scale=render_scale,
            timeout_seconds=timeout_seconds,
            spirv_cross=spirv_cross,
        )
    _validate(options)
    if appearance_mode == "shader_nodes":
        if reference_event_id is not None or reference_background:
            raise ValueError(
                "shader_nodes uses fragment inputs; "
                "remove captured reference options"
            )
        if node_recipes:
            from .fragment_graph import validate_registry
            from .generic_shader import read_json

            if not Path(node_recipes).is_absolute():
                raise ValueError("node_recipes must be an absolute JSON path")
            validate_registry(read_json(node_recipes))
        options["include_shader_evidence"] = True
        if shader_evidence_limits is not None:
            options["shader_evidence_limits"] = shader_evidence_limits
    elif node_recipes or shader_evidence_limits is not None:
        raise ValueError(
            "node_recipes and shader evidence require shader_nodes mode"
        )
    root = Path(output_directory).expanduser()
    if not root.is_absolute():
        raise ValueError("Specify an absolute output_directory")
    root = root.resolve()
    executable = find_blender(blender_executable)
    root.mkdir(parents=True, exist_ok=True)
    summary = await client.call(
        "stage_blender_scene",
        dict(
            output_directory=str(root),
            **{
                key: value
                for key, value in options.items()
                if value is not None
            },
        ),
    )
    folder = Path(summary["directory"]).resolve()
    if folder.parent != root or not folder.name.startswith("blender-scene-"):
        raise BridgeError("Unexpected Blender staging directory")
    manifest = Path(summary["manifest"]).resolve()
    if manifest != folder / "manifest.json" or not manifest.is_file():
        raise BridgeError("Unexpected Blender staging manifest")
    if not summary.get("mesh_count"):
        raise RuntimeError(
            f"No geometry could be reconstructed; see {manifest}"
        )
    if appearance_mode == "shader_nodes":
        from .generic_shader import build_generic

        evidence = Path(summary.get("shader_evidence_directory", "")).resolve()
        if evidence != folder / "evidence":
            raise BridgeError(
                "Unexpected shader evidence directory; "
                "update the replay extension"
            )
        return await build_generic(
            folder,
            evidence,
            manifest=manifest,
            blender_executable=str(executable),
            node_recipes=node_recipes,
            unsupported_shaders=unsupported_shaders,
            render_preview=render_preview,
            render_scale=render_scale,
            timeout_seconds=timeout_seconds,
            spirv_cross=spirv_cross,
        )
    blend = folder / "scene.blend"
    if blend.exists():
        raise FileExistsError(blend)
    command = [
        str(executable),
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(Path(__file__).with_name("blender_scene.py")),
        "--",
        "--manifest",
        str(manifest),
        "--output",
        str(blend),
        "--mode",
        appearance_mode,
        "--scale",
        str(render_scale),
    ]
    if reference_background:
        command.append("--reference-background")
    if render_preview:
        command.extend(
            [
                "--render",
                str(folder / "preview.png"),
                "--render-editable",
                str(folder / "preview_editable.png"),
            ]
        )
    await _run_blender(command, folder, timeout_seconds)
    report = _verify(folder, render_preview)
    result = {
        **summary,
        "blend_file": str(blend),
        "report": str(blend.with_suffix(".report.json")),
        "blender_log": str(folder / "blender.log"),
        "appearance_mode": report.get("appearance_mode", appearance_mode),
        "mesh_count": report["mesh_count"],
        "vertex_count": report["vertex_count"],
        "triangle_count": report.get("triangle_count", 0),
        "warnings": report.get("warnings", []),
        "reference_background": report.get("reference_background", False),
        "skinning": report.get("skinning", {}),
    }
    if render_preview:
        result.update(
            preview=str(folder / "preview.png"),
            editable_preview=str(folder / "preview_editable.png"),
        )
    (folder / "export_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return result
