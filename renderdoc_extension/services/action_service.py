"""Frame actions, marker hierarchy and hardware counters."""

import renderdoc as rd

FLAG_NAMES = (
    "Drawcall",
    "Dispatch",
    "Clear",
    "PushMarker",
    "PopMarker",
    "SetMarker",
    "Present",
    "Copy",
    "Resolve",
    "GenMips",
    "PassBoundary",
    "Indexed",
    "Instanced",
    "Auto",
    "Indirect",
    "ClearColor",
    "ClearDepthStencil",
    "BeginPass",
    "EndPass",
    "MeshDispatch",
    "CmdList",
    "MultiAction",
    "DispatchRay",
    "BuildAccStruct",
    "CommandBufferBoundary",
)


def flags(action):
    return [
        name
        for name in FLAG_NAMES
        if action.flags & getattr(rd.ActionFlags, name, 0)
    ]


def walk(actions):
    for action in actions:
        yield action
        yield from walk(action.children)


def describe(action, structured):
    return {
        "event_id": action.eventId,
        "action_id": action.actionId,
        "name": action.GetName(structured),
        "flags": flags(action),
        "num_indices": action.numIndices,
        "num_instances": action.numInstances,
    }


def get_draw_calls(
    controller,
    include_children=True,
    marker_filter=None,
    exclude_markers=None,
    event_id_min=None,
    event_id_max=None,
    only_actions=False,
    flags_filter=None,
):
    structured = controller.GetStructuredFile()
    markers = (
        rd.ActionFlags.PushMarker
        | rd.ActionFlags.SetMarker
        | rd.ActionFlags.PopMarker
    )
    wanted_flags = set(flags_filter or ())

    def visit(actions, inside):
        for action in actions:
            marker = bool(action.flags & markers)
            item = describe(action, structured)
            name = item["name"]
            if marker and any(
                term in name for term in (exclude_markers or ())
            ):
                continue
            matching = inside or (marker and marker_filter in name)
            children = (
                list(visit(action.children, matching))
                if (include_children and action.children)
                else []
            )
            in_range = (
                event_id_min is None or action.eventId >= event_id_min
            ) and (event_id_max is None or action.eventId <= event_id_max)
            in_flags = not wanted_flags or wanted_flags.intersection(
                item["flags"]
            )
            if marker and include_children and action.children:
                selected = not only_actions and bool(children)
            else:
                selected = (
                    matching
                    and in_range
                    and in_flags
                    and not (marker and only_actions)
                )
            if selected:
                if children:
                    item["children"] = children
                yield item
            else:
                yield from children

    return {
        "actions": list(visit(controller.GetRootActions(), not marker_filter))
    }


def get_frame_summary(controller):
    roots = controller.GetRootActions()
    structured = controller.GetStructuredFile()
    count_flags = {
        "draw_calls": rd.ActionFlags.Drawcall,
        "dispatches": rd.ActionFlags.Dispatch,
        "clears": rd.ActionFlags.Clear,
        "copies": rd.ActionFlags.Copy,
        "presents": rd.ActionFlags.Present,
        "markers": rd.ActionFlags.PushMarker | rd.ActionFlags.SetMarker,
    }
    statistics = dict.fromkeys(count_flags, 0)
    count = 0
    for action in walk(roots):
        count += 1
        for key, mask in count_flags.items():
            if action.flags & mask:
                statistics[key] += 1

    return {
        "api": str(controller.GetAPIProperties().pipelineType),
        "total_actions": count,
        "statistics": statistics,
        "top_level_markers": [
            {
                "name": action.GetName(structured),
                "event_id": action.eventId,
                "child_count": sum(1 for _ in walk(action.children)),
            }
            for action in roots
            if action.flags & rd.ActionFlags.PushMarker
        ],
        "resource_counts": {
            "textures": len(controller.GetTextures()),
            "buffers": len(controller.GetBuffers()),
        },
    }


def get_draw_call_details(controller, event_id):
    action = next(
        (
            item
            for item in walk(controller.GetRootActions())
            if item.eventId == event_id
        ),
        None,
    )
    if action is None:
        raise ValueError("No action at event {}".format(event_id))
    details = describe(action, controller.GetStructuredFile())
    details.update(
        base_vertex=action.baseVertex,
        vertex_offset=action.vertexOffset,
        instance_offset=action.instanceOffset,
        index_offset=action.indexOffset,
        outputs=[
            {"index": i, "resource_id": str(rid)}
            for i, rid in enumerate(action.outputs)
            if rid != rd.ResourceId.Null()
        ],
    )
    if action.depthOut != rd.ResourceId.Null():
        details["depth_output"] = str(action.depthOut)
    return details


def get_action_timings(
    controller, event_ids=None, marker_filter=None, exclude_markers=None
):
    counter = rd.GPUCounter.EventGPUDuration
    if counter not in controller.EnumerateCounters():
        return {
            "available": False,
            "error": "GPU duration counter unavailable",
        }
    seconds = {
        r.eventId: r.value.d
        for r in controller.FetchCounters([counter])
        if r.counter == int(counter)
    }
    included = None if event_ids is None else set(event_ids)
    marker_mask = rd.ActionFlags.PushMarker | rd.ActionFlags.SetMarker
    required = (marker_filter or "").casefold()
    excluded = tuple(term.casefold() for term in (exclude_markers or ()))
    structured = controller.GetStructuredFile()

    def timings(actions, parent):
        for action in actions:
            name = action.GetName(structured)
            path = parent
            if action.flags & marker_mask:
                path = parent + "/" + name.casefold()
                if any(term in name.casefold() for term in excluded):
                    continue
            eid = action.eventId
            if (
                eid in seconds
                and required in path
                and (included is None or eid in included)
            ):
                yield {
                    "event_id": eid,
                    "name": name,
                    "duration_seconds": seconds[eid],
                    "duration_ms": seconds[eid] * 1000,
                }
            yield from timings(action.children, path)

    rows = sorted(
        timings(controller.GetRootActions(), ""),
        key=lambda row: row["event_id"],
    )
    return {
        "available": True,
        "unit": str(controller.DescribeCounter(counter).unit),
        "count": len(rows),
        "timings": rows,
        "total_duration_ms": sum(r["duration_ms"] for r in rows),
    }
