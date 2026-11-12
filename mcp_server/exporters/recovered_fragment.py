"""Wrap an original SPIR-V fragment program for a captured-varying Unity draw.

The fragment equations and native constant-buffer layout come from SPIRV-Cross.
Only the vertex program and the final MRT routing are replaced. This is a
single captured draw, not an automatic reconstruction of a whole render graph.
"""

import gzip
import hashlib
import json
import re
import shutil
import struct
from pathlib import Path

from .unity_shader import _sampler_bindings, _spirv


def _texture_swizzles(source, reflection, views):
    """Apply native image-view channel selection after the original fetch."""
    if views is None:
        return source, []
    helpers, records = [], []
    identity = ["Red", "Green", "Blue", "Alpha"]
    for image in reflection.get("separate_images", []):
        view = next(
            (
                v
                for v in views
                if v.get("set_or_space", 0) == image["set"]
                and v["binding"] == image["binding"]
            ),
            None,
        )
        swizzle = view.get("view_swizzle", identity) if view else identity
        if swizzle == identity:
            continue
        declaration = re.search(
            r"\bTexture\w+\s*<\s*(float|uint|int)4\s*>\s*"
            + re.escape(image["name"])
            + r"\b",
            source,
        )
        if len(swizzle) != 4 or not declaration:
            raise ValueError("Unsupported image-view swizzle " + str(image))
        kind = declaration[1] + "4"
        channels = {
            "Red": "value.x",
            "Green": "value.y",
            "Blue": "value.z",
            "Alpha": "value.w",
            "Zero": "0",
            "One": "1",
        }
        if any(c not in channels for c in swizzle):
            raise ValueError("Unsupported captured swizzle " + str(swizzle))
        name = "CapturedSwizzle" + image["name"]
        helpers.append(
            f"{kind} {name}({kind} value) {{ return {kind}("
            + ",".join(channels[c] for c in swizzle)
            + "); }"
        )
        pattern = re.compile(
            r"\b"
            + re.escape(image["name"])
            + r"\.(Sample\w*|Load|Gather\w*)\("
        )
        replacements = []
        for match in pattern.finditer(source):
            if match[1].startswith(("Gather", "SampleCmp")):
                raise ValueError(
                    "Swizzled gather/comparison needs a sampling adapter"
                )
            balance, end = 1, match.end()
            while balance and end < len(source):
                balance += (source[end] == "(") - (source[end] == ")")
                end += 1
            if balance:
                raise ValueError("Unbalanced translated texture operation")
            replacements.append((match.start(), end))
        for start, end in reversed(replacements):
            source = (
                source[:start]
                + name
                + "("
                + source[start:end]
                + ")"
                + source[end:]
            )
        records.append(
            {
                "image": image["name"],
                "swizzle": swizzle,
                "implementation": "original fetch then view channel selection",
            }
        )
    return "\n".join(helpers) + "\n" + source, records


def _comparison_adapters(source, reflection, states):
    """Reproduce comparison filtering without Unity sampler defaults."""
    if states is None:
        return source, []
    reflected = {r["name"]: r for r in reflection.get("separate_samplers", [])}
    images = {r["name"]: r for r in reflection.get("separate_images", [])}
    pairs = set(
        re.findall(r"\b(\w+)\.SampleCmpLevelZero\(\s*(\w+)\s*,", source)
    )
    declarations, definitions, records = [], [], []
    comparisons = {
        "Never": "false",
        "AlwaysTrue": "true",
        "Less": "reference < depth",
        "LessEqual": "reference <= depth",
        "Greater": "reference > depth",
        "GreaterEqual": "reference >= depth",
        "Equal": "reference == depth",
        "NotEqual": "reference != depth",
    }
    for image, sampler in sorted(pairs):
        ref = reflected[sampler]
        state = next(
            (
                s
                for s in states
                if s.get("set_or_space", 0) == ref["set"]
                and s["binding"] == ref["binding"]
            ),
            None,
        )
        if state is None or images[image]["type"] != "texture2D":
            raise ValueError(
                "Unsupported captured comparison sampler " + sampler
            )
        compare = state["compare_function"].rsplit(".", 1)[-1]
        expression = comparisons[compare]
        name = "CapturedCompare" + image + sampler
        read = name + "Read"
        address_lines = []
        for axis, component in (("u", "x"), ("v", "y")):
            mode = state["address_" + axis].rsplit(".", 1)[-1]
            p, n = "p." + component, "size." + component
            if mode == "ClampBorder":
                address_lines.append(
                    f"if ({p} < 0 || {p} >= {n}) {{ "
                    f"float depth = {float(state['border_color'][0]):.9g}; "
                    f"return ({expression}) ? 1.0 : 0.0; }}"
                )
            elif mode == "Wrap":
                address_lines.append(f"{p} = (({p} % {n}) + {n}) % {n};")
            elif mode == "Mirror":
                address_lines.append(
                    f"{p} = (({p} % (2*{n})) + 2*{n}) % (2*{n}); "
                    f"{p} = {p} < {n} ? {p} : 2*{n}-1-{p};"
                )
            elif mode != "ClampEdge":
                raise ValueError("Unsupported comparison address mode " + mode)
            address_lines.append(f"{p} = clamp({p}, 0, {n}-1);")
        linear = state["min_filter"].endswith("Linear")
        if linear != state["mag_filter"].endswith("Linear"):
            raise ValueError("Mixed min/mag comparison filtering unsupported")
        if linear:
            samples = f"""float2 coordinate = uv * float2(size) - 0.5;
    int2 p = int2(floor(coordinate)) + offset;
    float2 f = frac(coordinate);
    float a = {read}(p, size, reference);
    float b = {read}(p+int2(1,0), size, reference);
    float c = {read}(p+int2(0,1), size, reference);
    float d = {read}(p+int2(1,1), size, reference);
    return lerp(lerp(a,b,f.x), lerp(c,d,f.x), f.y);"""
        else:
            samples = (
                f"return {read}(int2(floor(uv * float2(size))) "
                "+ offset, size, reference);"
            )
        declarations.extend(
            [
                f"float {name}(float2 uv, float reference);",
                f"float {name}(float2 uv, float reference, int2 offset);",
            ]
        )
        definitions.append(f"""
float {read}(int2 p, int2 size, float reference)
{{
    {"".join(address_lines)}
    float depth = {image}.Load(int3(p, 0)).x;
    return ({expression}) ? 1.0 : 0.0;
}}
float {name}(float2 uv, float reference, int2 offset)
{{
    uint width, height; {image}.GetDimensions(width, height);
    int2 size = int2(width, height);
    {samples}
}}
float {name}(float2 uv, float reference)
{{ return {name}(uv, reference, int2(0,0)); }}
""")
        source = re.sub(
            r"\b" + image + r"\.SampleCmpLevelZero\(\s*" + sampler + r"\s*,",
            name + "(",
            source,
        )
        records.append(
            {
                "image": image,
                "sampler": sampler,
                "comparison": compare,
                "filter": "linear_pcf" if linear else "point",
                "implementation": "native loads; captured compare/address",
            }
        )
    return "\n".join(declarations) + "\n" + source + "\n" + "\n".join(
        definitions
    ), records


def _fields(source, name):
    match = re.search(r"struct\s+" + name + r"\s*\{(.*?)\};", source, re.S)
    if not match:
        return []
    fields = []
    for line in match[1].splitlines():
        field = re.fullmatch(
            r"\s*((?:(?:nointerpolation|centroid|noperspective|sample|precise)\s+)*)"
            r"(\w+)\s+(\w+)\s*:\s*(\w+)\s*;\s*",
            line,
        )
        if field:
            fields.append(
                dict(
                    zip(
                        ("qualifier", "type", "name", "semantic"),
                        field.groups(),
                    )
                )
            )
        elif line.strip():
            raise ValueError("Unsupported captured signature: " + line.strip())
    return fields


def _resource_name(resource, prefix):
    return (
        "_Captured"
        + prefix
        + "_s{}_b{}".format(resource["set"], resource["binding"])
    )


def compile_fragment(
    spirv,
    destination,
    *,
    event_id,
    spirv_cross,
    entry="main",
    render_state=None,
    shader_name=None,
    output_mode="target0",
    sampler_states=None,
    render_target_count=None,
    unity_reversed_z=True,
    texture_views=None,
):
    """Write ShaderLab and return its exact captured binding contract."""
    spirv = Path(spirv)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    source, reflection = _spirv(
        Path(spirv_cross), spirv, "Pixel", entry, None, destination.parent
    )
    source, swizzle_adapters = _texture_swizzles(
        source, reflection, texture_views
    )
    source, comparison_adapters = _comparison_adapters(
        source, reflection, sampler_states
    )
    early_depth = bool(
        re.search(r"\[earlydepthstencil\]\s*\w+\s+main\s*\(", source)
    )
    if early_depth:
        source = re.sub(
            r"\[earlydepthstencil\]\s*(?=\w+\s+main\s*\()", "", source
        )
    struct_types = set(re.findall(r"\bstruct\s+(\w+)", source))
    cbuffer_renames = {}
    for block in re.finditer(r"\bcbuffer\s+(\w+)\s*\{(.*?)\};", source, re.S):
        if any(
            re.search(r"\b" + t + r"\s+\w+", block[2]) for t in struct_types
        ):
            cbuffer_renames[block[1]] = "UnityInstancing_Captured" + block[1]
    if cbuffer_renames:
        source = re.sub(
            r"\b[A-Za-z_]\w*\b",
            lambda m: cbuffer_renames.get(m[0], m[0]),
            source,
        )
    inputs = _fields(source, "SPIRV_Cross_Input")
    outputs = _fields(source, "SPIRV_Cross_Output")
    varyings = [f for f in inputs if f["semantic"].startswith("TEXCOORD")]
    other = [f for f in inputs if not f["semantic"].startswith("TEXCOORD")]
    supported_systems = {"SV_POSITION", "SV_ISFRONTFACE"}
    if any(f["semantic"].upper() not in supported_systems for f in other):
        raise ValueError("Unsupported fragment system input: " + str(other))
    varyings.sort(key=lambda f: int(f["semantic"][8:]))
    for slot, field in enumerate(varyings):
        field.update(slot=slot, location=int(field["semantic"][8:]))
        match = re.fullmatch(r"(float|int|uint)([1-4]?)", field["type"])
        if not match:
            raise ValueError("Unsupported varying type: " + field["type"])
        field.update(scalar_type=match[1], components=int(match[2] or 1))

    renames = {}
    textures = []
    dimension_names = {
        "texture2D": "2D",
        "texture3D": "3D",
        "textureCube": "Cube",
        "texture2DArray": "2DArray",
        "textureCubeArray": "CubeArray",
    }
    for resource in reflection.get("separate_images", []):
        name = _resource_name(resource, "Tex")
        renames[resource["name"]] = name
        dimension = dimension_names.get(resource["type"])
        if not dimension or resource.get("array"):
            raise ValueError("Unsupported captured texture: " + str(resource))
        textures.append(
            dict(
                resource,
                name=name,
                original_name=resource["name"],
                dimension=dimension,
            )
        )
    samplers = _sampler_bindings(source, renames, reflection)
    if sampler_states is not None:
        modes = {
            "Wrap": "Repeat",
            "ClampEdge": "Clamp",
            "Mirror": "Mirror",
            "MirrorOnce": "MirrorOnce",
        }
        for sampler in samplers:
            if sampler["type"] != "SamplerState":
                continue
            state = next(
                (
                    s
                    for s in sampler_states
                    if s.get("set_or_space", 0) == sampler["set"]
                    and s["binding"] == sampler["binding"]
                ),
                None,
            )
            if state is None:
                raise ValueError("Missing native sampler state")
            if (
                state.get("max_anisotropy", 0) > 1
                or state.get("mip_lod_bias", 0) != 0
                or state.get("min_lod", 0) != 0
                or state.get("max_lod", 1000) < 1000
                or state["min_filter"] != state["mag_filter"]
                or not state["min_filter"].endswith(("Linear", "Point"))
                or not state.get("filter", "Normal").endswith("Normal")
                or (
                    state["min_filter"].endswith("Point")
                    and state["mip_filter"].endswith("Linear")
                )
            ):
                raise ValueError(
                    "Sampler requires a native-state adapter: " + str(state)
                )
            # Unity's documented inline sampler names survive texture DCE.
            # A texture-owned sampler can become invalid when its owner image
            # is optimized away while another image still uses that sampler.
            filter_name = (
                "Trilinear"
                if state["mip_filter"].endswith("Linear")
                else (
                    "Point"
                    if state["min_filter"].endswith("Point")
                    else "Linear"
                )
            )
            addresses = [
                state["address_" + a].rsplit(".", 1)[-1]
                for a in ("u", "v", "w")
            ]
            if any(a not in modes for a in addresses):
                raise ValueError(
                    "Unsupported inline sampler addressing: " + str(state)
                )
            suffix = "_".join(
                modes[a] + axis for a, axis in zip(addresses, ("U", "V", "W"))
            )
            name = "sampler_Captured_s{}_b{}_{}_{}".format(
                sampler["set"], sampler["binding"], filter_name, suffix
            )
            renames[sampler["name"]] = name
            sampler["unity_sampler"] = name
            sampler["inline_state"] = state
    source = re.sub(
        r"\b[A-Za-z_]\w*\b", lambda m: renames.get(m[0], m[0]), source
    )
    source = re.sub(r"\bmain\s*\(", "CapturedOriginalFragment(", source)
    constant_buffers = []
    declared_cbuffers = set(re.findall(r"\bcbuffer\s+(\w+)", source))
    for resource in reflection.get("ubos", []):
        name = cbuffer_renames.get(resource["name"], resource["name"])
        if name not in declared_cbuffers:
            raise ValueError(
                "Cannot match original cbuffer " + resource["name"]
            )
        constant_buffers.append(
            dict(
                resource,
                name=name,
                original_name=resource["name"],
                byte_count=resource["block_size"],
            )
        )
    raw_buffers = []
    declared_raw = {
        name: kind
        for kind, name in re.findall(
            r"\b(RWByteAddressBuffer|ByteAddressBuffer)\s+(\w+)", source
        )
    }
    output_count = max(
        (
            int(f["semantic"][9:] or "0") + 1
            for f in outputs
            if f["semantic"].upper().startswith("SV_TARGET")
        ),
        default=0,
    )
    next_uav = max(output_count, render_target_count or 0)
    for resource in reflection.get("ssbos", []):
        original_name = resource["name"]
        candidates = [original_name, "_" + original_name.rsplit("_", 1)[-1]]
        name = next((n for n in candidates if n in declared_raw), None)
        if not name:
            raise ValueError(
                "Unsupported or unmapped storage buffer: " + str(resource)
            )
        writable = declared_raw[name] == "RWByteAddressBuffer"
        operations = set(
            re.findall(r"\b" + re.escape(name) + r"\.(\w+)\(", source)
        )
        reads = sorted(o for o in operations if o.startswith("Load"))
        writes = sorted(o for o in operations if o.startswith("Store"))
        atomics = sorted(o for o in operations if o.startswith("Interlocked"))
        entry = dict(
            resource,
            name=name,
            original_name=original_name,
            writable=writable,
            read_operations=reads + atomics,
            write_operations=writes + atomics,
        )
        if writable:
            if next_uav >= 8:
                raise ValueError("Captured pixel UAV exceeds D3D11 slot limit")
            entry["uav_register"] = next_uav
            source = re.sub(
                r"\bRWByteAddressBuffer\s+" + re.escape(name) + r"\s*;",
                f"RWByteAddressBuffer {name} : register(u{next_uav});",
                source,
            )
            next_uav += 1
        raw_buffers.append(entry)

    raster_fields = ["    float4 position : SV_POSITION;"]
    assignments = []
    for field in varyings:
        raster_fields.append(
            "    {qualifier}{type} {name} : {semantic};".format(**field)
        )
        bits = (
            "_CapturedVaryings[vertexID * _CapturedVaryingStride + {}]".format(
                field["slot"]
            )
        )
        conversion = {"float": "asfloat", "int": "asint", "uint": ""}[
            field["scalar_type"]
        ]
        value = (conversion + "(" + bits + ")") if conversion else bits
        value += "." + "xyzw"[: field["components"]]
        assignments.append("    result.{} = {};".format(field["name"], value))
    fragment_assignments = []
    for field in inputs:
        semantic = field["semantic"].upper()
        if semantic == "SV_POSITION":
            value = "input.position"
        elif semantic == "SV_ISFRONTFACE":
            value = "(_CapturedFrontFaceFlip > 0.5) ? !frontFace : frontFace"
        else:
            value = "input." + field["name"]
        fragment_assignments.append(
            "    original.{} = {};".format(field["name"], value)
        )
        if semantic == "SV_POSITION":
            fragment_assignments.append(
                (
                    "    original.{0}.xy = input.position.xy * "
                    "_CapturedScreenScale.xy + _CapturedScreenScale.zw;"
                ).format(field["name"])
            )
    target = next(
        (
            f
            for f in outputs
            if f["semantic"].upper() in ("SV_TARGET0", "SV_TARGET")
        ),
        None,
    )
    depth = next(
        (f for f in outputs if f["semantic"].upper().startswith("SV_DEPTH")),
        None,
    )
    if depth and output_mode != "all":
        raise ValueError(
            "Fragment depth output requires an explicit depth routing adapter"
        )
    if output_mode not in ("target0", "all"):
        raise ValueError("output_mode must be target0 or all")
    if outputs:
        fragment_return = (
            "    SPIRV_Cross_Output captured = "
            "CapturedOriginalFragment(original);\n"
        )
        fragment_return += (
            "    return "
            + (
                "captured." + target["name"] + " * _CapturedOutputScale"
                if target
                else "float4(0,0,0,0)"
            )
            + ";"
        )
    else:
        fragment_return = (
            "    CapturedOriginalFragment(original);\n"
            "    return float4(0,0,0,0);"
        )
    wrapper = """
StructuredBuffer<uint4> _CapturedVaryings;
StructuredBuffer<float4> _CapturedClipPositions;
uint _CapturedVaryingStride;
float _CapturedClipYSign;
float _CapturedFrontFaceFlip;
float4 _CapturedScreenScale;
float4 _CapturedOutputScale;
struct CapturedRaster
{
RASTER_FIELDS
};
CapturedRaster CapturedVertex(uint vertexID : SV_VertexID)
{
    CapturedRaster result;
    result.position = _CapturedClipPositions[vertexID];
    result.position.y *= _CapturedClipYSign;
ASSIGNMENTS
    return result;
}
float4 CapturedPixel(CapturedRaster input,
    bool frontFace : SV_IsFrontFace) : SV_Target0
{
    SPIRV_Cross_Input original = (SPIRV_Cross_Input)0;
FRAGMENT_ASSIGNMENTS
FRAGMENT_RETURN
}
"""
    wrapper = wrapper.replace("RASTER_FIELDS", "\n".join(raster_fields))
    wrapper = wrapper.replace(
        "FRAGMENT_ASSIGNMENTS", "\n".join(fragment_assignments)
    )
    wrapper = wrapper.replace("ASSIGNMENTS", "\n".join(assignments))
    wrapper = wrapper.replace("FRAGMENT_RETURN", fragment_return)
    if output_mode == "all" and outputs:
        wrapper = wrapper.replace(
            "float4 CapturedPixel(CapturedRaster input,\n"
            "    bool frontFace : SV_IsFrontFace) : SV_Target0",
            "SPIRV_Cross_Output CapturedPixel(CapturedRaster input,\n"
            "    bool frontFace : SV_IsFrontFace)",
        )
        wrapper = wrapper.replace(
            fragment_return, "    return CapturedOriginalFragment(original);"
        )
    if early_depth:
        wrapper = re.sub(
            r"(?m)^(\w+ CapturedPixel\()", r"[earlydepthstencil]\n\1", wrapper
        )
    properties = [
        '{} ("Captured set{} binding{}", {}) = "" {{}}'.format(
            t["name"], t["set"], t["binding"], t["dimension"]
        )
        for t in textures
    ]
    properties.extend(
        [
            '_CapturedClipYSign ("Capture clip Y sign", Float) = 1',
            '_CapturedFrontFaceFlip ("Capture front face flip", Float) = 0',
            '_CapturedScreenScale ("Pixel scale/offset", Vector) = (1,1,0,0)',
            '_CapturedOutputScale ("Output scale", Vector) = (1,1,1,1)',
        ]
    )
    state = render_state or [
        "Cull Off",
        "ZTest LEqual",
        "ZWrite On",
        "Blend One Zero",
    ]
    # ShaderLab reverses depth comparisons for reversed-Z graphics backends.
    # Captured post-VS depths already contain that backend's native values,
    # so translate the native test back to ShaderLab's abstract comparison.
    if unity_reversed_z:
        comparisons = {
            "Greater": "Less",
            "GEqual": "LEqual",
            "Less": "Greater",
            "LEqual": "GEqual",
        }
        state = [
            re.sub(
                r"^(\s*ZTest\s+)(Greater|GEqual|Less|LEqual)\s*$",
                lambda match: match[1] + comparisons[match[2]],
                line,
            )
            for line in state
        ]
    if any(
        not isinstance(line, str) or any(c in line for c in ('"', "\n", "#"))
        for line in state
    ):
        raise ValueError("Invalid explicit captured render state")
    shader_name = shader_name or "OmniRecovered/E" + str(event_id)
    if not re.fullmatch(r"[A-Za-z0-9_/.-]+", shader_name):
        raise ValueError("Invalid recovered shader name")
    shader = "\n".join(
        [
            'Shader "' + shader_name + '"',
            "{",
            "Properties {",
            *properties,
            "}",
            'SubShader { Tags { "RenderPipeline"="HDRenderPipeline" }',
            'Pass { Name "CapturedFragment"',
            'Tags { "LightMode"="ForwardOnly" }',
            *state,
            "HLSLPROGRAM",
            "#pragma target 5.0",
            "#pragma vertex CapturedVertex",
            "#pragma fragment CapturedPixel",
            source,
            wrapper,
            "ENDHLSL",
            "}",
            "}",
            "}",
        ]
    )
    destination.write_text(shader, encoding="utf-8")
    return {
        "event_id": event_id,
        "shader": shader_name,
        "source_sha256": hashlib.sha256(spirv.read_bytes()).hexdigest(),
        "shader_file": str(destination),
        "constant_buffers": constant_buffers,
        "textures": textures,
        "samplers": samplers,
        "raw_buffers": raw_buffers,
        "input_attributes": varyings,
        "varying_stride": len(varyings),
        "original_outputs": outputs,
        "render_state": state,
        "routing": output_mode,
        "comparison_adapters": comparison_adapters,
        "early_depth_stencil": early_depth,
        "unity_reversed_z": unity_reversed_z,
        "swizzle_adapters": swizzle_adapters,
    }


def pack_varyings(mesh, signature):
    """Return little-endian uint4 rows preserving integer varying bits."""
    attributes = {
        a.get("location"): a
        for a in mesh["attributes"]
        if a.get("system_value", "Undefined") == "Undefined"
    }
    count = len(mesh["positions"])
    native32_formats = {
        channels + "_" + kind
        for channels in ("R32", "R32G32", "R32G32B32", "R32G32B32A32")
        for kind in ("FLOAT", "SINT", "UINT")
    }
    native32 = {
        location: attr["raw"]
        for location, attr in attributes.items()
        if attr.get("format") in native32_formats and attr.get("raw")
    }
    packed = bytearray()
    for vertex in range(count):
        for field in signature:
            attr = attributes.get(field["location"])
            components = field["components"]
            raw = native32.get(field["location"])
            if raw is not None:
                if len(raw) != count:
                    raise ValueError("Captured varying row count mismatch")
                row = bytes.fromhex(raw[vertex])
                if len(row) < components * 4:
                    raise ValueError("Insufficient captured varying bytes")
                # Preserve all native float bits, including NaN payloads used
                # by UI shaders. JSON decoding intentionally omits nonfinite
                # values; passing through Python floats can also quiet NaNs.
                packed.extend(row[: components * 4])
                packed.extend(bytes((4 - components) * 4))
                continue
            if attr is None or not attr.get("decoded"):
                raise ValueError(
                    "Missing captured varying location "
                    + str(field["location"])
                )
            values = attr["values"][vertex]
            if len(values) < components:
                raise ValueError("Insufficient captured varying components")
            values = values[:components] + [0] * (4 - components)
            code = {"float": "f", "int": "i", "uint": "I"}[
                field["scalar_type"]
            ]
            packed.extend(struct.pack("<4" + code, *values))
    return bytes(packed)


def prepare_recovered_fragment_bundle(
    manifest_path,
    project_directory,
    event_ids,
    *,
    spirv_cross,
    asset_directory="Assets/RecoveredShaders",
    render_state_overrides=None,
    output_mode="target0",
    preview_width=None,
    preview_height=None,
    supplementary_buffers=None,
    allow_dynamic_textures=False,
):
    """Package real fragment programs, captured varyings and native bindings.

    Missing active resources fail explicitly. The runtime importer must render
    draws in event order and provide compatible depth/MRT attachments.
    """
    from .shader_state import shaderlab_state

    manifest_path = Path(manifest_path).resolve()
    project = Path(project_directory).resolve()
    source = manifest_path.parent
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    evidence_root = source / "evidence"
    evidence = json.loads(
        (evidence_root / "index.json").read_text(encoding="utf-8")
    )
    events = {e["event_id"]: e for e in evidence["events"]}
    modules = {m["id"]: m for m in evidence["modules"]}
    asset_root = project / asset_directory
    if not asset_root.resolve().is_relative_to(
        project
    ) or not asset_directory.startswith("Assets/"):
        raise ValueError("asset_directory must be inside project Assets")
    asset_root.mkdir(parents=True, exist_ok=True)
    mesh_records = []
    selected = set(event_ids)
    for name in manifest["meshes"]:
        with gzip.open(source / name, "rt", encoding="utf-8") as stream:
            mesh = json.load(stream)
        if mesh["event_id"] in selected:
            mesh_records.append(mesh)
    absent = selected - {m["event_id"] for m in mesh_records}
    if absent:
        raise ValueError(
            "Events absent from captured geometry: " + str(sorted(absent))
        )
    mesh_records.sort(key=lambda m: (m["event_id"], m.get("instance", 0)))
    copy_cache = {}
    supplemental = []
    supplemental_root = None
    if supplementary_buffers is not None:
        supplemental_path = Path(supplementary_buffers).resolve()
        supplemental_root = supplemental_path.parent
        supplemental = json.loads(supplemental_path.read_text())["records"]

    def save(relative, value):
        path = asset_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(value, bytes):
            path.write_bytes(value)
        else:
            path.write_text(
                json.dumps(value, allow_nan=False), encoding="utf-8"
            )
        return path.relative_to(project).as_posix()

    def resource_file(record, resource_root=None):
        name = record.get("file") if record else None
        if not name:
            raise ValueError(
                "Required captured bytes unavailable: " + str(record)
            )
        resource_root = resource_root or evidence_root
        path = (resource_root / name).resolve()
        if not path.is_relative_to(resource_root.resolve()):
            raise ValueError("Resource path escapes evidence bundle")
        if path not in copy_cache:
            destination = asset_root / "Data" / (path.stem + ".bytes")
            destination.parent.mkdir(exist_ok=True)
            shutil.copy2(path, destination)
            copy_cache[path] = destination.relative_to(project).as_posix()
        return copy_cache[path]

    def binding(rows, reference):
        found = [
            r
            for r in rows
            if r.get("set_or_space", 0) == reference["set"]
            and r.get("binding") == reference["binding"]
            and r.get("array_element", 0) == 0
        ]
        if not found:
            raise ValueError("Missing captured binding " + str(reference))
        return found[0]

    camera = manifest["camera"]
    source_width, source_height = camera["width"], camera["height"]
    width, height = (
        preview_width or source_width,
        preview_height or source_height,
    )
    draws = []
    compiled_events = {}
    for mesh in mesh_records:
        eid = mesh["event_id"]
        event = events[eid]
        if event["bindings"]["Pixel"].get("specialization"):
            raise ValueError(
                "Captured fragment specialization overrides need an adapter: E"
                + str(eid)
            )
        module = modules[event["stages"]["Pixel"]]
        state, state_notes = shaderlab_state(event["pipeline"])
        if any("Native depth bias" in note for note in state_notes):
            raise ValueError(
                "Active captured depth bias has no verified Unity adapter: E"
                + str(eid)
            )
        if render_state_overrides and eid in render_state_overrides:
            state = render_state_overrides[eid]
        shader_file = asset_root / "Shaders" / ("E" + str(eid) + ".shader")
        contract = compiled_events.get(eid)
        if contract is None:
            contract = compile_fragment(
                evidence_root / module["bytecode"],
                shader_file,
                event_id=eid,
                spirv_cross=spirv_cross,
                entry=module.get("entry_point", "main"),
                render_state=state,
                output_mode=output_mode,
                sampler_states=event["bindings"]["Pixel"]["samplers"],
                texture_views=event["bindings"]["Pixel"][
                    "read_only_resources"
                ],
                render_target_count=len(
                    event["pipeline"].get("render_targets", [])
                ),
            )
            compiled_events[eid] = contract
        stem = "E{}_I{}".format(eid, mesh.get("instance", 0))
        clips = mesh.get("clip_positions")
        if clips is None:
            clips = next(
                (
                    a["values"]
                    for a in mesh["attributes"]
                    if a.get("system_value") == "Position"
                ),
                None,
            )
        if clips is None or len(clips) != len(mesh["positions"]):
            raise ValueError("Exact clip positions unavailable: " + stem)
        triangles = []
        for face in mesh["faces"]:
            for i in range(1, len(face) - 1):
                triangles.extend((face[0], face[i], face[i + 1]))
        draw = {
            "name": stem,
            "event_id": eid,
            "instance": mesh.get("instance", 0),
            "shader": contract["shader"],
            "shader_asset": shader_file.relative_to(project).as_posix(),
            "mesh": save(
                "Meshes/" + stem + ".json",
                {
                    "name": stem,
                    "vertices": [
                        v for x, y, z in mesh["positions"] for v in (x, y, -z)
                    ],
                    "triangles": triangles,
                },
            ),
            "varyings": save(
                "Data/" + stem + "_varyings.bytes",
                pack_varyings(mesh, contract["input_attributes"]),
            ),
            "clip_positions": save(
                "Data/" + stem + "_clips.bytes",
                b"".join(struct.pack("<4f", *v) for v in clips),
            ),
            "varying_stride": contract["varying_stride"],
            "clip_y_sign": camera.get("clip_y_sign", 1),
            "screen_scale": [
                source_width / width,
                source_height / height,
                0,
                0,
            ],
            "output_scale": [1, 1, 1, 1],
            "front_face_flip": False,
            "constant_buffers": [],
            "raw_buffers": [],
            "textures": [],
            "source_sha256": contract["source_sha256"],
            "render_state_notes": state_notes,
            "original_render_state": event["pipeline"],
            "original_render_pass": event.get("render_pass"),
        }
        bindings = event["bindings"]["Pixel"]
        for cb in contract["constant_buffers"]:
            captured = binding(event["constants"]["Pixel"], cb)
            data = captured.get("raw_data")
            draw["constant_buffers"].append(
                {
                    "name": cb["name"],
                    "file": resource_file(data),
                    "byte_count": data["byte_length"],
                }
            )
        for raw in contract["raw_buffers"]:
            captured = binding(
                bindings["read_only_resources"]
                + bindings["read_write_resources"],
                raw,
            )
            data = captured.get("raw_data")
            raw_root = evidence_root
            if data is None:
                data = next(
                    (
                        r
                        for r in supplemental
                        if r["event_id"] == eid
                        and r["resource_id"] == captured["resource_id"]
                        and r.get("descriptor_offset")
                        == captured.get("descriptor_offset")
                    ),
                    None,
                )
                raw_root = supplemental_root
            readonly = not raw["write_operations"]
            write_only = not raw["read_operations"]
            if (
                captured.get("snapshot_timing") == "post_draw"
                and not readonly
                and not write_only
            ):
                raise ValueError(
                    "Read/write storage requires pre-draw bytes: " + str(raw)
                )
            draw["raw_buffers"].append(
                {
                    "name": raw["name"],
                    "file": resource_file(data, raw_root),
                    "byte_count": data["byte_length"],
                    "readonly_hlsl_verified": readonly,
                    "write_only_hlsl_verified": write_only,
                    "writable": raw["writable"],
                    "uav_register": raw.get("uav_register", -1),
                    "snapshot_timing": captured.get("snapshot_timing"),
                    "resource_id": captured["resource_id"],
                }
            )
        sampler_owners = {
            s["sampler_state_from_texture"]: s for s in contract["samplers"]
        }
        for texture in contract["textures"]:
            captured = binding(bindings["read_only_resources"], texture)
            dynamic_only = not captured.get("raw_subresources")
            overlap = any(
                "overlaps a writable output" in issue.get("reason", "")
                for issue in captured.get("issues", [])
            )
            if dynamic_only and not (allow_dynamic_textures and overlap):
                raise ValueError("No native texture data for " + str(texture))
            first_mip, first_slice = (
                captured.get("first_mip", 0),
                captured.get("first_slice", 0),
            )
            subresources = [
                dict(
                    s,
                    file=resource_file(s),
                    mip=s["mip"] - first_mip,
                    slice=s.get("slice", 0) - first_slice,
                )
                for s in captured["raw_subresources"]
            ]
            sampler = sampler_owners.get(texture["name"])
            sampler_state = (
                binding(bindings["samplers"], sampler) if sampler else {}
            )
            wrap_names = {
                "Wrap": "Repeat",
                "ClampEdge": "Clamp",
                "ClampBorder": "Clamp",
                "Mirror": "Mirror",
                "MirrorOnce": "MirrorOnce",
            }
            texture_record = {
                "name": texture["name"],
                "resource_id": captured["resource_id"],
                "dimension": captured["dimension"],
                "format": captured.get("view_format")
                or captured["resource_format"],
                "sRGB": "SRGB" in captured.get("view_format", "").upper(),
                "width": max(1, captured["width"] >> first_mip),
                "height": max(1, captured["height"] >> first_mip),
                "depth": max(1, captured["depth"] >> first_mip),
                "array_size": len({s["slice"] for s in subresources})
                or captured.get("array_size", 1),
                "mip_count": len({s["mip"] for s in subresources})
                or captured.get("mip_count", 1),
                "subresources": subresources,
                "filter": "Trilinear"
                if sampler_state.get("mip_filter", "").endswith("Linear")
                else (
                    "Point"
                    if sampler_state.get("min_filter", "").endswith("Point")
                    else "Bilinear"
                ),
                "aniso": max(1, int(sampler_state.get("max_anisotropy", 1))),
                "sampler_provenance": sampler_state,
                "view_swizzle": captured.get("view_swizzle", []),
                "first_mip": first_mip,
                "first_slice": first_slice,
                "requires_dynamic_binding": dynamic_only,
                "dynamic_binding_reason": "Graph must bind pre-draw contents"
                if dynamic_only
                else None,
            }
            for axis in ("u", "v", "w"):
                texture_record["wrap_" + axis] = wrap_names.get(
                    sampler_state.get("address_" + axis, "Wrap").rsplit(
                        ".", 1
                    )[-1],
                    "Repeat",
                )
            draw["textures"].append(texture_record)
        save("Contracts/" + stem + ".json", contract)
        draws.append(draw)
    result = {
        "version": 1,
        "source_width": source_width,
        "source_height": source_height,
        "preview_width": width,
        "preview_height": height,
        "draws": draws,
        "source_capture": evidence.get("source_capture"),
        "source_api": evidence.get("api"),
        "source_manifest": str(manifest_path),
        "fragment_equations": "original SPIR-V translated with SPIRV-Cross",
        "output_mode": output_mode,
    }
    target = save("draws.json", result)
    return {
        "draws_json": str(project / target),
        "draw_count": len(draws),
        "shader_count": len({d["shader"] for d in draws}),
    }
