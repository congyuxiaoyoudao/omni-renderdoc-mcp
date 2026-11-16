"""Bake captured data into ordinary, independently editable Unity meshes.

Captured vertex IDs are used only while importing source channels. The output
contains complete mesh attributes and topology, with no replay buffers or
dependency on the number/order of vertices in the capture.
"""

from __future__ import annotations

import gzip
import json
import math
import re
import struct
from pathlib import Path


def _vectors(values, dimension, label):
    if not isinstance(values, (list, tuple)):
        raise ValueError(label + " must be a list")
    if values and isinstance(values[0], (list, tuple)):
        rows = [list(row) for row in values]
    else:
        if len(values) % dimension:
            raise ValueError(label + " has an incomplete vertex")
        rows = [list(values[i:i + dimension])
                for i in range(0, len(values), dimension)]
    if any(len(row) != dimension or not all(
        isinstance(value, (int, float)) and math.isfinite(value)
        for value in row
    ) for row in rows):
        raise ValueError(label + " must contain finite vectors")
    return rows


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0]]


def _normalize(value):
    length = math.sqrt(_dot(value, value))
    return [v / length for v in value] if length > 1e-20 else [0.0] * 3


def _solve(matrix, rhs):
    size = len(rhs)
    rows = [list(row) + [value] for row, value in zip(matrix, rhs)]
    scale = max(abs(value) for row in matrix for value in row)
    if not scale:
        raise ValueError("Affine fit is singular")
    for column in range(size):
        pivot = max(range(column, size), key=lambda i: abs(rows[i][column]))
        if abs(rows[pivot][column]) <= scale * 1e-12:
            raise ValueError("Affine fit is rank deficient")
        rows[column], rows[pivot] = rows[pivot], rows[column]
        divisor = rows[column][column]
        rows[column] = [value / divisor for value in rows[column]]
        for row in range(size):
            if row == column:
                continue
            factor = rows[row][column]
            rows[row] = [a - factor * b
                         for a, b in zip(rows[row], rows[column])]
    return [row[-1] for row in rows]


def fit_geometry_to_shading(positions, shading_positions, tolerance=1e-5):
    """Fit a verified position channel, never infer its semantic/location.

    The fit uses all vertices and rejects a planar/degenerate point cloud or
    residual exceeding ``tolerance`` in shading-space units. A caller must
    supply a position channel whose meaning has independently been verified.
    """
    geometry = _vectors(positions, 3, "positions")
    shading = _vectors(shading_positions, 3, "shading positions")
    if len(geometry) != len(shading) or len(geometry) < 4:
        raise ValueError("Affine fit needs at least four corresponding points")
    if not math.isfinite(tolerance) or tolerance <= 0:
        raise ValueError("Affine tolerance must be finite and positive")
    count = len(geometry)
    mean_g = [sum(row[i] for row in geometry) / count for i in range(3)]
    mean_s = [sum(row[i] for row in shading) / count for i in range(3)]
    covariance = [[0.0] * 3 for _ in range(3)]
    cross = [[0.0] * 3 for _ in range(3)]
    for g, s in zip(geometry, shading):
        x = [g[i] - mean_g[i] for i in range(3)]
        y = [s[i] - mean_s[i] for i in range(3)]
        for i in range(3):
            for j in range(3):
                covariance[i][j] += x[i] * x[j]
                cross[i][j] += x[i] * y[j]
    matrix = []
    for axis in range(3):
        linear = _solve(covariance, [cross[i][axis] for i in range(3)])
        matrix.append(linear + [mean_s[axis] - _dot(linear, mean_g)])
    matrix.append([0.0, 0.0, 0.0, 1.0])
    errors = [math.sqrt(sum(
        (_dot(matrix[i][:3], g) + matrix[i][3] - s[i]) ** 2
        for i in range(3)
    )) for g, s in zip(geometry, shading)]
    maximum = max(errors)
    if maximum > tolerance:
        raise ValueError("Position channel is not affine within tolerance")
    return {"matrix": matrix, "max_error": maximum,
            "rms_error": math.sqrt(sum(e * e for e in errors) / count),
            "vertex_count": count, "tolerance": tolerance}


def _linear(matrix):
    rows = _vectors(matrix, 4, "geometry_to_shading")
    if len(rows) != 4 or any(abs(a - b) > 1e-8 for a, b in
                             zip(rows[3], [0, 0, 0, 1])):
        raise ValueError("geometry_to_shading must be an affine 4x4 matrix")
    return [row[:3] for row in rows[:3]]


def _directions(values, role, space, matrix):
    if space == "geometry":
        return [_normalize(row[:3]) + (row[3:4] if role == "tangent" else [])
                for row in values]
    if space != "shading" or matrix is None:
        raise ValueError(role + " requires a verified geometry/shading space")
    linear = _linear(matrix)
    # p_shading = A p_geometry + t. Thus n_geometry = transpose(A)n_shading,
    # while tangent_geometry = inverse(A)t_shading. Translation never applies.
    determinant = _dot(linear[0], _cross(linear[1], linear[2]))
    if abs(determinant) <= 1e-12:
        raise ValueError("geometry_to_shading has a singular linear transform")
    result = []
    for row in values:
        if role == "normal":
            direction = [sum(linear[j][i] * row[j] for j in range(3))
                         for i in range(3)]
        else:
            direction = _solve(linear, row[:3])
        value = _normalize(direction)
        if role == "tangent":
            # A reflected basis changes tangent-space handedness.
            value.append(row[3] * (-1 if determinant < 0 else 1))
        result.append(value)
    return result


def _source_uv_channel(attribute):
    if attribute.get("kind") != "uv" and (
        str(attribute.get("semantic", "")).upper() != "TEXCOORD"
    ):
        return None
    if "semantic_index" in attribute:
        return int(attribute["semantic_index"])
    match = re.search(r"(?:TEXCOORD|UV)[_ ]*(\d*)$",
                      attribute.get("name", ""), re.IGNORECASE)
    return int(match.group(1) or 0) if match else None


def _default_mapping(mesh):
    source = mesh.get("source_geometry", {})
    if source.get("posed_mapping_status") != "captured":
        return {}
    result = {}
    for attribute in source.get("attributes", []):
        if not attribute.get("decoded"):
            continue
        channel = (None if attribute.get("per_instance")
                   else _source_uv_channel(attribute))
        if channel is not None and 0 <= channel <= 7:
            result.setdefault("uv" + str(channel),
                              {"source": "input", "name": attribute["name"]})
        if attribute.get("kind") == "color":
            result.setdefault("color", {"source": "input",
                                        "name": attribute["name"]})
    return result


def _channel(mesh, selector, vertex_count):
    source = selector.get("source", "post")
    if source == "input":
        geometry = mesh.get("source_geometry", {})
        attributes = geometry.get("attributes", [])
        mapping = geometry.get("posed_vertex_map")
        if geometry.get("posed_mapping_status") != "captured":
            raise ValueError("Input attribute has no verified posed mapping")
    elif source == "post":
        attributes, mapping = mesh.get("attributes", []), None
    else:
        raise ValueError("Attribute source must be input or post")
    keys = [key for key in ("name", "location") if key in selector]
    if not keys:
        raise ValueError("Attribute selector needs a name or location")
    matches = [attribute for attribute in attributes if all(
        attribute.get(key) == selector[key] for key in keys
    )]
    if len(matches) != 1 or not matches[0].get("decoded"):
        raise ValueError(
            "Attribute selector must identify one decoded channel"
        )
    values = matches[0].get("values", [])
    if mapping is not None:
        if len(mapping) != vertex_count or any(
            not isinstance(row, int) or row < 0 or row >= len(values)
            for row in mapping
        ):
            raise ValueError("Input attribute mapping is invalid")
        values = [values[row] for row in mapping]
    if len(values) != vertex_count or not values:
        raise ValueError("Attribute vertex count does not match geometry")
    components = selector.get("components")
    if components is not None:
        if not isinstance(components, list) or not components or any(
            not isinstance(i, int) or i < 0 or i >= len(row)
            for row in values for i in components
        ):
            raise ValueError("Attribute component selector is invalid")
        values = [[row[i] for i in components] for row in values]
    return _vectors(values, len(values[0]), "attribute")


def _surface_normals(vertices, triangles):
    normals = [[0.0, 0.0, 0.0] for _ in vertices]
    for start in range(0, len(triangles), 3):
        ids = triangles[start:start + 3]
        a, b, c = [vertices[i] for i in ids]
        face = _cross([b[i] - a[i] for i in range(3)],
                      [c[i] - a[i] for i in range(3)])
        for index in ids:
            for axis in range(3):
                normals[index][axis] += face[axis]
    return [_normalize(row) for row in normals]


def convert_native_mesh(mesh, attribute_mapping=None, *, pivot=None,
                        geometry_to_shading=None):
    """Create complete ordinary mesh data using only verified channel roles.

    Named input UV/color channels take precedence over an explicit post-VS
    fallback. Input normals/tangents are never treated as posed directions
    automatically. Missing normals are computed from the actual topology.
    """
    vertices = _vectors(mesh.get("positions", mesh.get("vertices", [])),
                        3, "vertices")
    if not vertices:
        raise ValueError("Mesh has no vertices")
    triangles = mesh.get("triangles")
    if triangles is None:
        faces = mesh.get("faces", [])
        if any(len(face) != 3 for face in faces):
            raise ValueError("Native surface requires triangle primitives")
        triangles = [index for face in faces for index in face]
    if not isinstance(triangles, list) or len(triangles) % 3 or any(
        not isinstance(index, int) or isinstance(index, bool)
        or index < 0 or index >= len(vertices) for index in triangles
    ):
        raise ValueError("Triangle indices are outside the mesh or incomplete")
    if not triangles:
        raise ValueError("Mesh has no triangle primitives")
    mesh = dict(mesh)
    mesh["attributes"] = list(mesh.get("attributes", []))
    mapping = dict(attribute_mapping or {})
    # These are declared mesh fields, not inferred meanings of shader
    # TEXCOORD locations. Retain compatibility with ordinary mesh exporters.
    for role, field, dimension in (
        ("uv0", "uvs", 2), ("normal", "normals", 3),
        ("tangent", "tangents", 4), ("color", "colors", 4),
    ):
        if role not in mapping and field in mesh:
            name = "__declared_" + field
            mesh["attributes"].append({
                "name": name, "decoded": True,
                "values": _vectors(mesh[field], dimension, field),
            })
            mapping[role] = {"source": "post", "name": name,
                             "provenance": "declared mesh attribute"}
            if role in ("normal", "tangent"):
                mapping[role]["space"] = mesh.get(
                    role + "_space", "geometry"
                )
    mapping.update(_default_mapping(mesh))
    provenance = {"position": "captured posed geometry",
                  "topology": "ordinary mutable triangle mesh",
                  "runtime_capture_buffers": False, "channels": {}}
    result = {"name": mesh.get("name", "CapturedMesh"),
              "triangles": list(triangles), "uv_channels": [],
              "provenance": provenance}
    for key in ("event_id", "instance"):
        if key in mesh:
            result[key] = mesh[key]
    if pivot is not None:
        center = _vectors(pivot, 3, "pivot")
        if len(center) != 1:
            raise ValueError("Pivot must have three components")
        result["pivot"] = center[0]
        local = [[value[i] - center[0][i] for i in range(3)]
                 for value in vertices]
    else:
        local = vertices
    result["vertices"] = [value for row in local for value in row]
    for role, selector in mapping.items():
        if role == "geometry_to_shading":
            continue
        if role not in ("normal", "tangent", "color") and not re.fullmatch(
            r"uv[0-7]", role
        ):
            raise ValueError("Unknown mesh attribute role: " + role)
        values = _channel(mesh, selector, len(vertices))
        count = len(values[0])
        if role in ("normal", "tangent"):
            expected = 3 if role == "normal" else 4
            if count != expected:
                raise ValueError(role + " has an incorrect component count")
            values = _directions(values, role, selector.get("space"),
                                 geometry_to_shading)
            target = "normals" if role == "normal" else "tangents"
            result[target] = [value for row in values for value in row]
        elif role == "color":
            if count not in (3, 4):
                raise ValueError("Color must have three or four components")
            result["colors"] = [value for row in values
                                for value in (row + [1.0])[:4]]
        else:
            if not 1 <= count <= 4:
                raise ValueError("UV dimension must be between one and four")
            if count == 1:
                values = [[row[0], 0.0] for row in values]
                count = 2
            result["uv_channels"].append({
                "channel": int(role[-1]), "dimension": count,
                "values": [value for row in values for value in row],
            })
        provenance["channels"][role] = dict(selector)
    result["uv_channels"].sort(key=lambda item: item["channel"])
    if "normals" not in result:
        result["normals"] = [value for row in _surface_normals(
            vertices, triangles) for value in row]
        provenance["channels"]["normal"] = {
            "method": "area-weighted normals from actual triangle topology"
        }
    return result


def read_packed_attributes(path, contract, vertex_count, locations=None):
    """Read legacy varying bytes for import-time baking only."""
    stride = int(contract["varying_stride"])
    data = Path(path).read_bytes()
    if stride <= 0 or len(data) != vertex_count * stride * 16:
        raise ValueError("Captured varying bytes do not match mesh vertices")
    result = []
    for attribute in contract["input_attributes"]:
        if locations is not None and attribute["location"] not in locations:
            continue
        slot, count = int(attribute["slot"]), int(attribute["components"])
        scalar = attribute["scalar_type"]
        if not 0 <= slot < stride or not 1 <= count <= 4:
            raise ValueError("Captured varying contract exceeds its stride")
        if scalar not in ("float", "int", "uint"):
            raise ValueError("Unsupported captured varying scalar type")
        code = {"float": "f", "int": "i", "uint": "I"}[scalar]
        unpack = struct.Struct("<" + code * count).unpack_from
        values = [list(unpack(data, (vertex * stride + slot) * 16))
                  for vertex in range(vertex_count)]
        result.append(dict(attribute, decoded=True, values=values))
    return result


def _read_json(value):
    if isinstance(value, dict):
        return value
    path = Path(value)
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            return json.load(stream)
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _project_file(project, relative):
    path = (project / relative).resolve()
    if not path.is_relative_to(project):
        raise ValueError("Native mesh asset path leaves the Unity project")
    return path


def _draw_key(draw):
    return "E{}_I{}".format(
        int(draw["event_id"]), int(draw.get("instance", 0))
    )


def stage_native_meshes(
    project, drawdata, editable, *, camera=None, profiles=None,
    source_meshes=None, canonical_draws=None,
    output_directory="Assets/NativeCapture",
):
    """Stage one ordinary mesh per explicit editable geometry group.

    ``profiles`` has optional ``by_draw`` and ``by_shader`` dictionaries. Each
    entry contains ``attributes`` (explicit selectors for normal, tangent,
    color, uv0..uv7) and optional ``geometry_to_shading``. ``source_meshes``
    maps draw keys to original mesh dictionaries/files with source evidence.
    ``canonical_draws`` optionally maps group IDs to draw keys; otherwise the
    editable manifest's unique primary draw is used. No shader semantics,
    camera, event, or resource IDs are hardcoded by this staging function.
    """
    project = Path(project).resolve()
    draws_data = _read_json(drawdata)
    editable_data = _read_json(editable)
    profiles = _read_json(profiles) if profiles is not None else {}
    source_meshes = source_meshes or {}
    canonical_draws = canonical_draws or {}
    folder = _project_file(project, output_directory)
    mesh_folder = folder / "Meshes"
    mesh_folder.mkdir(parents=True, exist_ok=True)
    draw_lookup = {_draw_key(draw): draw for draw in draws_data["draws"]}
    candidates = {}
    for draw in editable_data["draws"]:
        candidates.setdefault(draw["group_id"], []).append(draw)
    entries, channel_coverage, warnings = [], {}, []
    for group in editable_data["groups"]:
        group_id = group["id"]
        options = candidates.get(group_id, [])
        chosen = canonical_draws.get(group_id)
        if chosen is None:
            primary = [draw for draw in options if draw.get("primary")]
            if len(primary) != 1:
                raise ValueError("Geometry group needs one explicit primary: "
                                 + group_id)
            chosen = _draw_key(primary[0])
        if chosen not in {_draw_key(draw) for draw in options}:
            raise ValueError("Canonical draw is outside its geometry group")
        if chosen not in draw_lookup:
            raise ValueError("Canonical draw is absent from capture drawdata")
        draw = draw_lookup[chosen]
        profile = dict(profiles.get("by_shader", {}).get(
            draw.get("source_sha256"), {}
        ))
        profile.update(profiles.get("by_draw", {}).get(chosen, {}))
        mapping = dict(profile.get("attributes", {}))
        # The replay project's mesh already uses Unity coordinates. Original
        # bundles can use the opposite camera Z direction, so import only
        # their channel evidence, not a second copy of their positions.
        mesh = dict(_read_json(_project_file(project, draw["mesh"])))
        if chosen in source_meshes:
            source = _read_json(source_meshes[chosen])
            source_count = len(source.get("positions", [])) or (
                len(source.get("vertices", [])) // 3
            )
            if source_count != len(mesh["vertices"]) // 3:
                raise ValueError("Source evidence vertex count differs")
            for key in ("source_geometry", "attributes", "uvs", "colors"):
                if key in source:
                    mesh[key] = source[key]
        mesh.update(name=group.get("name", chosen),
                    event_id=draw["event_id"],
                    instance=draw.get("instance", 0))
        post_selectors = [selector for selector in mapping.values()
                          if selector.get("source", "post") == "post"]
        if post_selectors and not mesh.get("attributes"):
            contract_file = draw.get("contract") or (
                "Assets/RecoveredShaders/Contracts/" + chosen + ".json"
            )
            contract = _read_json(_project_file(project, contract_file))
            locations = None if any("location" not in selector
                                    for selector in post_selectors) else {
                selector["location"] for selector in post_selectors
            }
            vertex_count = len(mesh.get("positions", [])) or (
                len(mesh.get("vertices", [])) // 3
            )
            mesh["attributes"] = read_packed_attributes(
                _project_file(project, draw["varyings"]), contract,
                vertex_count, locations,
            )
        native = convert_native_mesh(
            mesh, mapping, pivot=group.get("pivot"),
            geometry_to_shading=profile.get("geometry_to_shading"),
        )
        native["group_id"] = group_id
        native["source_draw"] = chosen
        native["provenance"]["profile_evidence"] = profile.get("evidence", [])
        for role in native["provenance"]["channels"]:
            channel_coverage[role] = channel_coverage.get(role, 0) + 1
        if not native["uv_channels"]:
            warnings.append({"draw": chosen,
                             "reason": "No verified UV channel"})
        # IDs are metadata, never used as filesystem path fragments.
        file = mesh_folder / ("mesh_{:05d}.json".format(len(entries)))
        file.write_text(json.dumps(native, separators=(",", ":"),
                                   allow_nan=False), encoding="utf-8")
        entries.append({
            "file": file.relative_to(project).as_posix(),
            "material_key": chosen, "group_id": group_id,
            "event_id": draw["event_id"], "instance": draw.get("instance", 0),
            "vertex_count": len(native["vertices"]) // 3,
            "triangle_count": len(native["triangles"]) // 3,
        })
    manifest = {
        "version": 1, "camera": dict(camera or editable_data["camera"]),
        "meshes": entries,
        "material_library": (folder / "materials.json").relative_to(
            project).as_posix(),
        "analysis": {"mesh_count": len(entries),
                     "channel_coverage": channel_coverage,
                     "runtime_capture_buffers": False,
                     "warnings": warnings},
    }
    path = folder / "manifest.json"
    path.write_text(json.dumps(manifest, separators=(",", ":"),
                               allow_nan=False), encoding="utf-8")
    return dict(manifest, manifest_path=str(path))
