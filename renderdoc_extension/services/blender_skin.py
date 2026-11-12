"""Keep input-space rigged sources separate from posed camera-space draws."""

import math

from .mesh import _find_position, _finite_geometry, _input_data, _skin
from .mesh_formats import MeshError, enum_name
from .mesh_skin import has_skin_channels
from .mesh_topology import faces


def capture_rigged_source(
    controller, rd, pipeline, action, instance, skeleton=None, position=None
):
    """Return truthful skin coverage, plus a bind-space mesh when available.

    A capture normally exposes weights, not an authoring hierarchy. Automatic
    palettes retain those weights with explicitly reported placeholder binds.
    Neither form is attached to the baked post-VS scene object.
    """
    result = {"skin": "none", "skin_provenance": {}}
    try:
        if action.flags & rd.ActionFlags.MeshDispatch:
            raise MeshError("Mesh shader has no conventional VS input mesh")
        topology = enum_name(pipeline.GetPrimitiveTopology())
        if topology.startswith("PatchList"):
            raise MeshError(
                "Tessellation input is a control cage; export_frame_meshes "
                "preserves its skin separately from the generated surface"
            )
        inputs = [{"name": attr.name} for attr in pipeline.GetVertexInputs()]
        if skeleton is None and not has_skin_channels(inputs):
            result["skin_provenance"] = {
                "reason": (
                    "No named joint/weight channels in captured VS inputs; "
                    "this draw may be static or already deformed by an "
                    "earlier compute pass. No original rig was inferred."
                )
            }
            return result
        stream, ids, local_index, attrs = _input_data(
            controller, rd, pipeline, action, instance, {}
        )
        result["skin"] = "channels_only"
        source_position = _find_position(attrs, position)
        if source_position is None:
            raise MeshError(
                "Skin inputs have no identifiable input-space position; "
                "provide position_attributes for this draw"
            )
        polygons = faces(topology, stream)
        if not polygons:
            raise MeshError("Input mesh has no complete primitives")
        polygons = [[local_index(i) for i in polygon] for polygon in polygons]
        name = "event_{:08d}_instance_{:04d}_rigged_source".format(
            action.eventId, instance
        )
        mesh, rows, omissions = _finite_geometry(
            name, source_position, attrs, polygons
        )
        ids = [ids[i] for i in rows]
        rig, status, provenance = _skin(
            skeleton, mesh["attributes"], ids, "VSIn"
        )
        if rig is None:
            result.update(skin=status, skin_provenance=provenance or {})
            return result
        provenance = dict(provenance or {})
        if status == "bound":
            provenance.update(
                hierarchy="supplied skeleton",
                bind_matrices="supplied global bind matrices",
                names="supplied bone names",
            )
        provenance.update(
            position_attribute=source_position["name"],
            position_space="vertex_input",
            scene_placement=(
                "Separate input-space source; captured post-VS object "
                "keeps its baked pose. No camera-space rig is inferred."
            ),
        )
        source = {
            "name": name,
            "positions": mesh["positions"],
            "faces": mesh["polygons"],
            "attributes": mesh["attributes"],
            "source_vertex_ids": ids,
            "position_space": "vertex_input",
            "extraction_stage": "VSIn",
            "topology": topology,
            "skeleton": rig,
            "skin": status,
            "skin_provenance": provenance,
            **omissions,
        }
        # Nonfinite ancillary channels must not invalidate a valid surface or
        # its rig. Raw bytes stay available; only decoded data is suppressed.
        for attr in source["attributes"]:
            if not all(
                all(math.isfinite(v) for v in row)
                for row in attr.get("values", [])
            ):
                attr["decoded"] = False
                attr["values"] = []
                attr["reason"] = "Nonfinite input values; raw bytes retained"
        result.update(
            skin=status, skin_provenance=provenance, rigged_source=source
        )
    except (ValueError, RuntimeError, AttributeError) as error:
        if result["skin"] == "none":
            result["skin"] = "unavailable"
        result["skin_provenance"] = {"reason": str(error)}
    return result
