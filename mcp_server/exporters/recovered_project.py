"""Build an HDRP project from original fragment programs."""

import json
import shutil
from pathlib import Path

from .recovered_fragment import prepare_recovered_fragment_bundle


def _id(value):
    if isinstance(value, dict):
        value = value.get("resource_id")
    return str(value or "0").rsplit("::", 1)[-1]


def route_original_draws(
    data, project, final_target=None, final_is_srgb=False
):
    """Connect earlier outputs and retain unselected inputs as snapshots."""
    api = data.get("source_api", "").rsplit(".", 1)[-1]
    if api != "Vulkan":
        raise ValueError(
            "Original fragment coordinate adapter requires Vulkan"
        )
    targets, produced, frozen, defaults = {}, set(), [], []
    seen_passes = set()
    dimensions = {}
    for draw in data["draws"]:
        for texture in draw["textures"]:
            dimensions[_id(texture)] = (
                texture["width"] << texture.get("first_mip", 0),
                texture["height"] << texture.get("first_mip", 0),
            )
    last_color = None
    for draw in data["draws"]:
        pipe = draw["original_render_state"]
        unsupported = [
            note
            for note in draw.get("render_state_notes", [])
            if not note.startswith("Match imported front-face winding")
        ]
        if unsupported:
            raise ValueError(
                "Unsupported original raster state: " + str(unsupported)
            )
        viewport = (pipe.get("viewports") or [{}])[0]
        width, height = viewport.get("width"), viewport.get("height")
        if not width or not height or width < 0:
            raise ValueError("Original draw requires a captured viewport")
        if (
            viewport.get("min_depth", 0) != 0
            or viewport.get("max_depth", 1) != 1
        ):
            raise ValueError(
                "Nondefault viewport depth range needs an adapter"
            )
        if viewport.get("x", 0) or min(
            viewport.get("y", 0), viewport.get("y", 0) + height
        ):
            raise ValueError(
                "Offset captured viewports need a viewport adapter"
            )
        draw["viewport_width"] = int(width)
        draw["viewport_height"] = int(abs(height))
        draw["clip_y_sign"] = -1 if height > 0 else 1
        draw["front_face_flip"] = False
        draw["screen_scale"] = [1, 1, 0, 0]
        scissor = (pipe.get("scissors") or [None])[0]
        if scissor and any(
            scissor.get(k) != viewport.get(k)
            for k in ("x", "y", "width", "height")
        ):
            raise ValueError("Captured scissor clipping needs an adapter")
        # HDRP adjusts raster winding for render textures. Preserve captured
        # index order, including the original provoking vertex.
        if "front_ccw" not in pipe:
            raise ValueError("Captured front-face orientation is unavailable")
        if not pipe["front_ccw"]:
            path = project / draw["mesh"]
            mesh = json.loads(path.read_text(encoding="utf-8"))
            indices = mesh["triangles"]
            for offset in range(0, len(indices), 3):
                indices[offset + 1], indices[offset + 2] = (
                    indices[offset + 2],
                    indices[offset + 1],
                )
            path.write_text(json.dumps(mesh), encoding="utf-8")
        draw["coordinate_adapter"] = "Vulkan framebuffer to Unity D3D11"
        colors = pipe.get("render_targets", [])
        depth = pipe.get("depth_target")
        attachments = [(a, False) for a in colors]
        if depth and _id(depth) != "0":
            attachments.append((depth, True))
        if not attachments:
            raise ValueError("Captured draw has no render attachments")
        render_pass = draw.get("original_render_pass") or {}
        pass_id = render_pass.get("begin_event_id")
        pass_attachments = {
            _id(a): a for a in render_pass.get("attachments", [])
        }
        for attachment, is_depth in attachments:
            rid = _id(attachment)
            if attachment.get("first_mip", 0) or attachment.get(
                "first_slice", 0
            ):
                raise ValueError(
                    "Render-target mip/slice views need an adapter"
                )
            if rid in targets:
                clear = pass_attachments.get(rid, {})
                if pass_id not in seen_passes:
                    if is_depth and (
                        clear.get("applies_clear")
                        or clear.get("applies_stencil_clear")
                    ):
                        draw["clear_depth_target"] = {
                            "resource_id": rid,
                            "clear_depth": bool(clear.get("applies_clear")),
                            "depth": clear.get("clear_depth", 0),
                            "clear_stencil": bool(
                                clear.get("applies_stencil_clear")
                            ),
                            "stencil": clear.get("clear_stencil", 0),
                        }
                    elif not is_depth and clear.get("applies_clear"):
                        draw.setdefault("clear_color_targets", []).append(
                            {
                                "resource_id": rid,
                                "color": clear["clear_color"],
                            }
                        )
                continue
            tw, th = dimensions.get(rid, (int(width), int(abs(height))))
            target = {
                "id": rid,
                "width": tw,
                "height": th,
                "format": attachment["format"],
                "depth": is_depth,
                "clear_depth": 0
                if pipe.get("depth_func", "").startswith("Greater")
                else 1,
                "clear_stencil": 0,
                "clear_color": [0, 0, 0, 0],
            }
            targets[rid] = target
            clear = pass_attachments.get(rid, {})
            if clear.get("applies_clear"):
                if is_depth:
                    target["clear_depth"] = clear["clear_depth"]
                else:
                    target["clear_color"] = clear["clear_color"]
            else:
                defaults.append(rid)
            if is_depth and clear.get("applies_stencil_clear"):
                target["clear_stencil"] = clear["clear_stencil"]
        seen_passes.add(pass_id)
        draw["color_targets"] = [_id(a) for a in colors]
        draw["depth_target"] = _id(depth) if depth else None
        for texture in draw["textures"]:
            rid = _id(texture)
            if rid in produced:
                if rid in draw["color_targets"] or rid == draw["depth_target"]:
                    raise ValueError(
                        "Original target feedback requires a copy: " + rid
                    )
                if texture.get("first_mip", 0) or texture.get(
                    "first_slice", 0
                ):
                    raise ValueError(
                        "Live texture mip/slice view needs an adapter"
                    )
                texture["dynamic"] = "resource:" + rid
            elif texture.get("requires_dynamic_binding"):
                raise ValueError(
                    "Missing native input and earlier producer: " + rid
                )
            else:
                frozen.append(
                    {"event_id": draw["event_id"], "resource_id": rid}
                )
        produced.update(draw["color_targets"])
        if depth and pipe.get("depth_write"):
            produced.add(_id(depth))
        if colors:
            last_color = _id(colors[0])
    selected = _id(final_target) if final_target else last_color
    if selected not in targets or targets[selected]["depth"]:
        raise ValueError("final_target must name a selected color attachment")
    data.update(
        render_targets=list(targets.values()),
        final_target=selected,
        final_is_srgb=final_is_srgb,
        native_render_width=data["source_width"],
        native_render_height=data["source_height"],
        render_graph_provenance={
            "frozen_auxiliary_inputs": frozen,
            "default_clear_resources": defaults,
            "coordinate_adapter": "Vulkan to Direct3D11",
        },
    )
    warnings = [
        "Translated fragment ShaderLab/HLSL is editable and uses captured "
        "post-vertex geometry. Original source names, material inspectors "
        "and vertex animation are not recovered.",
        "Inputs whose producers are outside the selected draws remain native "
        "capture snapshots; compute producers are not replayed.",
    ]
    if defaults:
        warnings.append(
            "No captured load-clear initialization is available for "
            + str(len(defaults))
            + " attachments; they use zero color/stencil and depth chosen "
            "from the captured depth test. Prior contents are not replayed."
        )
    return warnings


def prepare_original_project(
    manifest_path,
    project,
    editor_version,
    hdrp_version,
    render_preview,
    render_scale,
    spirv_cross,
    supplementary_buffers=None,
    final_target=None,
    final_is_srgb=False,
    final_flip_y=False,
):
    from .unity_hdrp import _inside, _read_json, _write_json

    manifest = _read_json(manifest_path)
    if manifest.get("version") != 1 or not manifest.get("meshes"):
        raise ValueError("Expected a nonempty version-1 geometry manifest")
    events, vertices, triangles = set(), 0, 0
    for relative in manifest["meshes"]:
        mesh = _read_json(_inside(manifest_path.parent, relative))
        events.add(mesh["event_id"])
        vertices += len(mesh["positions"])
        triangles += sum(len(face) - 2 for face in mesh["faces"])
    camera = manifest["camera"]
    result = prepare_recovered_fragment_bundle(
        manifest_path,
        project,
        sorted(events),
        spirv_cross=spirv_cross,
        output_mode="all",
        supplementary_buffers=supplementary_buffers,
        allow_dynamic_textures=True,
        preview_width=max(1, round(camera["width"] * render_scale / 100)),
        preview_height=max(1, round(camera["height"] * render_scale / 100)),
    )
    draw_path = Path(result["draws_json"])
    data = _read_json(draw_path)
    warnings = route_original_draws(data, project, final_target, final_is_srgb)
    data["render_preview"] = render_preview
    data["final_flip_y"] = final_flip_y
    _write_json(draw_path, data)
    from .editable_geometry import (
        EditableProjectionUnavailable,
        build_editable_manifest,
    )

    try:
        editable = build_editable_manifest(project, data, camera)
    except EditableProjectionUnavailable as error:
        editable = {
            "version": 1,
            "groups": [],
            "draws": [],
            "warnings": [
                "Editable geometry is unavailable; original fragment replay "
                "is preserved: " + str(error)
            ],
        }
    _write_json(
        project / "Assets/RecoveredShaders/editable_capture.json", editable
    )
    warnings.extend(editable.get("warnings", []))
    data["build_editable_scene"] = bool(editable.get("draws"))
    if data["build_editable_scene"]:
        from .native_mesh import stage_native_meshes

        source_meshes = {}
        for relative in manifest["meshes"]:
            mesh = _read_json(_inside(manifest_path.parent, relative))
            key = "E{}_I{}".format(mesh["event_id"], mesh.get("instance", 0))
            source_meshes[key] = mesh
        native = stage_native_meshes(
            project, data, editable, camera=camera, source_meshes=source_meshes
        )
        native["render_preview"] = render_preview
        for axis in ("width", "height"):
            native["camera"][axis] = max(
                1, round(camera[axis] * render_scale / 100)
            )
        _write_json(project / "Assets/NativeCapture/manifest.json", native)
        _stage_native_materials(project, manifest_path.parent, native,
                                source_meshes, data)
        warnings.append(
            "CaptureScene uses ordinary editable meshes and realtime HDRP "
            "materials; original shader fidelity is provided separately by "
            "RecoveredCaptureScene. Unknown material semantics require a "
            "verified native material profile."
        )
    _write_json(draw_path, data)
    templates = Path(__file__).parent.parent / "unity_templates"
    support_shaders = []
    for name, folder in (
        ("RecoveredCaptureBuilder.cs", "Assets/Editor"),
        ("CaptureFragmentRuntime.cs", "Assets/Scripts"),
        ("EditableCapturedDraw.cs", "Assets/Scripts"),
        ("CapturedProjection.cs", "Assets/Scripts"),
        ("EditableCaptureSceneBuilder.cs", "Assets/Editor"),
        ("NativeCaptureSceneBuilder.cs", "Assets/Editor"),
        ("NativeMaterialFactory.cs", "Assets/Editor"),
        ("NativeMaterialPostProcess.cs", "Assets/Scripts"),
        ("NativeOriginalBloomRenderer.cs", "Assets/Scripts"),
        ("NativeSourceBloomProfile.cs", "Assets/Scripts"),
        ("NativeSourceBloomBuilder.cs", "Assets/Editor"),
        ("NativeOriginalBloomComposite.hlsl", "Assets/Shaders"),
        ("NativeBloomCopy.compute", "Assets/NativeCapture/SourceBloom"),
        ("NativeBloomPrefilter.compute", "Assets/NativeCapture/SourceBloom"),
        ("NativeBloomDownsample.compute", "Assets/NativeCapture/SourceBloom"),
        ("NativeBloomUpsample.compute", "Assets/NativeCapture/SourceBloom"),
        ("NativeTemporalHistory.cs", "Assets/Scripts"),
        ("NativeTemporalProfile.cs", "Assets/Scripts"),
        ("NativeTemporalGeometryPass.cs", "Assets/Scripts"),
        ("NativeGeometryBufferPass.cs", "Assets/Scripts"),
        ("NativeTemporalPostProcess.cs", "Assets/Scripts"),
        ("NativeTemporalJitter.cs", "Assets/Scripts"),
        ("NativeTemporalBuilder.cs", "Assets/Editor"),
        ("NativeTemporalDilation.shader", "Assets/Shaders"),
        ("NativeTemporalMaskReduce.shader", "Assets/Shaders"),
        ("NativeTemporalResolve.shader", "Assets/Shaders"),
        ("NativeTemporalSeed.shader", "Assets/Shaders"),
        ("NativeTemporalCopy.shader", "Assets/Shaders"),
        ("NativeSunShadowPass.cs", "Assets/Scripts"),
        ("NativeSunCasterLink.cs", "Assets/Scripts"),
        ("NativeSunShadowBuilder.cs", "Assets/Editor"),
        ("NativeSunCasterBuilder.cs", "Assets/Editor"),
        ("NativeSurfaceDepthPass.cs", "Assets/Scripts"),
        ("NativeCameraColorPass.cs", "Assets/Scripts"),
        ("NativeActorShadowPass.cs", "Assets/Scripts"),
        ("NativeActorShadowBuilder.cs", "Assets/Editor"),
        ("NativeSurfaceModifierPass.cs", "Assets/Scripts"),
        ("NativeSurfaceModifierBuilder.cs", "Assets/Editor"),
        ("NativeActorShadow.hlsl", "Assets/Shaders"),
        ("NativeScreenAoPass.cs", "Assets/Scripts"),
        ("NativeScreenAoProfile.cs", "Assets/Scripts"),
        ("NativeScreenAoSampling.hlsl", "Assets/Shaders"),
        ("NativeSurfaceDepth.shader", "Assets/Shaders"),
        ("CaptureSceneBuilder.cs", "Assets/Editor"),
        ("RecoveredFinalCopy.shader", "Assets/RecoveredShaders"),
    ):
        destination = project / folder
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copy2(templates / name, destination / name)
        if name.endswith(".shader"):
            support_shaders.append((destination / name).relative_to(project))
    from .native_picking import adapt_project_native_picking

    adapt_project_native_picking(project, support_shaders)
    _write_json(
        project / "Packages/manifest.json",
        {
            "dependencies": {
                "com.unity.render-pipelines.high-definition": hdrp_version
            }
        },
    )
    settings = project / "ProjectSettings"
    settings.mkdir(parents=True, exist_ok=True)
    (settings / "ProjectVersion.txt").write_text(
        "m_EditorVersion: " + editor_version + "\n", encoding="utf-8"
    )
    _write_json(
        project / "provenance.json",
        {
            "source_manifest": str(manifest_path),
            "source_capture": manifest.get("source_capture"),
            "material_mode": "original_fragment",
            "warnings": warnings,
            "render_graph": data["render_graph_provenance"],
        },
    )
    (project / ".gitignore").write_text(
        "/Library/\n/Temp/\n/Logs/\n/Obj/\n/UserSettings/\n", encoding="utf-8"
    )
    return dict(
        mesh_count=result["draw_count"],
        vertex_count=vertices,
        triangle_count=triangles,
        shader_count=result["shader_count"],
        warnings=warnings,
    )


def _stage_native_materials(project, source, native, source_meshes, drawdata):
    """Transfer declared material roles without assigning anonymous bindings.

    More detailed, verified profiles can replace this import-time library.
    Texture copies are private to the editable scene; the replay's native
    texture assets and their import settings are never modified.
    """
    import hashlib

    from .unity_hdrp import _inside, _write_json, material_spec

    draws = {
        "E{}_I{}".format(d["event_id"], d.get("instance", 0)): d
        for d in drawdata["draws"]
    }
    entries = []
    for reference in native["meshes"]:
        key = reference["material_key"]
        source_material = source_meshes.get(key, {}).get("material", {})
        values = material_spec(source_material)
        state = draws[key].get("original_render_state", {})
        spec = {
            "name": key,
            "renderer_category": "surface",
            "renderer_enabled": True,
            "fidelity": "declared_material_roles_hdrp_approximation",
            "provenance": (
                "Declared source bundle material roles; current HDRP lighting"
            ),
            "base_color": values["base_color"],
            "metallic": values["metallic"],
            "roughness": values["roughness"],
            "alpha_clip": values["alpha_clip"],
            "alpha_from_base": values["alpha_clip"],
            "alpha_cutoff": values["alpha_cutoff"],
            "double_sided": str(state.get("cull_mode", "")).endswith("NoCull"),
            "cull_mode": {
                "NoCull": "off", "Front": "front", "Back": "back",
            }.get(str(state.get("cull_mode", "")).rsplit(".", 1)[-1], "back"),
        }
        blends = state.get("blends", [])
        if blends and blends[0].get("enabled"):
            color_blend = blends[0].get("color", {})
            factors = (color_blend.get("source"),
                       color_blend.get("destination"))
            supported = {
                ("SrcAlpha", "InvSrcAlpha"): "alpha",
                ("One", "InvSrcAlpha"): "premultiply",
                ("SrcAlpha", "One"): "additive",
                ("One", "One"): "additive",
            }
            spec["surface"] = "transparent"
            if color_blend.get("operation") == "Add" and factors in supported:
                spec["blend"] = supported[factors]
                spec["provenance"] += (
                    "; captured color blending preserved; shader opacity "
                    "still requires declared material semantics"
                )
            else:
                spec["blend"] = "alpha"
                spec["fidelity"] = "unsupported_blend_hdrp_approximation"
                spec["provenance"] += (
                    "; unsupported blend operation approximated as alpha"
                )
        # A blending state does not prove that texture alpha is opacity.
        # Respect declared roles without interpreting anonymous channels.
        if "alpha_from_base" in source_material:
            spec["alpha_from_base"] = bool(source_material["alpha_from_base"])
        if "opacity" in source_material:
            spec["opacity"] = float(source_material["opacity"])
        for role, field in (("base_color_texture", "base_map"),
                            ("normal_texture", "normal_map")):
            if not source_material.get(role):
                continue
            texture = _inside(source, source_material[role])
            digest = hashlib.sha256(
                (str(texture) + role).encode()
            ).hexdigest()[:24]
            asset = (
                "Assets/NativeCapture/InputTextures/" + digest
                + texture.suffix.lower()
            )
            destination = project / asset
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                shutil.copy2(texture, destination)
            spec[field] = {
                "asset": asset, "uv": 0,
                "source_color_space_explicit": True,
                "source_is_srgb": (
                    values["base_color_srgb"] if field == "base_map" else False
                ),
            }
            if field == "normal_map":
                spec["normal_encoding"] = (
                    "rg" if "BC5" in values["normal_format"].upper() else "rgb"
                )
        entries.append({"key": key, "spec": spec})
    _write_json(project / "Assets/NativeCapture/materials.json", {
        "version": 1, "entries": entries,
    })
