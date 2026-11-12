"""Stage captured camera-space geometry in a standalone Unity HDRP project.

The replay bridge's version-1 mesh bundle is shared with the Blender exporter.
No Blender installation or frame-specific event/resource IDs are required.
The editable HDRP/Lit mode and recovered original-fragment mode are explicit.
"""

import asyncio
import gzip
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path, PureWindowsPath

from ..bridge.client import BridgeClient, BridgeError
from .blender import _validate

DEFAULT_EDITOR_VERSION = "6000.3.7f1"
DEFAULT_HDRP_VERSION = "17.3.0"


def _read_json(path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8-sig") as stream:
        return json.load(stream)


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )


def _inside(root, relative):
    if (
        not isinstance(relative, str)
        or Path(relative).is_absolute()
        or PureWindowsPath(relative).drive
        or relative.startswith("\\")
    ):
        raise ValueError("Bundle file paths must be relative")
    candidate = (root / relative.replace("\\", "/")).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ValueError("Bundle file path escapes its directory")
    if not candidate.is_file():
        raise FileNotFoundError(candidate)
    return candidate


def find_unity(executable=None):
    requested = executable or os.environ.get("UNITY_EXECUTABLE")
    if requested:
        path = Path(requested).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise ValueError("unity_executable must be an absolute file")
        return path.resolve()
    located = shutil.which("Unity") or shutil.which("unity")
    if located:
        return Path(located).resolve()
    roots = [
        Path(os.environ.get("ProgramFiles", "C:/Program Files"))
        / "Unity/Hub/Editor",
        Path("/Applications/Unity/Hub/Editor"),
        Path.home() / "Unity/Hub/Editor",
    ]
    for root in roots:
        for suffix in (
            "*/Editor/Unity.exe",
            "*/Unity.app/Contents/MacOS/Unity",
            "*/Editor/Unity",
        ):
            candidates = sorted(
                (p for p in root.glob(suffix) if p.is_file()),
                key=lambda p: tuple(
                    int(n) for n in re.findall(r"\d+", str(p))
                ),
                reverse=True,
            )
            if candidates:
                return candidates[0].resolve()
    raise FileNotFoundError(
        "Unity was not found; supply unity_executable or UNITY_EXECUTABLE"
    )


def _finite_rows(rows, components, label, count=None):
    if not isinstance(rows, list) or (
        count is not None and len(rows) != count
    ):
        raise ValueError(label + " has an invalid vertex count")
    for row in rows:
        if not isinstance(row, list) or len(row) != components:
            raise ValueError(label + " has invalid components")
        if any(
            isinstance(v, bool)
            or not isinstance(v, (int, float))
            or not math.isfinite(v)
            for v in row
        ):
            raise ValueError(label + " contains nonfinite/non-numeric values")
    return rows


def convert_mesh(mesh):
    """Flip camera Z and winding together, preserving projected appearance."""
    vertices = _finite_rows(mesh.get("positions"), 3, "positions")
    if not vertices:
        raise ValueError("Empty captured mesh")
    triangles = []
    for face in mesh.get("faces", []):
        if (
            not isinstance(face, list)
            or len(face) < 3
            or any(
                type(i) is not int or i < 0 or i >= len(vertices) for i in face
            )
        ):
            raise ValueError("Invalid captured face indices")
        for i in range(1, len(face) - 1):
            triangles.extend((face[0], face[i + 1], face[i]))
    if not triangles:
        raise ValueError("Mesh contains no triangles")
    uvs = mesh.get("uvs", [])
    if uvs:
        _finite_rows(uvs, 2, "uvs", len(vertices))
    return {
        "name": str(mesh.get("name", "CapturedMesh")),
        "event_id": mesh.get("event_id", 0),
        "instance": mesh.get("instance", 0),
        "category": str(mesh.get("category", "scene")),
        "vertices": [value for x, y, z in vertices for value in (x, y, -z)],
        "triangles": triangles,
        "uv": [value for row in uvs for value in row],
    }


def _camera(source, render_scale):
    camera = dict(source)
    for key in ("p00", "p11", "width", "height"):
        value = camera.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Camera requires numeric " + key)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("Camera " + key + " must be positive and finite")
    camera.setdefault("near", 0.01)
    camera.setdefault("far", 10000.0)
    if not 0 < camera["near"] < camera["far"] < float("inf"):
        raise ValueError("Invalid camera clipping planes")
    camera["width"] = max(1, round(camera["width"] * render_scale / 100))
    camera["height"] = max(1, round(camera["height"] * render_scale / 100))
    return camera


def material_spec(material):
    base_color = _finite_rows(
        [material.get("base_color", [1, 1, 1, 1])], 4, "material.base_color"
    )[0]
    if min(base_color) < 0 or base_color[3] > 1:
        raise ValueError(
            "Material base_color must be nonnegative with alpha <= 1"
        )
    bindings = material.get("bindings", [])
    base = next((b for b in bindings if b.get("role") == "base_color"), {})
    normal = next((b for b in bindings if b.get("role") == "normal"), {})
    base_format = base.get("view_format") or base.get("resource_format", "")
    srgb = (
        not base
        or "SRGB" in str(base_format).upper()
        or "SRGB" in str(base.get("component_type", "")).upper()
    )
    normal_format = str(
        normal.get("view_format") or normal.get("resource_format", "")
    )
    if "SNORM" in str(normal.get("component_type", "")).upper():
        normal_format += " SNORM"
    result = {
        "base_color": [float(value) for value in base_color],
        "roughness": float(material.get("roughness", 0.72)),
        "metallic": float(material.get("metallic", 0)),
        "alpha_clip": bool(material.get("alpha_clip", False)),
        "alpha_cutoff": float(material.get("alpha_cutoff", 0.5)),
        "base_color_srgb": srgb,
        "normal_format": normal_format,
    }
    for name in ("roughness", "metallic", "alpha_cutoff"):
        if not math.isfinite(result[name]) or not 0 <= result[name] <= 1:
            raise ValueError("Material " + name + " must be in 0..1")
    return result


def validate_lighting(spec):
    if spec is None:
        return None
    if not isinstance(spec, dict) or set(spec) - {
        "directional_lights",
        "exposure_ev100",
        "tonemapping",
        "background_color_linear",
    }:
        raise ValueError("Invalid lighting specification or unknown keys")
    result = {
        "exposure_ev100": spec.get("exposure_ev100", 11),
        "tonemapping": spec.get("tonemapping", "ACES"),
    }
    exposure = result["exposure_ev100"]
    if (
        isinstance(exposure, bool)
        or not isinstance(exposure, (int, float))
        or not math.isfinite(exposure)
    ):
        raise ValueError("lighting exposure_ev100 must be finite")
    if result["tonemapping"] not in ("None", "ACES", "Neutral"):
        raise ValueError("lighting tonemapping must be None, ACES or Neutral")
    if "background_color_linear" in spec:
        background = spec["background_color_linear"]
        if not isinstance(background, list) or len(background) not in (3, 4):
            raise ValueError(
                "lighting background_color_linear requires RGB/RGBA"
            )
        _finite_rows([background], len(background), "background_color_linear")
        if min(background) < 0 or (len(background) == 4 and background[3] > 1):
            raise ValueError("Invalid background_color_linear component range")
        result["background_color_linear"] = background
    if "directional_lights" not in spec:
        return result
    if not isinstance(spec["directional_lights"], list):
        raise ValueError("lighting directional_lights must be an array")
    result["directional_lights"] = []
    for light in spec["directional_lights"]:
        if not isinstance(light, dict) or set(light) - {
            "name",
            "direction",
            "color_linear",
            "lux",
            "intensity_is_approximation",
            "provenance",
        }:
            raise ValueError("Invalid directional light or unknown keys")
        direction = _finite_rows([light.get("direction")], 3, "direction")[0]
        magnitude = math.hypot(*direction)
        if magnitude < 1e-12:
            raise ValueError("Directional light direction must be nonzero")
        color = _finite_rows(
            [light.get("color_linear", [1, 1, 1])], 3, "color_linear"
        )[0]
        if min(color) < 0:
            raise ValueError("Light color_linear must be nonnegative")
        lux = light.get("lux", 10000)
        if (
            isinstance(lux, bool)
            or not isinstance(lux, (int, float))
            or not math.isfinite(lux)
            or lux < 0
        ):
            raise ValueError("Light lux must be finite and nonnegative")
        approximate = light.get("intensity_is_approximation", True)
        if type(approximate) is not bool:
            raise ValueError("intensity_is_approximation must be boolean")
        result["directional_lights"].append(
            {
                "name": str(light.get("name", "Supplied directional light")),
                "direction": [value / magnitude for value in direction],
                "color_linear": color,
                "lux": lux,
                "intensity_is_approximation": approximate,
                "provenance": str(
                    light.get(
                        "provenance", "User-supplied lighting parameters"
                    )
                ),
            }
        )
    return result


def prepare_project(
    manifest_path,
    project,
    editor_version,
    hdrp_version,
    render_preview=True,
    render_scale=100,
    lighting=None,
):
    """Convert a saved replay bundle without requiring a live capture."""
    source = manifest_path.parent
    manifest = _read_json(manifest_path)
    if manifest.get("version") != 1:
        raise ValueError("Expected a version-1 captured geometry manifest")
    if not manifest.get("meshes"):
        raise ValueError("Capture manifest contains no meshes")
    camera = _camera(manifest.get("camera", {}), render_scale)
    template = Path(__file__).parent.parent / "unity_templates"
    editor = project / "Assets/Editor"
    editor.mkdir(parents=True, exist_ok=True)
    shutil.copy2(template / "CaptureSceneBuilder.cs", editor)
    scripts = project / "Assets/Scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    shutil.copy2(template / "CapturedProjection.cs", scripts)
    # Templates may include support shaders in later exporter versions.
    for shader in template.glob("*.shader"):
        destination = project / "Assets/Capture/Shaders" / shader.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(shader, destination)
    _write_json(
        project / "Packages/manifest.json",
        {
            "dependencies": {
                "com.unity.render-pipelines.high-definition": hdrp_version,
            }
        },
    )
    settings = project / "ProjectSettings"
    settings.mkdir(parents=True, exist_ok=True)
    (settings / "ProjectVersion.txt").write_text(
        "m_EditorVersion: " + editor_version + "\n", encoding="utf-8"
    )
    textures = {}
    meshes = []
    vertices = triangles = 0
    for index, relative in enumerate(manifest["meshes"]):
        mesh = _read_json(_inside(source, relative))
        converted = convert_mesh(mesh)
        material = mesh.get("material", {})
        converted["source_material"] = material
        converted["material"] = material_spec(material)
        for role in ("base_color_texture", "normal_texture"):
            if not material.get(role):
                continue
            texture = _inside(source, material[role])
            # A role-specific copy permits different sRGB/normal importers.
            interpretation = str(
                converted["material"]["base_color_srgb"]
                if role == "base_color_texture"
                else converted["material"]["normal_format"]
            )
            key = (str(texture), role, interpretation)
            if key not in textures:
                digest = hashlib.sha256(
                    (str(texture) + interpretation).encode()
                ).hexdigest()[:16]
                asset = (
                    "Assets/Capture/Textures/"
                    + role
                    + "_"
                    + digest
                    + texture.suffix.lower()
                )
                destination = project / asset
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(texture, destination)
                textures[key] = asset
            converted["material"][role] = textures[key]
        mesh_asset = "Assets/Capture/MeshData/{:06d}.json".format(index)
        _write_json(project / mesh_asset, converted)
        meshes.append({"file": mesh_asset})
        vertices += len(converted["vertices"]) // 3
        triangles += len(converted["triangles"]) // 3
    warnings = [
        "HDRP/Lit materials and lighting approximate captured shaders; "
        "original shaders, custom passes and post effects are not replayed.",
        "Geometry is the captured camera-local posed mesh, not the original "
        "Unity scene, hierarchy, animation or world-space transforms.",
        "UV and texture selection may be heuristic; use explicit bindings "
        "and visually compare the resulting preview.",
        "Normals are reconstructed from triangles; captured tangent-space "
        "normal packing may require a custom material.",
    ]
    if manifest.get("skipped"):
        warnings.append(
            str(len(manifest["skipped"]))
            + " source draw instances were skipped; see provenance.json."
        )
    _write_json(
        project / "Assets/Capture/capture.json",
        {
            "version": 1,
            "source_capture": manifest.get("source_capture", ""),
            "camera": camera,
            "meshes": meshes,
            "render_preview": render_preview,
            "lighting": lighting,
            "warnings": warnings,
        },
    )
    _write_json(
        project / "provenance.json",
        {
            "source_manifest": str(manifest_path),
            "source_capture": manifest.get("source_capture"),
            "camera": manifest.get("camera"),
            "source_meshes": manifest["meshes"],
            "skipped": manifest.get("skipped", []),
            "duplicates": manifest.get("duplicates", []),
            "skinning": manifest.get("skinning", {}),
            "source_warnings": manifest.get("warnings", []),
            "conversion": "(x,y,z) -> (x,y,-z), reversed triangle winding; "
            "UVs preserve the staged PNG orientation",
            "material_mode": "hdrp_lit_approximation",
            "lighting": lighting,
            "warnings": warnings,
        },
    )
    (project / ".gitignore").write_text(
        "/Library/\n/Temp/\n/Logs/\n/Obj/\n/UserSettings/\n", encoding="utf-8"
    )
    return {
        "mesh_count": len(meshes),
        "vertex_count": vertices,
        "triangle_count": triangles,
        "texture_count": len(textures),
        "warnings": warnings,
    }


async def _run_unity(command, project, timeout_seconds):
    with (project / "unity-process.log").open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(project),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=(
                subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            ),
        )
        try:
            await asyncio.wait_for(process.wait(), timeout_seconds)
        except (asyncio.TimeoutError, asyncio.CancelledError) as error:
            if process.returncode is None:
                process.kill()
                await process.wait()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise RuntimeError(
                f"Unity exceeded {timeout_seconds:g}s; project/logs "
                f"retained at {project}"
            ) from error
    if process.returncode:
        raise RuntimeError(
            f"Unity exited with code {process.returncode}; "
            f"inspect {project / 'unity.log'}"
        )


def _verify(project, render_preview, expected_meshes):
    report_path = project / "result.json"
    if not report_path.is_file():
        raise RuntimeError("Unity produced no result.json verification report")
    try:
        report = _read_json(report_path)
    except (ValueError, UnicodeError) as error:
        raise RuntimeError("Unity produced invalid result.json") from error
    if not isinstance(report, dict):
        raise RuntimeError("Unity result.json must contain a JSON object")
    if report.get("status") != "complete":
        raise RuntimeError(
            "Unity scene builder did not complete: " + str(report)
        )
    if report.get("mesh_count") != expected_meshes:
        raise RuntimeError("Unity scene mesh count does not match staged data")
    if (
        type(report.get("vertex_count")) is not int
        or report["vertex_count"] <= 0
    ):
        raise RuntimeError("Unity scene contains no reconstructed geometry")
    if not isinstance(report.get("warnings", []), list) or any(
        not isinstance(warning, str) for warning in report.get("warnings", [])
    ):
        raise RuntimeError("Unity result.json contains invalid warnings")
    scene = project / "Assets/CaptureScene.unity"
    if not scene.is_file() or not scene.stat().st_size:
        raise RuntimeError("Unity produced no saved CaptureScene.unity")
    if render_preview:
        preview = project / "preview.png"
        if (
            not preview.is_file()
            or preview.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n"
        ):
            raise RuntimeError("Unity produced no valid preview.png")
    return report


def _verify_original(project, render_preview, expected_meshes):
    report_path = project / "recovered-result.json"
    if not report_path.is_file():
        raise RuntimeError("Unity produced no recovered-result.json")
    try:
        report = _read_json(report_path)
    except (ValueError, UnicodeError) as error:
        raise RuntimeError(
            "Unity produced invalid recovered-result.json"
        ) from error
    if not isinstance(report, dict) or report.get("status") != "complete":
        raise RuntimeError(
            "Original fragment scene did not complete: " + str(report)
        )
    if report.get("draw_count") != expected_meshes or not report.get(
        "shader_count"
    ):
        raise RuntimeError("Original fragment draw/shader count mismatch")
    scene = project / "Assets/RecoveredCaptureScene.unity"
    if not scene.is_file() or not scene.stat().st_size:
        raise RuntimeError("Unity produced no saved recovered scene")
    if render_preview:
        preview = project / "recovered-preview.png"
        if (
            not preview.is_file()
            or preview.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n"
        ):
            raise RuntimeError("Unity produced no valid recovered preview")
    return report


async def export_unity_hdrp(
    client,
    output_directory: str,
    unity_executable: str | None = None,
    event_ids: list[int] | None = None,
    pass_event_id: int | None = None,
    camera: dict | None = None,
    material_bindings: dict | None = None,
    uv_attributes: dict[str, str] | None = None,
    lighting: dict | None = None,
    staged_manifest: str | None = None,
    editor_version: str = DEFAULT_EDITOR_VERSION,
    hdrp_version: str = DEFAULT_HDRP_VERSION,
    run_unity: bool = True,
    render_preview: bool = True,
    render_scale: int = 100,
    timeout_seconds: float = 1200,
    shader_mode: str = "hdrp_lit",
    spirv_cross_executable: str | None = None,
    supplementary_buffers: str | None = None,
    final_target: str | None = None,
    final_is_srgb: bool = False,
    final_flip_y: bool = False,
    shader_evidence_limits: dict | None = None,
) -> dict:
    """Preserve the generated project and logs, including on build failure."""
    if not re.fullmatch(r"\d+\.\d+\.\d+[abfp]\d+", editor_version):
        raise ValueError("editor_version must be a Unity editor version")
    if not re.fullmatch(r"\d+\.\d+\.\d+", hdrp_version):
        raise ValueError("hdrp_version must be a numeric package version")
    if type(run_unity) is not bool or type(render_preview) is not bool:
        raise ValueError("run_unity and render_preview must be booleans")
    if shader_mode not in ("hdrp_lit", "original_fragment"):
        raise ValueError("shader_mode must be hdrp_lit or original_fragment")
    original = shader_mode == "original_fragment"
    if type(final_is_srgb) is not bool:
        raise ValueError("final_is_srgb must be a boolean")
    if type(final_flip_y) is not bool:
        raise ValueError("final_flip_y must be a boolean")
    if shader_evidence_limits is not None:
        if not original:
            raise ValueError(
                "shader_evidence_limits requires original_fragment mode"
            )
        from renderdoc_extension.services.capture_evidence import _limits

        _limits(shader_evidence_limits)
    if original and lighting is not None:
        raise ValueError(
            "Original fragment mode uses captured shader lighting"
        )
    if not original and any(
        (
            spirv_cross_executable,
            supplementary_buffers,
            final_target,
            final_is_srgb,
            final_flip_y,
        )
    ):
        raise ValueError(
            "Original shader options require original_fragment mode"
        )
    if original:
        from .unity_shader import _tool

        spirv_cross_executable = _tool(spirv_cross_executable, "spirv-cross")
        if run_unity and os.name != "nt":
            raise ValueError(
                "Original fragment execution currently targets Windows D3D11"
            )
        if supplementary_buffers and not Path(supplementary_buffers).is_file():
            raise ValueError(
                "supplementary_buffers must name a captured buffer index"
            )
    if type(render_scale) is not int or not 1 <= render_scale <= 100:
        raise ValueError("render_scale must be an integer in 1..100")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be positive and finite")
    root = Path(output_directory).expanduser()
    if not root.is_absolute():
        raise ValueError("output_directory must be absolute")
    options = {
        "event_ids": event_ids,
        "pass_event_id": pass_event_id,
        "camera": camera,
        "material_bindings": material_bindings,
        "uv_attributes": uv_attributes,
        "shader_evidence_limits": shader_evidence_limits,
    }
    manifest = None
    if staged_manifest is not None:
        if any(value is not None for value in options.values()):
            raise ValueError("staged_manifest cannot combine capture options")
        manifest = Path(staged_manifest).expanduser()
        if not manifest.is_absolute() or not manifest.is_file():
            raise ValueError(
                "staged_manifest must be an existing absolute file"
            )
        manifest = manifest.resolve()
    else:
        _validate(options)
    lighting = validate_lighting(lighting)
    executable = (
        find_unity(unity_executable)
        if run_unity or unity_executable is not None
        else None
    )
    root.mkdir(parents=True, exist_ok=True)
    project = Path(tempfile.mkdtemp(prefix="unity-hdrp-", dir=root)).resolve()
    if manifest is None:
        staging = project / "CaptureEvidence"
        staging.mkdir()
        # Capture staging can take as long as the Unity build. Give this
        # request its own budget without changing concurrent bridge calls or
        # replacing a caller's custom client/overridden transport behavior.
        stage_client = (
            BridgeClient(
                client.directory, max(client.timeout, timeout_seconds)
            )
            if type(client) is BridgeClient
            else client
        )
        summary = await stage_client.call(
            "stage_blender_scene",
            {
                "output_directory": str(staging),
                **{
                    key: value
                    for key, value in options.items()
                    if value is not None
                },
                **(
                    {
                        "include_shader_evidence": True,
                        "shader_evidence_options": {
                            "native_only": True,
                            "decode_constants": False,
                        },
                    }
                    if original
                    else {}
                ),
            },
        )
        folder = Path(summary["directory"]).resolve()
        if folder.parent != staging or not folder.name.startswith(
            "blender-scene-"
        ):
            raise BridgeError("Unexpected capture staging directory")
        manifest = Path(summary["manifest"]).resolve()
        if manifest != folder / "manifest.json" or not manifest.is_file():
            raise BridgeError("Unexpected capture staging manifest")
    if original:
        from .recovered_project import prepare_original_project

        counts = prepare_original_project(
            manifest,
            project,
            editor_version,
            hdrp_version,
            render_preview,
            render_scale,
            spirv_cross_executable,
            supplementary_buffers,
            final_target,
            final_is_srgb,
            final_flip_y,
        )
    else:
        counts = prepare_project(
            manifest,
            project,
            editor_version,
            hdrp_version,
            render_preview,
            render_scale,
            lighting,
        )
    command = [
        str(executable or "Unity"),
        "-batchmode",
        "-quit",
        "-projectPath",
        str(project),
        "-executeMethod",
        "OmniRenderDoc.RecoveredCaptureBuilder.Build"
        if original
        else "OmniRenderDoc.CaptureSceneBuilder.Build",
        "-logFile",
        str(project / "unity.log"),
    ]
    if original:
        command.append("-force-d3d11")
    _write_json(project / "build_command.json", {"argv": command})
    result = {
        "status": "project_staged",
        "directory": str(project),
        "project_directory": str(project),
        "source_manifest": str(manifest),
        "editor_version": editor_version,
        "hdrp_version": hdrp_version,
        "appearance_mode": "original_fragment_equations"
        if original
        else "hdrp_lit_approximation",
        "shader_mode": shader_mode,
        **counts,
        "build_command": command,
    }
    _write_json(project / "export_result.json", result)
    if run_unity:
        try:
            await _run_unity(command, project, timeout_seconds)
            verifier = _verify_original if original else _verify
            report = verifier(project, render_preview, counts["mesh_count"])
        except asyncio.CancelledError:
            result.update(status="build_cancelled")
            _write_json(project / "export_result.json", result)
            raise
        except (RuntimeError, OSError) as error:
            result.update(status="build_failed", error=str(error))
            _write_json(project / "export_result.json", result)
            raise
        result.update(
            status="complete",
            scene=str(
                project
                / (
                    "Assets/RecoveredCaptureScene.unity"
                    if original
                    else "Assets/CaptureScene.unity"
                )
            ),
            report=str(
                project
                / ("recovered-result.json" if original else "result.json")
            ),
            unity_log=str(project / "unity.log"),
            warnings=list(
                dict.fromkeys(counts["warnings"] + report.get("warnings", []))
            ),
        )
        if render_preview:
            result["preview"] = str(
                project
                / ("recovered-preview.png" if original else "preview.png")
            )
        _write_json(project / "export_result.json", result)
    return result
