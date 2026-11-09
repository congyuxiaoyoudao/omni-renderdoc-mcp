"""Translate captured GPU programs to Unity ShaderLab and compute shaders."""

import json
import re
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

_STAGES = {"Vertex": ("vert", 1), "Pixel": ("frag", 0), "Compute": ("comp", 5)}
_SPIRV = b"\x03\x02\x23\x07"
_TEXTURES = re.compile(
    r"^\s*(?:uniform\s+)?(Texture\w+)\s*(?:<[^;\n]+?>)?\s+"
    r"(\w+)\s*(\[[^;\n]*?\])?\s*"
    r"(?::\s*register\s*\(\s*(t\d+)\s*\))?\s*;",
    re.MULTILINE,
)
_SAMPLERS = re.compile(
    r"^\s*(?:uniform\s+)?(Sampler(?:Comparison)?State)\s+(\w+)\s*"
    r"(?::\s*register\s*\(\s*(s\d+)\s*\))?\s*;",
    re.MULTILINE,
)
_SAMPLES = re.compile(
    r"\b(\w+)\s*\.\s*(?:Sample\w*|Gather\w*|CalculateLevelOfDetail\w*)"
    r"\s*\(\s*(\w+)\b"
)
_DIMENSIONS = {
    "Texture2D": "2D",
    "Texture2DArray": "2DArray",
    "Texture3D": "3D",
    "TextureCube": "Cube",
    "TextureCubeArray": "CubeArray",
}


def _tool(configured, default):
    binary = shutil.which(str(configured or default))
    if not binary:
        raise RuntimeError(f"Cannot find {configured or default} executable")
    return str(Path(binary).resolve())


def _run(argv, cwd, input_bytes=None):
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            input=input_bytes,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"{Path(argv[0]).name} timed out after 120s"
        ) from exc
    if result.returncode:
        log = result.stderr
        if input_bytes is None:
            log += b"\n" + result.stdout
        log = log.decode(errors="replace").strip()
        raise RuntimeError(f"{Path(argv[0]).name} failed: {log[-4000:]}")
    if input_bytes is not None:
        return result.stdout
    return result.stdout.decode("utf-8", errors="replace").replace(
        "\r\n", "\n"
    )


def _dxbc_stage(data):
    if len(data) < 32:
        raise ValueError("Truncated DXBC container header")
    size, count = struct.unpack_from("<II", data, 24)
    if size != len(data) or count > (size - 32) // 4:
        raise ValueError("Invalid DXBC container size or chunk table")
    for i in range(count):
        offset = struct.unpack_from("<I", data, 32 + 4 * i)[0]
        if offset + 8 > size:
            raise ValueError("Invalid DXBC chunk offset")
        tag = data[offset : offset + 4]
        length = struct.unpack_from("<I", data, offset + 4)[0]
        if offset + 8 + length > size:
            raise ValueError("Truncated DXBC chunk")
        if tag == b"DXIL":
            raise ValueError("DXIL translation is unsupported")
        if tag in (b"SHDR", b"SHEX") and length >= 8:
            return struct.unpack_from("<I", data, offset + 8)[0] >> 16
    raise ValueError("DXBC has no shader-model 4/5 executable chunk")


def _spirv(cross, path, stage, entry, semantics, directory):
    binary = _tool(cross, "spirv-cross")
    cmd = [binary, str(path), "--entry", entry, "--stage", _STAGES[stage][0]]
    reflection = json.loads(_run([*cmd, "--reflect"], directory))
    options = ["--hlsl", "--shader-model", "50", "--hlsl-auto-binding", "all"]
    for location, name in (semantics or {}).items():
        if not str(location).isdigit() or not re.fullmatch(
            r"[A-Za-z_]\w*", name
        ):
            raise ValueError("Invalid vertex input location or semantic")
        options.extend(
            ["--set-hlsl-vertex-input-semantic", str(location), name]
        )
    return _run(cmd + options, directory), reflection


def _compute_dxbc(data, directory, vkd3d, cross):
    spv = _run(
        [
            _tool(vkd3d, "vkd3d-compiler"),
            "-x",
            "dxbc-tpf",
            "-b",
            "spirv-binary",
            "--buffer-uav=storage-buffer",
        ],
        directory,
        input_bytes=data,
    )
    if not spv.startswith(_SPIRV):
        raise ValueError("vkd3d did not produce SPIR-V bytecode")
    path = directory / "Compute.spv"
    path.write_bytes(spv)
    return _spirv(cross, path, "Compute", "main", None, directory)


def _stage(
    path,
    stage,
    entry,
    directory,
    cross,
    decompiler,
    semantics=None,
    vkd3d=None,
):
    path = Path(path).resolve()
    data = path.read_bytes()
    reflection = {}
    toolchain = ["spirv-cross"]
    if data.startswith(_SPIRV):
        source, reflection = _spirv(
            cross, path, stage, entry, semantics, directory
        )
        encoding = "SPIR-V"
    elif data.startswith(b"DXBC"):
        if _dxbc_stage(data) != _STAGES[stage][1]:
            raise ValueError(
                f"DXBC executable does not contain a {stage} stage"
            )
        if stage == "Compute":
            source, reflection = _compute_dxbc(data, directory, vkd3d, cross)
            toolchain = ["vkd3d-compiler", "spirv-cross"]
        else:
            local = directory / f"{stage}.bin"
            local.write_bytes(data)
            _run(
                [
                    _tool(decompiler, "cmd_Decompiler"),
                    "-D",
                    "--validate",
                    local.name,
                ],
                directory,
            )
            source = local.with_suffix(".hlsl").read_text(encoding="utf-8-sig")
            if "Needs manual fix" in source:
                raise ValueError(
                    "DXBC decompiler reported unsupported instructions"
                )
            toolchain = ["cmd_Decompiler"]
        encoding = "DXBC"
    else:
        raise ValueError(f"Unsupported {stage} GPU bytecode encoding")
    if not re.search(r"\bmain\s*\(", source):
        raise ValueError(f"Translated {stage} program has no HLSL entry point")
    return source, {
        "stage": stage,
        "encoding": encoding,
        "entry_point": entry,
        "toolchain": toolchain,
        "reflection": reflection,
    }


def _texture_properties(source, record):
    stage = record["stage"]
    reflection = record["reflection"]
    images = {
        r["name"]: r
        for category in ("separate_images", "textures")
        for r in reflection.get(category, ())
    }
    textures = []
    renames = {}
    for match in _TEXTURES.finditer(source):
        kind, name, array, register = match.groups()
        dimension = None if array else _DIMENSIONS.get(kind)
        property_name = f"_Omni{stage}_{name}" if dimension else None
        if property_name:
            renames[name] = property_name
        image = images.get(name, {})
        textures.append(
            {
                "stage": stage,
                "name": name,
                "type": kind,
                "dimension": dimension,
                "property": property_name,
                "register": register,
                "set": image.get("set"),
                "binding": image.get("binding"),
            }
        )

    record["samplers"] = _sampler_bindings(source, renames, reflection)
    if renames:
        # 3DMigoto can use t0 both as a variable and as a register index.
        tokens = re.compile(r"\bregister\s*\([^)]*\)|\b[A-Za-z_]\w*\b")
        source = tokens.sub(lambda m: renames.get(m[0], m[0]), source)
    return source, textures


def _sampler_bindings(source, renames, reflection):
    """Associate samplers with the Unity textures supplying their state."""
    uses = {}
    for image, sampler in _SAMPLES.findall(source):
        if image in renames:
            uses.setdefault(sampler, set()).add(image)
    owners = {}

    def assign(sampler, visited):
        for image in sorted(uses[sampler]):
            if image in visited:
                continue
            visited.add(image)
            if image not in owners or assign(owners[image], visited):
                owners[image] = sampler
                return True
        return False

    for sampler in uses:
        if not assign(sampler, set()):
            raise ValueError(
                "Independent sampler states share too few Unity texture "
                f"properties to bind {sampler}"
            )
    bindings = {sampler: renames[image] for image, sampler in owners.items()}
    reflected = {s["name"]: s for s in reflection.get("separate_samplers", ())}
    details = []
    for kind, name, register in _SAMPLERS.findall(source):
        prop = bindings.get(name)
        if prop:
            renames[name] = "sampler" + prop
        details.append(
            {
                "name": name,
                "type": kind,
                "register": register or None,
                "set": reflected.get(name, {}).get("set"),
                "binding": reflected.get(name, {}).get("binding"),
                "sampled_textures": sorted(uses.get(name, ())),
                "unity_sampler": renames.get(name, name),
                "sampler_state_from_texture": prop,
            }
        )
    return details


def convert_shader_pair(
    vertex,
    fragment,
    destination,
    *,
    shader_name,
    vertex_entry="main",
    fragment_entry="main",
    spirv_cross=None,
    dxbc_converter=None,
    vertex_semantics=None,
    render_state=(),
):
    """Write a ShaderLab pass translated from both supplied stages."""
    if not re.fullmatch(r"[\w ./-]+", shader_name):
        raise ValueError("Invalid Unity shader name")
    if any(
        line not in {"Stencil {", "}"} and re.search(r"[\r\n{}#]", line)
        for line in render_state
    ):
        raise ValueError(
            "Pass state must contain individual ShaderLab commands"
        )
    textures, records, programs = [], [], []
    with tempfile.TemporaryDirectory(prefix="omni-shader-") as tmp:
        for path, stage, entry, semantics in (
            (vertex, "Vertex", vertex_entry, vertex_semantics),
            (fragment, "Pixel", fragment_entry, None),
        ):
            source, record = _stage(
                path,
                stage,
                entry,
                Path(tmp),
                spirv_cross,
                dxbc_converter,
                semantics,
            )
            source, properties = _texture_properties(source, record)
            textures.extend(properties)
            records.append(record)
            macro = "VERTEX" if stage == "Vertex" else "FRAGMENT"
            programs.extend(
                (
                    f"#if defined(SHADER_STAGE_{macro})",
                    f"#define main Omni{stage}",
                    source,
                    "#undef main",
                    "#endif",
                )
            )
    properties = [
        f'        {t["property"]} ("{t["stage"]} {t["name"]}", '
        f'{t["dimension"]}) = "" {{}}'
        for t in textures
        if t["property"]
    ]
    lines = [
        f'Shader "{shader_name}"',
        "{",
        "    Properties",
        "    {",
        *properties,
        "    }",
        "    SubShader",
        "    {",
        "        Pass",
        "        {",
        *(f"            {command}" for command in render_state),
        "            HLSLPROGRAM",
        "#pragma target 5.0",
        "#pragma vertex OmniVertex",
        "#pragma fragment OmniPixel",
        *programs,
        "            ENDHLSL",
        "        }",
        "    }",
        "}",
        "",
    ]
    Path(destination).write_text("\n".join(lines), encoding="utf-8")
    return {"file": str(destination), "stages": records, "textures": textures}


def convert_compute_shader(
    source,
    destination,
    *,
    entry_point="main",
    spirv_cross=None,
    dxbc_converter=None,
    vkd3d_compiler=None,
):
    """Write a Unity compute kernel using the captured thread-group size."""
    with tempfile.TemporaryDirectory(prefix="omni-shader-") as tmp:
        hlsl, record = _stage(
            source,
            "Compute",
            entry_point,
            Path(tmp),
            spirv_cross,
            dxbc_converter,
            vkd3d=vkd3d_compiler,
        )
    if not re.search(r"\[\s*numthreads\s*\(", hlsl, re.IGNORECASE):
        raise ValueError("Translated compute shader has no thread-group size")
    hlsl, textures = _texture_properties(hlsl, record)
    text = "#pragma kernel OmniCompute\n#define main OmniCompute\n" + hlsl
    Path(destination).write_text(text + "\n#undef main\n", encoding="utf-8")
    return {
        "file": str(destination),
        "stages": [record],
        "kernel": "OmniCompute",
        "textures": textures,
    }
