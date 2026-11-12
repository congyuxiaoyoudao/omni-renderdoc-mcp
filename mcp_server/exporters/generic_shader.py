"""Capture-independent shader evidence, recipe preparation and Blender runs.

Bytecode hashes select reusable fragment equations. Descriptor bindings and
constant values always come from the selected capture's individual draw.
No game, frame, event or resource identifiers are part of this workflow.
"""

import asyncio
import hashlib
import json
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .blender import _run_blender, find_blender


def asset(root, relative):
    if not isinstance(relative, str) or not relative:
        raise ValueError("Evidence asset needs a relative path")
    value = Path(relative)
    root = Path(root).resolve()
    target = (root / value).resolve()
    if value.is_absolute() or not target.is_relative_to(root):
        raise ValueError("Evidence asset escapes its bundle: " + relative)
    if not target.is_file():
        raise ValueError("Missing evidence asset: " + relative)
    return target


def read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise ValueError("Invalid JSON file: " + str(path)) from error
    if not isinstance(data, dict):
        raise ValueError("Expected JSON object: " + str(path))
    return data


def load_evidence(directory):
    root = Path(directory).resolve()
    data = read_json(root / "index.json")
    if (
        data.get("format") != "omni_shader_evidence"
        or data.get("version") != 1
    ):
        raise ValueError("Expected omni_shader_evidence version 1")
    modules, events = data.get("modules"), data.get("events")
    if (
        not isinstance(modules, list)
        or not isinstance(events, list)
        or not events
    ):
        raise ValueError("Shader evidence needs modules and captured events")
    by_id = {}
    for module in modules:
        if not isinstance(module, dict) or not re.fullmatch(
            r"[A-Za-z0-9_-]+", str(module.get("id", ""))
        ):
            raise ValueError("Invalid shader module")
        if module["id"] in by_id:
            raise ValueError("Duplicate shader module ID")
        by_id[module["id"]] = module
        if module.get("sha256") and not module.get("bytecode"):
            raise ValueError("Shader hash requires captured bytecode")
        if module.get("bytecode"):
            path = asset(root, module["bytecode"])
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if module.get("sha256") != digest:
                raise ValueError(
                    "Shader bytecode hash does not match evidence"
                )
        for source in module.get("embedded_sources", []):
            asset(root, source["file"])
        if module.get("disassembly"):
            value = module["disassembly"]
            asset(root, value["file"] if isinstance(value, dict) else value)
    seen = set()
    validated_raw = {}
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("Invalid shader event")
        eid = event.get("event_id")
        if type(eid) is not int or eid < 0 or eid in seen:
            raise ValueError("Shader events require unique nonnegative IDs")
        seen.add(eid)
        if not isinstance(event.get("stages"), dict) or any(
            mid not in by_id for mid in event["stages"].values()
        ):
            raise ValueError("Shader event references missing modules")
        for binding in event.get("bindings", {}).values():
            for key in ("read_only_resources", "read_write_resources"):
                for resource in binding.get(key, []):
                    for image in resource.get("images", []):
                        asset(root, image["file"])
                    for subresource in resource.get("raw_subresources", []):
                        _raw_asset(root, subresource, validated_raw)
                    for field in ("raw_data", "data_file", "buffer_data"):
                        if resource.get(field):
                            _raw_asset(root, resource[field], validated_raw)
        for blocks in event.get("constants", {}).values():
            for block in blocks:
                for field in ("raw_data", "data_file"):
                    if block.get(field):
                        _raw_asset(root, block[field], validated_raw)
    return data


def _raw_asset(root, value, cache):
    path = asset(root, value["file"] if isinstance(value, dict) else value)
    if isinstance(value, dict):
        if path not in cache:
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    size += len(chunk)
                    digest.update(chunk)
            cache[path] = (size, digest.hexdigest())
        size, digest = cache[path]
        if value.get("byte_length") != size:
            raise ValueError("Captured raw input byte length mismatch")
        if digest != value.get("sha256"):
            raise ValueError("Captured raw input hash mismatch")


def _recipe_samplers(event):
    result = {}
    samplers = event.get("bindings", {}).get("Pixel", {}).get("samplers", [])
    for sampler in samplers:
        minimum = str(sampler.get("min_filter", "")).split(".")[-1]
        maximum = str(sampler.get("mag_filter", "")).split(".")[-1]
        u = str(sampler.get("address_u", "")).split(".")[-1]
        v = str(sampler.get("address_v", "")).split(".")[-1]
        if (
            minimum != maximum
            or minimum not in ("Point", "Linear")
            or u != v
            or u not in ("Wrap", "ClampEdge")
            or str(sampler.get("filter", "")).split(".")[-1] != "Normal"
        ):
            continue
        result[sampler["name"]] = {
            "set": sampler.get("set_or_space", 0),
            "binding": sampler["binding"],
            "filter": "nearest" if minimum == "Point" else "linear",
            "wrap": "repeat" if u == "Wrap" else "clamp",
        }
    return result


def _source(module, evidence, folder, spirv_cross):
    """Use actual shader bytes; debug author files may not be compiled code."""
    encoding = module.get("encoding", "")
    if encoding in ("GLSL", "OpenGLGLSL") and module.get("bytecode"):
        return asset(evidence, module["bytecode"]).read_text(), "captured GLSL"
    if encoding in ("SPIRV", "OpenGLSPIRV") and module.get("bytecode"):
        tool = shutil.which(str(spirv_cross or "spirv-cross"))
        if not tool:
            return (
                None,
                "SPIR-V source analysis needs spirv-cross on PATH "
                "or configured",
            )
        path = asset(evidence, module["bytecode"])
        command = [
            tool,
            str(path),
            "--vulkan-semantics",
            "--entry",
            module.get("entry_point") or "main",
            "--stage",
            "frag",
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                timeout=120,
                cwd=folder,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return None, "Source conversion failed: " + str(error)
        if result.returncode:
            return None, "Source conversion failed: " + result.stderr.decode(
                errors="replace"
            )[-1500:]
        source = result.stdout.decode("utf-8", errors="replace")
        target = folder / (module["id"] + ".frag")
        target.write_text(source, encoding="utf-8")
        return source, "decompiled current captured bytecode: " + target.name
    return None, "No automatic source translator for encoding " + str(encoding)


def prepare_recipes(evidence, folder, node_recipes=None, spirv_cross=None):
    from .auto_fragment import infer_recipe
    from .fragment_graph import validate_registry

    evidence, folder = Path(evidence).resolve(), Path(folder).resolve()
    data = load_evidence(evidence)
    explicit = (
        read_json(node_recipes)
        if node_recipes
        else {"version": 1, "materials": []}
    )
    registry = validate_registry(explicit)
    selected, analyses, templates = [], [], []
    used = {event["stages"].get("Pixel") for event in data["events"]}
    emitted = set()
    for module in data["modules"]:
        if module["id"] not in used:
            continue
        digest = module.get("sha256")
        row = {
            "module_id": module["id"],
            "shader_sha256": digest,
            "entry_point": module.get("entry_point"),
            "encoding": module.get("encoding"),
        }
        recipe = registry.get(digest)
        if recipe and recipe.get("entry_point", "main") != (
            module.get("entry_point") or "main"
        ):
            row.update(
                status="needs_recipe",
                reason="Recipe entry point differs from captured shader",
            )
            recipe = None
        elif recipe:
            row.update(
                status="explicit_recipe",
                reason="User-reviewed reusable fragment equations",
            )
        else:
            event = next(
                e
                for e in data["events"]
                if e["stages"].get("Pixel") == module["id"]
            )
            event = dict(
                event,
                recipe_samplers=_recipe_samplers(event),
                shader_sha256=digest,
                shader_entry_point=module.get("entry_point") or "main",
            )
            source, provenance = _source(module, evidence, folder, spirv_cross)
            recipe = (
                infer_recipe(source, digest, event=event)
                if source and digest
                else None
            )
            if (
                not recipe
                and module.get("encoding") == "DXBC"
                and module.get("disassembly")
            ):
                from .auto_fragment import infer_dxbc_recipe

                disassembly = module["disassembly"]
                path = asset(
                    evidence,
                    disassembly["file"]
                    if isinstance(disassembly, dict)
                    else disassembly,
                )
                recipe = infer_dxbc_recipe(
                    path.read_text(encoding="utf-8-sig"),
                    digest,
                    event,
                    module.get("reflection", {}),
                )
                if recipe:
                    provenance = "validated complete captured DXBC fragment"
            row.update(
                status="automatic_recipe" if recipe else "needs_recipe",
                reason=provenance,
            )
            if source and not recipe:
                row["reason"] = (
                    "Fragment is outside the automatic expression subset; "
                    + provenance
                )
        if recipe and digest not in emitted:
            selected.append(recipe)
            emitted.add(digest)
        if not recipe:
            templates.append(
                {
                    "shader_sha256": digest,
                    "entry_point": module.get("entry_point"),
                    "event_ids": [
                        e["event_id"]
                        for e in data["events"]
                        if e["stages"].get("Pixel") == module["id"]
                    ],
                    "status": "requires_shader_analysis",
                    "reason": row["reason"],
                    "module": module,
                    "recipe_contract": (
                        "version 1 registry; shader_sha256 + nodes + outputs"
                    ),
                }
            )
        analyses.append(row)
    registry_path = folder / "prepared_recipes.json"
    registry_path.write_text(
        json.dumps({"version": 1, "materials": selected}, indent=2),
        encoding="utf-8",
    )
    analysis = {
        "format": "omni_fragment_analysis",
        "version": 1,
        "source_capture": data.get("source_capture"),
        "modules": analyses,
        "evidence_issues": data.get("issues", []),
        "source_frame_color_images_used": False,
        "limitations": [
            "Arbitrary programs are not automatically equivalent to "
            "Blender shaders.",
            "Unsupported programs require reviewed node recipes; "
            "bytecode and inputs remain in the bundle.",
            "Recipes match shader content; resource IDs and constant values "
            "come from each current draw.",
        ],
    }
    (folder / "shader_analysis.json").write_text(
        json.dumps(analysis, indent=2), encoding="utf-8"
    )
    (folder / "recipe_tasks.json").write_text(
        json.dumps({"version": 1, "tasks": templates}, indent=2),
        encoding="utf-8",
    )
    return registry_path, analysis


def verify_output(folder, render_preview):
    blend = folder / "scene.blend"
    if not blend.is_file() or blend.open("rb").read(7) != b"BLENDER":
        raise RuntimeError(
            "Generic shader export produced no native Blender file"
        )
    report = read_json(folder / "scene.coverage.json")
    if (
        not report.get("mesh_count")
        or report.get("source_frame_color_images_used") is not False
    ):
        raise RuntimeError("Invalid generic shader export coverage")
    if report.get("status") not in ("translated", "partial", "needs_recipes"):
        raise RuntimeError(
            "Generic shader export must report translation status"
        )
    if render_preview:
        preview = folder / "preview.png"
        if (
            not preview.is_file()
            or preview.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n"
        ):
            raise RuntimeError(
                "Generic shader export produced no valid preview"
            )
    return report


def validate_options(
    render_preview, render_scale, timeout_seconds, unsupported_shaders
):
    if type(render_preview) is not bool:
        raise ValueError("render_preview must be a boolean")
    if unsupported_shaders not in ("report", "error"):
        raise ValueError("unsupported_shaders must be report or error")
    if type(render_scale) is not int or not 1 <= render_scale <= 100:
        raise ValueError("render_scale must be an integer from 1 to 100")
    if (
        type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be positive and finite")


async def build_generic(
    folder,
    evidence,
    *,
    manifest=None,
    geometry=None,
    blender_executable=None,
    node_recipes=None,
    unsupported_shaders="report",
    render_preview=True,
    render_scale=100,
    timeout_seconds=1200,
    spirv_cross=None,
):
    validate_options(
        render_preview, render_scale, timeout_seconds, unsupported_shaders
    )
    folder, evidence = Path(folder).resolve(), Path(evidence).resolve()
    executable = find_blender(blender_executable)
    recipes, analysis = await asyncio.to_thread(
        prepare_recipes, evidence, folder, node_recipes, spirv_cross
    )
    command = [
        str(executable),
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(Path(__file__).with_name("generic_nodes.py")),
        "--",
        "--evidence",
        str(evidence),
        "--recipes",
        str(recipes),
        "--output",
        str(folder / "scene.blend"),
        "--scale",
        str(render_scale),
        "--unsupported",
        unsupported_shaders,
    ]
    if manifest:
        command.extend(["--manifest", str(Path(manifest).resolve())])
    elif geometry:
        command.extend(["--input-blend", str(Path(geometry).resolve())])
    else:
        raise ValueError(
            "A geometry manifest or exported generic blend is required"
        )
    if render_preview:
        command.extend(["--render", str(folder / "preview.png")])
    (folder / "shader_request.json").write_text(
        json.dumps(
            {
                "reconstruction_mode": "generic_shader_nodes",
                "evidence": str(evidence),
                "node_recipes": str(node_recipes) if node_recipes else None,
                "unsupported_shaders": unsupported_shaders,
                "command": command,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    await _run_blender(command, folder, timeout_seconds)
    coverage = verify_output(folder, render_preview)
    result = {
        "directory": str(folder),
        "blend_file": str(folder / "scene.blend"),
        "status": coverage["status"],
        "reconstruction_mode": "generic_shader_nodes",
        "coverage_report": str(folder / "scene.coverage.json"),
        "skinning": coverage.get("skinning", {}),
        "shader_evidence_directory": str(evidence),
        "shader_analysis": str(folder / "shader_analysis.json"),
        "node_recipes": str(recipes),
        "recipe_tasks": str(folder / "recipe_tasks.json"),
        "coverage": {k: v for k, v in coverage.items() if k != "materials"},
        "limitations": coverage.get("limitations", analysis["limitations"]),
        "blender_log": str(folder / "blender.log"),
    }
    if render_preview:
        result["preview"] = str(folder / "preview.png")
    (folder / "export_result.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    return result


async def rebuild_generic(
    output_directory, geometry_blend, shader_evidence_directory, **kwargs
):
    validate_options(
        kwargs.get("render_preview", True),
        kwargs.get("render_scale", 100),
        kwargs.get("timeout_seconds", 1200),
        kwargs.get("unsupported_shaders", "report"),
    )
    paths = [
        Path(value).expanduser()
        for value in (
            output_directory,
            geometry_blend,
            shader_evidence_directory,
        )
    ]
    if any(not p.is_absolute() for p in paths):
        raise ValueError("Generic rebuild paths must be absolute")
    root, geometry, evidence = [p.resolve() for p in paths]
    if not geometry.is_file():
        raise ValueError("geometry_blend does not exist")
    with geometry.open("rb") as stream:
        if stream.read(7) != b"BLENDER":
            raise ValueError("geometry_blend must be a native Blender file")
    load_evidence(evidence)
    node_recipes = kwargs.get("node_recipes")
    if node_recipes:
        from .fragment_graph import validate_registry

        if not Path(node_recipes).is_absolute():
            raise ValueError("node_recipes must be an absolute JSON file")
        validate_registry(read_json(node_recipes))
    root.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="shader-nodes-", dir=root))
    return await build_generic(folder, evidence, geometry=geometry, **kwargs)
