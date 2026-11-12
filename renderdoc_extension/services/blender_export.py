"""Export post-transform geometry and sampled material images for Blender.

The bundle deliberately keeps game shader approximation separate from the
captured camera projection.  Positions are posed camera-space coordinates;
this is not a claim to recover original rigs or engine shader source.
"""

import gzip
import hashlib
import json
import math
import tempfile
from pathlib import Path

from .blender_skin import capture_rigged_source
from .mesh import _lookup
from .mesh_formats import MeshError, enum_name
from .mesh_outputs import post_mesh
from .mesh_skin import skinning_summary
from .mesh_textures import MeshTextures, _binding_info, _write_bindings


def _walk(actions):
    for action in actions:
        yield action
        yield from _walk(action.children)


def _projection(controller, rd, pipeline, supplied):
    camera = dict(supplied or {})
    viewport = pipeline.GetViewport(0)
    camera.setdefault("width", int(abs(viewport.width)))
    camera.setdefault("height", int(abs(viewport.height)))
    vulkan = getattr(pipeline, "IsCaptureVK", lambda: False)()
    camera.setdefault(
        "clip_y_sign", -1 if vulkan and viewport.height >= 0 else 1
    )
    if "p00" not in camera or "p11" not in camera:
        stage = rd.ShaderStage.Vertex
        reflection = pipeline.GetShaderReflection(stage)
        if reflection is None:
            raise MeshError("Supply camera p00/p11 for this shader")
        candidates = []
        for index, block in enumerate(reflection.constantBlocks):
            if not 192 <= block.byteSize <= 4096:
                continue
            desc = pipeline.GetConstantBlock(stage, index, 0).descriptor
            variables = controller.GetCBufferVariableContents(
                pipeline.GetGraphicsPipelineObject(),
                reflection.resourceId,
                stage,
                reflection.entryPoint,
                index,
                desc.resource,
                desc.byteOffset,
                desc.byteSize,
            )
            for variable in variables:
                if variable.rows != 4 or variable.columns != 4:
                    continue
                values = list(variable.value.f32v[:16])
                # Recognise a conventional perspective matrix, allowing
                # transposed storage and projection jitter in row/column 2.
                if (
                    abs(values[15]) < 1e-7
                    and 0.05 < abs(values[0]) < 100
                    and 0.05 < abs(values[5]) < 100
                    and min(abs(abs(values[11]) - 1), abs(abs(values[14]) - 1))
                    < 1e-5
                    and max(abs(values[x]) for x in (1, 3, 4, 7)) < 1e-5
                ):
                    candidates.append((block.name, variable.name, values))
        if not candidates:
            raise MeshError("No perspective projection found; supply camera")
        block_name, variable_name, values = candidates[0]
        camera.setdefault("p00", abs(values[0]))
        camera.setdefault("p11", abs(values[5]))
        camera["source_projection"] = values
        camera["projection_source"] = {
            "block": block_name,
            "variable": variable_name,
            "method": "perspective matrix shape inference",
        }
    for key in ("p00", "p11"):
        value = camera[key]
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("camera {} must be finite".format(key))
        if value <= 0:
            raise ValueError("camera {} must be positive".format(key))
    if camera["clip_y_sign"] not in (-1, 1):
        raise ValueError("camera clip_y_sign must be -1 or 1")
    if min(camera["width"], camera["height"]) <= 0:
        raise ValueError("Camera resolution must be positive")
    camera["fov_y"] = 2 * math.atan(1 / camera["p11"])
    camera.setdefault("near", 0.01)
    camera.setdefault("far", 10000.0)
    camera["coordinate_space"] = "camera"
    camera["geometry_method"] = "post-VS clip XY scaled by projection; Z=-W"
    return camera


def _geometry(
    position, attributes, faces, camera, uv_name=None, keep_attributes=False
):
    clips = position["values"]
    if not clips or len(clips[0]) != 4:
        raise MeshError("Post-transform position must have four components")
    if uv_name is None:
        uv = next(
            (
                a
                for a in attributes
                if a["decoded"] and a["values"] and len(a["values"][0]) == 2
            ),
            None,
        )
    else:
        uv = next((a for a in attributes if a["name"] == uv_name), None)
        if uv is None:
            raise MeshError("Requested UV output is absent: " + uv_name)
    valid = {
        i
        for i, p in enumerate(clips)
        if all(math.isfinite(v) for v in p) and abs(p[3]) > 1e-8
    }
    kept_faces = [
        f for f in faces if len(f) >= 3 and all(i in valid for i in f)
    ]
    used = sorted({i for f in kept_faces for i in f})
    if not kept_faces:
        raise MeshError("No finite triangle primitives")
    remap = {old: new for new, old in enumerate(used)}
    px, py, sy = camera["p00"], camera["p11"], camera["clip_y_sign"]
    result = {
        "positions": [
            [clips[i][0] / px, sy * clips[i][1] / py, -clips[i][3]]
            for i in used
        ],
        "faces": [[remap[i] for i in f] for f in kept_faces],
        "projected_uvs": [
            [
                0.5 + 0.5 * clips[i][0] / clips[i][3],
                0.5 + 0.5 * sy * clips[i][1] / clips[i][3],
            ]
            for i in used
        ],
        "omitted_primitive_count": len(faces) - len(kept_faces),
        "uv_attribute": uv["name"] if uv else None,
        "coordinate_space": "camera",
    }
    if uv:
        # RenderDoc PNG rows follow texture API origin, Blender UV V points up.
        result["uvs"] = [
            [uv["values"][i][0], 1 - uv["values"][i][1]] for i in used
        ]
    if keep_attributes:
        result["source_vertex_indices"] = used
        result["clip_positions"] = [clips[i] for i in used]
        result["attributes"] = []
        for attribute in attributes:
            entry = {
                k: v
                for k, v in attribute.items()
                if k not in ("values", "raw")
            }
            if attribute.get("decoded") and len(
                attribute.get("values", [])
            ) == len(clips):
                values = [attribute["values"][i] for i in used]
                entry["components"] = len(values[0]) if values else 0
                if all(
                    all(math.isfinite(v) for v in value) for value in values
                ):
                    entry["values"] = values
                else:
                    entry["decoded"] = False
                    entry["reason"] = (
                        "Nonfinite varying values; raw bytes retained"
                    )
            else:
                entry["reason"] = (
                    "Varying unavailable or has incorrect vertex count"
                )
            if len(attribute.get("raw", [])) == len(clips):
                entry["raw"] = [attribute["raw"][i] for i in used]
            result["attributes"].append(entry)
    return result


def _varying_metadata(rd, pipeline, data_stage, geometry):
    stage = rd.ShaderStage.Vertex
    if data_stage == rd.MeshDataStage.MeshOut:
        stage = rd.ShaderStage.Mesh
    elif data_stage == rd.MeshDataStage.GSOut:
        stage = rd.ShaderStage.Geometry
        if pipeline.GetShaderReflection(stage) is None:
            stage = rd.ShaderStage.Domain
    reflection = pipeline.GetShaderReflection(stage)
    if reflection is None:
        return
    for signature in reflection.outputSignature:
        name = signature.varName or signature.semanticIdxName
        for attr in geometry.get("attributes", []):
            if attr["name"] != name:
                continue
            attr.update(
                location=signature.regIndex,
                register=signature.regIndex,
                semantic=signature.semanticName,
                semantic_index=signature.semanticIndex,
                semantic_indexed=signature.semanticIdxName,
                system_value=enum_name(signature.systemValue),
                type=enum_name(signature.varType),
                stream=signature.stream,
                per_primitive=signature.perPrimitiveRate,
            )


class _MaterialImages(MeshTextures):
    def _candidates(self, pipeline):
        stage = self.rd.ShaderStage.Pixel
        reflection = pipeline.GetShaderReflection(stage)
        if reflection is None:
            return []
        result = []
        for used in pipeline.GetReadOnlyResources(stage):
            if enum_name(used.access.type) not in ("Image", "ImageSampler"):
                continue
            texture = self.textures.get(used.descriptor.resource)
            if texture is None or texture.dimension != 2:
                continue
            info = _binding_info(
                used, reflection, "Pixel", pipeline.GetShader(stage)
            )
            if info["input_attachment"]:
                continue
            result.append((info, used.descriptor, texture))
        return result

    def material(self, pipeline, override):
        candidates = self._candidates(pipeline)
        result = {"roughness": 0.72, "bindings": []}
        writes = _write_bindings(pipeline, self.rd)
        cache = {}
        # Explicit binding maps take precedence over the intentionally modest
        # name/size heuristic; material interpretation remains in the manifest.
        for role in ("base_color", "normal"):
            wanted = override.get(role)
            chosen = None
            method = None
            if wanted is not None:
                matches = [
                    candidate
                    for candidate in candidates
                    if candidate[0]["binding"] == wanted.get("binding")
                    and (
                        "set" not in wanted
                        or candidate[0]["set_or_space"] == wanted["set"]
                    )
                ]
                if len(matches) == 1:
                    chosen = matches[0]
                    method = "explicit binding"
                elif len(matches) > 1:
                    result.setdefault("warnings", []).append(
                        "Requested {} binding is ambiguous: {} resources"
                        " match{}".format(
                            role,
                            len(matches),
                            "; specify set" if "set" not in wanted else "",
                        )
                    )
                    continue
            elif role == "base_color":
                named = [
                    c
                    for c in candidates
                    if any(
                        token in (c[0]["binding_name"] or "").lower()
                        for token in (
                            "basecolor",
                            "base_color",
                            "albedo",
                            "diffuse",
                        )
                    )
                ]
                if named:
                    chosen, method = named[0], "resource name inference"
                else:
                    materials = [
                        c
                        for c in candidates
                        if min(c[2].width, c[2].height) >= 64
                        and max(c[2].width, c[2].height)
                        <= 8 * min(c[2].width, c[2].height)
                        and c[2].arraysize == 1
                    ]
                    if materials:
                        chosen = max(
                            materials,
                            key=lambda c: (
                                "SRGB" in c[2].format.Name(),
                                c[2].width * c[2].height,
                                c[0]["binding"] or 0,
                            ),
                        )
                        method = "image format/size heuristic"
            if chosen is None:
                if wanted is not None:
                    result.setdefault("warnings", []).append(
                        "Requested {} binding unavailable".format(role)
                    )
                continue
            info, descriptor, texture = chosen
            self._images(
                info,
                descriptor,
                texture,
                cache,
                writes.get(descriptor.resource, ()),
            )
            info.update(role=role, selection_method=method)
            result["bindings"].append(info)
            if info["images"]:
                result[role + "_texture"] = info["images"][0]["file"]
        result.update(
            {
                k: override[k]
                for k in ("roughness", "metallic", "alpha_clip")
                if k in override
            }
        )
        return result


def _save_reference(controller, rd, folder, event_id, resource_id):
    if event_id is not None:
        controller.SetFrameEvent(event_id, True)
    texture = next(
        (
            t
            for t in controller.GetTextures()
            if str(t.resourceId) == str(resource_id)
        ),
        None,
    )
    if texture is None:
        raise ValueError("Reference texture is absent: " + str(resource_id))
    save = rd.TextureSave()
    save.resourceId = texture.resourceId
    save.destType = rd.FileType.PNG
    # Swapchain alpha can be undefined or zero despite valid displayed RGB.
    # The reference is an opaque color image for camera projection.
    save.alpha = rd.AlphaMapping.Discard
    save.mip = 0
    save.slice.sliceIndex = 0
    save.sample.sampleIndex = 0
    path = folder / "capture_reference.png"
    status = controller.SaveTexture(save, str(path))
    if status != rd.ResultCode.Succeeded:
        raise RuntimeError("Cannot save reference texture: " + str(status))
    return path.name


def export_blender_scene(
    controller,
    output_directory,
    event_ids,
    camera=None,
    material_bindings=None,
    character_event_ids=None,
    reference_event_id=None,
    reference_resource_id=None,
    source_capture=None,
    uv_attributes=None,
    include_shader_evidence=False,
    shader_evidence_limits=None,
    skeletons=None,
    position_attributes=None,
    shader_evidence_options=None,
):
    """Export a scene on the replay thread; caller restores the event.

    event_ids must identify visible scene/character draws.  Shadow, depth-only,
    fullscreen and UI draws should be excluded by the caller.  Captured shader
    outputs bake deformation and instancing, while original rigs are unknown.
    """
    import renderdoc as rd

    if not isinstance(output_directory, str) or not output_directory.strip():
        raise ValueError("Specify an absolute output_directory")
    root = Path(output_directory)
    if not root.is_absolute():
        raise ValueError("output_directory must be absolute")
    if (
        not isinstance(event_ids, list)
        or not event_ids
        or any(type(eid) is not int or eid < 0 for eid in event_ids)
    ):
        raise ValueError("event_ids must be a non-empty integer list")
    for value in (
        camera,
        material_bindings,
        uv_attributes,
        skeletons,
        position_attributes,
    ):
        if value is not None and not isinstance(value, dict):
            raise ValueError("Camera and per-event overrides must be objects")
    if any(
        not isinstance(value, str)
        for value in (position_attributes or {}).values()
    ):
        raise ValueError("position_attributes values must be attribute names")
    if type(include_shader_evidence) is not bool:
        raise ValueError("include_shader_evidence must be boolean")
    if shader_evidence_limits is not None and not include_shader_evidence:
        raise ValueError("shader_evidence_limits require shader evidence")
    if shader_evidence_options is not None and not include_shader_evidence:
        raise ValueError("shader_evidence_options require shader evidence")
    if include_shader_evidence:
        from .capture_evidence import CaptureEvidence, _limits, _options

        _limits(shader_evidence_limits)
        _options(shader_evidence_options)
    native_only = bool(
        (shader_evidence_options or {}).get("native_only", False)
    )
    selected = set(event_ids)
    characters = set(character_event_ids or ())
    root.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="blender-scene-", dir=str(root)))
    (folder / "meshes").mkdir()
    images = _MaterialImages(controller, rd, folder)
    evidence = (
        CaptureEvidence(
            controller,
            rd,
            folder / "evidence",
            source_capture,
            shader_evidence_limits,
            shader_evidence_options,
        )
        if include_shader_evidence
        else None
    )
    meshes, skipped, duplicates, seen = [], [], [], {}
    skin_records = {}
    actions = {a.eventId: a for a in _walk(controller.GetRootActions())}
    active_camera = None
    total_vertices, total_faces = 0, 0
    progress = folder / "progress.json"
    for sequence, eid in enumerate(sorted(selected)):
        progress.write_text(
            json.dumps(
                {
                    "event_id": eid,
                    "completed_draws": sequence,
                    "total_draws": len(selected),
                }
            )
        )
        action = actions.get(eid)
        if (
            action is None
            or not action.flags
            & (rd.ActionFlags.Drawcall | rd.ActionFlags.MeshDispatch)
            or action.flags & rd.ActionFlags.MultiAction
        ):
            skipped.append({"event_id": eid, "reason": "Not a draw"})
            continue
        controller.SetFrameEvent(eid, True)
        pipeline = controller.GetPipelineState()
        if evidence:
            try:
                evidence.capture(pipeline, action)
            except (ValueError, RuntimeError, AttributeError) as error:
                evidence.issues.append({"event_id": eid, "reason": str(error)})
                evidence.flush()
        if active_camera is None:
            try:
                active_camera = _projection(controller, rd, pipeline, camera)
            except MeshError:
                if not include_shader_evidence:
                    raise
                fallback = dict(camera or {})
                fallback.setdefault("p00", 1.0)
                fallback.setdefault("p11", 1.0)
                active_camera = _projection(controller, rd, pipeline, fallback)
                active_camera["geometry_method"] = (
                    "canonical clip embedding; "
                    "physical scale/projection unknown"
                )
                active_camera["projection_source"] = {
                    "method": "canonical fallback",
                    "physical_scale_known": False,
                }
        stage = rd.MeshDataStage.VSOut
        if action.flags & rd.ActionFlags.MeshDispatch:
            stage = rd.MeshDataStage.MeshOut
        elif enum_name(pipeline.GetPrimitiveTopology()).startswith(
            "PatchList"
        ):
            stage = rd.MeshDataStage.GSOut
        material = None
        count = max(1, action.numInstances)
        for instance in range(count):
            try:
                position, attrs, faces, _, topology = post_mesh(
                    controller, rd, pipeline, action, instance, stage
                )
                geometry = _geometry(
                    position,
                    attrs,
                    faces,
                    active_camera,
                    (uv_attributes or {}).get(str(eid)),
                    keep_attributes=include_shader_evidence,
                )
                if include_shader_evidence:
                    _varying_metadata(rd, pipeline, stage, geometry)
                digest = hashlib.sha256(
                    json.dumps(
                        [geometry["positions"], geometry["faces"]],
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                if material is None:
                    if native_only:
                        material = {"mode": "native_shader_evidence"}
                    else:
                        material = images.material(
                            pipeline,
                            (material_bindings or {}).get(str(eid), {}),
                        )
                name = "event_{:08d}_instance_{:04d}".format(eid, instance)
                geometry.update(
                    name=name,
                    event_id=eid,
                    instance=instance,
                    category="character" if eid in characters else "scene",
                    material=material,
                    topology=topology,
                    extraction_stage=enum_name(stage),
                )
                geometry.update(
                    capture_rigged_source(
                        controller,
                        rd,
                        pipeline,
                        action,
                        instance,
                        _lookup(skeletons or {}, eid, instance),
                        _lookup(position_attributes or {}, eid, instance),
                    )
                )
                path = "meshes/" + name + ".json.gz"
                with gzip.open(
                    str(folder / path), "wt", encoding="utf-8",
                    compresslevel=1 if native_only else 9,
                ) as stream:
                    if native_only:
                        stream.write(json.dumps(
                            geometry, separators=(",", ":"), allow_nan=False
                        ))
                    else:
                        json.dump(
                            geometry, stream, separators=(",", ":"),
                            allow_nan=False,
                        )
                if (
                    digest in seen
                    and not include_shader_evidence
                    and not geometry.get("rigged_source")
                    and not skin_records[meshes[seen[digest]]].get(
                        "has_rigged_source"
                    )
                ):
                    previous = seen[digest]
                    skin_records.pop(meshes[previous], None)
                    duplicates.append(
                        {
                            "omitted_file": meshes[previous],
                            "retained_file": path,
                            "reason": "same posed geometry; later draw wins",
                        }
                    )
                    meshes[previous] = path
                else:
                    seen[digest] = len(meshes)
                    meshes.append(path)
                    total_vertices += len(geometry["positions"])
                    total_faces += len(geometry["faces"])
                skin_records[path] = {
                    "skin": geometry["skin"],
                    "skin_provenance": geometry["skin_provenance"],
                    "has_rigged_source": "rigged_source" in geometry,
                }
            except (ValueError, RuntimeError) as error:
                skipped.append(
                    {
                        "event_id": eid,
                        "instance": instance,
                        "reason": str(error),
                    }
                )
    if active_camera is None:
        raise MeshError("No selected draw supplied a usable camera")
    reference = None
    if reference_resource_id is not None:
        reference = _save_reference(
            controller, rd, folder, reference_event_id, reference_resource_id
        )
    detail = {
        "version": 1,
        "source_capture": source_capture,
        "camera": active_camera,
        "resolution": [active_camera["width"], active_camera["height"]],
        "reference_image": reference,
        "meshes": meshes,
        "mesh_count": len(meshes),
        "vertex_count": total_vertices,
        "primitive_count": total_faces,
        "skipped": skipped,
        "duplicates": duplicates,
        "texture_file_count": len(images.files),
        "skinning": skinning_summary(list(skin_records.values())),
        "warnings": [
            "Scene geometry is a baked camera-local pose; available input "
            "skin is exported separately in its original coordinate space.",
            "Native materials approximate engine shaders and illumination.",
            "Projected appearance is valid only from the capture camera.",
            "Automatic UV and material selection require visual verification.",
        ],
    }
    if evidence:
        status = evidence.flush()
        detail.update(
            shader_evidence="evidence/index.json",
            shader_evidence_status=status["status"],
            draw_deduplication="disabled; each source draw/instance retained",
        )
        detail["warnings"] = [
            "Static posed scene geometry; available input-space rigs are "
            "preserved separately. No animation or world scale is inferred.",
            "Shader evidence contains original bound inputs, "
            "not a final-color projection.",
            "Draw order, depth/stencil, blend and viewports are recorded; "
            "Blender reconstruction must report its supported subset.",
            "A single scene camera is used; mixed projection/viewport "
            "draws require separate reconstruction.",
        ]
        if (
            active_camera.get("projection_source", {}).get("method")
            == "canonical fallback"
        ):
            detail["warnings"].append(active_camera["geometry_method"])
    manifest = folder / "manifest.json"
    manifest.write_text(json.dumps(detail, indent=2), encoding="utf-8")
    progress.write_text(
        json.dumps({"status": "complete", "mesh_count": len(meshes)})
    )
    result = {
        "directory": str(folder),
        "manifest": str(manifest),
        "mesh_count": len(meshes),
        "skipped_count": len(skipped),
        "duplicate_count": len(duplicates),
        "texture_file_count": len(images.files),
        "skinning": detail["skinning"],
    }
    if evidence:
        result["shader_evidence_directory"] = str(folder / "evidence")
        result["shader_evidence_status"] = status["status"]
    return result
