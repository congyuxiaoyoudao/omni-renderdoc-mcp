"""Adapt native HDRP editor passes to Unity's actual GPU-picking matrices.

Only editor-pass program text changes. The original shared source, visible
passes, arithmetic, mesh deformation and alpha coverage are retained.
The editor entries and their shader-local helper closure are recompiled after
HDRP's PickingSpaceTransforms aliases; adding that include after an existing
HLSLINCLUDE vertex definition would not change its already-expanded transforms.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

_MARKER = "// Omni HDRP editor picking transform adapter v2"
_LEGACY_MARKER = "// Omni HDRP editor picking transform adapter v1"
_PICKING_INCLUDE = (
    "Packages/com.unity.render-pipelines.high-definition/Runtime/"
    "ShaderLibrary/PickingSpaceTransforms.hlsl"
)
_EDITOR_MODES = {
    "Picking": "SCENEPICKINGPASS",
    "SceneSelectionPass": "SCENESELECTIONPASS",
}


class PickingAdapterError(ValueError):
    """This shader needs explicit adaptation to preserve its semantics."""


@dataclass(frozen=True)
class _Function:
    name: str
    start: int
    body: int
    end: int
    text: str


def _mask(source: str) -> str:
    """Retain offsets/newlines while hiding comments and string literals."""
    pattern = r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"'
    return re.sub(pattern, lambda m: re.sub(r"[^\r\n]", " ", m[0]), source)


def _closing(mask: str, opening: int) -> int:
    depth = 1
    for index in range(opening + 1, len(mask)):
        if mask[index] == "{":
            depth += 1
        elif mask[index] == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    raise PickingAdapterError("Unbalanced shader braces")


def _functions(source: str) -> dict[str, list[_Function]]:
    mask = _mask(source)
    # HLSL function definitions with optional qualifiers/return semantics.
    pattern = re.compile(
        r"(?m)^[ \t]*(?:(?:inline|static|const|precise)\s+)*"
        r"[A-Za-z_]\w*(?:\s*<[^;{}>]+>)?\s+"
        r"(?P<name>[A-Za-z_]\w*)\s*\([^;{}]*?\)"
        r"\s*(?::[^;{}]+)?\s*(?P<brace>\{)"
    )
    result: dict[str, list[_Function]] = {}
    previous_end = 0
    for match in pattern.finditer(mask):
        if match.start() < previous_end:
            continue
        end = _closing(mask, match.start("brace"))
        previous_end = end
        record = _Function(
            match["name"],
            match.start(),
            match.start("brace"),
            end,
            source[match.start() : end],
        )
        result.setdefault(record.name, []).append(record)
    return result


def _rename_calls(source: str, names: dict[str, str]) -> str:
    mask = _mask(source)
    edits = []
    for match in re.finditer(r"\b([A-Za-z_]\w*)\s*(?=\()", mask):
        if match[1] in names:
            edits.append((match.start(1), match.end(1), names[match[1]]))
    for start, end, text in reversed(edits):
        source = source[:start] + text + source[end:]
    return source


def _conditional_context(mask: str, position: int) -> list[str]:
    """Reproduce enclosing source #if branches for a copied definition."""
    stack: list[list[str]] = []
    for match in re.finditer(
        r"(?m)^\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b[^\n]*",
        mask[:position],
    ):
        kind = match[1]
        directive = match[0].strip()
        if kind in {"if", "ifdef", "ifndef"}:
            stack.append([directive])
        elif kind in {"elif", "else"}:
            if not stack:
                raise PickingAdapterError("Unmatched preprocessor branch")
            stack[-1].append(directive)
        elif stack:
            stack.pop()
        else:
            raise PickingAdapterError("Unmatched preprocessor end")
    return [line for level in stack for line in level]


def _clone_entries(
    source: str, entries: list[str]
) -> tuple[str, dict[str, str]]:
    functions = _functions(source)
    mask = _mask(source)
    macros = {}
    pattern = (
        r"(?m)^[ \t]*#[ \t]*define[ \t]+(?P<name>[A-Za-z_]\w*)"
        r"(?P<params>\([^\r\n)]*\))?"
        r"(?P<body>(?:[^\r\n]*\\\r?\n)*[^\r\n]*)"
    )
    for match in re.finditer(pattern, mask):
        macros.setdefault(match["name"], []).append(match)
    requested = set(entries)
    selected_macros: set[str] = set()
    missing = requested - functions.keys()
    if missing:
        raise PickingAdapterError(
            "Editor entry definition unavailable: "
            + ", ".join(sorted(missing))
        )
    pending = [("function", name) for name in requested]
    while pending:
        kind, name = pending.pop()
        if kind == "function":
            texts = [_mask(item.text) for item in functions[name]]
        else:
            texts = []
            for macro in macros[name]:
                parameters = set(re.findall(r"\w+", macro["params"] or ""))
                text = macro["body"]
                for parameter in parameters:
                    text = re.sub(r"\b" + parameter + r"\b", " ", text)
                texts.append(text)
        for text in texts:
            references = set(re.findall(r"\b[A-Za-z_]\w*\b", text))
            for call in references & functions.keys() - requested:
                requested.add(call)
                pending.append(("function", call))
            for macro in references & macros.keys() - selected_macros:
                selected_macros.add(macro)
                pending.append(("macro", macro))
    names = {name: name + "_OmniEditorPicking" for name in requested}
    if any(name in functions for name in names.values()):
        raise PickingAdapterError(
            "Generated editor function name already exists"
        )

    def wrapped(text: str, position: int) -> str:
        context = _conditional_context(mask, position)
        opens = sum(
            bool(re.match(r"#\s*(?:if|ifdef|ifndef)\b", line))
            for line in context
        )
        prefix = "\n".join(context) + ("\n" if context else "")
        suffix = (
            "\n" + "\n".join("#endif" for _ in range(opens)) if opens else ""
        )
        return prefix + text + suffix

    records = sorted(
        (item for name in requested for item in functions[name]),
        key=lambda item: item.start,
    )
    prototypes, definitions, macro_aliases = [], [], []
    for item in records:
        declaration = item.text[: item.body - item.start].rstrip()
        prototypes.append(
            wrapped(_rename_calls(declaration, names) + ";", item.start)
        )
        definitions.append(
            wrapped(_rename_calls(item.text, names), item.start)
        )
    # A constant can be an object-like macro around a live view helper.
    # Redirect that helper too; otherwise a cloned caller could still enter the
    # old definition whose transforms were expanded before the editor aliases.
    for name in sorted(selected_macros):
        for macro in macros[name]:
            start, end = macro.span("body")
            body = source[start:end]
            parameters = set(re.findall(r"\w+", macro["params"] or ""))
            edits = []
            for token in re.finditer(r"\b[A-Za-z_]\w*\b", _mask(body)):
                if token[0] in names and token[0] not in parameters:
                    edits.append((token.start(), token.end(), names[token[0]]))
            for left, right, value in reversed(edits):
                body = body[:left] + value + body[right:]
            if edits:
                text = (
                    "#undef "
                    + name
                    + "\n"
                    + source[macro.start() : start]
                    + body
                )
                macro_aliases.append(wrapped(text, macro.start()))
    return "\n\n".join(prototypes + macro_aliases + definitions), names


def _editor_state(source: str) -> str:
    # Editor IDs need ordinary nearest-surface depth and unblended RGBA writes,
    # independent of a transparent/additive color pass's compositing state.
    factor = r"(?:[A-Za-z_]\w*|\[[^\]]+\])"
    patterns = {
        "ZWrite": r"\bZWrite\s+(?:On|Off|\[[^\]]+\])",
        "ZTest": r"\bZTest\s+(?:\w+|\[[^\]]+\])",
        "Blend": r"\bBlend(?:\s+[0-7])?\s+(?:Off|"
        + factor
        + r"\s+"
        + factor
        + r"(?:\s*,\s*"
        + factor
        + r"\s+"
        + factor
        + r")?)",
        "ColorMask": r"\bColorMask\s+(?:[RGBA]+|0|\[[^\]]+\])(?:\s+[0-7]\b)?",
    }
    expected = {
        "ZWrite": "On",
        "ZTest": "LEqual",
        "Blend": "Off",
        "ColorMask": "RGBA",
    }
    for name, pattern in patterns.items():
        matches = list(re.finditer(pattern, _mask(source), re.IGNORECASE))
        if not matches:
            source += "\n" + name + " " + expected[name] + "\n"
        else:
            for match in reversed(matches):
                source = (
                    source[: match.start()]
                    + name
                    + " "
                    + expected[name]
                    + source[match.end() :]
                )
    return source


def adapt_hdrp_editor_picking(shader: str) -> str:
    """Return shader with camera-correct Picking/SceneSelection programs.

    Supports a single shared HLSLINCLUDE and shader-local HLSL entry/helper
    definitions. Included HDRP SpaceTransforms are remapped by the official
    package. Custom deformation helpers must be defined in shared/program text;
    externally defined custom helpers require the caller to inline them first.
    Original function definitions and all non-editor passes stay byte-for-byte
    identical. Editor depth/blend/color-mask state uses unblended nearest IDs.
    Reapplying this function is a no-op.
    """
    mask = _mask(shader)
    includes = list(re.finditer(r"\bHLSLINCLUDE\b([\s\S]*?)\bENDHLSL\b", mask))
    if len(includes) > 1:
        raise PickingAdapterError(
            "Multiple scoped HLSLINCLUDE blocks need explicit scope resolution"
        )
    shared = (
        shader[includes[0].start(1) : includes[0].end(1)] if includes else ""
    )
    edits = []
    for match in re.finditer(r"\bPass\s*\{", mask):
        start = match.start()
        end = _closing(mask, mask.index("{", match.start()))
        block = shader[start:end]
        program = re.search(
            r"\bHLSLPROGRAM\b([\s\S]*?)\bENDHLSL\b", _mask(block)
        )
        if program is None:
            continue
        tags = re.search(
            r'"LightMode"\s*=\s*"([^"]+)"', block[: program.start()]
        )
        if tags is None or tags[1] not in _EDITOR_MODES:
            continue
        body = block[program.start(1) : program.end(1)]
        if _MARKER in body:
            continue
        if _LEGACY_MARKER in body:
            body = body[: body.index(_LEGACY_MARKER)]
            body = re.sub(
                r"(#\s*pragma\s+(?:vertex|fragment)\s+)(\w+)_OmniEditorPicking\b",
                r"\1\2",
                body,
            )
        pragmas = list(
            re.finditer(
                r"(?m)^\s*#\s*pragma\s+(vertex|fragment)\s+(\w+)\b",
                _mask(body),
            )
        )
        if {p[1] for p in pragmas} != {"vertex", "fragment"} or len(
            pragmas
        ) != 2:
            raise PickingAdapterError(
                "An editor pass needs one vertex and fragment entry"
            )
        if re.search(r"#\s*pragma\s+(?:hull|domain|geometry)\b", _mask(body)):
            raise PickingAdapterError(
                "Tessellation/geometry editor stages need an explicit adapter"
            )
        clones, names = _clone_entries(
            shared + "\n" + body, [p[2] for p in pragmas]
        )
        for pragma in reversed(pragmas):
            body = (
                body[: pragma.start(2)]
                + names[pragma[2]]
                + body[pragma.end(2) :]
            )
        # The aliases must precede the copied definitions, not just the pragma.
        mode = _EDITOR_MODES[tags[1]]
        body += (
            "\n" + _MARKER + "\n#define " + mode + " 1\n"
            '#include "' + _PICKING_INCLUDE + '"\n'
            "#pragma editor_sync_compilation\n" + clones + "\n"
        )
        prefix = _editor_state(block[: program.start()])
        replacement = prefix + "HLSLPROGRAM" + body
        edits.append((start, start + program.end(1), replacement))
    for start, end, text in reversed(edits):
        shader = shader[:start] + text + shader[end:]
    return shader


def adapt_project_native_picking(project, shader_assets=()) -> list[str]:
    """Adapt explicitly staged support shaders and native material shaders.

    Only caller-listed shader assets and ``explicit_shader.asset`` references
    in the exported native material library are eligible. Arbitrary project
    shaders and package sources are never scanned. Validate the entire batch
    before writing so an unsupported editor program leaves every asset intact.
    """
    project = Path(project).resolve()
    assets = project / "Assets"

    def asset_path(relative):
        path = (project / relative).resolve()
        if not path.is_relative_to(assets) or not path.is_file():
            raise PickingAdapterError(
                "Native picking requires an existing project Assets file: "
                + str(relative)
            )
        return path

    paths = {asset_path(relative) for relative in shader_assets}
    manifest_path = assets / "NativeCapture/manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        library = manifest.get("material_library")
        if library:
            material_path = asset_path(library)
            materials = json.loads(material_path.read_text(encoding="utf-8"))
            for entry in materials.get("entries", []):
                descriptor = entry.get("spec", {}).get("explicit_shader")
                if descriptor and descriptor.get("asset"):
                    paths.add(asset_path(descriptor["asset"]))

    edits = []
    for path in sorted(paths):
        relative = path.relative_to(project).as_posix()
        if path.suffix.lower() != ".shader":
            raise PickingAdapterError(
                "Native picking shader asset needs a .shader extension: "
                + relative
            )
        # Decode bytes directly to preserve CRLF, BOM and all visible programs.
        source = path.read_bytes().decode("utf-8")
        try:
            result = adapt_hdrp_editor_picking(source)
        except PickingAdapterError as error:
            raise PickingAdapterError(f"{relative}: {error}") from error
        if result != source:
            edits.append((path, result.encode("utf-8")))
    for path, content in edits:
        path.write_bytes(content)
    return [path.relative_to(project).as_posix() for path, _ in edits]
