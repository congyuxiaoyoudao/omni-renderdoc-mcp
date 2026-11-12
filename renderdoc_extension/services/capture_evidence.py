"""Frame-independent, event-local inputs for editable shader reconstruction.

All paths are relative to the evidence directory. Resources are deduplicated
by their exported content, never just by ResourceId: render targets and
buffers can change between draws. Data unavailable at an event is reported.
"""

import hashlib
import json
from pathlib import Path

from .mesh_formats import enum_name
from .mesh_textures import (
    MeshTextures,
    _binding_info,
    _format_layout,
    _image_format,
    _within,
    _write_bindings,
)

DEFAULT_LIMITS = {
    "max_total_bytes": 2 * 1024 * 1024 * 1024,
    "max_resource_bytes": 256 * 1024 * 1024,
    "max_subresources_per_view": 4096,
}


def _options(value):
    result = {"native_only": False, "decode_constants": True}
    if value is not None:
        if not isinstance(value, dict) or set(value) - set(result):
            raise ValueError("Unknown shader evidence option")
        if any(type(v) is not bool for v in value.values()):
            raise ValueError("Shader evidence options must be booleans")
        result.update(value)
    return result


def _native_texture_bytes(fmt, width, height, depth):
    """Bound known native formats; keep unknown layouts conservative."""
    kind = enum_name(fmt.type)
    block_bytes = {
        "BC1": 8, "BC4": 8, "BC2": 16, "BC3": 16,
        "BC5": 16, "BC6": 16, "BC7": 16,
    }
    if kind in block_bytes:
        blocks = ((width + 3) // 4) * ((height + 3) // 4) * depth
        return blocks * block_bytes[kind]
    if kind == "Regular" and fmt.compByteWidth > 0:
        return width * height * depth * fmt.compCount * fmt.compByteWidth
    packed = {"R11G11B10": 4, "R10G10B10A2": 4, "R9G9B9E5": 4,
              "D16S8": 4, "D24S8": 4, "D32S8": 8}
    return width * height * depth * packed.get(kind, 16)


def _limits(value):
    result = dict(DEFAULT_LIMITS)
    if value is not None:
        if not isinstance(value, dict) or set(value) - set(result):
            raise ValueError("Unknown shader evidence limit")
        for key, number in value.items():
            if type(number) is not int or number <= 0:
                raise ValueError(
                    "Shader evidence limits must be positive integers"
                )
            result[key] = number
    return result


def _annotate_values(values, declarations, parent_offset=0):
    """Attach source byte offsets without guessing anonymous variable roles."""
    by_name = {d.name: d for d in declarations}
    for value in values:
        declaration = by_name.get(value["name"])
        if declaration is None:
            continue
        offset = parent_offset + declaration.byteOffset
        value["byte_offset"] = offset
        if value.get("members"):
            _annotate_values(
                value["members"], declaration.type.members, offset
            )


def _constant_bindings(pipeline, stage, reflection):
    used = list(pipeline.GetConstantBlocks(stage))
    for index, block in enumerate(reflection.constantBlocks):
        instances = [item for item in used if item.access.index == index]
        if instances:
            for item in instances:
                yield index, block, item.access.arrayElement, item.descriptor
        else:
            # Inline/push/specialization blocks need not have buffer access.
            item = pipeline.GetConstantBlock(stage, index, 0)
            yield index, block, 0, item.descriptor


def _descriptor_spaces(bindings, reflection):
    if reflection is None:
        return
    for role, declarations in (
        ("samplers", reflection.samplers),
        ("constant_blocks", reflection.constantBlocks),
        ("read_write_resources", reflection.readWriteResources),
    ):
        for value in bindings.get(role, []):
            slot = value.get("slot")
            if type(slot) is int and 0 <= slot < len(declarations):
                value["set_or_space"] = declarations[slot].fixedBindSetOrSpace
                value["binding"] = declarations[slot].fixedBindNumber


class CaptureEvidence:
    def __init__(
        self, controller, rd, folder, source_capture=None, limits=None,
        options=None,
    ):
        self.controller, self.rd = controller, rd
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        for name in ("modules", "resources", "textures"):
            (self.folder / name).mkdir(exist_ok=True)
        self.limits = _limits(limits)
        self.options = _options(options)
        self.bytes_written = 0
        self.files = set()
        self.modules = {}
        self.events = []
        self.issues = []
        self.textures = {t.resourceId: t for t in controller.GetTextures()}
        self.buffers = {b.resourceId: b for b in controller.GetBuffers()}
        self.images = MeshTextures(controller, rd, self.folder)
        self.names = {
            str(r.resourceId): r.name for r in controller.GetResources()
        }
        self.source_capture = source_capture
        self.render_pass_metadata = None

    def _write(self, data, suffix=".bin"):
        data = bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        relative = "resources/" + digest + suffix
        if relative not in self.files:
            if self.bytes_written + len(data) > self.limits["max_total_bytes"]:
                raise ValueError("Shader evidence total-byte limit reached")
            (self.folder / relative).write_bytes(data)
            self.files.add(relative)
            self.bytes_written += len(data)
        return {"file": relative, "sha256": digest, "byte_length": len(data)}

    def _buffer(self, descriptor):
        buffer = self.buffers.get(descriptor.resource)
        if buffer is None:
            raise ValueError("Bound buffer unavailable")
        offset = descriptor.byteOffset
        size = min(
            descriptor.byteSize or buffer.length, buffer.length - offset
        )
        if offset < 0 or size < 0:
            raise ValueError("Invalid bound buffer range")
        if size > self.limits["max_resource_bytes"]:
            raise ValueError("Bound buffer exceeds per-resource byte limit")
        data = self.controller.GetBufferData(descriptor.resource, offset, size)
        if len(data) != size:
            raise ValueError("Bound buffer read is incomplete")
        return dict(self._write(data), byte_offset=offset, byte_size=size)

    def _constants(self, pipeline, stage, reflection):
        result = []
        for index, block, element, desc in _constant_bindings(
            pipeline, stage, reflection
        ):
            row = {
                "name": block.name,
                "slot": index,
                "binding": block.fixedBindNumber,
                "set_or_space": block.fixedBindSetOrSpace,
                "byte_size": block.byteSize,
                "size": block.byteSize,
                "variables": [],
                "array_element": element,
            }
            try:
                row.update(
                    resource_id=str(desc.resource), byte_offset=desc.byteOffset
                )
                if self.options["decode_constants"] or (
                    desc.resource not in self.buffers
                ):
                    from .pipeline_service import variable_info

                    values = self.controller.GetCBufferVariableContents(
                        pipeline.GetGraphicsPipelineObject(),
                        reflection.resourceId,
                        stage,
                        reflection.entryPoint,
                        index,
                        desc.resource,
                        desc.byteOffset,
                        desc.byteSize,
                    )
                    row["variables"] = [variable_info(v) for v in values]
                    _annotate_values(row["variables"], block.variables)
                else:
                    row["decoded_values_omitted"] = True
                if desc.resource in self.buffers:
                    row["raw_data"] = self._buffer(desc)
                row["status"] = "captured"
            except (RuntimeError, ValueError, AttributeError) as error:
                row.update(status="unavailable", reason=str(error))
            result.append(row)
        return result

    def _storage_buffers(self, pipeline, stage, bindings):
        """Keep post-draw bytes without claiming they are pre-draw inputs.

        Vulkan may expose read-only shader buffers through storage descriptors.
        Consumers must prove that the shader does not read modified bytes
        before using these snapshots to reconstruct the draw.
        """
        used = list(pipeline.GetReadWriteResources(stage))
        for item in bindings.get("read_write_resources", []):
            unused = item.get("statically_unused")
            item.update(
                status="unused" if unused else "unavailable",
                reason="Read/write resources may have changed in this "
                "draw; pre-draw contents are not exported",
            )
            if unused:
                continue
            match = next(
                (
                    u
                    for u in used
                    if str(u.descriptor.resource) == item.get("resource_id")
                    and u.access.index == item.get("slot")
                    and u.access.arrayElement == item.get("array_element", 0)
                ),
                None,
            )
            if match is None or match.descriptor.resource not in self.buffers:
                continue
            try:
                item.update(
                    raw_data=self._buffer(match.descriptor),
                    status="partial",
                    snapshot_timing="post_draw",
                    requires_readonly_shader_verification=True,
                )
            except (RuntimeError, ValueError, AttributeError) as error:
                item["reason"] = str(error)

    def _texture(self, descriptor, row, writes, cache):
        texture = self.textures[descriptor.resource]
        fmt = descriptor.format
        if enum_name(fmt.type) == "Undefined":
            fmt = texture.format
        row.update(
            resource_format=texture.format.Name(),
            view_format=fmt.Name(),
            format=texture.format.Name(),
            width=texture.width,
            height=texture.height,
            depth=texture.depth,
            dimension=enum_name(texture.type),
            array_size=texture.arraysize,
            first_mip=descriptor.firstMip,
            mip_count=descriptor.numMips,
            first_slice=descriptor.firstSlice,
            slice_count=descriptor.numSlices,
            mip_policy="all_visible",
            raw_subresources=[],
            images=[],
            component_type=enum_name(fmt.compType),
            view_swizzle=[
                enum_name(getattr(descriptor.swizzle, c))
                for c in ("red", "green", "blue", "alpha")
            ],
            image_channels="before_view_swizzle",
            raw_layout="native format; separate view reinterpretation",
        )
        encoding = None if self.options["native_only"] else _image_format(fmt)
        if _format_layout(fmt) != _format_layout(texture.format):
            encoding = None
            row.setdefault("issues", []).append(
                {
                    "reason": "Image conversion cannot reinterpret the view "
                    "layout; raw bytes retained"
                }
            )
        first_mip = descriptor.firstMip
        last_mip = min(
            texture.mips, first_mip + (descriptor.numMips or texture.mips)
        )
        if first_mip < 0 or first_mip >= last_mip:
            raise ValueError("Invalid texture view mip range")
        # Writable overlap cannot be recovered from a post-event snapshot.
        for mip in range(first_mip, last_mip):
            depth = max(1, texture.depth >> mip)
            width, height = (
                max(1, texture.width >> mip),
                max(1, texture.height >> mip),
            )
            first_slice = (
                descriptor.firstSlice if texture.dimension != 3 else 0
            )
            last_slice = min(
                texture.arraysize,
                first_slice + (descriptor.numSlices or texture.arraysize),
            )
            for layer in range(first_slice, last_slice):
                for sample in range(max(1, texture.msSamp)):
                    sub = {
                        "mip": mip,
                        "slice": layer,
                        "sample": sample,
                        "width": width,
                        "height": height,
                        "depth": depth,
                    }
                    if (
                        len(row["raw_subresources"])
                        >= self.limits["max_subresources_per_view"]
                    ):
                        raise ValueError(
                            "Texture view exceeds subresource limit"
                        )
                    if any(
                        _within(mip, w.firstMip, w.numMips)
                        and _within(layer, w.firstSlice, w.numSlices)
                        for w in writes
                    ):
                        row.setdefault("issues", []).append(
                            dict(
                                sub,
                                reason="Input overlaps a writable output; "
                                "pre-draw contents unavailable",
                            )
                        )
                        continue
                    # Native compressed readback does not allocate decoded
                    # pixels. Image conversions retain the conservative guard.
                    estimated_bytes = _native_texture_bytes(
                        texture.format, width, height, depth
                    )
                    if encoding:
                        estimated_bytes = max(
                            estimated_bytes, width * height * depth * 16
                        )
                    if (
                        estimated_bytes
                        > self.limits["max_resource_bytes"]
                    ):
                        row.setdefault("issues", []).append(
                            dict(
                                sub,
                                reason="Texture subresource exceeds "
                                "readback budget",
                            )
                        )
                        continue
                    key = (descriptor.resource, mip, layer, sample)
                    if key not in cache:
                        resource = self.rd.Subresource()
                        resource.mip, resource.slice, resource.sample = (
                            mip,
                            layer,
                            sample,
                        )
                        data = self.controller.GetTextureData(
                            descriptor.resource, resource
                        )
                        if not data:
                            raise ValueError(
                                "Texture readback returned no data"
                            )
                        if len(data) > self.limits["max_resource_bytes"]:
                            raise ValueError(
                                "Native texture exceeds per-resource limit"
                            )
                        cache[key] = self._write(data)
                    row["raw_subresources"].append(dict(sub, **cache[key]))
                    if encoding:
                        slices = (
                            range(depth)
                            if texture.dimension == 3
                            else (layer,)
                        )
                        if (
                            len(row["images"]) + len(slices)
                            > self.limits["max_subresources_per_view"]
                        ):
                            row.setdefault("issues", []).append(
                                dict(
                                    sub,
                                    reason="Volume image slices exceed "
                                    "subresource limit; raw volume retained",
                                )
                            )
                            continue
                        for image_slice in slices:
                            image_key = key + (
                                fmt.Name(),
                                image_slice,
                                encoding,
                            )
                            if image_key not in cache:
                                item = self.images._save(
                                    descriptor,
                                    fmt,
                                    encoding,
                                    mip,
                                    image_slice,
                                    sample,
                                )
                                if (
                                    "file" in item
                                    and item["file"] not in self.files
                                ):
                                    path = self.folder / item["file"]
                                    size = path.stat().st_size
                                    if (
                                        self.bytes_written + size
                                        > self.limits["max_total_bytes"]
                                    ):
                                        path.unlink()
                                        self.images.files.discard(path.name)
                                        item = {
                                            "reason": "Shader evidence "
                                            "total-byte limit reached"
                                        }
                                    else:
                                        self.files.add(item["file"])
                                        self.bytes_written += size
                                cache[image_key] = item
                            item = dict(cache[image_key], **sub)
                            item["depth"] = 1
                            if texture.dimension == 3:
                                item["depth_slice"] = image_slice
                            if "file" in item:
                                row["images"].append(item)
                            else:
                                row.setdefault("issues", []).append(item)
        row["status"] = "partial" if row.get("issues") else "captured"

    def _resources(self, pipeline, stage, reflection, writes, cache):
        result = []
        for used in pipeline.GetReadOnlyResources(stage):
            row = _binding_info(
                used, reflection, enum_name(stage), pipeline.GetShader(stage)
            )
            if row["statically_unused"]:
                row["status"] = "unused"
                result.append(row)
                continue
            try:
                desc = used.descriptor
                if desc.resource in self.textures:
                    self._texture(
                        desc, row, writes.get(desc.resource, ()), cache
                    )
                elif desc.resource in self.buffers:
                    row.update(raw_data=self._buffer(desc), status="captured")
                else:
                    row.update(
                        status="unavailable",
                        reason="Unbound or unsupported resource type",
                    )
            except (RuntimeError, ValueError, AttributeError) as error:
                row.update(
                    status="partial"
                    if row.get("raw_subresources")
                    else "unavailable",
                    reason=str(error),
                )
            result.append(row)
        return result

    def capture(self, pipeline, action):
        from .resource_service import texture_description
        from .shader_export import (
            _bindings,
            _graphics,
            _module,
            _specialization,
        )

        eid = action.eventId
        row = {
            "event_id": eid,
            "kind": "draw",
            "stages": {},
            "bindings": {},
            "constants": {},
            "pipeline": {},
            "issues": [],
        }
        row.update(
            name=action.GetName(self.controller.GetStructuredFile()),
            flags=str(action.flags),
            instance_count=action.numInstances,
            index_count=action.numIndices,
            first_index=action.indexOffset,
            first_vertex=action.vertexOffset,
            first_instance=action.instanceOffset,
            base_vertex=action.baseVertex,
        )
        try:
            row["pipeline"] = _graphics(self.controller, pipeline)
        except (RuntimeError, ValueError, AttributeError) as error:
            row["issues"].append("Pipeline state: " + str(error))
        if pipeline.IsCaptureVK():
            from .renderpass_metadata import (
                capture_renderpass_metadata,
                event_renderpass_metadata,
            )

            try:
                if self.render_pass_metadata is None:
                    self.render_pass_metadata = capture_renderpass_metadata(
                        self.controller
                    )
                    (self.folder / "renderpass_metadata.json").write_text(
                        json.dumps(self.render_pass_metadata, allow_nan=False),
                        encoding="utf-8",
                    )
                row["render_pass"] = event_renderpass_metadata(
                    self.render_pass_metadata, eid
                )
            except (RuntimeError, ValueError, AttributeError) as error:
                row["issues"].append("Render pass metadata: " + str(error))
        descriptions = {
            str(t.resourceId): dict(texture_description(t), type="texture")
            for t in self.textures.values()
        }
        descriptions.update(
            {
                str(b.resourceId): {"type": "buffer", "length": b.length}
                for b in self.buffers.values()
            }
        )
        # A graphics action can retain an unrelated bound compute shader.
        # Capture only the stages which execute for this action.
        labels = ("Vertex", "Hull", "Domain", "Geometry", "Pixel")
        if pipeline.IsCaptureVK() or pipeline.IsCaptureD3D12():
            labels += ("Task", "Mesh")
        writes = _write_bindings(
            pipeline,
            self.rd,
            tuple(getattr(self.rd.ShaderStage, label) for label in labels),
        )
        cache = {}
        for label in labels:
            stage = getattr(self.rd.ShaderStage, label)
            rid = pipeline.GetShader(stage)
            if rid == self.rd.ResourceId.Null():
                continue
            ref = pipeline.GetShaderReflection(stage)
            entry = pipeline.GetShaderEntryPoint(stage) or (
                ref.entryPoint if ref else ""
            )
            key = (str(rid), label, entry)
            if key not in self.modules:
                module_id = "m{:04d}".format(len(self.modules) + 1)
                module = _module(self.folder / "modules", module_id, key, ref)
                if ref:
                    try:
                        obj = pipeline.GetGraphicsPipelineObject()
                        targets = self.controller.GetDisassemblyTargets(
                            obj != self.rd.ResourceId.Null()
                        )
                        if targets:
                            disassembly = self.controller.DisassembleShader(
                                obj, ref, targets[0]
                            )
                            filename = (
                                "modules/" + module_id + ".disassembly.txt"
                            )
                            (self.folder / filename).write_text(
                                disassembly, encoding="utf-8"
                            )
                            module["disassembly"] = filename
                            module["disassembly_target"] = targets[0]
                    except (RuntimeError, ValueError) as error:
                        module["disassembly_reason"] = str(error)
                self.modules[key] = module
            row["stages"][label] = self.modules[key]["id"]
            try:
                bindings = _bindings(
                    pipeline, stage, ref, descriptions, self.names
                )
                _descriptor_spaces(bindings, ref)
                bindings["read_only_resources"] = self._resources(
                    pipeline, stage, ref, writes, cache
                )
                self._storage_buffers(pipeline, stage, bindings)
                if ref:
                    row["constants"][label] = self._constants(
                        pipeline, stage, ref
                    )
                    if pipeline.IsCaptureVK():
                        spec = _specialization(
                            self.controller, label, ref.constantBlocks
                        )
                        if spec:
                            bindings["specialization"] = spec
                row["bindings"][label] = bindings
            except (RuntimeError, ValueError, AttributeError) as error:
                row["issues"].append(label + ": " + str(error))
        self.events.append(row)
        self.flush()
        return row

    def flush(self):
        partial = any(
            e["issues"]
            or any(
                b.get("status") in ("partial", "unavailable")
                for stage in e["bindings"].values()
                for role in ("read_only_resources", "read_write_resources")
                for b in stage.get(role, [])
            )
            or any(
                b.get("status") == "unavailable"
                for blocks in e["constants"].values()
                for b in blocks
            )
            for e in self.events
        )
        data = {
            "format": "omni_shader_evidence",
            "version": 1,
            "source_capture": self.source_capture,
            "api": enum_name(self.controller.GetAPIProperties().pipelineType),
            "events": self.events,
            "modules": list(self.modules.values()),
            "status": "partial"
            if partial
            or self.issues
            or any(m.get("unavailable_reason") for m in self.modules.values())
            else "captured",
            "issues": self.issues,
            "limits": self.limits,
            "unique_resource_bytes": self.bytes_written,
            "source_frame_color_images_used": False,
            "snapshot": "Replay event inputs; overlapping "
            "writable outputs are excluded",
        }
        (self.folder / "index.json").write_text(
            json.dumps(data, indent=2, allow_nan=False), encoding="utf-8"
        )
        return data
