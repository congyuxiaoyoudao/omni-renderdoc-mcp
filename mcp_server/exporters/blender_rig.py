"""Keep editable input-space rigs separate from captured posed geometry."""

import json
import math

COLLECTION = "Rigged source meshes (vertex input)"
ROLE = "rigged_source"


def matrix_rows(values, rigid=False):
    """Convert FBX column arrays (translation 12..14) to matrix rows."""
    if (
        not isinstance(values, (list, tuple))
        or len(values) != 16
        or any(
            type(v) not in (int, float) or not math.isfinite(v) for v in values
        )
        or any(
            abs(values[i] - expected) > 1e-7
            for i, expected in zip((3, 7, 11, 15), (0, 0, 0, 1))
        )
    ):
        raise ValueError("Bind matrix must be finite and affine")
    basis = [values[i : i + 3] for i in (0, 4, 8)]
    determinant = sum(
        basis[0][i]
        * (
            basis[1][(i + 1) % 3] * basis[2][(i + 2) % 3]
            - basis[1][(i + 2) % 3] * basis[2][(i + 1) % 3]
        )
        for i in range(3)
    )
    if abs(determinant) < 1e-10:
        raise ValueError("Bind matrix is singular")
    if rigid and (
        abs(determinant - 1) > 1e-5
        or any(
            abs(
                sum(a * b for a, b in zip(basis[i], basis[j]))
                - (1 if i == j else 0)
            )
            > 1e-5
            for i in range(3)
            for j in range(3)
        )
    ):
        raise ValueError(
            "Blender edit bones require rigid bind matrices; "
            "bone scale, reflection or shear cannot be preserved"
        )
    return [[values[col * 4 + row] for col in range(4)] for row in range(4)]


def validate_source(source):
    positions, faces = source.get("positions"), source.get("faces")
    if (
        not positions
        or not faces
        or any(
            len(row) != 3
            or any(
                type(v) not in (float, int) or not math.isfinite(v)
                for v in row
            )
            for row in positions
        )
    ):
        raise ValueError("Rigged source requires finite input positions")
    if any(
        len(face) < 3
        or any(type(i) is not int or not 0 <= i < len(positions) for i in face)
        for face in faces
    ):
        raise ValueError("Invalid rigged source face indices")
    skeleton = source.get("skeleton", {})
    bones = skeleton.get("bones", [])
    if not bones:
        raise ValueError("Rigged source requires bones")
    names = []
    for bone in bones:
        name = bone.get("name")
        if (
            not isinstance(name, str)
            or not name
            or "\x00" in name
            or len(name.encode("utf-8")) > 63
            or name in names
        ):
            raise ValueError("Bone names must be unique and fit Blender IDs")
        names.append(name)
        matrix_rows(bone.get("bind_matrix"), rigid=True)
    for start in range(len(bones)):
        seen, current = set(), start
        while current != -1:
            if (
                type(current) is not int
                or not 0 <= current < len(bones)
                or current in seen
            ):
                raise ValueError("Invalid or cyclic bone hierarchy")
            seen.add(current)
            current = bones[current].get("parent")
    influences = skeleton.get("influences")
    if not isinstance(influences, list) or len(influences) != len(positions):
        raise ValueError("Skin weights must match the input vertex count")
    for row in influences:
        totals = {}
        if not isinstance(row, (list, tuple)):
            raise ValueError("Invalid vertex influences")
        for pair in row:
            if (
                not isinstance(pair, (list, tuple))
                or len(pair) != 2
                or type(pair[0]) is not int
                or not 0 <= pair[0] < len(bones)
                or type(pair[1]) not in (float, int)
                or not math.isfinite(pair[1])
                or pair[1] < 0
            ):
                raise ValueError("Invalid vertex influence")
            totals[pair[0]] = totals.get(pair[0], 0) + pair[1]
        if any(value > 1 for value in totals.values()):
            raise ValueError("Blender vertex group weights cannot exceed one")
    if "mesh_bind_matrix" in skeleton:
        matrix_rows(skeleton["mesh_bind_matrix"])
    return skeleton


def create_rigged_source(bpy, scene, record):
    """Return coverage even when the capture has no recoverable skin."""
    report = {
        "event_id": record.get("event_id"),
        "instance": record.get("instance", 0),
        "status": record.get("skin", "unavailable"),
        "provenance": record.get("skin_provenance") or {},
    }
    source = record.get("rigged_source")
    if not source:
        return report
    try:
        skeleton = validate_source(source)
    except (ValueError, TypeError, KeyError) as error:
        report.update(status="unsupported", reason=str(error))
        return report
    from mathutils import Matrix

    collection = next(
        (
            c
            for c in scene.collection.children
            if c.get("capture_role") == ROLE
        ),
        None,
    )
    if collection is None:
        collection = bpy.data.collections.new(COLLECTION)
        scene.collection.children.link(collection)
        collection["capture_role"] = ROLE
        collection["usage"] = (
            "Enable this collection in the viewport to edit source rigs. "
            "Input-space bind meshes are separate from captured scene poses."
        )
    name = source.get("name", record.get("name", "Captured input"))
    mesh = bpy.data.meshes.new(name + " - rigged input")
    mesh.from_pydata(source["positions"], [], source["faces"])
    mesh.update()
    from .blender_scene import add_uv_layer
    from .generic_nodes import add_attributes

    attributes, omitted_attributes = [], []
    for attribute in source.get("attributes", []):
        values = attribute.get("values", [])
        if (
            not attribute.get("decoded", True)
            or not values
            or len(values) != len(mesh.vertices)
            or not 1 <= len(values[0]) <= 4
            or any(
                len(row) != len(values[0])
                or any(
                    type(value) not in (float, int) or not math.isfinite(value)
                    for value in row
                )
                for row in values
            )
        ):
            omitted_attributes.append(attribute.get("name", "unnamed"))
        else:
            attributes.append(attribute)
    add_attributes(mesh, attributes)
    for attribute in attributes:
        values = attribute.get("values", [])
        if (
            attribute.get("decoded", True)
            and len(values) == len(mesh.vertices)
            and values
        ):
            if attribute.get("kind") == "uv":
                add_uv_layer(mesh, attribute["name"], values)
            elif attribute.get("kind") == "normal" and all(
                len(value) >= 3 for value in values
            ):
                mesh.normals_split_custom_set_from_vertices(
                    [value[:3] for value in values]
                )
    mesh["source_vertex_ids"] = json.dumps(source.get("source_vertex_ids"))
    obj = bpy.data.objects.new(mesh.name, mesh)
    collection.objects.link(obj)
    mesh_matrix = skeleton.get("mesh_bind_matrix")
    if mesh_matrix:
        obj.matrix_world = Matrix(matrix_rows(mesh_matrix))
    armature = bpy.data.armatures.new(name + " - skeleton")
    rig = bpy.data.objects.new(armature.name, armature)
    collection.objects.link(rig)
    for item in (obj, rig):
        item["capture_role"] = ROLE
        item["event_id"] = record.get("event_id", -1)
        item["instance"] = record.get("instance", 0)
        item["coordinate_space"] = "vertex_input_bind"
        item["skin_provenance"] = json.dumps(report["provenance"])
        item.hide_render = True
    previous = bpy.context.view_layer.objects.active
    selected = list(bpy.context.selected_objects)
    for item in selected:
        item.select_set(False)
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        created = []
        for bone in skeleton["bones"]:
            edit = armature.edit_bones.new(bone["name"])
            edit.head, edit.tail = (0, 0, 0), (0, 0.1, 0)
            edit.matrix = Matrix(matrix_rows(bone["bind_matrix"], rigid=True))
            created.append(edit)
        for index, bone in enumerate(skeleton["bones"]):
            if bone["parent"] >= 0:
                created[index].parent = created[bone["parent"]]
            created[index].use_connect = False
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")
        rig.select_set(False)
        for item in selected:
            item.select_set(True)
        bpy.context.view_layer.objects.active = previous
    groups = [
        obj.vertex_groups.new(name=bone["name"]) for bone in skeleton["bones"]
    ]
    for index, row in enumerate(skeleton["influences"]):
        totals = {}
        for bone, weight in row:
            totals[bone] = totals.get(bone, 0) + weight
        for bone, weight in totals.items():
            if weight:
                groups[bone].add([index], weight, "REPLACE")
    modifier = obj.modifiers.new("Captured source skin", "ARMATURE")
    modifier.object = rig
    modifier.use_vertex_groups = True
    modifier.use_bone_envelopes = False
    # Keep the bind object's world transform when parenting to the armature.
    obj.parent = rig
    obj.matrix_parent_inverse = rig.matrix_world.inverted()
    rig.show_in_front = True
    collection.hide_render = True
    # Layer exclusion is applied after all records have been built.
    report.update(
        status=source.get("skin", report["status"]),
        provenance=source.get("skin_provenance", report["provenance"]),
        mesh=obj.name,
        armature=rig.name,
        bone_count=len(groups),
        vertex_count=len(mesh.vertices),
        collection=collection.name,
        captured_pose_object=record.get("name"),
        limitations=[
            "Input-space rig is separate from the visible captured pose; "
            "camera/world transforms and animation are not recovered.",
            "Blender normalizes nonzero vertex influences during deformation.",
            "Bone display lengths are placeholders. Source mesh materials "
            "are unassigned because captured post-transform fragment inputs "
            "do not describe arbitrary bind-pose shading.",
        ],
    )
    if omitted_attributes:
        report["omitted_blender_attributes"] = omitted_attributes
    if report["status"] == "captured_joint_palette":
        report["limitations"].append(
            "Joint palette uses identity placeholder bind matrices and "
            "generated names; original pivots and hierarchy are unknown."
        )
    obj["skin_status"] = rig["skin_status"] = report["status"]
    obj["skin_provenance"] = rig["skin_provenance"] = json.dumps(
        report["provenance"]
    )
    return report


def store_skinning(scene, reports):
    coverage = {
        "rigged_source_count": sum("armature" in row for row in reports),
        "bound_source_count": sum(
            row["status"] == "bound" and "armature" in row for row in reports
        ),
        "palette_source_count": sum(
            row["status"] == "captured_joint_palette" and "armature" in row
            for row in reports
        ),
        "draws": reports,
    }
    scene["skinning_coverage"] = json.dumps(coverage)
    for layer in scene.view_layers:
        for child in layer.layer_collection.children:
            if child.collection.get("capture_role") == ROLE:
                child.hide_viewport = True
    return coverage


def skinning_coverage(scene):
    return json.loads(scene.get("skinning_coverage", "{}"))
