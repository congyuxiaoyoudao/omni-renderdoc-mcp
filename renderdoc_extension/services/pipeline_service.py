"""Inspect event-local shader bindings and graphics pipeline state."""

import math
import struct

import renderdoc as rd

from .resource_service import texture_description

STAGES = (
    "Vertex",
    "Hull",
    "Domain",
    "Geometry",
    "Pixel",
    "Compute",
    "Task",
    "Mesh",
)


def shader_stage(name):
    aliases = {"fragment": "pixel", "amplification": "task"}
    name = aliases.get(name.casefold(), name.casefold())
    for label in STAGES:
        if name == label.casefold():
            return getattr(rd.ShaderStage, label)
    raise ValueError("Unknown shader stage: {}".format(name))


def active_shaders(pipe):
    names = (
        STAGES if pipe.IsCaptureVK() or pipe.IsCaptureD3D12() else STAGES[:6]
    )
    for label in names:
        stage = getattr(rd.ShaderStage, label)
        shader = pipe.GetShader(stage)
        if shader != rd.ResourceId.Null():
            yield stage, shader


def finite_value(value):
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def variable_info(variable):
    fields = {
        "Float": "f32v",
        "Double": "f64v",
        "Half": "f16v",
        "SInt": "s32v",
        "UInt": "u32v",
        "SLong": "s64v",
        "ULong": "u64v",
        "SByte": "s8v",
        "UByte": "u8v",
        "SShort": "s16v",
        "UShort": "u16v",
        "Bool": "u32v",
    }
    result = {
        "name": variable.name,
        "type": str(variable.type),
        "rows": variable.rows,
        "columns": variable.columns,
    }
    for name, member in fields.items():
        if variable.type == getattr(rd.VarType, name):
            size = variable.rows * variable.columns
            values = getattr(variable.value, member)[:size]
            if name == "Half":
                values = [
                    struct.unpack("<e", struct.pack("<H", v))[0]
                    for v in values
                ]
            result["value"] = [finite_value(v) for v in values]
            break
    if variable.members:
        result["members"] = [variable_info(v) for v in variable.members]
    return result


def constant_values(controller, pipe, stage, reflection, pipeline):
    rows = []
    for index, block in enumerate(reflection.constantBlocks):
        desc = pipe.GetConstantBlock(stage, index, 0).descriptor
        variables = []
        if not block.bufferBacked or desc.resource != rd.ResourceId.Null():
            data = controller.GetCBufferVariableContents(
                pipeline,
                reflection.resourceId,
                stage,
                reflection.entryPoint,
                index,
                desc.resource,
                desc.byteOffset,
                desc.byteSize,
            )
            variables = [variable_info(v) for v in data]
        rows.append(
            {
                "name": block.name,
                "slot": index,
                "size": block.byteSize,
                "variables": variables,
            }
        )
    return rows


def get_shader_info(controller, event_id, stage):
    enum = shader_stage(stage)
    controller.SetFrameEvent(event_id, False)
    pipe = controller.GetPipelineState()
    shader = pipe.GetShader(enum)
    if shader == rd.ResourceId.Null():
        raise ValueError(
            "No {} shader bound at event {}".format(stage, event_id)
        )
    result = {
        "resource_id": str(shader),
        "stage": stage,
        "entry_point": pipe.GetShaderEntryPoint(enum),
    }
    reflection = pipe.GetShaderReflection(enum)
    if reflection is None:
        return result
    pipeline = (
        pipe.GetComputePipelineObject()
        if enum == rd.ShaderStage.Compute
        else pipe.GetGraphicsPipelineObject()
    )
    targets = controller.GetDisassemblyTargets(
        pipeline != rd.ResourceId.Null()
    )
    if targets:
        result["disassembly"] = controller.DisassembleShader(
            pipeline,
            reflection,
            targets[0],
        )
    result["constant_buffers"] = constant_values(
        controller,
        pipe,
        enum,
        reflection,
        pipeline,
    )
    result["resources"] = [
        {
            "name": r.name,
            "type": str(r.descriptorType),
            "binding": r.fixedBindNumber,
            "access": access,
        }
        for access, resources in (
            ("ReadOnly", reflection.readOnlyResources),
            ("ReadWrite", reflection.readWriteResources),
        )
        for r in resources
    ]
    return result


def binding_info(used, reflected):
    index = used.access.index
    result = {
        "slot": index,
        "name": "",
        "array_element": used.access.arrayElement,
    }
    if 0 <= index < len(reflected):
        binding = reflected[index]
        result.update(name=binding.name, binding=binding.fixedBindNumber)
    return result


def views(used_resources, reflected, descriptions, names, writable):
    for used in used_resources:
        desc = used.descriptor
        if desc.resource == rd.ResourceId.Null():
            continue
        rid = str(desc.resource)
        item = dict(
            descriptions.get(rid, ()),
            resource_id=rid,
            resource_name=names.get(rid, ""),
        )
        item.update(binding_info(used, reflected))
        if item.get("type") == "buffer":
            item.update(byte_offset=desc.byteOffset, byte_size=desc.byteSize)
            if writable and desc.elementByteSize:
                item.update(
                    first_element=desc.byteOffset // desc.elementByteSize,
                    num_elements=desc.byteSize // desc.elementByteSize,
                )
        else:
            item.update(
                first_mip=desc.firstMip,
                num_mips=desc.numMips,
                first_slice=desc.firstSlice,
                num_slices=desc.numSlices,
            )
        yield item


def sampler_info(used, reflected):
    item = binding_info(used, reflected)
    sampler = used.sampler
    color = sampler.borderColorValue
    field = {rd.CompType.UInt: "uintValue", rd.CompType.SInt: "intValue"}
    values = getattr(color, field.get(sampler.borderColorType, "floatValue"))
    item.update(
        address_u=str(sampler.addressU),
        address_v=str(sampler.addressV),
        address_w=str(sampler.addressW),
        filter=str(sampler.filter.filter),
        min_filter=str(sampler.filter.minify),
        mag_filter=str(sampler.filter.magnify),
        mip_filter=str(sampler.filter.mip),
        max_anisotropy=sampler.maxAnisotropy,
        min_lod=finite_value(sampler.minLOD),
        max_lod=finite_value(sampler.maxLOD),
        mip_lod_bias=finite_value(sampler.mipBias),
        compare_function=str(sampler.compareFunction),
        border_color=[finite_value(v) for v in values],
    )
    return item


def block_description(block):
    return {
        "name": block.name,
        "slot": block.fixedBindNumber,
        "byte_size": block.byteSize,
        "variable_count": len(block.variables),
        "variables": [
            {"name": v.name, "type": v.type.name, "byte_offset": v.byteOffset}
            for v in block.variables
        ],
    }


def viewports(controller, pipe):
    if pipe.IsCaptureVK():
        state = controller.GetVulkanPipelineState().viewportScissor
        return [v.vp for v in state.viewportScissors]
    if pipe.IsCaptureD3D11():
        return controller.GetD3D11PipelineState().rasterizer.viewports
    if pipe.IsCaptureD3D12():
        return controller.GetD3D12PipelineState().rasterizer.viewports
    return controller.GetGLPipelineState().rasterizer.viewports


def buffer_binding(buffer):
    return {
        "resource_id": str(buffer.resourceId),
        "byte_offset": buffer.byteOffset,
        "byte_size": buffer.byteSize,
        "byte_stride": buffer.byteStride,
    }


def input_assembly(pipe):
    index = pipe.GetIBuffer()
    return {
        "topology": str(pipe.GetPrimitiveTopology()),
        "vertex_buffers": [
            dict(buffer_binding(buffer), slot=slot)
            for slot, buffer in enumerate(pipe.GetVBuffers())
            if buffer.resourceId != rd.ResourceId.Null()
        ],
        "index_buffer": buffer_binding(index)
        if index.resourceId != rd.ResourceId.Null()
        else None,
    }


def get_pipeline_state(controller, names, event_id):
    controller.SetFrameEvent(event_id, False)
    pipe = controller.GetPipelineState()
    descriptions = {
        str(t.resourceId): dict(texture_description(t), type="texture")
        for t in controller.GetTextures()
    }
    descriptions.update(
        {
            str(b.resourceId): {"type": "buffer", "length": b.length}
            for b in controller.GetBuffers()
        }
    )
    shaders = {}
    for stage, shader in active_shaders(pipe):
        ref = pipe.GetShaderReflection(stage)
        shaders[str(stage)] = {
            "resource_id": str(shader),
            "entry_point": pipe.GetShaderEntryPoint(stage),
            "resources": list(
                views(
                    pipe.GetReadOnlyResources(stage),
                    ref.readOnlyResources if ref else (),
                    descriptions,
                    names,
                    False,
                )
            ),
            "uavs": list(
                views(
                    pipe.GetReadWriteResources(stage),
                    ref.readWriteResources if ref else (),
                    descriptions,
                    names,
                    True,
                )
            ),
            "samplers": [
                sampler_info(s, ref.samplers if ref else ())
                for s in pipe.GetSamplers(stage)
            ],
            "constant_buffers": [
                block_description(cb) for cb in ref.constantBlocks
            ]
            if ref
            else [],
        }
    data = {
        "api": str(controller.GetAPIProperties().pipelineType),
        "event_id": event_id,
        "shaders": shaders,
        "input_assembly": input_assembly(pipe),
        "viewports": [
            {
                "x": v.x,
                "y": v.y,
                "width": v.width,
                "height": v.height,
                "min_depth": v.minDepth,
                "max_depth": v.maxDepth,
            }
            for v in viewports(controller, pipe)
        ],
        "render_targets": [
            {"index": i, "resource_id": str(target.resource)}
            for i, target in enumerate(pipe.GetOutputTargets())
            if target.resource != rd.ResourceId.Null()
        ],
    }
    depth = pipe.GetDepthTarget().resource
    if depth != rd.ResourceId.Null():
        data["depth_target"] = str(depth)
    return data
