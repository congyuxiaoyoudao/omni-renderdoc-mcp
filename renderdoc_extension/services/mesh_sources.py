"""Preserve real vertex inputs alongside the baked, post-transform surface.

The vertex shader may skin, deform, or procedurally generate its output. Input
positions and directions therefore retain their own space; they are never
silently substituted for the posed surface or its transformed directions.
"""

import math

from .mesh import _find_position, _input_data
from .mesh_formats import MeshError, enum_name
from .mesh_topology import faces


def _safe_attributes(attributes):
    result = []
    for attribute in attributes:
        entry = dict(attribute)
        values = entry.get("values", [])
        if entry.get("decoded") and not all(
            all(math.isfinite(value) for value in row) for row in values
        ):
            entry["decoded"] = False
            entry.pop("values", None)
            entry["reason"] = "Nonfinite input values; raw bytes retained"
        result.append(entry)
    return result


def capture_vertex_source(
    controller, rd, pipeline, action, instance, data_stage,
    post_rows, kept_post_vertices, position_name=None,
):
    """Return input topology/attributes and a verified posed-to-input map.

    ``post_rows`` comes from ``post_mesh``: for each compact post-VS vertex it
    identifies the first corresponding row in the draw's input index stream.
    ``kept_post_vertices`` applies the later nonfinite-primitive filtering.
    This map is capture provenance only; consumers can bake input attributes
    into ordinary mutable mesh channels and discard it after import.
    """
    result = {
        "status": "unavailable",
        "extraction_stage": "VSIn",
        "coordinate_space": "vertex_input",
        "posed_mapping_status": "unavailable",
        "object_transform_status": "not_recovered",
        "pose_provenance": (
            "Inputs as bound at the draw, before the vertex shader. They "
            "may already contain an earlier compute deformation. The "
            "separate post-transform surface retains the captured pose."
        ),
    }
    try:
        if action.flags & rd.ActionFlags.MeshDispatch:
            raise MeshError(
                "Mesh shader output has no conventional VS input mapping"
            )
        topology = enum_name(pipeline.GetPrimitiveTopology())
        stream, source_ids, local_index, attributes = _input_data(
            controller, rd, pipeline, action, instance, {}
        )
        result.update(
            status="captured",
            topology=topology,
            source_vertex_ids=list(source_ids),
            index_stream=list(stream),
            attributes=_safe_attributes(attributes),
            position_attribute=None,
        )
        indexed = bool(action.flags & rd.ActionFlags.Indexed)
        assembly = {
            "indexed": indexed,
            "vertex_offset": action.vertexOffset,
            "instance_offset": action.instanceOffset,
            "instance": instance,
            "num_indices": action.numIndices,
        }
        if indexed:
            binding = pipeline.GetIBuffer()
            assembly.update(
                resource_id=str(binding.resourceId),
                binding_byte_offset=binding.byteOffset,
                binding_byte_size=binding.byteSize,
                byte_stride=binding.byteStride,
                index_offset=action.indexOffset,
                base_vertex=action.baseVertex,
                restart_index=(
                    pipeline.GetRestartIndex()
                    if pipeline.IsRestartEnabled() else None
                ),
            )
        result["input_assembly"] = assembly
        if topology.startswith("PatchList"):
            result["faces"] = []
            result["surface_status"] = "tessellation_control_points"
        else:
            result["faces"] = [
                [local_index(index) for index in polygon]
                for polygon in faces(topology, stream)
            ]
            result["surface_status"] = "input_assembly"
        position = _find_position(result["attributes"], position_name)
        if position is not None and position.get("decoded"):
            result["position_attribute"] = position["name"]
            result["position_role_source"] = (
                "explicit override" if position_name else "attribute name"
            )
            result["positions"] = [
                list((tuple(value) + (0, 0, 0))[:3])
                for value in position["values"]
            ]
        elif position_name is not None:
            result["position_reason"] = (
                "Requested position is not decodable as finite values"
            )
        else:
            result["position_reason"] = (
                "No identifiable position semantic; attributes retained "
                "without guessing a shader input location"
            )
        if data_stage != rd.MeshDataStage.VSOut:
            result["posed_mapping_reason"] = (
                "Generated geometry does not have a verified one-to-one "
                "vertex shader input correspondence"
            )
            return result
        if any(
            vertex < 0 or vertex >= len(post_rows)
            for vertex in kept_post_vertices
        ):
            raise MeshError("Retained posed vertex is outside post-VS rows")
        rows = [post_rows[vertex] for vertex in kept_post_vertices]
        if any(
            row < 0 or row >= len(stream) or stream[row] is None
            for row in rows
        ):
            raise MeshError("Post-VS row does not map to an input vertex")
        result["posed_vertex_map"] = [
            local_index(stream[row]) for row in rows
        ]
        result["posed_mapping_status"] = "captured"
        result["posed_mapping_method"] = (
            "post-VS index row to original draw index stream"
        )
    except (ValueError, RuntimeError, AttributeError, IndexError) as error:
        result["reason"] = str(error)
        # Read failures must not erase an otherwise valid posed mesh. If the
        # inputs were captured but their map failed, they remain independently
        # useful as source evidence, with no fabricated posed correspondence.
        if result["status"] == "captured":
            result["posed_mapping_reason"] = str(error)
    return result
