"""Publish captured pass bytecode and its Unity program variants together."""

import asyncio
import hashlib
import json
import tempfile
from pathlib import Path

from renderdoc_extension.utils.protocol import read_json

from ..bridge.client import BridgeClient, BridgeError
from .shader_state import shaderlab_state


def _bytecode(folder: Path, module: dict) -> Path:
    if not module["bytecode"]:
        raise ValueError(module.get("unavailable_reason", "Missing bytecode"))
    path = (folder / module["bytecode"]).resolve()
    if path.parent != folder / "modules" or not path.is_file():
        raise BridgeError("Invalid bytecode path returned by the extension")
    return path


def _convert(
    directory,
    variant,
    modules,
    semantics,
    spirv_cross,
    dxbc_decompiler,
    vkd3d_compiler,
):
    from .unity_shader import convert_compute_shader, convert_shader_pair

    if variant["specialization"]:
        raise ValueError(
            "Captured specialization constants require substitution before "
            "translation; see specialization and the original bytecode"
        )
    stages = {s: modules[i] for s, i in variant["stages"].items()}
    tools = {"spirv_cross": spirv_cross, "dxbc_converter": dxbc_decompiler}
    base = directory / "unity" / variant["id"]
    if set(stages) == {"Compute"}:
        cs = stages["Compute"]
        destination = base.with_suffix(".compute")
        detail = convert_compute_shader(
            _bytecode(directory, cs),
            destination,
            entry_point=cs["entry_point"],
            vkd3d_compiler=vkd3d_compiler,
            **tools,
        )
    elif set(stages) == {"Vertex", "Pixel"}:
        vs, ps = stages["Vertex"], stages["Pixel"]
        destination = base.with_suffix(".shader")
        detail = convert_shader_pair(
            _bytecode(directory, vs),
            _bytecode(directory, ps),
            destination,
            shader_name="OmniRenderDoc/" + variant["id"],
            vertex_entry=vs["entry_point"],
            fragment_entry=ps["entry_point"],
            render_state=variant["render_state"],
            vertex_semantics=semantics.get(vs["resource_id"], {}),
            **tools,
        )
    else:
        raise ValueError(
            "Unity conversion requires a vertex/pixel pair or compute kernel; "
            "captured stages: " + (", ".join(stages) or "none")
        )
    if not destination.is_file():
        raise RuntimeError("Converter produced no Unity asset")
    detail.pop("file", None)
    return {**detail, "asset": destination.relative_to(directory).as_posix()}


def _variants(data: dict, semantics: dict) -> list[dict]:
    modules = {m["id"]: m for m in data["modules"]}
    unknown = set(semantics) - {
        m["resource_id"] for m in modules.values() if m["stage"] == "Vertex"
    }
    if unknown:
        raise ValueError(
            "Unknown vertex shader IDs: " + ", ".join(sorted(unknown))
        )
    variants = {}
    for event in data["events"]:
        stages = event["stages"]
        lines, notes = shaderlab_state(event["pipeline"])
        vertex = modules[stages["Vertex"]] if "Vertex" in stages else {}
        specialization = {
            s: bindings["specialization"]
            for s, bindings in event.get("bindings", {}).items()
            if bindings.get("specialization")
        }
        signature = [
            [
                (stage, modules[i]["sha256"] or i, modules[i]["entry_point"])
                for stage, i in sorted(stages.items())
            ],
            lines,
            notes,
            semantics.get(vertex.get("resource_id"), {}),
            specialization,
        ]
        digest = hashlib.sha256(json.dumps(signature, sort_keys=True).encode())
        key = "pass_{}_{}".format(
            data["scope"]["event_id"], digest.hexdigest()[:20]
        )
        if key not in variants:
            variants[key] = {
                "id": key,
                "stages": stages,
                "event_ids": [],
                "render_state": lines,
                "state_notes": notes,
                "specialization": specialization,
            }
        variants[key]["event_ids"].append(event["event_id"])
        event["variant"] = key
    return list(variants.values())


def _texture_properties(event, modules, conversion):
    properties = {}
    for tex in conversion.get("textures", []):
        binding = tex.get("binding")
        space = tex.get("set") or 0
        register = tex.get("register")
        if binding is None and register and register.startswith("t"):
            binding = int(register[1:])
        if binding is not None and tex["property"]:
            properties[tex["stage"], space, binding] = tex["property"]
    matches = []
    for stage, bindings in event.get("bindings", {}).items():
        module = modules[event["stages"][stage]]
        reflection = module.get("reflection") or {}
        slots = reflection.get("read_only_resources", [])
        for resource in bindings.get("read_only_resources", []):
            slot = resource["slot"]
            if resource.get("type") != "texture" or not 0 <= slot < len(slots):
                continue
            space = slots[slot]["set_or_space"]
            binding = slots[slot]["binding"]
            prop = properties.get((stage, space, binding))
            if prop is not None:
                matches.append(
                    {
                        "property": prop,
                        "stage": stage,
                        "set_or_space": space,
                        "binding": binding,
                        "array_element": resource["array_element"],
                        "resource_id": resource["resource_id"],
                    }
                )
    return matches


def _finish(folder, data, to_unity, semantics, spirv_cross, decompiler, vkd3d):
    variants = _variants(data, semantics)
    modules = {m["id"]: m for m in data["modules"]}
    if to_unity:
        (folder / "unity").mkdir()
    for item in variants:
        if not to_unity:
            item["status"] = "native_only"
            continue
        try:
            detail = _convert(
                folder,
                item,
                modules,
                semantics,
                spirv_cross,
                decompiler,
                vkd3d,
            )
        except (OSError, ValueError, RuntimeError) as error:
            if isinstance(error, BridgeError):
                raise
            item.update(status="conversion_failed", reason=str(error))
        else:
            item.update(status="converted", conversion=detail)
    data["variants"] = variants
    by_id = {v["id"]: v for v in variants}
    for event in data["events"]:
        variant = by_id[event["variant"]]
        if variant["status"] == "converted":
            event["unity_texture_bindings"] = _texture_properties(
                event, modules, variant["conversion"]
            )
    converted = sum(v["status"] == "converted" for v in variants)
    return {
        "variant_count": len(variants),
        "unity_asset_count": converted,
        "conversion_failed_count": sum(
            v["status"] == "conversion_failed" for v in variants
        ),
    }


async def export_shaders(
    client: BridgeClient,
    output_directory: str,
    pass_event_id: int,
    convert_to_unity: bool,
    vertex_semantics: dict,
    spirv_cross: Path | None,
    dxbc_decompiler: Path | None,
    vkd3d_compiler: Path | None = None,
) -> dict:
    root = Path(output_directory).expanduser()
    if not root.is_absolute():
        raise ValueError("Specify an absolute output_directory")
    if type(pass_event_id) is not int or pass_event_id < 0:
        raise ValueError("pass_event_id must be a non-negative integer")
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root, prefix=".omni-shader-") as tmp:
        staging = Path(tmp).resolve()
        summary = await client.call(
            "stage_pass_shaders",
            {"output_directory": str(staging), "pass_event_id": pass_event_id},
        )
        folder = Path(summary["directory"]).resolve()
        if folder.parent != staging or not folder.name.startswith(
            "pass-shaders-"
        ):
            raise BridgeError("Unexpected shader staging directory")
        data = read_json(folder / "manifest.json")
        worker = asyncio.create_task(
            asyncio.to_thread(
                _finish,
                folder,
                data,
                convert_to_unity,
                vertex_semantics,
                spirv_cross,
                dxbc_decompiler,
                vkd3d_compiler,
            )
        )
        try:
            counts = await asyncio.shield(worker)
        except asyncio.CancelledError:
            # Let the converter release files before removing the staging tree.
            await worker
            raise
        destination = root / folder.name
        if destination.exists():
            raise FileExistsError(destination)
        summary.update(
            counts,
            directory=str(destination),
            manifest=str(destination / "manifest.json"),
        )
        data.update(summary)
        (folder / "manifest.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        folder.rename(destination)
    return summary
