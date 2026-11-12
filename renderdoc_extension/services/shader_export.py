"""Collect shader modules and their event-local state in a RenderDoc pass."""

import hashlib
import json
import sys
import tempfile
from pathlib import Path

import renderdoc as rd

from .action_service import flags, walk
from .mesh_formats import enum_name
from .pipeline_service import (
    STAGES,
    binding_info,
    finite_value,
    input_assembly,
    sampler_info,
    viewports,
    views,
)
from .resource_service import texture_description


def _kind(action):
    for name, kind in (
        ("DispatchRay", "ray_dispatch"),
        ("MeshDispatch", "mesh_dispatch"),
        ("Dispatch", "dispatch"),
        ("Drawcall", "draw"),
    ):
        if action.flags & getattr(rd.ActionFlags, name, 0):
            return kind
    return None


def _find_scope(actions, eid, ancestors=()):
    markers = rd.ActionFlags.PushMarker | rd.ActionFlags.SetMarker
    for action in actions:
        is_marker = bool(action.flags & markers)
        path = ancestors + (action,) if is_marker else ancestors
        if action.eventId == eid:
            if is_marker:
                return action, path, "marker"
            if _kind(action):
                if path:
                    return path[-1], path, "enclosing_marker"
                return action, (), "single_action"
            raise ValueError(
                "Event {} is not a marker or draw/dispatch".format(eid)
            )
        found = _find_scope(action.children, eid, path)
        if found:
            return found
    return None


def select_pass(controller, pass_event_id):
    """Select the marker subtree, or the enclosing marker of a draw."""
    if type(pass_event_id) is not int or pass_event_id < 0:
        raise ValueError("pass_event_id must be a non-negative integer")
    found = _find_scope(controller.GetRootActions(), pass_event_id)
    if not found:
        raise ValueError("No action at event {}".format(pass_event_id))
    root, path, selection = found
    structured = controller.GetStructuredFile()
    scope = {
        "event_id": root.eventId,
        "name": root.GetName(structured),
        "selection": selection,
        "requested_event_id": pass_event_id,
        "markers": [
            {"event_id": a.eventId, "name": a.GetName(structured)}
            for a in path
        ],
    }
    actions = [
        a
        for a in walk([root])
        if _kind(a) and not a.flags & rd.ActionFlags.MultiAction
    ]
    return scope, actions


def _signature(s):
    return {
        "name": s.varName,
        "semantic": s.semanticName,
        "semantic_index": s.semanticIndex,
        "semantic_indexed": s.semanticIdxName,
        "register": s.regIndex,
        "system_value": enum_name(s.systemValue),
        "type": enum_name(s.varType),
        "components": s.compCount,
        "register_mask": s.regChannelMask,
        "used_mask": s.channelUsedMask,
        "stream": s.stream,
        "per_primitive": s.perPrimitiveRate,
    }


def _type(t):
    return {
        "name": t.name,
        "base_type": enum_name(t.baseType),
        "rows": t.rows,
        "columns": t.columns,
        "elements": t.elements,
        "array_stride": t.arrayByteStride,
        "matrix_stride": t.matrixByteStride,
        "flags": str(t.flags),
        "pointer_type_id": t.pointerTypeID,
        "members": [_constant(c) for c in t.members],
    }


def _constant(c):
    return {
        "name": c.name,
        "offset": c.byteOffset,
        "type": _type(c.type),
        "bit_offset": c.bitFieldOffset,
        "bit_size": c.bitFieldSize,
        "default_bits": c.defaultValue,
    }


def _binding(b):
    return {
        "name": b.name,
        "binding": b.fixedBindNumber,
        "set_or_space": b.fixedBindSetOrSpace,
        "array_size": b.bindArraySize,
    }


_VK_STAGES = {
    "Vertex": "vertexShader",
    "Hull": "tessControlShader",
    "Domain": "tessEvalShader",
    "Geometry": "geometryShader",
    "Pixel": "fragmentShader",
    "Compute": "computeShader",
    "Task": "taskShader",
    "Mesh": "meshShader",
}


def _specialization(controller, label, blocks):
    block = next((b for b in blocks if b.compileConstants), None)
    if block is None:
        return None
    vk = controller.GetVulkanPipelineState()
    shader = getattr(vk, _VK_STAGES[label])
    ids = list(shader.specializationIds)
    if not ids:
        return None
    data = bytes(shader.specializationData)
    changed = []
    if len(ids) == len(block.variables):
        for spec_id, variable in zip(ids, block.variables):
            offset = variable.byteOffset
            current = data[offset : offset + 8]
            default = variable.defaultValue.to_bytes(8, sys.byteorder)
            if current != default:
                changed.append(spec_id)
        if not changed:
            return None
    return {"ids": ids, "data_hex": data.hex(), "nondefault_ids": changed}


def _resource(b):
    return dict(
        _binding(b),
        descriptor_type=enum_name(b.descriptorType),
        texture_type=enum_name(b.textureType),
        is_texture=b.isTexture,
        has_sampler=b.hasSampler,
        input_attachment=b.isInputAttachment,
        element_type=_type(b.variableType),
    )


def _reflection(ref):
    return {
        "input_signature": [_signature(s) for s in ref.inputSignature],
        "output_signature": [_signature(s) for s in ref.outputSignature],
        "read_only_resources": [_resource(r) for r in ref.readOnlyResources],
        "read_write_resources": [_resource(r) for r in ref.readWriteResources],
        "samplers": [_binding(s) for s in ref.samplers],
        "constant_blocks": [
            dict(
                _binding(b),
                byte_size=b.byteSize,
                buffer_backed=b.bufferBacked,
                inline=b.inlineDataBytes,
                compile_constants=b.compileConstants,
                variables=[_constant(c) for c in b.variables],
            )
            for b in ref.constantBlocks
        ],
        "workgroup_size": list(ref.dispatchThreadsDimension),
        "output_topology": enum_name(ref.outputTopology),
        "pointer_types": [_type(t) for t in ref.pointerTypes],
    }


_SUFFIXES = {
    "SPIRV": ".spv",
    "OpenGLSPIRV": ".spv",
    "DXBC": ".dxbc",
    "DXIL": ".dxil",
    "HLSL": ".hlsl",
    "GLSL": ".glsl",
    "Slang": ".slang",
    "SPIRVAsm": ".spvasm",
    "OpenGLSPIRVAsm": ".spvasm",
}


def _module(directory, module_id, key, ref):
    resource, stage, entry = key
    row = {
        "id": module_id,
        "resource_id": resource,
        "stage": stage,
        "entry_point": entry,
        "encoding": None,
        "bytecode": None,
        "sha256": None,
        "reflection": None,
        "embedded_sources": [],
    }
    if ref is None:
        row["unavailable_reason"] = "Shader reflection unavailable"
        return row
    encoding = enum_name(ref.encoding)
    row.update(encoding=encoding, reflection=_reflection(ref))
    data = bytes(ref.rawBytes)
    if data:
        digest = hashlib.sha256(data).hexdigest()
        filename = digest + _SUFFIXES.get(encoding, ".bin")
        target = directory / filename
        if not target.exists():
            target.write_bytes(data)
        row.update(bytecode="modules/" + filename, sha256=digest)
    else:
        row["unavailable_reason"] = "Shader reflection contains no raw bytes"
    debug = ref.debugInfo
    source_encoding = enum_name(debug.encoding)
    row.update(
        source_entry_point=debug.entrySourceName,
        source_encoding=source_encoding,
    )
    for i, source in enumerate(debug.files):
        filename = "{}-source-{}{}".format(
            module_id, i, _SUFFIXES.get(source_encoding, ".txt")
        )
        (directory / filename).write_bytes(source.contents.encode("utf-8"))
        row["embedded_sources"].append(
            {
                "name": source.filename,
                "file": "modules/" + filename,
                "encoding": source_encoding,
            }
        )
    return row


def _equation(blend):
    return {
        "source": enum_name(blend.source),
        "destination": enum_name(blend.destination),
        "operation": enum_name(blend.operation),
    }


def _stencil(face):
    return {
        "fail": enum_name(face.failOperation),
        "depth_fail": enum_name(face.depthFailOperation),
        "pass": enum_name(face.passOperation),
        "func": enum_name(face.function),
        "reference": face.reference,
        "compare_mask": face.compareMask,
        "write_mask": face.writeMask,
    }


def _attachment(view):
    return {
        "resource_id": str(view.resource),
        "format": view.format.Name(),
        "first_mip": view.firstMip,
        "first_slice": view.firstSlice,
    }


def _graphics(controller, pipe):
    raster, depth = pipe.GetRasterState(), pipe.GetDepthTestState()
    dsv = pipe.GetDepthTarget()
    state = {
        "object": str(pipe.GetGraphicsPipelineObject()),
        "cull_mode": enum_name(raster.cullMode),
        "fill_mode": enum_name(raster.fillMode),
        "front_ccw": raster.frontCCW,
        "depth_test": depth.depthEnable,
        "depth_write": depth.depthWrites,
        "depth_func": enum_name(depth.depthFunction),
        "depth_bounds": depth.depthBounds,
        "min_depth_bounds": finite_value(depth.minDepthBounds),
        "max_depth_bounds": finite_value(depth.maxDepthBounds),
        "stencil_test": pipe.IsStencilTestEnabled(),
        "stencil_faces": [_stencil(s) for s in pipe.GetStencilFaces()],
        "independent_blend": pipe.IsIndependentBlendingEnabled(),
        "blend_factor": [finite_value(v) for v in pipe.GetBlendFactor()],
        "blends": [
            {
                "enabled": b.enabled,
                "write_mask": b.writeMask,
                "logic_enabled": b.logicOperationEnabled,
                "logic_op": enum_name(b.logicOperation),
                "color": _equation(b.colorBlend),
                "alpha": _equation(b.alphaBlend),
            }
            for b in pipe.GetColorBlends()
        ],
        "render_targets": [
            dict(_attachment(t), index=i)
            for i, t in enumerate(pipe.GetOutputTargets())
            if t.resource != rd.ResourceId.Null()
        ],
        "depth_target": _attachment(dsv)
        if dsv.resource != rd.ResourceId.Null()
        else None,
        "input_assembly": input_assembly(pipe),
        "vertex_inputs": [
            {
                "name": v.name,
                "slot": v.vertexBuffer,
                "byte_offset": v.byteOffset,
                "format": v.format.Name(),
                "per_instance": v.perInstance,
                "instance_rate": v.instanceRate,
                "used": v.used,
                "generic_enabled": v.genericEnabled,
            }
            for v in pipe.GetVertexInputs()
        ],
        "viewports": [],
        "scissors": [],
    }
    if getattr(pipe, "IsCaptureVK", lambda: False)():
        native = controller.GetVulkanPipelineState().rasterizer
        state["depth_bias"] = {
            "enabled": native.depthBiasEnable,
            "constant": finite_value(native.depthBias),
            "slope": finite_value(native.slopeScaledDepthBias),
            "clamp": finite_value(native.depthBiasClamp),
            "exact": getattr(native, "depthBiasExact", None),
            "representation": enum_name(native.depthBiasRepresentation)
            if hasattr(native, "depthBiasRepresentation")
            else None,
        }
    for index, v in enumerate(viewports(controller, pipe)):
        state["viewports"].append(
            {
                "x": finite_value(v.x),
                "y": finite_value(v.y),
                "width": finite_value(v.width),
                "height": finite_value(v.height),
                "min_depth": finite_value(v.minDepth),
                "max_depth": finite_value(v.maxDepth),
            }
        )
        sc = pipe.GetScissor(index)
        state["scissors"].append(
            {"x": sc.x, "y": sc.y, "width": sc.width, "height": sc.height}
        )
    return state


def _access(used):
    a = used.access
    return {
        "statically_unused": a.staticallyUnused,
        "descriptor_store": str(a.descriptorStore),
        "descriptor_offset": a.byteOffset,
        "descriptor_type": enum_name(a.type),
    }


def _bound_views(bindings, reflected, descriptions, names, writable):
    for binding in bindings:
        for row in views((binding,), reflected, descriptions, names, writable):
            row.update(
                _access(binding),
                view_id=str(binding.descriptor.view),
                view_format=binding.descriptor.format.Name(),
            )
            yield row


def _bindings(pipe, stage, ref, descriptions, names):
    used_blocks = []
    for used in pipe.GetConstantBlocks(stage):
        desc = used.descriptor
        row = binding_info(used, ref.constantBlocks if ref else ())
        row.update(
            resource_id=str(desc.resource),
            byte_offset=desc.byteOffset,
            byte_size=desc.byteSize,
            **_access(used),
        )
        used_blocks.append(row)
    return {
        "read_only_resources": list(
            _bound_views(
                pipe.GetReadOnlyResources(stage),
                ref.readOnlyResources if ref else (),
                descriptions,
                names,
                False,
            )
        ),
        "read_write_resources": list(
            _bound_views(
                pipe.GetReadWriteResources(stage),
                ref.readWriteResources if ref else (),
                descriptions,
                names,
                True,
            )
        ),
        "samplers": [
            dict(sampler_info(s, ref.samplers if ref else ()), **_access(s))
            for s in pipe.GetSamplers(stage)
        ],
        "constant_blocks": used_blocks,
    }


def _stages(kind):
    if kind == "dispatch":
        return ("Compute",)
    if kind == "ray_dispatch":
        return ()
    if kind == "mesh_dispatch":
        return ("Task", "Mesh", "Pixel")
    return STAGES[:5]


def _collect(controller, directory, scope, actions):
    modules, events = {}, []
    names = {str(r.resourceId): r.name for r in controller.GetResources()}
    resources = {
        str(t.resourceId): dict(texture_description(t), type="texture")
        for t in controller.GetTextures()
    }
    resources.update(
        {
            str(b.resourceId): {"type": "buffer", "length": b.length}
            for b in controller.GetBuffers()
        }
    )
    structured = controller.GetStructuredFile()
    for a in actions:
        controller.SetFrameEvent(a.eventId, False)
        pipe = controller.GetPipelineState()
        kind = _kind(a)
        pipeline = {}
        if kind in ("draw", "mesh_dispatch"):
            pipeline = _graphics(controller, pipe)
        elif kind == "dispatch":
            pipeline["object"] = str(pipe.GetComputePipelineObject())
        row = {
            "event_id": a.eventId,
            "name": a.GetName(structured),
            "kind": kind,
            "flags": flags(a),
            "stages": {},
            "pipeline": pipeline,
            "bindings": {},
        }
        if kind in ("draw", "mesh_dispatch"):
            row.update(
                index_count=a.numIndices,
                instance_count=a.numInstances,
                first_index=a.indexOffset,
                base_vertex=a.baseVertex,
                first_vertex=a.vertexOffset,
                first_instance=a.instanceOffset,
            )
        if kind in ("dispatch", "mesh_dispatch"):
            row["dispatch_dimensions"] = list(a.dispatchDimension)
        if kind == "ray_dispatch":
            row["unavailable_reason"] = (
                "Ray pipeline shader table unavailable through PipeState"
            )
        for label in _stages(kind):
            stage = getattr(rd.ShaderStage, label)
            rid = pipe.GetShader(stage)
            if rid == rd.ResourceId.Null():
                continue
            ref = pipe.GetShaderReflection(stage)
            entry = pipe.GetShaderEntryPoint(stage) or (
                ref.entryPoint if ref else ""
            )
            key = (str(rid), label, entry)
            if key not in modules:
                module_id = "m{:04d}".format(len(modules) + 1)
                modules[key] = _module(directory, module_id, key, ref)
            row["stages"][label] = modules[key]["id"]
            row["bindings"][label] = _bindings(
                pipe, stage, ref, resources, names
            )
            if ref and pipe.IsCaptureVK():
                spec = _specialization(controller, label, ref.constantBlocks)
                if spec:
                    row["bindings"][label]["specialization"] = spec
        events.append(row)
    return {
        "api": enum_name(controller.GetAPIProperties().pipelineType),
        "scope": scope,
        "modules": list(modules.values()),
        "events": events,
    }


def stage_pass_shaders(
    controller, output_directory, pass_event_id, source_capture=None
):
    """Write a self-contained pass manifest and the original shader bytes."""
    if (
        not isinstance(output_directory, str)
        or not Path(output_directory).is_absolute()
    ):
        raise ValueError("output_directory must be an absolute path")
    scope, actions = select_pass(controller, pass_event_id)
    if not actions:
        raise ValueError("The selected marker contains no draws or dispatches")
    root = Path(output_directory)
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(root), prefix=".shader-") as temp:
        staging = Path(temp) / "pass"
        directory = staging / "modules"
        directory.mkdir(parents=True)
        data = _collect(controller, directory, scope, actions)
        if source_capture:
            data["source_capture"] = source_capture
        (staging / "manifest.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        destination = root / ("pass-shaders-" + Path(temp).name[8:])
        staging.rename(destination)
    return {
        "directory": str(destination),
        "manifest": str(destination / "manifest.json"),
        "event_count": len(actions),
        "module_count": len(data["modules"]),
        "pass_event_id": scope["event_id"],
        "pass_name": scope["name"],
    }
