"""Write one indexed mesh and optional bind skeleton as ASCII FBX 7.4."""

import itertools
import math
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from .fbx_attributes import attribute_properties
from .fbx_transform import IDENTITY, affine, decompose, inverse, multiply

# FBX object IDs 0..4 are reserved here for root, mesh, model, skin and pose.
GEOMETRY, MODEL, SKIN, POSE = range(1, 5)
LAYERS = {
    "normal": ("Normal", "Normals", (0, 0, 0)),
    "tangent": ("Tangent", "Tangents", (0, 0, 0)),
    "binormal": ("Binormal", "Binormals", (0, 0, 0)),
    "uv": ("UV", "UV", (0, 0)),
    "color": ("Color", "Colors", (0, 0, 0, 1)),
}


def _label(name):
    return quote(name, safe=" !$'()+,-.=@[]^_{}~")


def _number(value):
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        raise ValueError("FBX geometry and transforms must be finite")
    return repr(value)


class _Writer:
    def __init__(self, stream):
        self.stream = stream
        self.level = 0

    def line(self, text):
        self.stream.write("    " * self.level + text + "\n")

    @contextmanager
    def block(self, header):
        self.line(header + " {")
        self.level += 1
        yield
        self.level -= 1
        self.line("}")

    def array(self, name, count, values):
        values = iter(values)
        with self.block("{}: *{}".format(name, count)):
            prefix = "a: "
            for start in range(0, count, 256):
                end = min(count, start + 256)
                chunk = itertools.islice(values, end - start)
                comma = "," if end < count else ""
                self.line(prefix + ",".join(map(_number, chunk)) + comma)
                prefix = ""
            if not count:
                self.line(prefix)


def _model(out, ident, name, kind, matrix):
    with out.block('Model: {}, "Model::{}", "{}"'.format(ident, name, kind)):
        out.line("Version: 232")
        with out.block("Properties70:"):
            # RrSs would reorder a parent's scale for rotated children.
            out.line('P: "InheritType", "enum", "", "", 1')
            for prop, v in zip(
                ("Translation", "Rotation", "Scaling"), decompose(matrix)
            ):
                label = "Lcl " + prop
                out.line(
                    'P: "{0}", "{0}", "", "A", {1}'.format(
                        label, ",".join(map(_number, v))
                    )
                )


def _attributes(out, attributes, count):
    with out.block("Properties70:"):
        for name, value in attribute_properties(attributes, count):
            if isinstance(value, int):
                line = 'P: "{}", "int", "Integer", "U", {}'
            else:
                line = 'P: "{}", "KString", "", "U", "{}"'
            out.line(line.format(name, value))


def _layers(out, attributes, count):
    versions = Counter()
    layers = defaultdict(list)
    for attribute in attributes:
        kind = attribute.get("kind")
        if kind not in LAYERS:
            continue
        suffix, key, default = LAYERS[kind]
        name = "LayerElement" + suffix
        index = versions[name]
        versions[name] += 1
        layers[index].append(name)
        with out.block("{}: {}".format(name, index)):
            out.line("Version: 101")
            out.line('Name: "{}"'.format(_label(attribute["name"])))
            out.line('MappingInformationType: "ByVertice"')
            out.line('ReferenceInformationType: "Direct"')
            values = (
                row[i] if i < len(row) and math.isfinite(row[i]) else fallback
                for row in attribute["values"]
                for i, fallback in enumerate(default)
            )
            out.array(key, count * len(default), values)
            rows = attribute["values"]
            if (
                kind in ("normal", "tangent", "binormal")
                and rows
                and len(rows[0]) >= 4
            ):
                out.array(
                    key + "W",
                    count,
                    (r[3] if math.isfinite(r[3]) else 0 for r in rows),
                )
    for index, names in sorted(layers.items()):
        with out.block("Layer: {}".format(index)):
            out.line("Version: 100")
            for name in names:
                with out.block("LayerElement:"):
                    out.line('Type: "{}"'.format(name))
                    out.line("TypedIndex: {}".format(index))


def _geometry(out, mesh, name):
    positions = mesh["positions"]
    polygons = mesh["polygons"]
    count = len(positions)
    if any(len(v) != 3 for v in positions):
        raise ValueError("Expected XYZ positions")
    if any(
        not face
        or any(not isinstance(i, int) or i < 0 or i >= count for i in face)
        for face in polygons
    ):
        raise ValueError("Polygon references an invalid control point")
    with out.block(
        'Geometry: {}, "Geometry::{}", "Mesh"'.format(GEOMETRY, name)
    ):
        out.line("GeometryVersion: 124")
        out.array(
            "Vertices", count * 3, itertools.chain.from_iterable(positions)
        )
        indices = (
            index if col < len(face) - 1 else ~index
            for face in polygons
            for col, index in enumerate(face)
        )
        out.array("PolygonVertexIndex", sum(map(len, polygons)), indices)
        attributes = mesh.get("attributes", [])
        _attributes(out, attributes, count)
        _layers(out, attributes, count)


def _hierarchy(bones):
    parents = [b["parent"] for b in bones]
    done = set()
    for i in range(len(bones)):
        node = i
        visited = set()
        while node != -1 and node not in done:
            if (
                type(node) is not int
                or node in visited
                or not 0 <= node < len(bones)
            ):
                raise ValueError("Invalid bone parent or cyclic hierarchy")
            visited.add(node)
            node = parents[node]
        done.update(visited)
    return parents


def _clusters(bones, influences, count):
    if len(influences) != count:
        raise ValueError("Expected one influence list per control point")
    grouped = [[] for _ in bones]
    for point, row in enumerate(influences):
        seen = set()
        for index, weight in row:
            if (
                type(index) is not int
                or not 0 <= index < len(bones)
                or not math.isfinite(weight)
                or weight < 0
                or index in seen
            ):
                raise ValueError("Invalid bone index or skin weight")
            seen.add(index)
            if weight:
                grouped[index].append((point, weight))
    return grouped


def _bone_id(index):
    return 10 + index * 3


def _skeleton(out, skeleton, mesh_matrix, count, links):
    bones = skeleton["bones"]
    if not bones:
        raise ValueError("Skeleton requires at least one bone")
    parents = _hierarchy(bones)
    weights = _clusters(bones, skeleton["influences"], count)
    matrices = [affine(bone["bind_matrix"]) for bone in bones]
    inverses = [inverse(m) for m in matrices]
    with out.block('Deformer: {}, "Deformer::Skin", "Skin"'.format(SKIN)):
        out.line("Version: 101")
        out.line('SkinningType: "Linear"')
    links.append((SKIN, GEOMETRY))
    pose_nodes = [(MODEL, mesh_matrix)]
    for i, bone in enumerate(bones):
        model = _bone_id(i)
        attribute, cluster = model + 1, model + 2
        name = _label(bone["name"])
        parent = parents[i]
        local = matrices[i]
        if parent >= 0:
            local = multiply(local, inverses[parent])
        _model(out, model, name, "LimbNode", local)
        with out.block(
            'NodeAttribute: {}, "NodeAttribute::{}", "LimbNode"'.format(
                attribute, name
            )
        ):
            out.line('TypeFlags: "Skeleton"')
        with out.block(
            'Deformer: {}, "SubDeformer::{}", "Cluster"'.format(cluster, name)
        ):
            out.line("Version: 100")
            rows = weights[i]
            out.array("Indexes", len(rows), (r[0] for r in rows))
            out.array("Weights", len(rows), (r[1] for r in rows))
            out.array("Transform", 16, multiply(mesh_matrix, inverses[i]))
            out.array("TransformLink", 16, matrices[i])
        links.extend(
            [
                (attribute, model),
                (cluster, SKIN),
                (model, cluster),
                (model, _bone_id(parent) if parent >= 0 else 0),
            ]
        )
        pose_nodes.append((model, matrices[i]))
    with out.block('Pose: {}, "Pose::BindPose", "BindPose"'.format(POSE)):
        out.line('Type: "BindPose"')
        out.line("Version: 100")
        out.line("NbPoseNodes: {}".format(len(pose_nodes)))
        for model, matrix in pose_nodes:
            with out.block("PoseNode:"):
                out.line("Node: {}".format(model))
                out.array("Matrix", 16, matrix)


def _scene(out, mesh, skeleton):
    name = _label(mesh["name"])
    out.line("; FBX 7.4.0 project file")
    with out.block("FBXHeaderExtension:"):
        out.line("FBXHeaderVersion: 1003")
        out.line("FBXVersion: 7400")
        out.line('Creator: "omni-renderdoc-mcp"')
    types = {"Model": 1, "Geometry": 1}
    matrix = IDENTITY
    if skeleton is not None:
        n = len(skeleton["bones"])
        matrix = affine(skeleton.get("mesh_bind_matrix", IDENTITY))
        types.update(Model=n + 1, NodeAttribute=n, Deformer=n + 1, Pose=1)
    with out.block("Definitions:"):
        out.line("Version: 100")
        out.line("Count: {}".format(sum(types.values())))
        for kind, count in types.items():
            with out.block('ObjectType: "{}"'.format(kind)):
                out.line("Count: {}".format(count))
    links = [(GEOMETRY, MODEL), (MODEL, 0)]
    with out.block("Objects:"):
        _model(out, MODEL, name, "Mesh", matrix)
        _geometry(out, mesh, name)
        if skeleton is not None:
            _skeleton(out, skeleton, matrix, len(mesh["positions"]), links)
    with out.block("Connections:"):
        for child, parent in links:
            out.line('C: "OO",{},{}'.format(child, parent))


def write_fbx(path, mesh, skeleton=None):
    """Create a new ASCII FBX and discard partial output on failure."""
    path = Path(path)
    stream = path.open("x", encoding="ascii", newline="\n")
    complete = False
    try:
        with stream:
            _scene(_Writer(stream), mesh, skeleton)
        complete = True
    finally:
        if not complete:
            path.unlink()
