"""Rebuild fragment nodes from the current capture's staged evidence."""

from pathlib import Path
from typing import Literal

from .generic_shader import rebuild_generic


async def rebuild_shader_nodes(
    output_directory: str,
    geometry_blend: str,
    shader_evidence_directory: str,
    blender_executable: str | None = None,
    render_preview: bool = True,
    render_scale: int = 50,
    timeout_seconds: float = 1200,
    node_recipes: str | None = None,
    unsupported_shaders: Literal["report", "error"] = "report",
    spirv_cross: Path | None = None,
) -> dict:
    """Resolve reviewed equations against each captured draw's inputs."""
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
