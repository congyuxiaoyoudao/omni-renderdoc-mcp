"""The public MCP interface. RenderDoc itself runs in the GUI process."""

from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

from .bridge.client import BridgeClient
from .exporters.blender import export_blender
from .exporters.shader_pass import export_shaders
from .exporters.unity_hdrp import export_unity_hdrp as build_unity_hdrp

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
        Recognized joint/weight inputs are exported automatically as a flat
        joint palette when bind matrices or hierarchy are unavailable; this
        preserves weights but does not recover the original animation rig.
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

    @server.tool()
    async def export_blender_scene(
        output_directory: str,
        blender_executable: str | None = None,
        event_ids: list[int] | None = None,
        pass_event_id: int | None = None,
        camera: dict | None = None,
        material_bindings: dict | None = None,
        character_event_ids: list[int] | None = None,
        reference_event_id: int | None = None,
        reference_resource_id: str | None = None,
        uv_attributes: dict[str, str] | None = None,
        appearance_mode: Literal[
            "shader_nodes", "captured", "editable"
        ] = "shader_nodes",
        render_preview: bool = True,
        reference_background: bool = False,
        timeout_seconds: float = 600,
        node_recipes: str | None = None,
        unsupported_shaders: Literal["report", "error"] = "report",
        shader_evidence_limits: dict | None = None,
        geometry_blend: str | None = None,
        shader_evidence_directory: str | None = None,
        render_scale: int = 100,
        skeletons: dict | None = None,
        position_attributes: dict[str, str] | None = None,
    ) -> dict[str, object]:
        """Export capture geometry, shader inputs and editable Blender nodes.

        Select scene draws with event_ids OR one pass with pass_event_id.
        Alternatively, supply geometry_blend AND shader_evidence_directory
        to rebuild the exported scene's fragment materials offline. These
        absolute paths replace capture selection; no active replay is needed.
        Offline rebuilding requires shader_nodes and rejects capture options.
        Reconstruct camera-space geometry from post-VS output. Preserve the
        current pose and post-transform shader varyings. Default shader_nodes
        stages current draw bytecode, constants, pipeline state and textures,
        then builds fragment equations from node_recipes matched by shader
        bytecode hash. Simple proven fragments can be translated automatically.
        Unsupported shaders are reported as needs_recipes with a marked
        placeholder; unsupported_shaders=error refuses incomplete materials.
        node_recipes is an absolute version-1 fragment recipe JSON path.
        shader_evidence_limits bounds evidence bytes/subresources; omissions
        are reported. No fixed frame/event/resource IDs or final-color
        projection are used in shader_nodes. skeletons maps draw or
        'event:instance' keys to explicit bind data, like export_frame_meshes.
        Recognized joint/weight channels are preserved where mapping permits;
        unavailable original hierarchy and bind data are reported explicitly.
        For anonymous skin inputs, skeletons can specify joints/weights
        channel lists. Available input-space rigs are saved in a separate
        hidden source collection; captured posed meshes are not skinned twice.
        position_attributes selects anonymous input position channels for
        the optional rigged source meshes, keyed by draw or 'event:instance'.
        Explicit editable mode uses approximate Principled materials.
        Explicit captured mode with reference_event_id/resource_id adds
        camera-projected captured appearance, valid from that viewpoint.
        reference_background adds a separate labeled image backdrop.
        camera may override p00, p11, width, height and clip_y_sign.
        material_bindings maps event IDs to base_color/normal bindings
        {set: N, binding: N}; uv_attributes maps event IDs to channel names.
        render_scale controls output resolution percentage (1..100).
        Blender runs in background; each export creates a fresh folder.
        """
        return await export_blender(
            client,
            output_directory,
            blender_executable=blender_executable,
            event_ids=event_ids,
            pass_event_id=pass_event_id,
            camera=camera,
            material_bindings=material_bindings,
            character_event_ids=character_event_ids,
            reference_event_id=reference_event_id,
            reference_resource_id=reference_resource_id,
            uv_attributes=uv_attributes,
            appearance_mode=appearance_mode,
            render_preview=render_preview,
            reference_background=reference_background,
            timeout_seconds=timeout_seconds,
            node_recipes=node_recipes,
            unsupported_shaders=unsupported_shaders,
            shader_evidence_limits=shader_evidence_limits,
            spirv_cross=spirv_cross,
            geometry_blend=geometry_blend,
            shader_evidence_directory=shader_evidence_directory,
            render_scale=render_scale,
            skeletons=skeletons,
            position_attributes=position_attributes,
        )

    @server.tool()
    async def export_unity_hdrp(
        output_directory: str,
        unity_executable: str | None = None,
        event_ids: list[int] | None = None,
        pass_event_id: int | None = None,
        camera: dict | None = None,
        material_bindings: dict | None = None,
        uv_attributes: dict[str, str] | None = None,
        lighting: dict | None = None,
        staged_manifest: str | None = None,
        editor_version: str = "6000.3.7f1",
        hdrp_version: str = "17.3.0",
        run_unity: bool = True,
        render_preview: bool = True,
        render_scale: int = 100,
        timeout_seconds: float = 1200,
        shader_mode: str = "hdrp_lit",
        spirv_cross_executable: str | None = None,
        supplementary_buffers: str | None = None,
        final_target: str | None = None,
        final_is_srgb: bool = False,
        final_flip_y: bool = False,
        shader_evidence_limits: dict | None = None,
    ) -> dict[str, object]:
        """Export a Unity HDRP project with editable captured meshes.

        Select draws with event_ids OR pass_event_id. In hdrp_lit mode exclude
        shadow, depth-only, fullscreen and UI draws. Alternatively supply an
        absolute staged_manifest from an earlier geometry export; offline
        reconstruction needs no open capture. Each export uses a fresh folder.
        Reconstruct the captured camera-local posed geometry, textures,
        HDRP asset, camera and scene. HDRP/Lit materials and lighting are
        approximations: original custom shaders, postprocessing, animation
        and Unity hierarchy are not recovered. Explicit material_bindings
        maps event IDs to base_color/normal {set:N,binding:N}; uv_attributes
        maps event IDs to varying names. camera may override p00/p11 and size.
        Optional lighting supplies directional_lights with camera-local ray
        direction, color_linear, lux and provenance, exposure_ev100 and
        tonemapping (None/ACES/Neutral). Default lighting is approximate.
        Targets Unity 6000.3/HDRP 17.3 by default. Unity must be licensed and
        able to resolve the requested package. run_unity=False only stages
        the project; True builds/verifies the scene and optional preview.
        render_scale is a percentage 1..100. Logs remain on build failure.
        timeout_seconds is a separate budget for capture staging and the
        Unity build, not a combined deadline. Built-in bridge staging keeps
        a longer configured bridge timeout if one is already set.

        shader_mode='original_fragment' instead translates original Vulkan
        fragment SPIR-V and replays captured post-vertex varyings, native
        constant/storage buffers, texture formats, MRT, depth and stencil.
        Supply spirv_cross_executable (or install spirv-cross on PATH). Select
        dependent depth, surface, lighting and fullscreen draws together.
        Earlier selected outputs are connected; omitted producers stay native
        capture snapshots. This mode currently targets Windows/D3D11. Native
        shaders run at captured resolution; render_scale controls the preview.
        It cannot restore animation, original source code or omitted compute
        passes. Unsupported bindings/states fail instead of using HDRP/Lit.
        supplementary_buffers optionally supplies a captured raw-buffer index.
        final_target selects a produced color resource ID; otherwise the last
        selected draw's first color attachment is displayed. final_is_srgb
        decodes shader-encoded sRGB stored in a linear final target.
        final_flip_y adapts the final presentation orientation without changing
        any original shader or intermediate texture coordinates.
        shader_evidence_limits optionally raises native capture budgets via
        max_total_bytes, max_resource_bytes and max_subresources_per_view.
        Values must be positive integers. Defaults are 2 GiB total, 256 MiB
        per resource and 4096 subresources per view. This capture-only option
        requires original_fragment and cannot combine with staged_manifest.
        """
        return await build_unity_hdrp(
            client, output_directory, unity_executable=unity_executable,
            event_ids=event_ids, pass_event_id=pass_event_id, camera=camera,
            material_bindings=material_bindings, uv_attributes=uv_attributes,
            lighting=lighting,
            staged_manifest=staged_manifest, editor_version=editor_version,
            hdrp_version=hdrp_version, run_unity=run_unity,
            render_preview=render_preview, render_scale=render_scale,
            timeout_seconds=timeout_seconds,
            shader_mode=shader_mode,
            spirv_cross_executable=spirv_cross_executable,
            supplementary_buffers=supplementary_buffers,
            final_target=final_target, final_is_srgb=final_is_srgb,
            final_flip_y=final_flip_y,
            shader_evidence_limits=shader_evidence_limits,
        )

    # Reject removed or misspelled options instead of silently ignoring them.
    for name in ("export_blender_scene", "export_unity_hdrp"):
        tool = server._tool_manager.get_tool(name)
        arguments = tool.fn_metadata.arg_model
        arguments.model_config = dict(arguments.model_config, extra="forbid")
        arguments.model_rebuild(force=True)
        tool.parameters = arguments.model_json_schema(by_alias=True)

    return server


def run(
    directory: Path,
    timeout: float,
    spirv_cross: Path | None = None,
    dxbc_decompiler: Path | None = None,
    vkd3d_compiler: Path | None = None,
) -> None:
    client = BridgeClient(directory, timeout)
    make_server(client, spirv_cross, dxbc_decompiler, vkd3d_compiler).run(
        transport="stdio"
    )
