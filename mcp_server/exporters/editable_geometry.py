"""Infer editable camera-space geometry from recovered post-VS evidence.

The exporter reconstructs mesh XYZ from captured clip XYW. Consequently an
excellent XY fit alone does *not* distinguish world geometry from fullscreen
triangles or UI. This module additionally fits and validates the shared depth
projection. Original per-vertex clip residuals must be retained by the runtime;
the fitted projection supplies the delta caused by an edit, not replacement
positions for the reference frame.
"""

from __future__ import annotations

import array
import hashlib
import json
import math
import statistics
import struct
import sys
from collections import defaultdict
from pathlib import Path


class EditableProjectionUnavailable(ValueError):
    """Valid captured draws do not identify a shared editable 3D projection."""


def _asset(project, name):
    path = (project / name).resolve()
    if not path.is_relative_to(project):
        raise ValueError("Geometry asset escapes the Unity project")
    return path


def _packed(values, code):
    packed = array.array(code, values)
    if sys.byteorder != "little":
        packed.byteswap()
    return packed.tobytes()


def _geometry_hash(vertices, triangles):
    digest = hashlib.sha256()
    digest.update(struct.pack("<QQ", len(vertices), len(triangles)))
    digest.update(_packed(vertices, "f"))
    digest.update(_packed(triangles, "I"))
    return digest.hexdigest()


def _line_fit(xs, ys):
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    variance = sum((x - mx) ** 2 for x in xs)
    if variance <= 1e-20:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / variance
    intercept = my - slope * mx
    residual = max(abs(slope * x + intercept - y) for x, y in zip(xs, ys))
    return slope, intercept, residual


def _read_geometry(project, draw, sample_count):
    mesh = json.loads(_asset(project, draw["mesh"]).read_text("utf-8"))
    vertices, triangles = mesh["vertices"], mesh["triangles"]
    clips = _asset(project, draw["clip_positions"]).read_bytes()
    if len(vertices) % 3 or len(clips) != len(vertices) // 3 * 16:
        raise ValueError("Mesh and original clip vertex counts differ")
    count = len(vertices) // 3
    if not count or not triangles:
        raise ValueError("Editable geometry requires vertices and triangles")
    if len(triangles) % 3 or any(i < 0 or i >= count for i in triangles):
        raise ValueError("Recovered triangle indices are invalid")
    samples = min(sample_count, count)
    indices = (
        sorted(
            {round(i * (count - 1) / (samples - 1)) for i in range(samples)}
        )
        if samples > 1
        else [0]
    )
    pairs = [
        (vertices[i * 3 : i * 3 + 3], struct.unpack_from("<4f", clips, i * 16))
        for i in indices
    ]
    if any(not math.isfinite(v) for p, c in pairs for v in (*p, *c)):
        raise ValueError("Non-finite position cannot define editable geometry")
    scales = []
    for axis in (0, 1):
        denominator = sum(p[axis] ** 2 for p, _ in pairs)
        scales.append(
            sum(p[axis] * c[axis] for p, c in pairs) / denominator
            if denominator
            else None
        )
    ws, zs = [c[3] for _, c in pairs], [c[2] for _, c in pairs]
    bounds = [[min(vertices[k::3]), max(vertices[k::3])] for k in range(3)]
    return {
        "draw": draw,
        "hash": _geometry_hash(vertices, triangles),
        "vertices": count,
        "samples": pairs,
        "scales": scales,
        "depth_fit": _line_fit(ws, zs),
        "w_min": min(ws),
        "w_max": max(ws),
        "bounds": bounds,
    }


def _projection(records, camera):
    supplied = camera.get("projection")
    if supplied is not None:
        if len(supplied) != 16 or not all(map(math.isfinite, supplied)):
            raise ValueError(
                "Captured projection must contain 16 finite values"
            )
        return list(supplied), "supplied"
    scales = []
    for axis in (0, 1):
        candidates = [
            record["scales"][axis]
            for record in records
            if record["scales"][axis] is not None
        ]
        if not candidates:
            raise EditableProjectionUnavailable(
                "Captured geometry cannot determine XY projection"
            )
        scales.append(statistics.median(candidates))
    fits = [
        record["depth_fit"]
        for record in records
        if record["depth_fit"] is not None
        and record["w_max"] - record["w_min"]
        > max(1e-5, abs(record["w_max"]) * 1e-6)
        and record["depth_fit"][2] < 1e-6
        and abs(record["depth_fit"][1]) > 1e-8
    ]
    if not fits:
        raise EditableProjectionUnavailable(
            "No non-degenerate shared perspective depth projection; "
            "supply a verified camera projection"
        )
    a = statistics.median(fit[0] for fit in fits)
    b = statistics.median(fit[1] for fit in fits)
    supporting = sum(
        max(
            abs((a - fit[0]) * w + b - fit[1])
            for w in (record["w_min"], record["w_max"])
        )
        < 2e-6
        for record in records
        if (fit := record["depth_fit"]) in fits
    )
    if supporting < max(1, math.ceil(len(fits) / 2)):
        raise EditableProjectionUnavailable(
            "Ambiguous shared perspective depth projection; "
            "supply a verified camera projection"
        )
    return [
        scales[0],
        0,
        0,
        0,
        0,
        scales[1],
        0,
        0,
        0,
        0,
        a,
        b,
        0,
        0,
        1,
        0,
    ], "shared_clip_depth_fit"


def _classify(record, projection, width, height):
    maximum_xy = maximum_w = maximum_z = 0.0
    for position, clip in record["samples"]:
        predicted = [
            sum(projection[k * 4 + j] * position[j] for j in range(3))
            + projection[k * 4 + 3]
            for k in range(4)
        ]
        scale = max(abs(clip[3]), 1e-8)
        maximum_xy = max(
            maximum_xy,
            abs(predicted[0] - clip[0]) / scale * width / 2,
            abs(predicted[1] - clip[1]) / scale * height / 2,
        )
        maximum_w = max(maximum_w, abs(predicted[3] - clip[3]) / scale)
        maximum_z = max(maximum_z, abs(predicted[2] - clip[2]))
    metrics = {
        "sample_count": len(record["samples"]),
        "xy_max_error_pixels": maximum_xy,
        "w_max_relative_error": maximum_w,
        "clip_z_max_residual": maximum_z,
    }
    if maximum_xy > 0.01 or maximum_w > 1e-5:
        return "different_projection", metrics
    state = record["draw"].get("original_render_state", {})
    depth_func = str(state.get("depth_func", "")).lower()
    has_depth_test = depth_func not in {
        "",
        "always",
        "alwaystrue",
        "never",
        "alwaysfalse",
        "disabled",
    }
    constant_w = record["w_max"] - record["w_min"] <= max(
        1e-5, abs(record["w_max"]) * 1e-6
    )
    if constant_w and not has_depth_test:
        return "screen_or_overlay", metrics
    depth_scale = max(abs(projection[11]), 1e-8)
    if maximum_z <= max(2e-6, depth_scale * 1e-5):
        return "shared_world_projection", metrics
    if (
        has_depth_test
        and record["w_min"] > 0
        and record["w_max"] - record["w_min"] > 1e-5
        and maximum_z <= depth_scale * 0.1
    ):
        return "world_with_preserved_depth_offset", metrics
    if constant_w:
        return "screen_or_overlay", metrics
    return "ambiguous_projection", metrics


def build_editable_manifest(
    project, drawdata, camera=None, *, sample_count=256
):
    """Return edit bindings without modifying the project or varying semantics.

    ``drawdata`` is the decoded recovered ``draws.json``. Geometry identity
    uses original vertex order and topology, so depth/color passes share one
    edit transform without combining different meshes. Unclassified
    draws remain in the original replay graph, outside the editable set.
    """
    project = Path(project).resolve()
    if sample_count < 4:
        raise ValueError("At least four position samples are required")
    camera = dict(camera or {})
    width = camera.get("width", drawdata.get("source_width"))
    height = camera.get("height", drawdata.get("source_height"))
    if width is None or height is None or int(width) <= 0 or int(height) <= 0:
        raise ValueError("Captured viewport dimensions must be positive")
    width, height = int(width), int(height)
    records = [
        _read_geometry(project, draw, sample_count)
        for draw in drawdata["draws"]
    ]
    if not records:
        raise ValueError("No recovered draws to analyze")
    projection, projection_source = _projection(records, camera)
    a, b = projection[10:12]
    near = b / (1 - a) if a != 1 else 0.01
    far = -b / a if a else 10000.0
    if not (0 < near < far):
        near, far = 0.01, 10000.0
    fitted_camera = {
        **camera,
        "p00": projection[0],
        "p11": projection[5],
        "width": width,
        "height": height,
        "near": camera.get("near", near),
        "far": camera.get("far", far),
        "projection": projection,
    }
    grouped = defaultdict(list)
    excluded = []
    classifications = defaultdict(int)
    for record in records:
        category, metrics = _classify(record, projection, width, height)
        classifications[category] += 1
        record["metrics"] = metrics
        record["classification"] = category
        if category in {
            "shared_world_projection",
            "world_with_preserved_depth_offset",
        }:
            grouped[record["hash"]].append(record)
        else:
            excluded.append(
                {
                    "event_id": record["draw"]["event_id"],
                    "instance": record["draw"].get("instance", 0),
                    "reason": category,
                    **metrics,
                }
            )
    groups, draws = [], []
    for fingerprint, members in grouped.items():
        primary = next(
            (
                r
                for r in members
                if r["draw"].get("color_targets")
                and r["draw"]
                .get("original_render_state", {})
                .get("depth_func")
                == "Equal"
            ),
            next(
                (r for r in members if r["draw"].get("color_targets")),
                members[0],
            ),
        )
        group_id = "geometry_" + fingerprint[:20]
        name = primary["draw"].get(
            "name", "E" + str(primary["draw"]["event_id"])
        )
        groups.append(
            {
                "id": group_id,
                "name": name,
                "pivot": [(lo + hi) / 2 for lo, hi in primary["bounds"]],
                "geometry_sha256": fingerprint,
            }
        )
        for record in members:
            draw = record["draw"]
            draws.append(
                {
                    "event_id": draw["event_id"],
                    "instance": draw.get("instance", 0),
                    "group_id": group_id,
                    "primary": record is primary,
                    "classification": record["classification"],
                    **record["metrics"],
                }
            )
    return {
        "version": 1,
        "camera": fitted_camera,
        "groups": groups,
        "draws": draws,
        "excluded_draws": excluded,
        "warnings": [
            "Editable geometry contains captured posed camera-space vertices; "
            "original hierarchy, rigs and animation are unavailable.",
            "Fragment varying semantics are not inferred: shading positions "
            "and normals stay captured unless explicitly adapted.",
            "Light/shadow maps, constant buffers and temporal-history inputs "
            "remain captured unless the recovered graph regenerates them.",
            "Screen-space, overlay and ambiguous draws remain fixed in the "
            "original rendering graph.",
        ],
        "analysis": {
            "projection_source": projection_source,
            "sample_limit_per_draw": sample_count,
            "classifications": dict(classifications),
            "input_draws": len(records),
            "editable_draws": len(draws),
            "editable_groups": len(groups),
            "reference_clip_residuals_must_be_preserved": True,
            "shading_varying_semantics_inferred": False,
        },
    }


def write_editable_manifest(
    project,
    drawdata,
    camera=None,
    *,
    relative_path="Assets/RecoveredShaders/editable_capture.json",
):
    """Write the generated edit bindings inside the selected Unity project."""
    project = Path(project).resolve()
    result = build_editable_manifest(project, drawdata, camera)
    target = _asset(project, relative_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2), "utf-8")
    return target
