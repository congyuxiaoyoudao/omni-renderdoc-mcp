"""Map capture joint channels to explicitly supplied skeleton bind data."""

import math
import re

from ..utils.fbx_transform import IDENTITY
from .mesh_formats import MeshError


def _skin_role(name):
    token = re.sub(r"[^a-z0-9]", "", name.lower())
    match = re.search(
        r"(blendindices|blendweights?|joints|weights)(\d*)$", token
    )
    if match is None:
        return None
    role = match.group(1)
    return (
        "joints" if role in ("joints", "blendindices") else "weights",
        int(match.group(2) or 0),
    )


def has_skin_channels(attributes):
    return any(_skin_role(a["name"]) for a in attributes)


def _channel_names(spec, attributes):
    if "joints" in spec or "weights" in spec:
        joints, weights = spec.get("joints"), spec.get("weights")
        if (
            not isinstance(joints, list)
            or not isinstance(weights, list)
            or not joints
            or any(not isinstance(name, str) for name in joints + weights)
            or len(joints) != len(weights)
        ):
            raise MeshError(
                "Specify matching joints and weights channel lists"
            )
        return list(zip(joints, weights))
    roles = {}
    for attr in attributes:
        role = _skin_role(attr["name"])
        if role is not None:
            if role in roles:
                raise MeshError("Ambiguous skin channels; specify their names")
            roles[role] = attr["name"]
    slots = sorted({slot for _, slot in roles})
    if not slots or any(
        (r, s) not in roles for s in slots for r in ("joints", "weights")
    ):
        raise MeshError("Missing paired joint-index and weight attributes")
    return [(roles["joints", i], roles["weights", i]) for i in slots]


def _from_channels(spec, attributes, count):
    names = {attr["name"]: attr for attr in attributes}
    pairs = _channel_names(spec, attributes)
    if any(j not in names or w not in names for j, w in pairs):
        raise MeshError("Specified joint or weight channel is absent")
    influences = [{} for _ in range(count)]
    for joints, weights in pairs:
        if any(not names[n].get("decoded", True) for n in (joints, weights)):
            raise MeshError("Undecoded skin attribute format")
        for out, indices, amounts in zip(
            influences, names[joints]["values"], names[weights]["values"]
        ):
            if len(indices) != len(amounts):
                raise MeshError("Joint and weight components do not match")
            for index, weight in zip(indices, amounts):
                if not math.isfinite(weight) or weight < 0:
                    raise MeshError("Invalid captured skin weight")
                if weight:
                    if (
                        not math.isfinite(index)
                        or index < 0
                        or index != int(index)
                    ):
                        raise MeshError("Invalid captured joint index")
                    joint = int(index)
                    out[joint] = out.get(joint, 0) + weight
    rows = [[[j, w] for j, w in point.items()] for point in influences]
    return rows, pairs


def captured_palette(spec, attributes, count):
    """Expose observed joint IDs and influences as a flat palette."""
    if not isinstance(spec, dict) or set(spec) - {"joints", "weights"}:
        raise MeshError(
            "Provide bones for an explicit rig, or only joints/weights"
        )
    influences, channels = _from_channels(spec, attributes, count)
    joint_ids = sorted({joint for row in influences for joint, _ in row})
    if not joint_ids:
        raise MeshError("No nonzero weights reference a captured joint")
    dense_index = {joint: i for i, joint in enumerate(joint_ids)}
    bones = [
        {
            "name": "PaletteJoint_{}".format(i),
            "parent": -1,
            "bind_matrix": IDENTITY,
        }
        for i in joint_ids
    ]
    rig = {
        "bones": bones,
        "influences": [
            [[dense_index[j], w] for j, w in row] for row in influences
        ],
    }
    provenance = {
        "captured_joint_ids": joint_ids,
        "channels": channels,
        "hierarchy": "unknown; all palette nodes are roots",
        "names": "generated from captured palette indices",
        "bind_matrices": "identity placeholders, not captured",
    }
    return rig, provenance


def bind_skin(spec, attributes, source_ids):
    if not isinstance(spec, dict) or not spec.get("bones"):
        raise MeshError(
            "A skeleton must provide bones and global bind matrices"
        )
    if set(spec) - {
        "bones",
        "mesh_bind_matrix",
        "joints",
        "weights",
        "influences",
    }:
        raise MeshError("Unrecognized field in skeleton mapping")
    if not isinstance(spec["bones"], list):
        raise MeshError("bones must be a list of bone descriptors")
    matrices = []
    for i, bone in enumerate(spec["bones"]):
        if (
            not isinstance(bone, dict)
            or not isinstance(bone.get("name"), str)
            or type(bone.get("parent")) is not int
            or "bind_matrix" not in bone
        ):
            raise MeshError(
                "Bone {} requires name, parent and bind_matrix".format(i)
            )
        matrices.append(bone["bind_matrix"])
    if "mesh_bind_matrix" in spec:
        matrices.append(spec["mesh_bind_matrix"])
    if any(
        not isinstance(m, (list, tuple))
        or len(m) != 16
        or any(type(v) not in (int, float) or not math.isfinite(v) for v in m)
        for m in matrices
    ):
        raise MeshError("Bind matrices require 16 finite numbers")
    if "influences" in spec:
        table = spec["influences"]
        if isinstance(table, dict):
            if any(str(i) not in table for i in source_ids):
                raise MeshError("Missing influences for a source vertex")
            influences = [table[str(i)] for i in source_ids]
        elif isinstance(table, list) and all(
            0 <= i < len(table) for i in source_ids
        ):
            influences = [table[i] for i in source_ids]
        else:
            raise MeshError("Invalid source-vertex influences table")
    else:
        influences, _ = _from_channels(spec, attributes, len(source_ids))
    for row in influences:
        if not isinstance(row, (list, tuple)) or any(
            not isinstance(pair, (list, tuple))
            or len(pair) != 2
            or type(pair[0]) is not int
            or not 0 <= pair[0] < len(spec["bones"])
            or type(pair[1]) not in (int, float)
            or not math.isfinite(pair[1])
            or pair[1] < 0
            for pair in row
        ):
            raise MeshError("Invalid explicit rig influence index or weight")
    result = {"bones": spec["bones"], "influences": influences}
    if "mesh_bind_matrix" in spec:
        result["mesh_bind_matrix"] = spec["mesh_bind_matrix"]
    return result
