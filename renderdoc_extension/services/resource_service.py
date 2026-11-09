"""Read captured texture subresources and buffer byte ranges."""

import base64

import renderdoc as rd


def resource_key(resource_id):
    prefix, separator, suffix = str(resource_id).rpartition("::")
    if (separator and prefix != "ResourceId") or not suffix.isdecimal():
        raise ValueError("Invalid resource ID: {}".format(resource_id))
    return "ResourceId::{}".format(int(suffix))


def find_resource(items, resource_id):
    key = resource_key(resource_id)
    for item in items:
        if str(item.resourceId) == key:
            return item
    raise ValueError("Resource not found: {}".format(resource_id))


def texture_description(texture):
    return {
        "width": texture.width,
        "height": texture.height,
        "depth": texture.depth,
        "array_size": texture.arraysize,
        "mip_levels": texture.mips,
        "format": texture.format.Name(),
        "dimension": str(texture.type),
        "msaa_samples": texture.msSamp,
    }


def get_texture_info(controller, resource_id):
    texture = find_resource(controller.GetTextures(), resource_id)
    return dict(
        texture_description(texture),
        resource_id=resource_id,
        byte_size=texture.byteSize,
    )


def get_buffer_contents(controller, resource_id, offset=0, length=0):
    buffer = find_resource(controller.GetBuffers(), resource_id)
    if not 0 <= offset <= buffer.length or length < 0:
        raise ValueError(
            "Invalid buffer range (size {})".format(buffer.length)
        )
    size = min(length or buffer.length, buffer.length - offset)
    data = (
        controller.GetBufferData(buffer.resourceId, offset, size)
        if size
        else b""
    )
    return {
        "resource_id": resource_id,
        "total_size": buffer.length,
        "length": len(data),
        "offset": offset,
        "content_base64": base64.b64encode(data).decode("ascii"),
    }


def check_index(index, count, label):
    if not 0 <= index < count:
        raise ValueError("{} {} outside [0, {})".format(label, index, count))


def get_texture_data(
    controller, resource_id, mip=0, slice=0, sample=0, depth_slice=None
):
    texture = find_resource(controller.GetTextures(), resource_id)
    check_index(mip, texture.mips, "Mip level")
    check_index(slice, texture.arraysize, "Array slice")
    check_index(sample, max(1, texture.msSamp), "Sample")
    depth = max(1, texture.depth >> mip)
    volume = texture.dimension == 3
    if depth_slice is not None:
        if not volume:
            raise ValueError("depth_slice requires a 3D texture")
        check_index(depth_slice, depth, "Depth slice")
        if texture.format.ASTCDimension() == 3 and depth > 1:
            raise ValueError(
                "3D ASTC blocks span multiple depth slices. "
                "Omit depth_slice to read the encoded mip volume."
            )

    subresource = rd.Subresource()
    subresource.mip = mip
    subresource.slice = slice
    subresource.sample = sample
    data = controller.GetTextureData(texture.resourceId, subresource)
    if depth_slice is not None:
        stride, remainder = divmod(len(data), depth)
        if remainder:
            raise ValueError("Texture data has no integral depth slice stride")
        start = depth_slice * stride
        data = data[start : start + stride]
    return {
        "resource_id": resource_id,
        "width": max(1, texture.width >> mip),
        "height": max(1, texture.height >> mip),
        "depth": 1 if depth_slice is not None else depth,
        "mip": mip,
        "slice": slice,
        "sample": sample,
        "depth_slice": depth_slice,
        "format": texture.format.Name(),
        "dimension": str(texture.type),
        "is_3d": volume,
        "total_depth": depth,
        "data_length": len(data),
        "content_base64": base64.b64encode(data).decode("ascii"),
    }
