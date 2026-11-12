"""Build an editable Blender scene from the RenderDoc Blender manifest.

Run with Blender --background --python blender_scene.py -- --manifest FILE
--output FILE.blend [--render FILE.png] [--mode captured|editable].

Captured mode projects the capture image onto real replayed geometry. It is
view-dependent appearance preservation, not a translation of game shaders.
The second scene keeps independent editable Principled BSDF materials.
"""

import argparse
import gzip
import json
import math
import sys
from pathlib import Path


def load_json(path):
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def asset_path(folder, value):
    path = Path(value)
    return path if path.is_absolute() else folder / path


def mesh_records(manifest, folder):
    for record in manifest.get("meshes", []):
        if isinstance(record, str):
            yield load_json(asset_path(folder, record))
        elif record.get("file"):
            yield {**load_json(asset_path(folder, record["file"])), **record}
        else:
            yield record


def camera_settings(manifest):
    spec = manifest.get("camera", {})
    resolution = manifest.get("resolution", [1920, 1080])
    width = int(spec.get("width", resolution[0]))
    height = int(spec.get("height", resolution[1]))
    projection = spec.get("projection", {})
    matrix = spec.get("projection_matrix")
    p11 = abs(float(projection.get("p11", spec.get("p11", 0))))
    p00 = abs(float(projection.get("p00", spec.get("p00", 0))))
    if matrix:
        p00 = p00 or abs(float(matrix[0][0]))
        p11 = p11 or abs(float(matrix[1][1]))
    if not p11:
        p11 = 1 / math.tan(float(spec.get("fov_y", math.radians(60))) / 2)
    if not p00:
        p00 = p11 * height / width
    return spec, width, height, p00, p11


def make_camera(bpy, scene, manifest):
    from mathutils import Matrix

    spec, width, height, p00, p11 = camera_settings(manifest)
    data = bpy.data.cameras.new("Captured camera")
    data.type = "PERSP"
    data.sensor_fit = "HORIZONTAL"
    data.sensor_width = 36
    data.lens = 18 * p00
    data.clip_start = max(0.00001, float(spec.get("near", 0.01)))
    data.clip_end = max(data.clip_start + 1, float(spec.get("far", 100000)))
    camera = bpy.data.objects.new("Captured camera", data)
    camera.matrix_world = Matrix(spec.get("matrix_world", Matrix.Identity(4)))
    scene.collection.objects.link(camera)
    scene.camera = camera
    scene.render.resolution_x = width
    scene.render.resolution_y = height
    scene.render.resolution_percentage = 100
    # Pixel aspect also supports projections whose aspect differs from output.
    scene.render.pixel_aspect_x = p11 * height / (p00 * width)
    scene.render.pixel_aspect_y = 1
    return camera, p00, p11


def load_image(bpy, folder, value, warnings):
    if not value:
        return None
    path = asset_path(folder, value)
    if not path.is_file():
        warnings.append("Missing texture: " + str(path))
        return None
    try:
        return bpy.data.images.load(str(path), check_existing=True)
    except Exception as error:
        warnings.append("Cannot load texture {}: {}".format(path, error))
        return None


def projected_material(bpy, camera, image, p00, p11):
    image.alpha_mode = "NONE"
    material = bpy.data.materials.new("Captured look - camera projection")
    material.use_nodes = True
    material["appearance_mode"] = "camera_projected_capture"
    material["limitation"] = (
        "Captured illumination and post effects projected "
        "onto replay geometry. "
        "Only the original camera is a valid appearance reference. "
        "This does not reconstruct game shaders or unseen surfaces."
    )
    tree = material.node_tree
    tree.nodes.clear()
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    output.location = (800, 0)
    emission = tree.nodes.new("ShaderNodeEmission")
    emission.location = (600, 0)
    texture = tree.nodes.new("ShaderNodeTexImage")
    texture.image = image
    texture.extension = "CLIP"
    texture.interpolation = "Linear"
    texture.location = (350, 0)
    texture.label = "Original capture; projected, not a game shader"
    coords = tree.nodes.new("ShaderNodeTexCoord")
    coords.object = camera
    coords.location = (-950, 0)
    split = tree.nodes.new("ShaderNodeSeparateXYZ")
    split.location = (-750, 0)
    tree.links.new(coords.outputs["Object"], split.inputs[0])

    def math_node(operation, left, right, x, y):
        node = tree.nodes.new("ShaderNodeMath")
        node.operation = operation
        node.location = (x, y)
        for index, value in enumerate((left, right)):
            if isinstance(value, (int, float)):
                node.inputs[index].default_value = value
            else:
                tree.links.new(value, node.inputs[index])
        return node.outputs[0]

    depth = math_node("MULTIPLY", split.outputs["Z"], -1, -550, -300)
    u = math_node("DIVIDE", split.outputs["X"], depth, -550, 150)
    v = math_node("DIVIDE", split.outputs["Y"], depth, -550, -50)
    u = math_node("MULTIPLY_ADD", u, 0.5 * p00, -350, 150)
    v = math_node("MULTIPLY_ADD", v, 0.5 * p11, -350, -50)
    u.node.inputs[2].default_value = 0.5
    v.node.inputs[2].default_value = 0.5
    combine = tree.nodes.new("ShaderNodeCombineXYZ")
    combine.location = (-100, 0)
    tree.links.new(u, combine.inputs["X"])
    tree.links.new(v, combine.inputs["Y"])
    tree.links.new(combine.outputs[0], texture.inputs["Vector"])
    tree.links.new(texture.outputs["Color"], emission.inputs["Color"])
    tree.links.new(emission.outputs[0], output.inputs["Surface"])
    return material


def editable_material(bpy, record, folder, cache, warnings):
    spec = record.get("material", {})
    texture_path = spec.get("base_color_texture") or record.get("texture")
    normal_path = spec.get("normal_texture")
    base_binding = next(
        (b for b in spec.get("bindings", []) if b.get("role") == "base_color"),
        {},
    )
    base_format = base_binding.get("view_format") or base_binding.get(
        "resource_format", ""
    )
    base_srgb = (
        "SRGB" in base_format.upper()
        or "SRGB" in str(base_binding.get("component_type", "")).upper()
    )
    base_colorspace = "sRGB" if not base_binding or base_srgb else "Non-Color"
    normal_binding = next(
        (b for b in spec.get("bindings", []) if b.get("role") == "normal"),
        {},
    )
    normal_format = normal_binding.get("resource_format", "")
    key = (
        texture_path,
        normal_path,
        spec.get("roughness", 0.7),
        spec.get("metallic", 0),
        spec.get("use_alpha", False),
        spec.get("alpha_clip", False),
        base_colorspace,
        normal_format,
    )
    if key in cache:
        return cache[key]
    material = bpy.data.materials.new("Editable " + record.get("name", "draw"))
    material.use_nodes = True
    material["appearance_mode"] = "editable_approximation"
    material["source_texture"] = texture_path or "unavailable"
    tree = material.node_tree
    shader = tree.nodes.get("Principled BSDF")
    shader.inputs["Base Color"].default_value = (0.45, 0.45, 0.45, 1)
    shader.inputs["Roughness"].default_value = float(
        spec.get("roughness", 0.7)
    )
    shader.inputs["Metallic"].default_value = float(spec.get("metallic", 0))
    image = load_image(bpy, folder, texture_path, warnings)
    if image is not None:
        # Respect the captured descriptor view's sampling color space.
        if image.users and (
            image.colorspace_settings.name != base_colorspace
            or image.alpha_mode != "CHANNEL_PACKED"
        ):
            image = image.copy()
        image.colorspace_settings.name = base_colorspace
        # Game alpha often stores material masks, not transparency.
        image.alpha_mode = "CHANNEL_PACKED"
        texture = tree.nodes.new("ShaderNodeTexImage")
        texture.image = image
        texture.location = (-450, 200)
        texture.label = "Captured bound texture; binding may need adjustment"
        tree.links.new(texture.outputs["Color"], shader.inputs["Base Color"])
        if spec.get("use_alpha") or spec.get("alpha_clip"):
            tree.links.new(texture.outputs["Alpha"], shader.inputs["Alpha"])
    normal_image = load_image(bpy, folder, normal_path, warnings)
    if normal_image is not None:
        if (
            normal_image.users
            and normal_image.colorspace_settings.name != "Non-Color"
        ):
            normal_image = normal_image.copy()
            normal_image.name += " (linear normal)"
        normal_image.colorspace_settings.name = "Non-Color"
        texture = tree.nodes.new("ShaderNodeTexImage")
        texture.image = normal_image
        texture.location = (-650, -200)
        normal = tree.nodes.new("ShaderNodeNormalMap")
        normal.location = (-350, -150)
        normal.uv_map = "Source UV"
        normal_color = texture.outputs["Color"]
        if "BC5" in normal_format.upper():
            # BC5 stores XY only. Blender's Normal Map expects encoded XYZ.
            split = tree.nodes.new("ShaderNodeSeparateColor")
            split.location = (-1100, -400)
            tree.links.new(normal_color, split.inputs["Color"])
            squares = []
            for channel in ("Red", "Green"):
                decode = tree.nodes.new("ShaderNodeMath")
                decode.operation = "MULTIPLY_ADD"
                decode.inputs[1].default_value = 2
                decode.inputs[2].default_value = -1
                tree.links.new(split.outputs[channel], decode.inputs[0])
                square = tree.nodes.new("ShaderNodeMath")
                square.operation = "MULTIPLY"
                tree.links.new(decode.outputs[0], square.inputs[0])
                tree.links.new(decode.outputs[0], square.inputs[1])
                squares.append(square.outputs[0])
            add = tree.nodes.new("ShaderNodeMath")
            add.operation = "ADD"
            for i, value in enumerate(squares):
                tree.links.new(value, add.inputs[i])
            subtract = tree.nodes.new("ShaderNodeMath")
            subtract.operation = "SUBTRACT"
            subtract.inputs[0].default_value = 1
            subtract.use_clamp = True
            tree.links.new(add.outputs[0], subtract.inputs[1])
            sqrt = tree.nodes.new("ShaderNodeMath")
            sqrt.operation = "SQRT"
            tree.links.new(subtract.outputs[0], sqrt.inputs[0])
            encode = tree.nodes.new("ShaderNodeMath")
            encode.operation = "MULTIPLY_ADD"
            encode.inputs[1].default_value = 0.5
            encode.inputs[2].default_value = 0.5
            tree.links.new(sqrt.outputs[0], encode.inputs[0])
            combine = tree.nodes.new("ShaderNodeCombineColor")
            tree.links.new(split.outputs["Red"], combine.inputs["Red"])
            tree.links.new(split.outputs["Green"], combine.inputs["Green"])
            tree.links.new(encode.outputs[0], combine.inputs["Blue"])
            normal_color = combine.outputs["Color"]
            material["normal_encoding"] = "BC5 XY; reconstructed positive Z"
        tree.links.new(normal_color, normal.inputs["Color"])
        tree.links.new(normal.outputs["Normal"], shader.inputs["Normal"])
    cache[key] = material
    return material


def add_uv_layer(mesh, name, values):
    if not values:
        return
    layer = mesh.uv_layers.new(name=name)
    flat = []
    for loop in mesh.loops:
        uv = values[loop.vertex_index]
        flat.extend((float(uv[0]), float(uv[1])))
    layer.data.foreach_set("uv", flat)


def configure_scene(scene):
    scene.render.engine = "BLENDER_EEVEE"
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.film_transparent = False
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "None"
    scene.view_settings.exposure = 0
    scene.view_settings.gamma = 1
    scene.render.image_settings.color_depth = "8"


def add_lighting(bpy, scene):
    world = bpy.data.worlds.new("Editable preview ambient")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (
        0.22,
        0.26,
        0.32,
        1,
    )
    world.node_tree.nodes["Background"].inputs["Strength"].default_value = 1.0
    scene.world = world
    data = bpy.data.lights.new("Approximate preview sun", "SUN")
    data.energy = 3
    data.angle = math.radians(8)
    obj = bpy.data.objects.new("Approximate preview sun", data)
    obj.rotation_euler = (
        math.radians(25),
        math.radians(-20),
        math.radians(25),
    )
    scene.collection.objects.link(obj)


def build(args):
    import bpy

    from .blender_rig import create_rigged_source, store_skinning

    scale = getattr(args, "scale", 100)
    if type(scale) is not int or not 1 <= scale <= 100:
        raise ValueError("scale must be an integer from 1 to 100")
    manifest_path = Path(args.manifest).resolve()
    folder = manifest_path.parent
    manifest = load_json(manifest_path)
    warnings = list(manifest.get("warnings", []))
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.name = "Captured look (projected)"
    configure_scene(scene)
    camera, p00, p11 = make_camera(bpy, scene, manifest)
    world = bpy.data.worlds.new("Capture gaps - black")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0
    scene.world = world
    reference = manifest.get("reference_image")
    if isinstance(reference, dict):
        reference = reference.get("file")
    reference_image = load_image(bpy, folder, reference, warnings)
    projection = None
    if reference_image:
        projection = projected_material(bpy, camera, reference_image, p00, p11)
    else:
        warnings.append(
            "No reference image; captured scene uses editable materials"
        )
    editable = bpy.data.scenes.new("Editable materials (approximate)")
    configure_scene(editable)
    editable.collection.objects.link(camera)
    editable.camera = camera
    for key in (
        "resolution_x",
        "resolution_y",
        "resolution_percentage",
        "pixel_aspect_x",
        "pixel_aspect_y",
    ):
        setattr(editable.render, key, getattr(scene.render, key))
    add_lighting(bpy, editable)
    collections = {}
    materials = {}
    objects = []
    skinning = []
    mesh_count, vertex_count, triangle_count = 0, 0, 0
    for record in mesh_records(manifest, folder):
        positions = record.get("positions", [])
        faces = record.get("faces", record.get("polygons", []))
        if not positions or not faces:
            warnings.append("Empty geometry: " + record.get("name", "draw"))
            continue
        name = record.get("name", "event_{}".format(record.get("event_id", 0)))
        mesh = bpy.data.meshes.new(name)
        mesh.from_pydata(positions, [], faces)
        mesh.update()
        category = record.get("collection") or record.get("category", "Scene")
        if str(category).lower().startswith("character"):
            mesh.polygons.foreach_set(
                "use_smooth", [True] * len(mesh.polygons)
            )
        add_uv_layer(mesh, "Source UV", record.get("uvs"))
        add_uv_layer(mesh, "Captured screen UV", record.get("projected_uvs"))
        if mesh.uv_layers:
            mesh.uv_layers.active_index = 0
        normals = record.get("normals")
        if normals and len(normals) == len(positions):
            try:
                mesh.normals_split_custom_set_from_vertices(normals)
            except Exception as error:
                warnings.append("Normals {}: {}".format(name, error))
        material = editable_material(bpy, record, folder, materials, warnings)
        mesh.materials.append(material)
        for target, appearance in (
            (scene, projection or material),
            (editable, material),
        ):
            key = (target.name, category)
            if key not in collections:
                collection = bpy.data.collections.new(category)
                target.collection.children.link(collection)
                collections[key] = collection
            obj = bpy.data.objects.new(name, mesh)
            collections[key].objects.link(obj)
            obj.material_slots[0].link = "OBJECT"
            obj.material_slots[0].material = appearance
            for field in ("event_id", "instance", "coordinate_space"):
                if field in record:
                    obj[field] = record[field]
            obj["source_record"] = json.dumps(
                {
                    k: record[k]
                    for k in ("file", "material_bindings", "material")
                    if k in record
                }
            )
            if target == scene:
                objects.append(obj)
        mesh_count += 1
        vertex_count += len(positions)
        triangle_count += sum(max(0, len(face) - 2) for face in faces)
        skinning.append(create_rigged_source(bpy, scene, record))
        if mesh_count % 50 == 0:
            print("BLENDER_IMPORT", mesh_count, vertex_count, flush=True)
    if not mesh_count:
        raise RuntimeError("No nonempty meshes in Blender manifest")
    coverage = store_skinning(scene, skinning)
    for collection in scene.collection.children:
        if collection.get("capture_role") == "rigged_source":
            editable.collection.children.link(collection)
    store_skinning(editable, skinning)
    selected = editable if args.mode == "editable" or not projection else scene
    bpy.context.window.scene = selected
    scene["reconstruction_mode"] = (
        "camera_projected_capture"
        if projection
        else "editable_material_approximation"
    )
    editable["reconstruction_mode"] = "editable_material_approximation"
    for target in (scene, editable):
        target.render.resolution_percentage = scale
        target["source_manifest"] = str(manifest_path)
        target["capture"] = json.dumps(
            manifest.get("capture", manifest.get("source_capture", {}))
        )
        target["geometry_space"] = manifest.get("coordinate_space", "camera")
        target["limitations"] = (
            "Visible geometry is the captured pose. Available input-space "
            "rigs are separate hidden source assets; no animation recovered. "
            "Original game lighting, materials and post effects "
            "are not rebuilt. "
            "Projected scene preserves the captured camera appearance only; "
            "editable scene uses captured texture bindings "
            "and preview lighting."
        )
    notes = bpy.data.texts.new("READ ME - reconstruction limits")
    notes.write(
        "RenderDoc scene reconstruction\n\n"
        + scene["limitations"]
        + "\n\n"
        + "Scenes:\n- Captured look (projected): "
        "image projected onto geometry.\n"
        + "- Editable materials (approximate): "
        "native Principled materials.\n\n"
        + "Capture manifest: "
        + str(manifest_path)
        + "\n\n"
        + "Warnings:\n"
        + "\n".join(str(w) for w in warnings)
    )
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == "VIEW_3D":
                area.spaces.active.region_3d.view_perspective = "CAMERA"
                area.spaces.active.shading.type = "MATERIAL"
    if projection and args.reference_background:
        distance = camera.data.clip_end * 0.95
        x, y = distance / p00 * 1.01, distance / p11 * 1.01
        backdrop_mesh = bpy.data.meshes.new("Reference background plane")
        backdrop_mesh.from_pydata(
            [
                (-x, -y, -distance),
                (x, -y, -distance),
                (x, y, -distance),
                (-x, y, -distance),
            ],
            [],
            [(0, 1, 2, 3)],
        )
        backdrop_mesh.materials.append(projection)
        backdrop = bpy.data.objects.new(
            "Reference sky/background (camera only)", backdrop_mesh
        )
        background_collection = bpy.data.collections.new(
            "Reference sky/background (camera only)"
        )
        scene.collection.children.link(background_collection)
        background_collection.objects.link(backdrop)
        backdrop.parent = camera
        backdrop.hide_select = True
        backdrop["reference_only"] = True
        backdrop["limitation"] = (
            "2D captured backdrop for missing sky and frame coverage; "
            "not extracted sky or effect geometry."
        )
        scene["reference_background"] = True
        notes.write(
            "\nCaptured scene includes a 2D reference background "
            "plane; sky and effects on that plane are not 3D geometry.\n"
        )
    bpy.ops.file.pack_all()
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(output_path), compress=False)
    report = {
        "blend_file": str(output_path),
        "mesh_count": mesh_count,
        "skinning": coverage,
        "vertex_count": vertex_count,
        "triangle_count": triangle_count,
        "material_count": len(materials),
        "image_count": len(bpy.data.images),
        "scene_names": [scene.name, editable.name],
        "warnings": warnings,
        "appearance_mode": selected["reconstruction_mode"],
        "reference_background": bool(projection and args.reference_background),
    }
    if args.render:
        selected.render.filepath = str(Path(args.render).resolve())
        bpy.ops.render.render(write_still=True)
        report["render_file"] = selected.render.filepath
    if args.render_editable:
        bpy.context.window.scene = editable
        editable.render.filepath = str(Path(args.render_editable).resolve())
        bpy.ops.render.render(write_still=True)
        report["editable_render_file"] = editable.render.filepath
        bpy.context.window.scene = selected
    report_path = output_path.with_suffix(".report.json")
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        "BLENDER_EXPORT_RESULT " + json.dumps(report, ensure_ascii=False),
        flush=True,
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--render")
    parser.add_argument("--render-editable")
    parser.add_argument("--scale", type=int, default=100)
    parser.add_argument("--reference-background", action="store_true")
    parser.add_argument(
        "--mode", choices=("captured", "editable"), default="captured"
    )
    arguments = (
        sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else None
    )
    build(parser.parse_args(arguments))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = Path(__file__).resolve().parent.parent.name + ".exporters"
    main()
