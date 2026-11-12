"""Conservative recognition of complete, single-expression GLSL fragments.

This is not a GLSL compiler. Anything outside the deliberately small grammar
returns None. It never extracts an attractive-looking subexpression from an
unsupported main function and labels that a reconstruction.
"""

import re

from .fragment_graph import (
    FragmentGraphError,
    _sampling,
    _uniform,
    validate_recipe,
)


class _Unsupported(Exception):
    pass


_NUMBER = r"(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?[fF]?"
_TOKEN = re.compile(_NUMBER + r"|[A-Za-z_]\w*|[()+*/,.\-]")


def _layout(text):
    result = {}
    for part in text.split(","):
        if part.strip() == "std140":
            continue
        match = re.fullmatch(r"\s*(set|binding|location)\s*=\s*(\d+)\s*", part)
        if not match:
            raise _Unsupported()
        result[match[1]] = int(match[2])
    return result


class _Parser:
    def __init__(self, expression, variables, textures, samplers, event):
        self.tokens = _TOKEN.findall(expression)
        if "".join(self.tokens) != re.sub(r"\s+", "", expression):
            raise _Unsupported()
        self.pos = 0
        self.nodes = {}
        self.variables = variables
        self.textures, self.samplers, self.event = textures, samplers, event

    def add(self, node, width):
        key = "n{}".format(len(self.nodes))
        self.nodes[key] = node
        return key, width

    def token(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else ""

    def take(self, expected=None):
        value = self.token()
        if not value or (expected is not None and value != expected):
            raise _Unsupported()
        self.pos += 1
        return value

    def expression(self, precedence=0):
        token = self.take()
        if token == "-":
            value = self.expression(3)
            result = self.add({"op": "negate", "inputs": [value[0]]}, value[1])
        elif token == "+":
            result = self.expression(3)
        elif token == "(":
            result = self.expression()
            self.take(")")
        elif re.fullmatch(_NUMBER, token):
            result = self.add(
                {"op": "const", "value": float(token.rstrip("fF"))}, 1
            )
        elif self.token() == "(":
            result = self.call(token)
        else:
            # Uniform block members are names, whereas a following component
            # swizzle belongs to the expression. Resolve longest exact symbol.
            name = token
            if self.token() == "." and self.pos + 1 < len(self.tokens):
                combined = name + "." + self.tokens[self.pos + 1]
                if combined in self.variables:
                    self.take(".")
                    self.take()
                    name = combined
            if name not in self.variables:
                raise _Unsupported()
            node, width = self.variables[name]
            result = self.add(dict(node), width)
        while self.token() == ".":
            self.take(".")
            channels = self.take()
            if not re.fullmatch(r"[xyzwrgba]{1,4}", channels):
                raise _Unsupported()
            if any(
                "xyzw".find(c) >= result[1] for c in channels if c in "xyzw"
            ) or any(
                "rgba".find(c) >= result[1] for c in channels if c in "rgba"
            ):
                raise _Unsupported()
            result = self.add(
                {"op": "swizzle", "inputs": [result[0]], "channels": channels},
                len(channels),
            )
        operators = {
            "+": (1, "add"),
            "-": (1, "sub"),
            "*": (2, "mul"),
            "/": (2, "div"),
        }
        while (
            self.token() in operators
            and operators[self.token()][0] > precedence
        ):
            priority, op = operators[self.take()]
            right = self.expression(priority)
            if result[1] != right[1] and min(result[1], right[1]) != 1:
                raise _Unsupported()
            result = self.add(
                {"op": op, "inputs": [result[0], right[0]]},
                max(result[1], right[1]),
            )
        return result

    def call(self, name):
        self.take("(")
        if name == "textureLod":
            texture = self.take()
            sampler = None
            if texture == "sampler2D":
                self.take("(")
                texture = self.take()
                self.take(",")
                sampler = self.take()
                self.take(")")
            if texture not in self.textures or sampler not in self.samplers:
                raise _Unsupported()
            self.take(",")
            uv = self.expression()
            self.take(",")
            lod = self.take()
            self.take(")")
            if not re.fullmatch(r"\d+(?:\.0+)?", lod) or uv[1] != 2:
                raise _Unsupported()
            sample = self.samplers[sampler]
            node = dict(self.textures[texture])
            node.update(
                op="texture", uv=uv[0], lod=int(float(lod)), sampler=sample
            )
            _sampling(self.event or {}, node)
            return self.add(node, 4)
        args = []
        if self.token() != ")":
            args.append(self.expression())
            while self.token() == ",":
                self.take(",")
                args.append(self.expression())
        self.take(")")
        if name in ("vec2", "vec3", "vec4"):
            width = int(name[-1])
            if len(args) == 1 and args[0][1] == 1:
                args *= width
            if sum(a[1] for a in args) != width:
                raise _Unsupported()
            return self.add(
                {"op": "vector", "inputs": [a[0] for a in args]}, width
            )
        signatures = {
            "mix": 3,
            "clamp": 3,
            "smoothstep": 3,
            "dot": 2,
            "cross": 2,
            "normalize": 1,
            "length": 1,
            "reflect": 2,
            "pow": 2,
            "min": 2,
            "max": 2,
            "abs": 1,
            "floor": 1,
            "ceil": 1,
            "fract": 1,
            "sqrt": 1,
            "sin": 1,
            "cos": 1,
            "exp": 1,
            "log": 1,
            "step": 2,
        }
        if name not in signatures or len(args) != signatures[name]:
            raise _Unsupported()
        width = 1 if name in ("dot", "length") else max(a[1] for a in args)
        return self.add({"op": name, "inputs": [a[0] for a in args]}, width)


def infer_recipe(source, shader_sha256, attributes=None, event=None):
    """Return a complete proven recipe, or None without guessing intent.

    `event` is needed to resolve named uniform paths. Automatic textureLod
    recognition resolves sampler layout bindings against the captured event;
    unsupported or missing sampler states reject recognition.
    Implicit texture() sampling is not automatically replaced with LOD zero.
    """
    if not isinstance(source, str) or len(source) > 1_000_000:
        return None
    return _infer_glsl(source, shader_sha256, attributes, event)


def infer_dxbc_recipe(disassembly, shader_sha256, event, module_reflection):
    """Recognize an entire DXBC pixel shader consisting of sample + return.

    Implicit LOD is equivalent to the single visible original mip only when
    the captured SRV exposes exactly one level. All operands are resolved
    through reflection and current binding slots, never texture/sampler names.
    """
    if not isinstance(disassembly, str) or len(disassembly) > 100_000:
        return None
    reflection = module_reflection or {}
    outputs = reflection.get("output_signature", [])
    if (
        len(outputs) != 1
        or outputs[0].get("register") != 0
        or outputs[0].get("components") != 4
        or outputs[0].get("system_value") != "ColorOutput"
    ):
        return None
    instructions = []
    declarations = []
    model_seen = False
    for raw in disassembly.splitlines():
        line = raw.strip()
        if (
            not line
            or line.startswith("//")
            or re.fullmatch(r"Shader hash [0-9a-fA-F-]+", line)
        ):
            continue
        if line == "ps_5_0":
            if model_seen:
                return None
            model_seen = True
            continue
        if line.startswith("dcl_"):
            declarations.append(line)
            continue
        match = re.fullmatch(r"\d+:\s*(.+)", line)
        if not match:
            return None
        instructions.append(match[1])
    if not model_seen or len(instructions) != 2 or instructions[-1] != "ret":
        return None
    sample = re.fullmatch(
        r"sample(?:_indexable\(texture2d\)\(float,float,float,float\))?\s+"
        r"o0\.xyzw,\s*v(\d+)\.([xyzw]{4}),\s*"
        r"([A-Za-z_]\w*)\.xyzw,\s*([A-Za-z_]\w*)",
        instructions[0],
    )
    if not sample:
        return None
    register, components, texture_name, sampler_name = sample.groups()
    register = int(register)
    if components[:2] != "xy":
        return None
    inputs = [
        s
        for s in reflection.get("input_signature", [])
        if s.get("register") == register
    ]
    if (
        len(inputs) != 1
        or inputs[0].get("components", 0) < 2
        or inputs[0].get("type") != "Float"
        or inputs[0].get("system_value") != "Undefined"
    ):
        return None
    allowed = (
        r"dcl_globalFlags refactoringAllowed",
        r"dcl_sampler [A-Za-z_]\w*(?: \(s\d+\))?, mode_default",
        r"dcl_resource_texture2d \(float,float,float,float\) "
        r"[A-Za-z_]\w*(?: \(t\d+\))?",
        r"dcl_input_ps linear v\d+\.[xyzw]+",
        r"dcl_output o0\.xyzw",
    )
    if any(
        not any(re.fullmatch(p, line) for p in allowed)
        for line in declarations
    ):
        return None

    def reflected(rows, operand, prefix):
        slot = re.fullmatch(prefix + r"(\d+)", operand)
        matches = [
            r
            for r in rows
            if r.get("name") == operand
            or (slot and r.get("binding") == int(slot[1]))
        ]
        return matches[0] if len(matches) == 1 else None

    texture_ref = reflected(
        reflection.get("read_only_resources", []), texture_name, "t"
    )
    sampler_ref = reflected(reflection.get("samplers", []), sampler_name, "s")
    if (
        not texture_ref
        or not sampler_ref
        or texture_ref.get("array_size", 1) != 1
        or sampler_ref.get("array_size", 1) != 1
    ):
        return None
    pixel = event.get("bindings", {}).get("Pixel", {})

    def bound(rows, ref):
        matches = [
            r
            for r in rows
            if r.get("binding") == ref.get("binding")
            and r.get("set_or_space", 0) == ref.get("set_or_space", 0)
            and r.get("array_element", 0) == 0
        ]
        return matches[0] if len(matches) == 1 else None

    texture = bound(pixel.get("read_only_resources", []), texture_ref)
    sampler = bound(pixel.get("samplers", []), sampler_ref)
    if (
        not texture
        or not sampler
        or texture.get("dimension") not in (2, "Texture2D")
        or texture.get("mip_count") != 1
        or texture.get("array_size", 1) != 1
        or texture.get("depth", 1) != 1
    ):
        return None
    if texture.get("view_swizzle", ["Red", "Green", "Blue", "Alpha"]) != [
        "Red",
        "Green",
        "Blue",
        "Alpha",
    ]:
        return None
    filters = {"FilterMode.Point": "nearest", "FilterMode.Linear": "linear"}
    address = {"AddressMode.Wrap": "repeat", "AddressMode.ClampEdge": "clamp"}
    filtering = filters.get(sampler.get("min_filter"))
    wrap = address.get(sampler.get("address_u"))
    if (
        not filtering
        or not wrap
        or sampler.get("min_filter") != sampler.get("mag_filter")
        or sampler.get("address_u") != sampler.get("address_v")
        or sampler.get("filter") != "FilterFunction.Normal"
    ):
        return None
    lod = texture.get("first_mip", 0)
    if not any(
        i.get("mip", 0) == lod and i.get("slice", 0) == 0
        for i in texture.get("images", [])
    ):
        return None
    result = {
        "shader_sha256": shader_sha256,
        "entry_point": event.get("shader_entry_point", "main"),
        "name": "Recognized DXBC sample " + shader_sha256[:12],
        "nodes": {
            "varying": {"op": "attribute", "location": register},
            "uv": {"op": "swizzle", "inputs": ["varying"], "channels": "xy"},
            "color": {
                "op": "texture",
                "set": texture_ref.get("set_or_space", 0),
                "binding": texture_ref["binding"],
                "uv": "uv",
                "lod": 0,
                "sampler": {
                    "set": sampler_ref.get("set_or_space", 0),
                    "binding": sampler_ref["binding"],
                },
                "require_single_mip": True,
            },
        },
        "outputs": {"color": "color"},
        "recognition": "complete DXBC sample/ret; "
        "one visible mip proves implicit LOD",
    }
    try:
        validate_recipe(result)
    except FragmentGraphError:
        return None
    return result


def _infer_glsl(source, shader_sha256, attributes, event):
    try:
        if event and event.get("bindings", {}).get("Pixel", {}).get(
            "specialization"
        ):
            return None
        clean = re.sub(r"/\*.*?\*/|//[^\n]*", "", source, flags=re.S)
        clean = re.sub(r"^\s*#version[^\n]*", "", clean, flags=re.M)
        entry = (event or {}).get("shader_entry_point", "main")
        main = re.search(
            r"\bvoid\s+" + re.escape(entry) + r"\s*\(\s*\)\s*\{([^{}]*)\}\s*$",
            clean,
        )
        if not main:
            return None
        statement = re.fullmatch(
            r"\s*([A-Za-z_]\w*)\s*=\s*(.+?)\s*;\s*", main[1], re.S
        )
        if not statement or ";" in statement[2]:
            return None
        prefix = clean[: main.start()]
        variables, textures = {}, {}
        samplers = {}
        outputs = []
        # Supported uniform blocks have primitive scalar/vector members only.
        block_pattern = (
            r"layout\s*\(([^)]*)\)\s*uniform\s+\w+\s*\{([^{}]*)\}\s*(\w+)\s*;"
        )
        for match in list(re.finditer(block_pattern, prefix)):
            layout = _layout(
                match[1].replace(", std140", "").replace("std140,", "")
            )
            if "binding" not in layout:
                raise _Unsupported()
            fields = re.findall(r"\b(float|vec[234])\s+(\w+)\s*;", match[2])
            remaining = re.sub(r"\b(float|vec[234])\s+(\w+)\s*;", "", match[2])
            if remaining.strip():
                raise _Unsupported()
            for typ, field in fields:
                node = {
                    "op": "uniform",
                    "set": layout.get("set", 0),
                    "binding": layout["binding"],
                    "path": [field],
                }
                width = 1 if typ == "float" else int(typ[-1])
                if len(_uniform(event or {}, node)) != width:
                    raise _Unsupported()
                variables[match[3] + "." + field] = node, width
        prefix = re.sub(block_pattern, "", prefix)
        declaration = (
            r"layout\s*\(([^)]*)\)\s*(in|out|uniform)\s+"
            r"(float|vec[234]|texture2D|sampler)\s+(\w+)\s*;"
        )
        for match in re.finditer(declaration, prefix):
            layout = _layout(match[1])
            direction, typ, name = match[2], match[3], match[4]
            if direction == "out":
                if layout.get("location") != 0 or typ != "vec4":
                    raise _Unsupported()
                outputs.append(name)
            elif direction == "in":
                if "location" not in layout or typ not in (
                    "float",
                    "vec2",
                    "vec3",
                    "vec4",
                ):
                    raise _Unsupported()
                variables[name] = (
                    {"op": "attribute", "location": layout["location"]},
                    1 if typ == "float" else int(typ[-1]),
                )
            elif typ == "texture2D":
                if "binding" not in layout:
                    raise _Unsupported()
                textures[name] = {
                    "set": layout.get("set", 0),
                    "binding": layout["binding"],
                }
            elif typ == "sampler":
                if "binding" not in layout:
                    raise _Unsupported()
                samplers[name] = {
                    "set": layout.get("set", 0),
                    "binding": layout["binding"],
                }
            else:
                raise _Unsupported()
        if re.sub(declaration, "", prefix).strip() or outputs != [
            statement[1]
        ]:
            return None
        parser = _Parser(statement[2], variables, textures, samplers, event)
        result = parser.expression()
        if parser.token() or result[1] != 4:
            return None
        recipe = {
            "shader_sha256": shader_sha256,
            "entry_point": entry,
            "name": "Recognized GLSL " + shader_sha256[:12],
            "nodes": parser.nodes,
            "outputs": {"color": result[0]},
            "recognition": "complete single-expression GLSL fragment",
        }
        validate_recipe(recipe)
        return recipe
    except (
        _Unsupported,
        FragmentGraphError,
        ValueError,
        KeyError,
        TypeError,
        OverflowError,
        RecursionError,
    ):
        return None
