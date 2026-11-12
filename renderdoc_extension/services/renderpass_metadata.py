"""Extract Vulkan attachment clears without replay or pixel readback.

Call capture_renderpass_metadata(controller) inside RenderDoc's Python process.
The SDObject API and Vulkan member names follow RenderDoc's primary source:
https://github.com/baldurk/renderdoc/blob/v1.x/renderdoc/api/replay/structured_data.h
https://github.com/baldurk/renderdoc/blob/v1.x/renderdoc/driver/vulkan/vk_serialise.cpp
"""

import json
import math
from pathlib import Path


def serialize(node, depth=0):
    """Convert an SDObject to JSON, retaining all union interpretations."""
    if node is None:
        return None
    if depth > 28:
        return {"truncated_type": str(node.type.name)}
    kind = str(node.type.basetype).split(".")[-1]
    count = int(node.NumChildren())
    if kind == "Array":
        return [serialize(node.GetChild(i), depth + 1) for i in range(count)]
    if count or kind in ("Struct", "Chunk"):
        output = {}
        for i in range(count):
            child = node.GetChild(i)
            name = str(child.name)
            value = serialize(child, depth + 1)
            if name in output:
                # Arrays have their own branch; duplicate structure members
                # should remain inspectable instead of silently disappearing.
                name = name + "#" + str(i)
            output[name] = value
        return output
    if kind == "Null":
        return None
    if kind == "Resource":
        return str(node.AsResourceId())
    if kind == "Float":
        number = float(node.AsFloat())
        return number if math.isfinite(number) else repr(number)
    if kind == "Boolean":
        return bool(node.AsBool())
    if kind == "String":
        return str(node.AsString())
    if kind in (
        "Enum",
        "UnsignedInteger",
        "SignedInteger",
        "GPUAddress",
        "Character",
    ):
        return int(node.AsInt())
    if kind == "Buffer":
        return {"opaque_buffer_bytes": int(node.type.byteSize)}
    return {"unknown_basetype": kind, "type": str(node.type.name)}


def _get(record, *names):
    if not isinstance(record, dict):
        return None
    wanted = {name.lower() for name in names}
    for key, value in record.items():
        if key.lower() in wanted:
            return value
    return None


def _walk(actions):
    for action in actions:
        yield action
        yield from _walk(action.children)


def _rid(value):
    return (
        value
        if isinstance(value, str) and value.startswith("ResourceId::")
        else None
    )


def capture_renderpass_metadata(controller):
    structured = controller.GetStructuredFile()
    actions = sorted(
        _walk(controller.GetRootActions()), key=lambda a: a.eventId
    )
    chunk_events = {}
    for action in actions:
        for event in action.events:
            chunk_events.setdefault(int(event.chunkIndex), set()).add(
                int(event.eventId)
            )

    image_views, framebuffers, renderpasses = {}, {}, {}
    begin_records, explicit_clears, issues = [], [], []
    interesting = (
        "vkCreateImageView",
        "vkCreateFramebuffer",
        "vkCreateRenderPass",
        "vkCmdBeginRenderPass",
        "vkCmdBeginRendering",
        "vkCmdClearColorImage",
        "vkCmdClearDepthStencilImage",
        "vkCmdClearAttachments",
    )
    for chunk_index, chunk in enumerate(structured.chunks):
        name = str(chunk.name)
        if not any(token in name for token in interesting):
            continue
        try:
            data = serialize(chunk)
            info = _get(data, "CreateInfo", "pCreateInfo") or {}
            if "vkCreateImageView" in name:
                key = _get(data, "View", "pView", "ImageView")
                if _rid(key):
                    image_views[key] = info
            elif "vkCreateFramebuffer" in name:
                key = _get(data, "Framebuffer", "pFramebuffer")
                if _rid(key):
                    framebuffers[key] = info
            elif "vkCreateRenderPass" in name:
                key = _get(data, "RenderPass", "pRenderPass")
                if _rid(key):
                    renderpasses[key] = info
            elif "vkCmdBeginRenderPass" in name:
                info = _get(
                    data,
                    "pRenderPassBegin",
                    "RenderPassBegin",
                    "pRenderPassBeginInfo",
                    "BeginInfo",
                )
                if not isinstance(info, dict):
                    info = next(
                        (
                            v
                            for v in data.values()
                            if isinstance(v, dict)
                            and _get(v, "clearValueCount") is not None
                        ),
                        None,
                    )
                begin_records.append(
                    {
                        "chunk_index": chunk_index,
                        "api": name,
                        "event_ids": sorted(chunk_events.get(chunk_index, [])),
                        "begin_info": info,
                        "raw_chunk": data if info is None else None,
                    }
                )
            else:
                explicit_clears.append(
                    {
                        "chunk_index": chunk_index,
                        "api": name,
                        "event_ids": sorted(chunk_events.get(chunk_index, [])),
                        "data": data,
                    }
                )
        except Exception as error:
            issues.append(
                {"chunk_index": chunk_index, "api": name, "error": str(error)}
            )

    passes = []
    for source in begin_records:
        info = source["begin_info"] or {}
        rp_id, fb_id = _get(info, "renderPass"), _get(info, "framebuffer")
        rp, fb = renderpasses.get(rp_id, {}), framebuffers.get(fb_id, {})
        descriptions = _get(rp, "pAttachments") or []
        views = _get(fb, "pAttachments") or []
        clear_values = _get(info, "pClearValues") or []
        attachments = []
        for index in range(
            max(len(views), len(descriptions), len(clear_values))
        ):
            view_id = views[index] if index < len(views) else None
            view = image_views.get(view_id, {})
            description = (
                descriptions[index] if index < len(descriptions) else {}
            )
            clear = clear_values[index] if index < len(clear_values) else {}
            depth_stencil = _get(clear, "depthStencil") or {}
            load, stencil_load = (
                _get(description, "loadOp"),
                _get(description, "stencilLoadOp"),
            )
            attachments.append(
                {
                    "attachment_index": index,
                    "view_id": view_id,
                    "resource_id": _get(view, "image"),
                    "format": _get(view, "format")
                    or _get(description, "format"),
                    "subresource_range": _get(view, "subresourceRange"),
                    "load_op": load,
                    "store_op": _get(description, "storeOp"),
                    "stencil_load_op": stencil_load,
                    "stencil_store_op": _get(description, "stencilStoreOp"),
                    "clear_color": _get(_get(clear, "color") or {}, "float32"),
                    "clear_depth": _get(depth_stencil, "depth"),
                    "clear_stencil": _get(depth_stencil, "stencil"),
                    "applies_clear": load == 1,
                    "applies_stencil_clear": stencil_load == 1,
                }
            )
        # One recorded command buffer can be executed multiple times. Keep a
        # distinct pass instance for each frame event mapped to its chunk.
        for eid in source["event_ids"] or [None]:
            record = dict(
                source,
                begin_event_id=eid,
                render_pass_id=rp_id,
                framebuffer_id=fb_id,
                render_area=_get(info, "renderArea"),
                attachments=attachments,
                framebuffer_width=_get(fb, "width"),
                framebuffer_height=_get(fb, "height"),
            )
            if rp_id and not rp:
                record["unresolved_renderpass"] = True
            if fb_id and not fb:
                record["unresolved_framebuffer"] = True
            passes.append(record)

    action_to_pass = {}
    by_begin = {
        p["begin_event_id"]: p
        for p in passes
        if p["begin_event_id"] is not None
    }
    active = None
    for action in actions:
        eid = int(action.eventId)
        if eid in by_begin:
            active = by_begin[eid]
            active["draw_event_ids"] = []
        if active is not None:
            action_to_pass[str(eid)] = active["begin_event_id"]
            if int(getattr(action, "numIndices", 0)) > 0:
                active["draw_event_ids"].append(eid)
        if "EndPass" in str(action.flags):
            if active is not None:
                active["end_event_id"] = eid
            active = None

    result = {
        "version": 1,
        "api": "Vulkan",
        "method": "Structured API metadata only; no replay or pixel readback",
        "vk_attachment_load_op": {"LOAD": 0, "CLEAR": 1, "DONT_CARE": 2},
        "render_passes": passes,
        "event_to_render_pass": action_to_pass,
        "explicit_clears_and_dynamic_rendering": explicit_clears,
        "counts": {
            "image_views": len(image_views),
            "framebuffers": len(framebuffers),
            "renderpass_definitions": len(renderpasses),
            "renderpass_instances": len(passes),
        },
        "issues": issues,
    }
    return result


def event_renderpass_metadata(metadata, event_id):
    """Return one draw's compact source pass record without replay."""
    begin = metadata.get("event_to_render_pass", {}).get(str(event_id))
    for record in metadata.get("render_passes", []):
        if record.get("begin_event_id") == begin and begin is not None:
            fields = (
                "begin_event_id",
                "render_pass_id",
                "framebuffer_id",
                "render_area",
                "attachments",
                "framebuffer_width",
                "framebuffer_height",
                "unresolved_renderpass",
                "unresolved_framebuffer",
            )
            return {key: record[key] for key in fields if key in record}
    return None


def export(controller, output_path):
    result = capture_renderpass_metadata(controller)
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(result, indent=2, allow_nan=False), encoding="utf-8"
    )
    return {
        "output": str(target),
        "counts": result["counts"],
        "issues": len(result["issues"]),
    }
