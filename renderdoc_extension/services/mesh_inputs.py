"""Fetch referenced indices and attributes from RenderDoc replay buffers."""

import re
import struct

from .mesh_formats import MeshError, enum_name, make_decoder


class Binding:
    def __init__(self, resource, offset, size, stride):
        self.resource = resource
        self.offset = offset
        self.size = size
        self.stride = stride

    @classmethod
    def from_pipeline(cls, bound):
        return cls(
            bound.resourceId,
            bound.byteOffset,
            bound.byteSize,
            bound.byteStride,
        )

    def read(self, controller, offset, size):
        if (
            offset < 0
            or size <= 0
            or (
                self.size not in (0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF)
                and offset + size > self.size
            )
        ):
            raise MeshError(
                "Mesh read exceeds the bound range of {}".format(self.resource)
            )
        data = controller.GetBufferData(
            self.resource, self.offset + offset, size
        )
        if len(data) != size:
            raise MeshError(
                "Short read from {} at {} ({} of {} bytes)".format(
                    self.resource, self.offset + offset, len(data), size
                )
            )
        return data


def _check_last_element(binding, lengths, end):
    available = binding.byteSize
    if binding.resourceId in lengths:
        physical = lengths[binding.resourceId] - binding.byteOffset
        if available in (0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
            available = physical
        else:
            available = min(physical, available)
    if end > available:
        raise MeshError(
            "Draw range exceeds buffer {}".format(binding.resourceId)
        )


def check_input_bounds(pipeline, action, indexed, lengths):
    if indexed:
        ib = pipeline.GetIBuffer()
        _check_last_element(
            ib,
            lengths,
            (action.indexOffset + action.numIndices) * ib.byteStride,
        )
    buffers = pipeline.GetVBuffers()
    for attr in pipeline.GetVertexInputs():
        if attr.genericEnabled or not 0 <= attr.vertexBuffer < len(buffers):
            continue
        buffer = buffers[attr.vertexBuffer]
        if buffer.resourceId not in lengths:
            continue
        if attr.perInstance:
            step = (
                (max(1, action.numInstances) - 1) // attr.instanceRate
                if attr.instanceRate
                else 0
            )
            last = action.instanceOffset + step
        elif not indexed:
            last = action.vertexOffset + action.numIndices - 1
        else:
            continue
        end = (
            last * buffer.byteStride
            + attr.byteOffset
            + attr.format.ElementSize()
        )
        _check_last_element(buffer, lengths, end)


def indices(controller, ib, count, first, base, restart):
    if not count:
        return []
    width = ib.stride
    if width not in (1, 2, 4):
        raise MeshError("Unsupported index size {}".format(width))
    if restart is not None:
        restart &= (1 << (width * 8)) - 1
    code = {1: "<B", 2: "<H", 4: "<I"}[width]
    result = []
    chunk = 1024 * 1024 // width
    for start in range(0, count, chunk):
        size = min(count - start, chunk) * width
        data = ib.read(controller, (first + start) * width, size)
        result.extend(
            None if index == restart else index + base
            for (index,) in struct.iter_unpack(code, data)
        )
    return result


def input_indices(controller, pipeline, action, indexed):
    if indexed:
        restart = (
            pipeline.GetRestartIndex() if pipeline.IsRestartEnabled() else None
        )
        return indices(
            controller,
            Binding.from_pipeline(pipeline.GetIBuffer()),
            action.numIndices,
            action.indexOffset,
            action.baseVertex,
            restart,
        )
    return range(action.vertexOffset, action.vertexOffset + action.numIndices)


def attribute_kind(name):
    parts = re.sub(r"([a-z])([A-Z])", r"\1_\2", name).upper()
    tokens = re.split(r"[^A-Z]+", parts)
    roles = {
        "POSITION": "position",
        "POS": "position",
        "VERTEXPOSITION": "position",
        "NORMAL": "normal",
        "TANGENT": "tangent",
        "BINORMAL": "binormal",
        "BITANGENT": "binormal",
        "TEXCOORD": "uv",
        "UV": "uv",
        "COLOR": "color",
        "COLOUR": "color",
    }
    return next((roles[t] for t in reversed(tokens) if t in roles), "custom")


def read_attribute(controller, name, fmt, binding, byte_offset, rows):
    size = fmt.ElementSize()
    if size <= 0 or not rows or min(rows) < 0:
        raise MeshError("Invalid vertex range or format for " + name)
    ids = sorted(set(rows)) if binding.stride else [0]
    elements = {}
    i = 0
    while i < len(ids):
        start = ids[i] * binding.stride + byte_offset
        end = i + 1
        while (
            end < len(ids)
            and (ids[end] - ids[i]) * binding.stride + size <= 1024 * 1024
            and (ids[end] - ids[end - 1]) * binding.stride <= 65536
        ):
            end += 1
        span = (ids[end - 1] - ids[i]) * binding.stride + size
        data = binding.read(controller, start, span)
        for row in ids[i:end]:
            pos = (row - ids[i]) * binding.stride
            elements[row] = bytes(data[pos : pos + size])
        i = end
    try:
        decode = make_decoder(fmt)
    except MeshError:
        decode = tuple
        kind = "custom"
        decoded = False
    else:
        kind = attribute_kind(name)
        decoded = True
    decoded_values = {row: decode(data) for row, data in elements.items()}
    hex_values = {row: data.hex() for row, data in elements.items()}
    keys = rows if binding.stride else [0] * len(rows)
    return {
        "name": name,
        "kind": kind,
        "format": fmt.Name(),
        "decoded": decoded,
        "values": [decoded_values[k] for k in keys],
        "raw": [hex_values[k] for k in keys],
    }


def _generic_attribute(attr, count):
    component = enum_name(attr.format.compType)
    key = {"SInt": "intValue", "UInt": "uintValue"}.get(
        component, "floatValue"
    )
    vector = tuple(getattr(attr.genericValue, key))[: attr.format.compCount]
    return {
        "name": attr.name,
        "kind": attribute_kind(attr.name),
        "format": attr.format.Name(),
        "values": [vector] * count,
        "decoded": True,
    }


def input_attributes(controller, rd, pipeline, action, instance, source_ids):
    buffers = pipeline.GetVBuffers()
    attributes = []
    for attr in pipeline.GetVertexInputs():
        if attr.genericEnabled:
            attributes.append(_generic_attribute(attr, len(source_ids)))
            continue
        slot = attr.vertexBuffer
        if (
            slot < 0
            or slot >= len(buffers)
            or buffers[slot].resourceId == rd.ResourceId.Null()
        ):
            if attr.used:
                raise MeshError("Missing bound input buffer for " + attr.name)
            continue
        if attr.perInstance:
            step = instance // attr.instanceRate if attr.instanceRate else 0
            rows = [action.instanceOffset + step] * len(source_ids)
        else:
            rows = source_ids
        attributes.append(
            read_attribute(
                controller,
                attr.name,
                attr.format,
                Binding.from_pipeline(buffers[slot]),
                attr.byteOffset,
                rows,
            )
        )
    return attributes
