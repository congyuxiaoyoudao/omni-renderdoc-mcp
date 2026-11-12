"""Build capture-independent fragment nodes from a staged draw bundle.

This backend consumes reflected draw inputs and hash-matched expression
recipes. It never imports an engine profile or a captured final color image.
"""

import argparse
import gzip
import hashlib
import json
import math
import sys
from pathlib import Path

MODE = "generic_shader_nodes"


def read_json(path):
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8-sig") as stream:
        return json.load(stream)


def safe_asset(root, value):
    if not isinstance(value, str) or not value:
        raise ValueError("Capture asset must be a nonempty relative path")
    path = Path(value)
    if path.is_absolute():
        raise ValueError("Capture assets must stay inside their bundle")
    root = Path(root).resolve()
    path = (root / path).resolve()
    if root not in path.parents or not path.is_file():
        raise ValueError("Missing or escaped capture asset: " + value)
    return path


def records(manifest, folder):
    for item in manifest.get("meshes", []):
        if isinstance(item, str):
            yield read_json(safe_asset(folder, item))
        elif isinstance(item, dict) and item.get("file"):
            yield {**read_json(safe_asset(folder, item["file"])), **item}
        elif isinstance(item, dict):
            yield item
        else:
            raise ValueError("Invalid captured mesh record")


def attribute_descriptors(attributes, count):
    """Validate original varyings before creating Blender point attributes."""
    result = []
    for index, source in enumerate(attributes):
        if source.get("decoded") is False:
            continue
        values = source.get("values")
        if not values:
            continue
        components = source.get("components", len(values[0]))
        if type(components) is not int or not 1 <= components <= 4:
            continue
        if len(values) != count or any(
            not isinstance(row, (tuple, list))
            or len(row) != components
            or any(
                type(value) not in (int, float) or not math.isfinite(value)
                for value in row
            )
            for row in values
        ):
            raise ValueError("Invalid varying values: " + source["name"])
        descriptor = {
            key: value
            for key, value in source.items()
            if key not in ("values", "raw")
        }
        descriptor.update(
            components=components,
            blender_name="Captured varying {:03d}".format(index),
        )
        if components == 4:
            descriptor["blender_w_name"] = descriptor["blender_name"] + " W"
        result.append((descriptor, values))
    return result


def add_attributes(mesh, attributes):
    descriptors = []
    for descriptor, values in attribute_descriptors(
        attributes, len(mesh.vertices)
    ):
        count = descriptor["components"]
        attribute = mesh.attributes.new(
            descriptor["blender_name"],
            "FLOAT" if count == 1 else "FLOAT_VECTOR",
            "POINT",
        )
        descriptor["blender_name"] = attribute.name
        if count == 1:
            attribute.data.foreach_set("value", [row[0] for row in values])
        else:
            attribute.data.foreach_set(
                "vector",
                [
                    v
                    for row in values
                    for v in (list(row[:3]) + [0] * (3 - min(3, count)))
                ],
            )
        if count == 4:
            fourth = mesh.attributes.new(
                descriptor["blender_w_name"], "FLOAT", "POINT"
            )
            fourth.data.foreach_set("value", [row[3] for row in values])
            descriptor["blender_w_name"] = fourth.name
        descriptors.append(descriptor)
    mesh["generic_attributes"] = json.dumps(descriptors)
    return descriptors


def pipeline_limitations(pipeline):
    """List draw-order semantics the material surface cannot emulate."""
    issues = []
    if pipeline.get("stencil_test"):
        issues.append("Original stencil tests/writes are not reproduced")
    if pipeline.get("depth_test") is False:
        issues.append("Depth-disabled draw ordering is not reproduced")
    if pipeline.get("depth_func") not in (
        None,
        "Less",
        "LessEqual",
        "Greater",
        "GreaterEqual",
    ):
        issues.append("Source depth comparison is not ordinary visibility")
    if pipeline.get("depth_write") is False:
        issues.append("Source depth-write suppression is not reproduced")
    if pipeline.get("depth_bounds"):
        issues.append("Source depth bounds are not reproduced")
    if pipeline.get("cull_mode") not in (None, "NoCull", "None"):
        issues.append("Source winding/culling requires independent validation")
    if pipeline.get("fill_mode") not in (None, "Solid"):
        issues.append("Source raster fill mode is not reproduced")
    if len(pipeline.get("render_targets", [])) > 1:
        issues.append("Multiple render-target outputs are not reconstructed")
    for blend in pipeline.get("blends", []):
        if blend.get("enabled"):
            issues.append(
                "Ordered framebuffer blending is approximated by EEVEE"
            )
        if blend.get("logic_enabled"):
            issues.append("Framebuffer logic operations are not reproduced")
        if blend.get("write_mask", 15) != 15:
            issues.append("Framebuffer channel write masks are not reproduced")
    return list(dict.fromkeys(issues))


def geometry_coverage(manifest):
    """Keep geometry omissions separate from unsupported fragment programs."""
    camera = manifest.get("camera", {})
    skipped = manifest.get("skipped", [])
    duplicates = manifest.get("duplicates", [])
    issues = []
    if skipped:
        issues.append("Some selected draw/instance geometry was not exported")
    if duplicates:
        issues.append("Some captured draw geometry was deduplicated")
    projection = camera.get("projection_source", {})
    if (
        projection.get("method") == "canonical fallback"
        or projection.get("physical_scale_known") is False
    ):
        issues.append(
            "Canonical clip embedding preserves screen placement only; "
            "physical scale/projection and orthographic depth are unknown"
        )
    return {
        "skipped": skipped,
        "skipped_count": len(skipped),
        "duplicates": duplicates,
        "duplicate_count": len(duplicates),
        "camera": camera,
        "limitations": issues,
    }


def reconstruction_status(unsupported_count, pipeline, geometry, evidence):
    if unsupported_count:
        return "needs_recipes"
    if pipeline or geometry or evidence:
        return "partial"
    return "translated"


def evidence_fingerprint(index):
    serialized = json.dumps(index, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def verify_geometry_evidence(fingerprint, index, draw_hashes):
    """Reject reusing a captured pose with another capture's event numbers."""
    current = evidence_fingerprint(index)
    if fingerprint and fingerprint != current:
        raise ValueError(
            "Geometry and shader evidence belong to different capture inputs; "
            "export geometry again for this evidence bundle"
        )
    events = {row["event_id"]: row for row in index["events"]}
    modules = {row["id"]: row for row in index["modules"]}
    for eid, previous in draw_hashes:
        if eid not in events:
            raise ValueError("Geometry event is absent from shader evidence")
        module = modules.get(events[eid].get("stages", {}).get("Pixel"), {})
        actual = module.get("sha256") or "missing-pixel-shader"
        if previous and previous != actual:
            raise ValueError(
                "Captured geometry shader differs at event {}; export "
                "geometry again for this evidence bundle".format(eid)
            )
    return current


def unsupported_material(bpy, digest, reason):
    material = bpy.data.materials.new("UNSUPPORTED shader " + digest[:12])
    material.use_nodes = True
    tree = material.node_tree
    tree.nodes.clear()
    emission = tree.nodes.new("ShaderNodeEmission")
    emission.inputs["Color"].default_value = (1, 0, 1, 1)
    emission.label = "Unsupported: see SHADER RECONSTRUCTION COVERAGE"
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    tree.links.new(emission.outputs[0], output.inputs["Surface"])
    material["shader_status"] = "needs_recipe"
    material["unsupported_reason"] = reason
    return material


def _new_scene(bpy, manifest, folder):
    from .blender_rig import create_rigged_source, store_skinning
    from .blender_scene import add_uv_layer, configure_scene, make_camera

    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.name = "Captured fragment nodes"
    configure_scene(scene)
    make_camera(bpy, scene, manifest)
    world = bpy.data.worlds.new("Unreconstructed background")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0
    scene.world = world
    scene["reconstruction_mode"] = MODE
    scene["source_capture"] = json.dumps(manifest.get("source_capture"))
    objects, skinning = [], []
    for record in records(manifest, folder):
        vertices, faces = record.get("positions"), record.get("faces")
        if not vertices or not faces:
            raise ValueError("Captured mesh is empty")
        if any(
            len(face) < 3
            or any(
                type(i) is not int or not 0 <= i < len(vertices) for i in face
            )
            for face in faces
        ):
            raise ValueError("Invalid captured mesh indices")
        name = record.get("name", "Captured draw")
        mesh = bpy.data.meshes.new(name)
        mesh.from_pydata(vertices, [], faces)
        mesh.update()
        add_uv_layer(mesh, "Source UV", record.get("uvs"))
        add_attributes(mesh, record.get("attributes", []))
        obj = bpy.data.objects.new(name, mesh)
        scene.collection.objects.link(obj)
        obj["event_id"] = record["event_id"]
        obj["instance"] = record.get("instance", 0)
        obj["coordinate_space"] = record.get("coordinate_space", "camera")
        objects.append(obj)
        skinning.append(create_rigged_source(bpy, scene, record))
    store_skinning(scene, skinning)
    return scene, objects


def build(args):
    import bpy

    from .blender_rig import ROLE, skinning_coverage
    from .fragment_graph import (
        FragmentGraphError,
        build_material,
        validate_registry,
    )

    output = Path(args.output).resolve()
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if args.manifest:
        manifest_path = Path(args.manifest).resolve()
        manifest = read_json(manifest_path)
        evidence_index = safe_asset(
            manifest_path.parent, manifest["shader_evidence"]
        )
        scene, objects = _new_scene(bpy, manifest, manifest_path.parent)
        warnings = list(manifest.get("warnings", []))
        geometry = geometry_coverage(manifest)
    else:
        evidence_index = Path(args.evidence).resolve()
        if evidence_index.is_dir():
            evidence_index /= "index.json"
        bpy.ops.wm.open_mainfile(
            filepath=str(Path(args.input_blend).resolve())
        )
        scene = bpy.context.scene
        if scene.get("reconstruction_mode") != MODE:
            raise ValueError("Generic rebuild requires a generic node scene")
        objects = [
            obj
            for obj in scene.objects
            if obj.type == "MESH" and obj.get("capture_role") != ROLE
        ]
        warnings = []
        geometry = json.loads(scene.get("generic_geometry_coverage", "{}"))
        if not geometry:
            geometry = geometry_coverage({})
            geometry["limitations"].append(
                "Older generic scene lacks geometry provenance; "
                "capture geometry equivalence has not been verified"
            )
    if not objects:
        raise ValueError("No captured draw geometry to rebuild")
    evidence = read_json(evidence_index)
    if evidence.get("format") != "omni_shader_evidence":
        raise ValueError("Expected generic shader evidence")
    fingerprint = evidence_fingerprint(evidence)
    if args.input_blend:
        previous_fingerprint = scene.get("shader_evidence_fingerprint")
        fingerprint = verify_geometry_evidence(
            previous_fingerprint,
            evidence,
            [
                (obj.get("event_id"), obj.get("shader_sha256"))
                for obj in objects
            ],
        )
        if not previous_fingerprint:
            geometry["limitations"].append(
                "Older generic scene has no evidence fingerprint; matching "
                "draw shader hashes cannot verify capture pose/input identity"
            )
    events = {event["event_id"]: event for event in evidence["events"]}
    modules = {module["id"]: module for module in evidence["modules"]}
    registry = validate_registry(read_json(args.recipes))
    materials, cache = [], {}
    for obj in objects:
        eid = obj.get("event_id")
        if eid not in events:
            raise ValueError("Mesh event is absent from the evidence bundle")
        event = dict(events[eid])
        module = modules.get(event.get("stages", {}).get("Pixel"), {})
        digest = module.get("sha256") or "missing-pixel-shader"
        event["shader_sha256"] = digest
        event["shader_entry_point"] = module.get("entry_point") or "main"
        attributes = json.loads(obj.data.get("generic_attributes", "[]"))
        key = (eid, digest, json.dumps(attributes, sort_keys=True))
        if key not in cache:
            recipe = registry.get(digest)
            reason = "No matching fragment recipe for this shader SHA256"
            material, coverage = None, {}
            if recipe is not None:
                try:
                    material, coverage = build_material(
                        bpy, recipe, event, evidence_index.parent, attributes
                    )
                except FragmentGraphError as error:
                    reason = str(error)
            if material is None:
                material = unsupported_material(bpy, digest, reason)
                coverage = {"status": "needs_recipe", "reason": reason}
            else:
                coverage["status"] = "translated"
            cache[key] = (material, coverage)
        material, coverage = cache[key]
        if not obj.data.materials:
            obj.data.materials.append(material)
        # Preserve shared meshes while allowing per-draw material bindings.
        for slot in obj.material_slots:
            slot.link = "OBJECT"
            slot.material = material
        obj["shader_sha256"] = digest
        obj["capture_pipeline"] = json.dumps(event.get("pipeline", {}))
        materials.append(
            {
                **coverage,
                "event_id": eid,
                "instance": obj.get("instance", 0),
                "object": obj.name,
                "material": material.name,
                "shader_sha256": digest,
                "pipeline_stages": event.get("stages", {}),
                "pipeline_limitations": pipeline_limitations(
                    event.get("pipeline", {})
                ),
            }
        )
    unsupported = [row for row in materials if row["status"] != "translated"]
    pipeline_issues = any(row["pipeline_limitations"] for row in materials)
    evidence_issues = evidence.get("issues", [])
    status = reconstruction_status(
        len(unsupported),
        pipeline_issues,
        geometry["limitations"],
        evidence_issues,
    )
    report = {
        "version": 1,
        "reconstruction_mode": MODE,
        "shader_evidence_fingerprint": fingerprint,
        "appearance_mode": MODE,
        "status": status,
        "blend_file": str(output),
        "mesh_count": len(objects),
        "skinning": skinning_coverage(scene),
        "vertex_count": sum(len(o.data.vertices) for o in objects),
        "triangle_count": sum(
            sum(max(0, len(p.vertices) - 2) for p in o.data.polygons)
            for o in objects
        ),
        "material_count": len(cache),
        "material_node_count": sum(
            len(material.node_tree.nodes) for material, _ in cache.values()
        ),
        "shader_material_count": len(materials),
        "translated_material_count": len(materials) - len(unsupported),
        "unsupported_material_count": len(unsupported),
        "geometry_coverage": geometry,
        "geometry_skipped": geometry["skipped"],
        "geometry_skipped_count": geometry["skipped_count"],
        "geometry_duplicates": geometry["duplicates"],
        "evidence_issues": evidence_issues,
        "materials": materials,
        "warnings": warnings,
        "source_frame_color_images_used": False,
        "reference_background": False,
        "limitations": [
            "Only supported fragment expressions are reconstructed; this does "
            "not reproduce the complete render graph, compute or post passes.",
            "Captured varyings use Blender interpolation; flat/noperspective "
            "qualifiers and raster sample locations can differ.",
            "Visible geometry preserves the captured pose. Available source "
            "rigs are separate, hidden input-space assets; no animation "
            "or original hierarchy is inferred from joint weights alone.",
            "Per-draw pipeline limitations are listed with each material.",
        ],
    }
    if unsupported and args.unsupported == "error":
        report["status"] = "failed"
        output.with_suffix(".coverage.json").write_text(
            json.dumps(report, indent=2)
        )
        raise RuntimeError("Unsupported fragment shaders; see coverage report")
    scene["reconstruction_mode"] = MODE
    scene["shader_evidence"] = str(evidence_index)
    scene["shader_reconstruction_status"] = status
    scene["shader_evidence_fingerprint"] = fingerprint
    scene["generic_geometry_coverage"] = json.dumps(geometry)
    for name, value in (
        ("SHADER RECONSTRUCTION COVERAGE", report),
        ("FRAGMENT RECIPES", read_json(args.recipes)),
    ):
        text = bpy.data.texts.get(name) or bpy.data.texts.new(name)
        text.clear()
        text.write(json.dumps(value, indent=2, ensure_ascii=False))
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == "VIEW_3D":
                area.spaces.active.region_3d.view_perspective = "CAMERA"
    scene.render.resolution_percentage = args.scale
    bpy.ops.file.pack_all()
    report["image_count"] = len(bpy.data.images)
    unpacked = [
        im.name
        for im in bpy.data.images
        if im.users
        and im.source in ("FILE", "GENERATED")
        and not (im.packed_file or im.packed_files)
    ]
    if unpacked:
        raise RuntimeError("Unpacked shader images: " + repr(unpacked))
    bpy.ops.wm.save_as_mainfile(filepath=str(output), compress=False)
    if args.render:
        scene.render.filepath = str(Path(args.render).resolve())
        bpy.ops.render.render(write_still=True)
        report["render_file"] = scene.render.filepath
    for suffix in (".report.json", ".coverage.json"):
        output.with_suffix(suffix).write_text(json.dumps(report, indent=2))
    print(
        "GENERIC_SHADER_RESULT",
        json.dumps(
            {key: value for key, value in report.items() if key != "materials"}
        ),
        flush=True,
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--manifest")
    group.add_argument("--input-blend")
    parser.add_argument("--evidence")
    parser.add_argument("--recipes", required=True)
    parser.add_argument(
        "--unsupported", choices=("report", "error"), default="report"
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--scale", type=int, default=100)
    parser.add_argument("--render")
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1 :])
    if args.input_blend and not args.evidence:
        parser.error("--input-blend requires --evidence")
    if not 1 <= args.scale <= 100:
        parser.error("--scale must be from 1 to 100")
    build(args)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = Path(__file__).resolve().parent.parent.name + ".exporters"
    main()
