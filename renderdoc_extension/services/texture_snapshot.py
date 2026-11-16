"""Export an event-bound native subresource without display conversion."""

import base64
import hashlib
import json
import re
from pathlib import Path

from .action_service import walk
from .resource_service import find_resource, get_texture_data, resource_key


def comparison_layout(format_name):
    """Identify unpacked scalar formats without guessing compressed layouts."""
    format_name = {"D16": "D16_UNORM", "D32": "D32_FLOAT"}.get(
        format_name, format_name
    )
    match = re.fullmatch(r"((?:[RGBA]\d+)+)_(FLOAT|UNORM)", format_name)
    if match:
        widths = [int(n) for n in re.findall(r"\d+", match[1])]
        kind = match[2]
    elif format_name in ("D16_UNORM", "D32_FLOAT"):
        widths = [int(format_name[1:3])]
        kind = format_name.split("_")[1]
    else:
        return {"encoding": "native", "channels": None}
    if len(set(widths)) != 1:
        return {"encoding": "native", "channels": None}
    encoding = {
        ("FLOAT", 32): "float32le", ("FLOAT", 16): "float16le",
        ("UNORM", 8): "unorm8", ("UNORM", 16): "unorm16",
    }.get((kind, widths[0]), "native")
    return {"encoding": encoding, "channels": len(widths)}


def export_texture_snapshot(
    controller, event_id, resource_id, output_directory, mip=0, slice=0,
    sample=0, depth_slice=None, max_bytes=268435456, source_capture=None,
):
    if type(event_id) is not int or event_id <= 0:
        raise ValueError("event_id must be a positive integer")
    if type(max_bytes) is not int or not 1 <= max_bytes <= 536870912:
        raise ValueError("max_bytes must be between 1 and 536870912")
    actions = walk(controller.GetRootActions())
    if not any(a.eventId == event_id for a in actions):
        raise ValueError("No action at event {}".format(event_id))
    texture = find_resource(controller.GetTextures(), resource_id)
    # Conservative admission also bounds a backend's full-resource readback.
    if texture.byteSize > max_bytes:
        raise ValueError("Texture exceeds max_bytes; raise the explicit limit")
    controller.SetFrameEvent(event_id, True)
    result = get_texture_data(
        controller, resource_id, mip, slice, sample, depth_slice
    )
    payload = base64.b64decode(result.pop("content_base64"), validate=True)
    if len(payload) > max_bytes:
        raise ValueError("Returned subresource exceeds max_bytes")
    digest = hashlib.sha256(payload).hexdigest()
    root = Path(output_directory).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    key = resource_key(resource_id).split("::")[-1]
    stem = "E{}_R{}_m{}_s{}_a{}_z{}_{}".format(
        event_id, key, mip, slice, sample, depth_slice, digest
    )
    target = root / (stem + ".bin")
    # Exclusive writes avoid replacing existing unrelated files or symlinks.
    try:
        with target.open("xb") as stream:
            stream.write(payload)
    except FileExistsError:
        if target.is_symlink() or target.read_bytes() != payload:
            raise ValueError("Existing snapshot differs or is redirected")
    result.update(comparison_layout(result["format"]))
    if result["depth"] != 1:
        result.update(encoding="native", channels=None)
    result.update(
        schema="omni-texture-snapshot/v1", event_id=event_id,
        resource_id=resource_key(resource_id), path=str(target), sha256=digest,
        byte_length=len(payload), observation="post-event",
        api=str(controller.GetAPIProperties().pipelineType),
        color_conversion="none", flip_y=False,
        limitations=[
            "Post-event contents, not necessarily this event's output.",
            "Native bytes: packed/compressed/sRGB formats require explicit "
            "decoding before comparison; orientation is not inferred.",
        ],
    )
    if source_capture:
        capture = Path(source_capture).resolve()
        stat = capture.stat()
        result["capture"] = {
            "path": str(capture), "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "identity_kind": "path-size-mtime; not a capture content hash",
        }
    metadata = target.with_suffix(".json")
    encoded = json.dumps(result, indent=2, allow_nan=False).encode("utf-8")
    try:
        with metadata.open("xb") as stream:
            stream.write(encoded)
    except FileExistsError:
        if metadata.is_symlink() or metadata.read_bytes() != encoded:
            raise ValueError("Existing snapshot metadata differs")
    return dict(result, metadata_path=str(metadata))
