"""Conservative, versioned resource dependencies from RenderDoc GetUsage.

GetUsage does not establish subresource coverage or shader dataflow. Writes
carry the preceding resource version as a possible partial-write dependency.
A clear, copy or discard is not assumed to replace every mip and slice.
No replay event is selected and no resource is modified by this service.
"""

from collections import defaultdict, deque

SCHEMA = "omni-renderdoc-resource-dependencies/v1"
_STAGES = ("VS", "HS", "DS", "GS", "PS", "CS", "TS", "MS", "All")
_READ = {
    "VertexBuffer",
    "IndexBuffer",
    "InputTarget",
    "Indirect",
    "CopySrc",
    "ResolveSrc",
} | {
    stage + suffix
    for stage in _STAGES
    for suffix in ("_Constants", "_Resource")
}
_WRITE = {"StreamOut", "Clear", "Discard", "CopyDst", "ResolveDst", "CPUWrite"}
_READ_WRITE = {
    "ColorTarget",
    "DepthStencilTarget",
    "GenMips",
    "Copy",
    "Resolve",
}
_READ_WRITE |= {stage + "_RWResource" for stage in _STAGES}
_IGNORE = {"Unused", "Barrier"}
_USAGES = _READ | _WRITE | _READ_WRITE | _IGNORE
_MARKERS = {"PushMarker", "SetMarker", "PopMarker", "CmdList", "MultiAction"}


def _budget(value, name, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError("{} must be between 1 and {}".format(name, maximum))
    return value


def _event(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(name + " must be a non-negative integer")
    return value


def _version(resource_id, event_id=None):
    return "{}@{}".format(
        resource_id, "initial" if event_id is None else event_id
    )


def usage_access(name):
    """Unknown future enum members stay unknown; never silently assume read."""
    if name in _READ:
        return "read"
    if name in _WRITE:
        return "write"
    if name in _READ_WRITE:
        return "read_write"
    if name in _IGNORE:
        return "none"
    return "unknown"


def build_dependency_graph(
    actions,
    resources,
    usages,
    event_ids,
    *,
    max_events=2048,
    max_depth=32,
    max_edges=65536,
    scan_issues=(),
):
    """Build a pure-data candidate graph, with bounded reverse traversal.

    actions: flat action dictionaries (event_id/name/flags).
    resources: resource dictionaries (resource_id/name/kind).
    usages: dictionaries (resource_id/event_id/usage), from one capture.
    Event IDs are RenderDoc's ordered replay timeline, never resource IDs.
    """
    _budget(max_events, "max_events", 100000)
    _budget(max_depth, "max_depth", 1024)
    _budget(max_edges, "max_edges", 1000000)
    if not isinstance(event_ids, (list, tuple)) or not event_ids:
        raise ValueError("event_ids must be a non-empty list")
    seeds = list(dict.fromkeys(_event(e, "event_id") for e in event_ids))
    if len(seeds) > max_events:
        raise ValueError("max_events must accommodate every selected event")
    action_map = {row["event_id"]: dict(row) for row in actions}
    missing = set(seeds) - action_map.keys()
    if missing:
        raise ValueError(
            "No action at selected events: " + str(sorted(missing))
        )
    resource_map = {row["resource_id"]: dict(row) for row in resources}
    grouped = defaultdict(lambda: defaultdict(set))
    issues = [dict(row) for row in scan_issues]
    for row in usages:
        rid, eid, name = row["resource_id"], row["event_id"], row["usage"]
        if usage_access(name) == "none":
            continue
        grouped[rid][eid].add(name)
        if usage_access(name) == "unknown":
            issues.append(
                {
                    "code": "unknown_resource_usage",
                    "event_id": eid,
                    "resource_id": rid,
                    "usage": name,
                }
            )
    timeline = sorted(
        set(action_map)
        | {eid for events in grouped.values() for eid in events}
    )
    indices = {eid: i for i, eid in enumerate(timeline)}
    nodes = {}
    for eid in timeline:
        action = action_map.get(eid, {})
        nodes[eid] = {
            "event_id": eid,
            "execution_index": indices[eid],
            "name": action.get("name", "Resource usage event {}".format(eid)),
            "flags": action.get("flags", []),
            "origin": "action-tree" if eid in action_map else "resource-usage",
            "reads": [],
            "writes": [],
        }
    incoming = defaultdict(list)
    versions = {}
    # One chain per resource avoids a quadratic list of all preceding writers.
    for rid, events in grouped.items():
        initial = _version(rid)
        versions[initial] = {
            "version_id": initial,
            "resource_id": rid,
            "writer_event_id": None,
            "previous_version_id": None,
        }
        previous, writer = initial, None
        for eid, names in sorted(events.items()):
            accesses = {usage_access(name) for name in names}
            reads = bool(accesses & {"read", "read_write", "unknown"})
            writes = bool(accesses & {"write", "read_write", "unknown"})
            edge = {
                "source_event_id": writer,
                "target_event_id": eid,
                "resource_id": rid,
                "version_id": previous,
                "confidence": "resource-level-candidate",
            }
            if reads:
                nodes[eid]["reads"].append(
                    {
                        "resource_id": rid,
                        "version_id": previous,
                        "usages": sorted(names),
                        "access": "unknown"
                        if "unknown" in accesses
                        else "read",
                    }
                )
                incoming[eid].append(dict(edge, kind="read"))
            if writes:
                # Even write-only commands may leave other regions untouched.
                # This is a conservative inheritance edge, not a proven read.
                incoming[eid].append(
                    dict(edge, kind="preserve_previous_contents")
                )
                current = _version(rid, eid)
                versions[current] = {
                    "version_id": current,
                    "resource_id": rid,
                    "writer_event_id": eid,
                    "previous_version_id": previous,
                    "write_usages": sorted(names),
                    "coverage": "unknown",
                }
                nodes[eid]["writes"].append(
                    {
                        "resource_id": rid,
                        "version_id": current,
                        "usages": sorted(names),
                        "access": "unknown"
                        if "unknown" in accesses
                        else "write",
                    }
                )
                previous, writer = current, eid

    depths = dict.fromkeys(seeds, 0)
    queue = deque(seeds)
    selected_edges = []
    boundaries = []
    truncated = False
    edge_budget_hit = False
    while queue:
        eid = queue.popleft()
        depth = depths[eid]
        for edge in incoming[eid]:
            if len(selected_edges) >= max_edges:
                truncated = edge_budget_hit = True
                break
            selected_edges.append(edge)
            parent = edge["source_event_id"]
            if parent is None or parent in depths:
                continue
            reason = (
                "max_depth"
                if depth >= max_depth
                else "max_events"
                if len(depths) >= max_events
                else None
            )
            if reason:
                truncated = True
                boundaries.append({"code": reason, **edge})
                continue
            depths[parent] = depth + 1
            queue.append(parent)
        if edge_budget_hit:
            boundaries.append({"code": "max_edges", "event_id": eid})
            break

    selected_nodes = []
    needed_versions = {edge["version_id"] for edge in selected_edges}
    for eid in sorted(depths):
        row = dict(nodes[eid], seed=eid in seeds, dependency_depth=depths[eid])
        selected_nodes.append(row)
        for entry in row["reads"] + row["writes"]:
            needed_versions.add(entry["version_id"])
    selected_versions = [versions[key] for key in sorted(needed_versions)]
    used_resources = {row["resource_id"] for row in selected_versions}
    external = [
        {
            "resource_id": row["resource_id"],
            "version_id": row["version_id"],
            "kind": "capture-initial-state",
            "provenance": "No earlier GetUsage write in the scanned timeline; "
            "may be an asset, prior-frame result or unobserved input.",
        }
        for row in selected_versions
        if row["writer_event_id"] is None
    ]
    all_issues = issues + boundaries
    return {
        "schema": SCHEMA,
        "scope": "resource-level-conservative",
        "exact": False,
        "complete": not truncated and not issues,
        "complete_meaning": "Bounded usage collection and candidate traversal "
        "finished; not proof of exact shader dependencies.",
        "truncated": truncated or any(row.get("truncated") for row in issues),
        "selection": {"event_ids": seeds},
        "nodes": selected_nodes,
        "edges": selected_edges,
        "versions": selected_versions,
        "external_inputs": external,
        "resources": [
            resource_map.get(rid, {"resource_id": rid, "kind": "unknown"})
            for rid in sorted(used_resources)
        ],
        "unresolved": all_issues,
        "warnings": [
            "GetUsage is resource-level evidence: mip/slice/byte coverage, "
            "load/store, blend/test reads and shader paths are unknown.",
            "Write chains conservatively retain prior versions, including "
            "clear/copy/discard. Closure may include unrelated passes.",
            "Initial state is not classified as static texture or history "
            "without more evidence; aliases and hidden accesses are unproven.",
        ],
        "limits": {
            "max_events": max_events,
            "max_depth": max_depth,
            "max_edges": max_edges,
        },
        "summary": {
            "seed_events": len(seeds),
            "selected_events": len(selected_nodes),
            "dependency_edges": len(selected_edges),
            "resource_versions": len(selected_versions),
            "resources": len(used_resources),
            "external_inputs": len(external),
            "unresolved": len(all_issues),
        },
    }


def _select_events(roots, action_rows, event_ids, pass_event_id):
    if (event_ids is None) == (pass_event_id is None):
        raise ValueError("Choose either event_ids or pass_event_id")
    if event_ids is not None:
        if not isinstance(event_ids, list) or not event_ids:
            raise ValueError("event_ids must be a non-empty list")
        return event_ids, None
    _event(pass_event_id, "pass_event_id")
    selected = None

    def find(items, parent=None):
        nonlocal selected
        for action in items:
            row = action_rows[action.eventId]
            marker = bool(set(row["flags"]) & {"PushMarker", "SetMarker"})
            if action.eventId == pass_event_id:
                selected = action if marker or parent is None else parent
                return True
            if find(action.children, action if marker else parent):
                return True
        return False

    find(roots)
    if selected is None:
        raise ValueError("No action at event {}".format(pass_event_id))
    seeds = []

    def collect(action):
        if not set(action_rows[action.eventId]["flags"]) & _MARKERS:
            seeds.append(action.eventId)
        for child in action.children:
            collect(child)

    collect(selected)
    if not seeds:
        raise ValueError("Selected marker has no executable actions")
    return seeds, {
        "requested_event_id": pass_event_id,
        "event_id": selected.eventId,
        "name": action_rows[selected.eventId]["name"],
    }


def get_resource_dependencies(
    controller,
    names,
    event_ids=None,
    pass_event_id=None,
    max_events=2048,
    max_depth=32,
    max_edges=65536,
    max_resources=16384,
    max_usage_rows=500000,
):
    """Inspect GetUsage without selecting events or modifying resources."""
    import renderdoc as rd

    from .action_service import flags, walk

    _budget(max_resources, "max_resources", 100000)
    _budget(max_usage_rows, "max_usage_rows", 2000000)
    _budget(max_events, "max_events", 100000)
    _budget(max_depth, "max_depth", 1024)
    _budget(max_edges, "max_edges", 1000000)
    roots = controller.GetRootActions()
    structured = controller.GetStructuredFile()
    actions = [
        {
            "event_id": a.eventId,
            "name": a.GetName(structured),
            "flags": flags(a),
        }
        for a in walk(roots)
    ]
    selected, scope = _select_events(
        roots,
        {row["event_id"]: row for row in actions},
        event_ids,
        pass_event_id,
    )
    # Reject bad selections before potentially expensive resource usage reads.
    known = {row["event_id"] for row in actions}
    for eid in selected:
        _event(eid, "event_id")
        if eid not in known:
            raise ValueError("No action at event {}".format(eid))
    if len(set(selected)) > max_events:
        raise ValueError("max_events must accommodate every selected event")
    inventory = {}
    for kind, items in (
        ("texture", controller.GetTextures()),
        ("buffer", controller.GetBuffers()),
    ):
        for item in items:
            rid = str(item.resourceId)
            inventory[rid] = (
                item.resourceId,
                {
                    "resource_id": rid,
                    "name": names.get(rid, ""),
                    "kind": kind,
                },
            )
    enum_names = {}
    for name in _USAGES:
        value = getattr(getattr(rd, "ResourceUsage", None), name, None)
        if value is not None:
            enum_names[value] = name
    usages, issues, resources = [], [], []
    records = list(inventory.values())
    if len(records) > max_resources:
        issues.append(
            {
                "code": "max_resources",
                "truncated": True,
                "available": len(records),
                "retained": max_resources,
            }
        )
    queried = 0
    for resource, metadata in records[:max_resources]:
        resources.append(metadata)
        queried += 1
        try:
            rows = controller.GetUsage(resource)
        except Exception as error:
            issues.append(
                {
                    "code": "usage_query_failed",
                    "resource_id": str(resource),
                    "error": str(error),
                }
            )
            continue
        exhausted = False
        for row in rows:
            if len(usages) >= max_usage_rows:
                issues.append(
                    {
                        "code": "max_usage_rows",
                        "truncated": True,
                        "resource_id": str(resource),
                    }
                )
                exhausted = True
                break
            usage = enum_names.get(
                row.usage, str(row.usage).rsplit(".", 1)[-1]
            )
            usages.append(
                {
                    "resource_id": str(resource),
                    "event_id": row.eventId,
                    "usage": usage,
                }
            )
        if exhausted:
            break
    result = build_dependency_graph(
        actions,
        resources,
        usages,
        selected,
        max_events=max_events,
        max_depth=max_depth,
        max_edges=max_edges,
        scan_issues=issues,
    )
    result["capture"] = {
        "api": str(controller.GetAPIProperties().pipelineType),
        "identity_scope": "active-replay-controller",
    }
    result["selection"]["pass"] = scope
    result["limits"].update(
        max_resources=max_resources, max_usage_rows=max_usage_rows
    )
    result["summary"].update(
        resources_available=len(inventory),
        resources_queried=queried,
        usage_rows_collected=len(usages),
    )
    return result
