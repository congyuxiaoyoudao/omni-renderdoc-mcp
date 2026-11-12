"""Export replayable frame geometry as one ASCII FBX for each draw instance."""

import json
import math
import os
import tempfile
from pathlib import Path

from ..utils.fbx import write_fbx
from .mesh_formats import MeshError, enum_name
from .mesh_inputs import check_input_bounds, input_attributes, input_indices
from .mesh_outputs import post_mesh
from .mesh_skin import (
    bind_skin,
    captured_palette,
    has_skin_channels,
    skinning_summary,
)
from .mesh_textures import MeshTextures
from .mesh_topology import compact, faces


def _draws(actions, flags):
    for action in actions:
        if (
            action.flags & (flags.Drawcall | flags.MeshDispatch)
            and not action.flags & flags.MultiAction
        ):
            yield action
        yield from _draws(action.children, flags)


def _xyz(position):
    if not position["decoded"]:
        raise MeshError("Position format cannot be decoded")
    return [(tuple(v) + (0, 0, 0))[:3] for v in position["values"]]


class NoFinitePrimitives(MeshError):
    def __init__(self, omissions):
        super().__init__("Every primitive references a non-finite position")
        self.omissions = omissions


def _finite_geometry(name, position, attrs, polygons):
    xyz = _xyz(position)
    mesh = {
        "name": name,
        "positions": xyz,
        "polygons": polygons,
        "attributes": attrs,
    }
    invalid = {
        i for i, point in enumerate(xyz) if not all(map(math.isfinite, point))
    }
    if not invalid:
        return mesh, range(len(xyz)), {}

    retained = []
    removed = invalid.copy()
    for polygon in polygons:
        if invalid.isdisjoint(polygon):
            retained.append(polygon)
        else:
            removed.update(polygon)
    for polygon in retained:
        removed.difference_update(polygon)
    omissions = {
        "nonfinite_position_count": len(invalid),
        "omitted_primitive_count": len(polygons) - len(retained),
        "omitted_vertex_count": len(removed) if retained else len(xyz),
    }
    if not retained:
        raise NoFinitePrimitives(omissions)

    rows = [i for i in range(len(xyz)) if i not in removed]
    remap = {old: new for new, old in enumerate(rows)}
    mesh["positions"] = [xyz[i] for i in rows]
    mesh["polygons"] = [[remap[i] for i in face] for face in retained]
    filtered = []
    for attr in attrs:
        channel = dict(attr, values=[attr["values"][i] for i in rows])
        if "raw" in attr:
            channel["raw"] = [attr["raw"][i] for i in rows]
        filtered.append(channel)
    mesh["attributes"] = filtered
    return mesh, rows, omissions


def _find_position(attributes, override):
    if override is not None:
        for attr in attributes:
            if attr["name"] == override:
                return attr
        raise MeshError("Specified position attribute is absent: " + override)
    for attr in attributes:
        if attr["kind"] == "position" and attr["decoded"]:
            return attr
    return None


def _set_roles(attrs, roles):
    for attr in attrs:
        if attr["name"] in roles:
            if not attr["decoded"] and roles[attr["name"]] != "custom":
                raise MeshError(
                    "Cannot map undecoded attribute " + attr["name"]
                )
            attr["kind"] = roles[attr["name"]]


def _input_data(controller, rd, pipeline, action, instance, roles):
    indexed = bool(action.flags & rd.ActionFlags.Indexed)
    stream = input_indices(controller, pipeline, action, indexed)
    if indexed:
        ids, remap = compact(stream)
        local_index = remap.__getitem__
    else:
        ids = stream
        local_index = ids.index
    if not ids:
        raise MeshError("Draw has no referenced vertices")
    attrs = input_attributes(controller, rd, pipeline, action, instance, ids)
    _set_roles(attrs, roles)
    return stream, ids, local_index, attrs


def _extract(
    controller, rd, pipeline, action, instance, override, view, roles
):
    attrs, input_stream = [], []
    is_mesh = bool(action.flags & rd.ActionFlags.MeshDispatch)
    topology = enum_name(pipeline.GetPrimitiveTopology())
    if not is_mesh and not topology.startswith("PatchList"):
        input_stream, source_ids, local_index, attrs = _input_data(
            controller, rd, pipeline, action, instance, roles
        )
        position = _find_position(attrs, override)
        if position is not None:
            polys = faces(topology, input_stream)
            if not polys:
                raise MeshError("Draw has no complete primitives")
            polys = [[local_index(i) for i in polygon] for polygon in polys]
            return position, attrs, polys, source_ids, "VSIn", topology
    if is_mesh:
        stage = rd.MeshDataStage.MeshOut
    elif topology.startswith("PatchList"):
        stage = rd.MeshDataStage.GSOut
    else:
        stage = rd.MeshDataStage.VSOut
    position, outputs, polygons, rows, topology = post_mesh(
        controller, rd, pipeline, action, instance, stage, view
    )
    _set_roles(outputs, roles)
    if stage == rd.MeshDataStage.VSOut:
        if not all(
            i < len(input_stream) and input_stream[i] is not None for i in rows
        ):
            raise MeshError("Post-VS row order does not match input geometry")
        source_ids = [input_stream[i] for i in rows]
        for attr in attrs:
            for key in ("values", "raw"):
                if key in attr:
                    attr[key] = [attr[key][local_index(i)] for i in source_ids]
        for attr in outputs:
            attr["name"] = "VSOut." + attr["name"]
        _set_roles(outputs, roles)
        attrs += outputs
    else:
        attrs = outputs
        source_ids = rows
    return position, attrs, polygons, source_ids, enum_name(stage), topology


def _lookup(table, eid, instance):
    return table.get("{}:{}".format(eid, instance), table.get(str(eid)))


def _save_mesh(path, mesh, skeleton):
    pending = path.with_suffix(".part")
    try:
        write_fbx(pending, mesh, skeleton)
        os.replace(str(pending), str(path))
    finally:
        if pending.exists():
            pending.unlink()


def _skin(spec, attrs, ids, stage):
    skeleton = None
    provenance = None
    skin = "channels_only" if has_skin_channels(attrs) else "none"
    if spec is not None:
        if stage != "VSIn":
            raise MeshError(
                "Skin binding requires input-space positions; "
                "provide position_attributes for this draw"
            )
        if isinstance(spec, dict) and "bones" in spec:
            skeleton = bind_skin(spec, attrs, ids)
            skin = "bound"
            provenance = {
                "hierarchy": "supplied skeleton",
                "bind_matrices": "supplied global bind matrices",
                "names": "supplied bone names",
            }
        else:
            skeleton, provenance = captured_palette(spec, attrs, len(ids))
            skin = "captured_joint_palette"
    elif skin == "channels_only" and stage == "VSIn":
        try:
            skeleton, provenance = captured_palette({}, attrs, len(ids))
        except MeshError as error:
            provenance = {"reason": str(error)}
        else:
            skin = "captured_joint_palette"
    elif skin == "channels_only":
        provenance = {
            "reason": (
                "Post-transform positions cannot be used as original bind "
                "geometry; provide position_attributes to select VS inputs"
            )
        }
    return skeleton, skin, provenance


def _write_patch_inputs(
    controller, rd, pipeline, action, instance, folder, options
):
    stream, ids, _, attrs = _input_data(
        controller, rd, pipeline, action, instance, options["roles"]
    )
    name = "event_{:08d}_instance_{:04d}_controls".format(
        action.eventId, instance
    )
    position = _find_position(attrs, options["position"])
    rig, skin, provenance = _skin(options["skeleton"], attrs, ids, "VSIn")
    record = {
        "event_id": action.eventId,
        "instance": instance,
        "topology": enum_name(pipeline.GetPrimitiveTopology()),
        "stage": "VSIn",
        "space": "vertex_input",
        "file": name + ".json",
        "skin": skin,
        "skin_provenance": provenance,
    }
    if position is not None:
        try:
            cage, rows, omissions = _finite_geometry(
                name, position, attrs, [(i,) for i in range(len(ids))]
            )
        except NoFinitePrimitives as error:
            record.update(error.omissions)
            record["points_fbx_omitted_reason"] = str(error)
        else:
            record.update(omissions)
            cage_rig = rig
            if rig and omissions:
                cage_rig = dict(
                    rig, influences=[rig["influences"][i] for i in rows]
                )
            _save_mesh(folder / (name + ".fbx"), cage, cage_rig)
            record["points_fbx"] = name + ".fbx"
    for attr in attrs:
        attr["values"] = [
            [v if math.isfinite(v) else str(v) for v in row]
            for row in attr["values"]
        ]
    detail = dict(
        record,
        index_stream=list(stream),
        source_vertex_ids=list(ids),
        attributes=attrs,
        skeleton=rig,
    )
    with (folder / record["file"]).open("x", encoding="utf-8") as handle:
        json.dump(detail, handle, ensure_ascii=False, allow_nan=False)
    return record


def _write_instance(
    controller, rd, pipeline, action, instance, view, folder, options
):
    eid = action.eventId
    position, attrs, polygons, ids, stage, topology = _extract(
        controller,
        rd,
        pipeline,
        action,
        instance,
        options["position"],
        view,
        options["roles"],
    )
    name = "event_{:08d}_instance_{:04d}".format(eid, instance)
    if view:
        name += "_view_{:02d}".format(view)
    mesh, rows, omissions = _finite_geometry(name, position, attrs, polygons)
    if omissions:
        ids = [ids[i] for i in rows]
    skeleton, skin, provenance = _skin(
        options["skeleton"], mesh["attributes"], ids, stage
    )
    filename = name + ".fbx"
    _save_mesh(folder / filename, mesh, skeleton)
    return {
        "event_id": eid,
        "instance": instance,
        "view": view,
        "file": filename,
        "vertex_count": len(ids),
        "primitive_count": len(mesh["polygons"]),
        "topology": topology,
        "stage": stage,
        "skin": skin,
        "skin_provenance": provenance,
        "position_attribute": position["name"],
        "position_space": "vertex_input" if stage == "VSIn" else "clip",
        **omissions,
        "attributes": [
            {
                "name": a["name"],
                "format": a["format"],
                "decoded": a["decoded"],
                "kind": a["kind"],
            }
            for a in attrs
        ],
    }


def export_frame_meshes(
    controller,
    output_directory,
    skeletons=None,
    position_attributes=None,
    event_ids=None,
    attribute_roles=None,
    include_textures=False,
    source_capture=None,
    scope=None,
):
    """Invoke on the replay thread; caller restores the previous event."""
    import renderdoc as rd

    if not isinstance(output_directory, str) or not output_directory.strip():
        raise ValueError("Specify an absolute output_directory")
    root = Path(output_directory)
    if not root.is_absolute():
        raise ValueError("output_directory must be absolute")
    if type(include_textures) is not bool:
        raise ValueError("include_textures must be a boolean")
    for arg in (skeletons, position_attributes, attribute_roles):
        if arg is not None and not isinstance(arg, dict):
            raise ValueError("Event overrides must be JSON objects")
    for roles in (attribute_roles or {}).values():
        valid = {
            "position",
            "normal",
            "tangent",
            "binormal",
            "uv",
            "color",
            "custom",
        }
        if not isinstance(roles, dict) or any(
            not isinstance(v, str) or v not in valid for v in roles.values()
        ):
            raise ValueError("Invalid attribute_roles mapping")
        if list(roles.values()).count("position") > 1:
            raise ValueError("Only one attribute can be mapped to position")
    if any(
        not isinstance(s, str) for s in (position_attributes or {}).values()
    ):
        raise ValueError("position_attributes values must be attribute names")
    if event_ids is not None and (
        not isinstance(event_ids, list)
        or any(type(i) is not int or i < 0 for i in event_ids)
    ):
        raise ValueError("event_ids must be a list of non-negative integers")
    root.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="frame-meshes-", dir=str(root)))
    selected = None if event_ids is None else set(event_ids)
    matched, exported, skipped = set(), [], []
    patch_inputs = []
    textures = (
        MeshTextures(controller, rd, folder) if include_textures else None
    )
    draw_texture_bindings = {}
    buffer_lengths = {b.resourceId: b.length for b in controller.GetBuffers()}
    for action in _draws(controller.GetRootActions(), rd.ActionFlags):
        eid = action.eventId
        if selected is not None and eid not in selected:
            continue
        matched.add(eid)
        controller.SetFrameEvent(eid, True)
        pipeline = controller.GetPipelineState()
        is_mesh = bool(action.flags & rd.ActionFlags.MeshDispatch)
        count = 1 if is_mesh else action.numInstances
        if not count and not action.flags & rd.ActionFlags.Instanced:
            count = 1
        if not count or not is_mesh and not action.numIndices:
            skipped.append({"event_id": eid, "reason": "Empty draw"})
            continue
        if not is_mesh:
            try:
                check_input_bounds(
                    pipeline,
                    action,
                    action.flags & rd.ActionFlags.Indexed,
                    buffer_lengths,
                )
            except MeshError as error:
                skipped.append({"event_id": eid, "reason": str(error)})
                continue
        wrote_mesh = False
        for instance in range(count):
            roles = _lookup(attribute_roles or {}, eid, instance) or {}
            override = _lookup(position_attributes or {}, eid, instance)
            if override is None:
                override = next(
                    (n for n, r in roles.items() if r == "position"), None
                )
            options = {
                "roles": roles,
                "position": override,
                "skeleton": _lookup(skeletons or {}, eid, instance),
            }
            if not is_mesh and enum_name(
                pipeline.GetPrimitiveTopology()
            ).startswith("PatchList"):
                try:
                    record = _write_patch_inputs(
                        controller,
                        rd,
                        pipeline,
                        action,
                        instance,
                        folder,
                        options,
                    )
                    patch_inputs.append(record)
                    if "points_fbx" in record:
                        wrote_mesh = True
                except (ValueError, RuntimeError) as error:
                    skipped.append(
                        {
                            "event_id": eid,
                            "instance": instance,
                            "stage": "control_points",
                            "reason": str(error),
                        }
                    )
                options["skeleton"] = None
            # Input vertices are shared across views. Post-transform positions
            # can differ, so export every view when using shader output.
            for view in range(max(1, pipeline.MultiviewBroadcastCount())):
                try:
                    info = _write_instance(
                        controller,
                        rd,
                        pipeline,
                        action,
                        instance,
                        view,
                        folder,
                        options,
                    )
                except (ValueError, RuntimeError) as error:
                    skipped.append(
                        {
                            "event_id": eid,
                            "instance": instance,
                            "view": view,
                            "reason": str(error),
                            **getattr(error, "omissions", {}),
                        }
                    )
                    if isinstance(error, NoFinitePrimitives):
                        continue
                    break
                exported.append(info)
                wrote_mesh = True
                if info["stage"] == "VSIn":
                    break
        if textures and wrote_mesh:
            draw_texture_bindings[str(eid)] = textures.capture(pipeline)
    if selected is not None:
        for eid in sorted(selected - matched):
            skipped.append({"event_id": eid, "reason": "Not a draw event"})
    manifest = folder / "manifest.json"
    summary = {
        "directory": str(folder),
        "manifest": str(manifest),
        "draw_count": len(matched),
        "exported_count": len(exported),
        "skipped_count": len(skipped),
        "skinning": skinning_summary(exported + patch_inputs),
    }
    summary["patch_input_count"] = len(patch_inputs)
    if source_capture:
        summary["source_capture"] = source_capture
    if scope is not None:
        summary["scope"] = scope
    if textures:
        summary["texture_file_count"] = len(textures.files)
    detail = dict(
        summary, meshes=exported, skipped=skipped, patch_inputs=patch_inputs
    )
    if textures:
        detail["draw_texture_bindings"] = draw_texture_bindings
    with manifest.open("x", encoding="utf-8") as handle:
        json.dump(detail, handle, indent=2, ensure_ascii=False)
    return summary
