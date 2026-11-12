"""Export the sampled texture views bound to a draw at its replay event."""

import hashlib
import tempfile
from pathlib import Path

from .mesh_formats import enum_name


def _image_format(fmt):
    component = enum_name(fmt.compType)
    layout = enum_name(fmt.type)
    byte_color = (
        layout == "Regular"
        and fmt.compByteWidth == 1
        or layout
        in {
            "A8",
            "BC1",
            "BC2",
            "BC3",
            "BC4",
            "BC5",
            "BC7",
            "ETC2",
            "PVRTC",
            "R4G4",
            "R4G4B4A4",
            "R5G5B5A1",
            "R5G6B5",
            "YUV8",
        }
    )
    if component in ("UNorm", "UNormSRGB") and byte_color:
        return "PNG"
    if component in ("UNorm", "UNormSRGB", "SNorm", "Float", "Depth"):
        return "EXR"
    return None


def _format_layout(fmt):
    return fmt.type, fmt.compCount, fmt.compByteWidth, fmt.BGRAOrder()


def _binding_info(used, reflection, stage, shader):
    access = used.access
    slot = access.index
    declared = reflection.readOnlyResources if reflection else ()
    resource = declared[slot] if 0 <= slot < len(declared) else None
    return {
        "stage": stage,
        "shader_id": str(shader),
        "slot": slot if resource is not None else None,
        "binding_name": resource.name if resource is not None else None,
        "binding": resource.fixedBindNumber if resource is not None else None,
        "set_or_space": (
            resource.fixedBindSetOrSpace if resource is not None else None
        ),
        "array_element": access.arrayElement,
        "descriptor_store": str(access.descriptorStore),
        "descriptor_offset": access.byteOffset,
        "statically_unused": access.staticallyUnused,
        "descriptor_type": enum_name(access.type),
        "resource_id": str(used.descriptor.resource),
        "view_id": str(used.descriptor.view),
        "input_attachment": bool(resource and resource.isInputAttachment),
        "images": [],
    }


def _write_bindings(pipeline, rd, stages=None):
    targets = list(pipeline.GetOutputTargets()) + [pipeline.GetDepthTarget()]
    targets.extend(
        item.descriptor
        for item in pipeline.GetAllUsedDescriptors()
        if enum_name(item.access.type) == "ReadWriteImage"
        and (
            stages is None
            or not hasattr(item.access, "stage")
            or item.access.stage in stages
        )
    )
    writable = {}
    for view in targets:
        if view.resource != rd.ResourceId.Null() and not (
            view.flags & rd.DescriptorFlags.ReadOnlyAccess
        ):
            writable.setdefault(view.resource, []).append(view)
    return writable


def _within(index, first, count):
    return count <= 0 or first <= index < first + count


class MeshTextures:
    def __init__(self, controller, rd, folder):
        self.controller = controller
        self.rd = rd
        self.directory = folder / "textures"
        self.textures = {t.resourceId: t for t in controller.GetTextures()}
        self.files = set()

    def _save(self, descriptor, fmt, encoding, mip, index, sample):
        rd = self.rd
        save = rd.TextureSave()
        save.resourceId = descriptor.resource
        save.typeCast = fmt.compType
        save.destType = getattr(rd.FileType, encoding)
        save.alpha = rd.AlphaMapping.Preserve
        save.mip = mip
        save.slice.sliceIndex = index
        save.sample.sampleIndex = sample
        self.directory.mkdir(exist_ok=True)
        suffix = "." + encoding.lower()
        with tempfile.NamedTemporaryFile(
            dir=str(self.directory), suffix=suffix, delete=False
        ) as temporary:
            pending = Path(temporary.name)
        try:
            try:
                result = self.controller.SaveTexture(save, str(pending))
            except RuntimeError as error:
                return {"reason": str(error)}
            if result != rd.ResultCode.Succeeded:
                return {"reason": str(result)}
            digest = hashlib.sha256()
            with pending.open("rb") as stream:
                header = stream.read(8)
                magic = (
                    b"\x89PNG\r\n\x1a\n" if encoding == "PNG" else b"v/1\x01"
                )
                if not header.startswith(magic):
                    return {"reason": "RenderDoc returned an invalid image"}
                digest.update(header)
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            filename = digest.hexdigest() + suffix
            if filename not in self.files:
                pending.replace(self.directory / filename)
                self.files.add(filename)
            return {"file": "textures/" + filename}
        finally:
            if pending.exists():
                pending.unlink()

    def _images(self, info, descriptor, texture, cache, writes):
        fmt = descriptor.format
        if enum_name(fmt.type) == "Undefined":
            fmt = texture.format
        mip = descriptor.firstMip
        first = descriptor.firstSlice
        count = descriptor.numSlices
        swizzle = descriptor.swizzle
        info.update(
            resource_format=texture.format.Name(),
            view_format=fmt.Name(),
            component_type=enum_name(fmt.compType),
            dimension=enum_name(texture.type),
            view_swizzle=[
                enum_name(getattr(swizzle, channel))
                for channel in ("red", "green", "blue", "alpha")
            ],
            first_mip=mip,
            mip_count=descriptor.numMips,
            first_slice=first,
            slice_count=count,
            msaa_samples=texture.msSamp,
            mip_policy="first_visible",
            image_channels="before_view_swizzle",
        )
        encoding = _image_format(fmt)
        if not encoding:
            info["reason"] = "No color-image export for this component type"
            return
        if _format_layout(fmt) != _format_layout(texture.format):
            info["reason"] = "SaveTexture cannot apply the view format layout"
            return
        if not 0 <= mip < texture.mips:
            info["reason"] = "View mip is outside the resource"
            return
        slices = (
            max(1, texture.depth >> mip)
            if texture.dimension == 3
            else texture.arraysize
        )
        end = min(first + count, slices)
        if not 0 <= first < end:
            info["reason"] = "View slices are outside the resource"
            return
        for index in range(first, end):
            overlap = any(
                _within(mip, view.firstMip, view.numMips)
                and _within(index, view.firstSlice, view.numSlices)
                for view in writes
            )
            if overlap:
                info.setdefault("issues", []).append(
                    {
                        "mip": mip,
                        "slice": index,
                        "reason": "Pre-draw pixels unknown for output slice",
                    }
                )
                continue
            for sample in range(max(1, texture.msSamp)):
                key = (descriptor.resource, fmt.Name(), mip, index, sample)
                if key not in cache:
                    cache[key] = self._save(
                        descriptor, fmt, encoding, mip, index, sample
                    )
                image = dict(
                    cache[key],
                    mip=mip,
                    slice=index,
                    sample=sample,
                    width=max(1, texture.width >> mip),
                    height=max(1, texture.height >> mip),
                )
                if "file" in image:
                    info["images"].append(image)
                else:
                    info.setdefault("issues", []).append(image)
        if not info["images"] and info.get("issues"):
            info["reason"] = info["issues"][0]["reason"]

    def capture(self, pipeline):
        """Share an event's views and deduplicate saved pixels across draws."""
        result = []
        cache = {}
        stages = ("Vertex", "Hull", "Domain", "Geometry", "Pixel")
        if pipeline.IsCaptureVK() or pipeline.IsCaptureD3D12():
            stages += ("Task", "Mesh")
        writable = _write_bindings(
            pipeline,
            self.rd,
            tuple(getattr(self.rd.ShaderStage, name) for name in stages),
        )
        for name in stages:
            stage = getattr(self.rd.ShaderStage, name)
            shader = pipeline.GetShader(stage)
            if shader == self.rd.ResourceId.Null():
                continue
            reflection = pipeline.GetShaderReflection(stage)
            for used in pipeline.GetReadOnlyResources(stage):
                if enum_name(used.access.type) not in (
                    "Image",
                    "ImageSampler",
                ):
                    continue
                info = _binding_info(used, reflection, name, shader)
                result.append(info)
                desc = used.descriptor
                texture = self.textures.get(desc.resource)
                if texture is None:
                    info["reason"] = "Unbound or unavailable texture"
                elif info["input_attachment"]:
                    info["reason"] = "Render-pass input attachment"
                else:
                    self._images(
                        info,
                        desc,
                        texture,
                        cache,
                        writable.get(desc.resource, ()),
                    )
        return result
