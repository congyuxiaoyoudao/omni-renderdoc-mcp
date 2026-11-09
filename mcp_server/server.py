"""The public MCP interface. RenderDoc itself runs in the GUI process."""

from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

from .bridge.client import BridgeClient
from .exporters.shader_pass import export_shaders

Stage = Literal["vertex", "hull", "domain", "geometry", "pixel", "compute"]


def make_server(
    client: BridgeClient,
    spirv_cross: Path | None = None,
    dxbc_decompiler: Path | None = None,
    vkd3d_compiler: Path | None = None,
) -> FastMCP:
    server = FastMCP("Omni RenderDoc MCP")

    async def call(method: str, **arguments) -> dict[str, object]:
        params = {
            key: value for key, value in arguments.items() if value is not None
        }
        return await client.call(method, params)

    @server.tool()
    async def get_capture_status() -> dict[str, object]:
        """Report whether a capture is open, its filename and graphics API."""
        return await client.call("get_capture_status", {})

    @server.tool()
    async def list_captures(directory: str) -> dict[str, object]:
        """List local .rdc files with sizes and modification times."""
        return await call("list_captures", directory=directory)

    @server.tool()
    async def open_capture(capture_path: str) -> dict[str, object]:
        """Open an .rdc file in RenderDoc, replacing the current capture."""
        return await call("open_capture", capture_path=capture_path)

    @server.tool()
    async def get_draw_calls(
        include_children: bool = True,
        marker_filter: str | None = None,
        exclude_markers: list[str] | None = None,
        event_id_min: int | None = None,
        event_id_max: int | None = None,
        only_actions: bool = False,
        flags_filter: list[str] | None = None,
    ) -> dict[str, object]:
        """List actions filtered by markers, event range or action flags."""
        return await call(
            "get_draw_calls",
            include_children=include_children,
            marker_filter=marker_filter,
            exclude_markers=exclude_markers,
            event_id_min=event_id_min,
            event_id_max=event_id_max,
            only_actions=only_actions,
            flags_filter=flags_filter,
        )

    @server.tool()
    async def get_frame_summary() -> dict[str, object]:
        """Summarize frame actions, markers, buffers and textures."""
        return await client.call("get_frame_summary", {})

    @server.tool()
    async def get_draw_call_details(event_id: int) -> dict[str, object]:
        """Inspect one event's action parameters, flags and outputs."""
        return await call("get_draw_call_details", event_id=event_id)

    @server.tool()
    async def get_action_timings(
        event_ids: list[int] | None = None,
        marker_filter: str | None = None,
        exclude_markers: list[str] | None = None,
    ) -> dict[str, object]:
        """Read GPU duration counters for selected actions when available."""
        return await call(
            "get_action_timings",
            event_ids=event_ids,
            marker_filter=marker_filter,
            exclude_markers=exclude_markers,
        )

    @server.tool()
    async def find_draws_by_shader(
        shader_name: str, stage: Stage | None = None
    ) -> dict[str, object]:
        """Find draws by shader name, optionally filtering by stage."""
        return await call(
            "find_draws_by_shader", shader_name=shader_name, stage=stage
        )

    @server.tool()
    async def find_draws_by_texture(texture_name: str) -> dict[str, object]:
        """Find draws referencing a texture by partial resource name."""
        return await call("find_draws_by_texture", texture_name=texture_name)

    @server.tool()
    async def find_draws_by_resource(resource_id: str) -> dict[str, object]:
        """Find actions referencing a specified RenderDoc resource ID."""
        return await call("find_draws_by_resource", resource_id=resource_id)

    @server.tool()
    async def get_shader_info(
        event_id: int, stage: Stage
    ) -> dict[str, object]:
        """Inspect the shader, its disassembly and bindings at an event."""
        return await call("get_shader_info", event_id=event_id, stage=stage)

    @server.tool()
    async def get_buffer_contents(
        resource_id: str,
        offset: int = 0,
        length: int = 0,
    ) -> dict[str, object]:
        """Read buffer bytes as base64. Length zero reads to the end."""
        return await call(
            "get_buffer_contents",
            resource_id=resource_id,
            offset=offset,
            length=length,
        )

    @server.tool()
    async def get_texture_info(resource_id: str) -> dict[str, object]:
        """Inspect texture dimensions, format, mip and array metadata."""
        return await call("get_texture_info", resource_id=resource_id)

    @server.tool()
    async def get_texture_data(
        resource_id: str,
        mip: int = 0,
        slice: int = 0,
        sample: int = 0,
        depth_slice: int | None = None,
    ) -> dict[str, object]:
        """Read a texture mip, array slice, sample and optional depth slice."""
        return await call(
            "get_texture_data",
            resource_id=resource_id,
            mip=mip,
            slice=slice,
            sample=sample,
            depth_slice=depth_slice,
        )

    @server.tool()
    async def get_pipeline_state(event_id: int) -> dict[str, object]:
        """Inspect shaders, descriptors, attachments and viewports."""
        return await call("get_pipeline_state", event_id=event_id)

    @server.tool()
    async def export_pass_shaders(
        output_directory: str,
        pass_event_id: int,
        convert_to_unity: bool = True,
        vertex_semantics: dict[str, dict[int, str]] | None = None,
    ) -> dict[str, object]:
        """Export the captured shaders for a marker and its draw/dispatches.

        Find pass marker event IDs with get_frame_summary or get_draw_calls.
        A draw selects its enclosing marker (a lone draw selects itself).
        Preserve native bytecode, reflection, per-event bindings and state.
        Convert supported programs to ShaderLab or Unity compute assets.
        For anonymous SPIR-V inputs, optionally map vertex shader resource
        IDs to {location: Unity semantic}. Check each variant's status in
        the manifest for conversion failures or unsupported stages.
        """
        return await export_shaders(
            client,
            output_directory,
            pass_event_id,
            convert_to_unity,
            vertex_semantics or {},
            spirv_cross,
            dxbc_decompiler,
            vkd3d_compiler,
        )

    @server.tool()
    async def export_frame_meshes(
        output_directory: str,
        skeletons: dict | None = None,
        position_attributes: dict[str, str] | None = None,
        event_ids: list[int] | None = None,
        attribute_roles: dict[str, dict[str, str]] | None = None,
        include_textures: bool = False,
        pass_event_id: int | None = None,
    ) -> dict[str, object]:
        """Export draw geometry to ASCII FBX files and a coverage manifest.

        Visit every draw and instance, or select a subset with event_ids.
        Key position_attributes by event ID to select a custom input.
        Map opaque channel names to native FBX roles with attribute_roles.
        Key skeletons by event ID or 'event:instance'. Each rig has bones,
        with a name, parent index and global bind_matrix, plus joint/weight
        channels or explicit vertex influences.
        Select a pass with pass_event_id, or individual draws with event_ids.
        include_textures saves bound shader textures and records their paths
        and binding slots per draw in the manifest.
        """
        params = {
            key: value
            for key, value in {
                "output_directory": output_directory,
                "skeletons": skeletons,
                "position_attributes": position_attributes,
                "event_ids": event_ids,
                "attribute_roles": attribute_roles,
                "pass_event_id": pass_event_id,
            }.items()
            if value is not None
        }
        if pass_event_id is not None and event_ids is not None:
            raise ValueError("Choose either pass_event_id or event_ids")
        if include_textures:
            params["include_textures"] = True
        return await client.call("export_frame_meshes", params)

    return server


def run(
    directory: Path,
    timeout: float,
    spirv_cross: Path | None = None,
    dxbc_decompiler: Path | None = None,
    vkd3d_compiler: Path | None = None,
) -> None:
    client = BridgeClient(directory, timeout)
    make_server(
        client, spirv_cross, dxbc_decompiler, vkd3d_compiler
    ).run(transport="stdio")
