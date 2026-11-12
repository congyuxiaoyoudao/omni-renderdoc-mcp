"""Capture-independent, deliberately bounded fragment-expression graphs.

Recipes are data, never Python. Shader SHA256 selects a recipe; every uniform,
resource and varying is resolved from the *current draw*. No capture IDs,
texture names, engine material families or final-frame projection are assumed.
"""

import hashlib
import json
import math
import re
from pathlib import Path


class FragmentGraphError(ValueError):
    """The expression cannot be represented faithfully by this backend."""


MAX_NODES = 2048
MAX_DEPTH = 128
ARITY = {
    "add": 2,
    "sub": 2,
    "mul": 2,
    "div": 2,
    "pow": 2,
    "min": 2,
    "max": 2,
    "dot": 2,
    "cross": 2,
    "lt": 2,
    "gt": 2,
    "eq": 2,
    "step": 2,
    "abs": 1,
    "floor": 1,
    "ceil": 1,
    "fract": 1,
    "sqrt": 1,
    "sin": 1,
    "cos": 1,
    "exp": 1,
    "log": 1,
    "negate": 1,
    "normalize": 1,
    "length": 1,
    "saturate": 1,
    "mix": 3,
    "clamp": 3,
    "smoothstep": 3,
    "select": 3,
    "reflect": 2,
}
OPS = set(ARITY) | {
    "const",
    "uniform",
    "attribute",
    "texture",
    "vector",
    "swizzle",
}
_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _fail(message):
    raise FragmentGraphError(message)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _value(value):
    result = value if isinstance(value, list) else [value]
    if not 1 <= len(result) <= 4 or not all(_number(v) for v in result):
        _fail("Values must have one to four finite numeric components")
    return tuple(result)


def _slot(node):
    for key in ("set", "binding"):
        if type(node.get(key)) is not int or node[key] < 0:
            _fail("Resource set and binding must be nonnegative integers")
    if (
        type(node.get("array_element", 0)) is not int
        or node.get("array_element", 0) < 0
    ):
        _fail("Resource array_element must be a nonnegative integer")


def _dependencies(node):
    return [node["uv"]] if node["op"] == "texture" else node.get("inputs", [])


def validate_recipe(recipe):
    """Validate all nodes, including unreachable ones; return their order."""
    if not isinstance(recipe, dict) or not _HASH.fullmatch(
        str(recipe.get("shader_sha256", ""))
    ):
        _fail("Recipe must identify a lowercase shader_sha256")
    if not isinstance(
        recipe.get("entry_point", "main"), str
    ) or not _ID.fullmatch(recipe.get("entry_point", "main")):
        _fail("Recipe entry_point must be a simple identifier")
    nodes = recipe.get("nodes")
    outputs = recipe.get("outputs")
    if not isinstance(nodes, dict) or not 1 <= len(nodes) <= MAX_NODES:
        _fail("Recipe node count must be between 1 and {}".format(MAX_NODES))
    if (
        not isinstance(outputs, dict)
        or set(outputs) - {"color", "alpha"}
        or "color" not in outputs
    ):
        _fail("Recipe outputs require color and optionally alpha")
    for key, node in nodes.items():
        if (
            not isinstance(key, str)
            or not _ID.fullmatch(key)
            or not isinstance(node, dict)
        ):
            _fail("Invalid recipe node identifier or object")
        op = node.get("op")
        if not isinstance(op, str) or op not in OPS:
            _fail("Unsupported fragment operation: {}".format(op))
        inputs = node.get("inputs", [])
        if not isinstance(inputs, list) or any(
            not isinstance(v, str) for v in inputs
        ):
            _fail("Node inputs must be an array of node identifiers")
        if op in ARITY and len(inputs) != ARITY[op]:
            _fail("{} requires {} inputs".format(op, ARITY[op]))
        if op == "const":
            _value(node.get("value"))
        if op in ("const", "uniform", "attribute", "texture") and inputs:
            _fail("Leaf nodes cannot declare arithmetic inputs")
        if op in ("uniform", "texture"):
            _slot(node)
        if op == "uniform":
            path = node.get("path")
            if (
                not isinstance(path, list)
                or not 1 <= len(path) <= 32
                or any(
                    not (isinstance(p, str) and _ID.fullmatch(p))
                    and not (type(p) is int and 0 <= p < 65536)
                    for p in path
                )
            ):
                _fail("Uniform path must contain bounded member names/indices")
        if op == "attribute":
            if ("name" in node) == ("location" in node):
                _fail("Attribute requires exactly one name or location")
            if "name" in node and (
                not isinstance(node["name"], str) or not node["name"]
            ):
                _fail("Attribute name must be nonempty")
            if "location" in node and (
                type(node["location"]) is not int or node["location"] < 0
            ):
                _fail("Attribute location must be a nonnegative integer")
        if op == "texture":
            if not isinstance(node.get("uv"), str):
                _fail("Texture requires a UV expression")
            if type(node.get("lod")) is not int or not 0 <= node["lod"] <= 31:
                _fail("Texture requires an explicit integer lod in [0,31]")
            sampler = node.get("sampler")
            if sampler is not None:
                if not isinstance(sampler, dict):
                    _fail("Sampler must be a binding reference")
                _slot(sampler)
            if "filter" in node or "wrap" in node or sampler is None:
                if node.get("filter") not in ("linear", "nearest") or node.get(
                    "wrap"
                ) not in ("repeat", "clamp"):
                    _fail("Texture needs a sampler reference or filter/wrap")
            if type(node.get("require_single_mip", False)) is not bool:
                _fail("require_single_mip must be boolean")
        if op == "vector" and not 1 <= len(inputs) <= 4:
            _fail("Vector requires one to four inputs")
        if op == "swizzle" and (
            len(inputs) != 1
            or not re.fullmatch(
                r"[xyzwrgba]{1,4}", str(node.get("channels", ""))
            )
        ):
            _fail("Swizzle requires one input and one to four channels")
        for dependency in _dependencies(node):
            if dependency not in nodes:
                _fail("Unknown node reference: " + dependency)
    for ref in outputs.values():
        if not isinstance(ref, str) or ref not in nodes:
            _fail("Unknown output node reference")
    visited, active, order, heights = set(), set(), [], {}

    def visit(key, depth):
        if depth > MAX_DEPTH:
            _fail("Fragment graph exceeds maximum dependency depth")
        if key in active:
            _fail("Cyclic fragment expression: " + key)
        if key in visited:
            return
        active.add(key)
        for dep in _dependencies(nodes[key]):
            visit(dep, depth + 1)
        heights[key] = 1 + max(
            (heights[d] for d in _dependencies(nodes[key])), default=0
        )
        if heights[key] > MAX_DEPTH:
            _fail("Fragment graph exceeds maximum dependency depth")
        active.remove(key)
        visited.add(key)
        order.append(key)

    for key in nodes:
        visit(key, 0)
    return order


def validate_registry(registry):
    if (
        not isinstance(registry, dict)
        or registry.get("version") != 1
        or type(registry.get("version")) is not int
    ):
        _fail("Fragment recipe registry requires version 1")
    materials = registry.get("materials")
    if not isinstance(materials, list) or len(materials) > 4096:
        _fail("Fragment registry requires a bounded materials array")
    result = {}
    for recipe in materials:
        validate_recipe(recipe)
        key = recipe["shader_sha256"]
        if key in result:
            _fail("Duplicate shader recipe: " + key)
        result[key] = recipe
    return result


def _match_slot(row, node):
    return (
        row.get("set_or_space", row.get("set")),
        row.get("binding"),
        row.get("array_element", 0),
    ) == (node["set"], node["binding"], node.get("array_element", 0))


def _uniform(event, node):
    constants = event.get("constants", {})
    blocks = (
        constants.get("Pixel", [])
        if isinstance(constants, dict)
        else constants
    )
    matches = [b for b in blocks if _match_slot(b, node)]
    if len(matches) != 1:
        _fail(
            "Uniform binding is missing or ambiguous: {}:{}".format(
                node["set"], node["binding"]
            )
        )
    values = matches[0].get("variables", [])
    current = None
    for step, part in enumerate(node["path"]):
        if isinstance(part, str):
            found = [
                v
                for v in values
                if isinstance(v, dict) and v.get("name") == part
            ]
            if len(found) != 1:
                _fail("Missing uniform member: " + part)
            current = found[0]
        else:
            if not 0 <= part < len(values):
                _fail("Uniform member index outside captured array")
            current = values[part]
        if not isinstance(current, dict):
            if step != len(node["path"]) - 1:
                _fail("Uniform path continues past scalar value")
            return _value(current)
        values = current.get("members") or current.get("value", [])
    if not isinstance(current, dict):
        return _value(current)
    if current.get("members"):
        _fail("Uniform path resolves to an aggregate; select its member")
    return _value(current.get("value"))


def _attribute(attributes, node):
    key = "name" if "name" in node else "location"
    matches = [a for a in attributes if a.get(key) == node[key]]
    if len(matches) != 1:
        _fail("Varying is missing or ambiguous: {}={}".format(key, node[key]))
    result = matches[0]
    count = result.get("components")
    if count is None and result.get("values"):
        count = len(result["values"][0])
    if type(count) is not int or not 1 <= count <= 4:
        _fail("Varying must have one to four decoded components")
    return result, count


def _texture(event, node, directory):
    rows = (
        event.get("bindings", {})
        .get("Pixel", {})
        .get("read_only_resources", [])
    )
    matches = [row for row in rows if _match_slot(row, node)]
    if len(matches) != 1:
        _fail("Texture binding is missing or ambiguous")
    texture = matches[0]
    if node.get("require_single_mip") and texture.get("mip_count") != 1:
        _fail("Implicit sample recipe requires one visible mip in this draw")
    dimension = texture.get("dimension")
    if (
        dimension not in (2, "Texture2D", "2D")
        or texture.get("array_size", 1) != 1
        or texture.get("depth", 1) != 1
    ):
        _fail("Only non-array 2D textures are supported by this backend")
    mip = texture.get("first_mip", 0) + node["lod"]
    images = [
        im
        for im in texture.get("images", [])
        if im.get("mip", 0) == mip
        and im.get("slice", 0) == 0
        and im.get("sample", 0) == 0
    ]
    if len(images) != 1:
        _fail("Requested original texture mip is unavailable or ambiguous")
    base = Path(directory).resolve()
    path = (base / images[0].get("file", "")).resolve()
    if base not in path.parents or not path.is_file():
        _fail("Texture image path must stay inside shader evidence")
    return texture, path


def _sampling(event, node):
    if "filter" in node and "wrap" in node:
        return node["filter"], node["wrap"]
    ref = node["sampler"]
    rows = event.get("bindings", {}).get("Pixel", {}).get("samplers", [])
    candidates = [
        s
        for s in rows
        if _match_slot(dict(s, set_or_space=s.get("set_or_space", 0)), ref)
    ]
    if len(candidates) != 1:
        _fail("Captured sampler binding is missing or ambiguous")
    sampler = candidates[0]
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
        _fail("Captured sampler filtering/addressing is unsupported")
    if not node.get("require_single_mip") and (
        sampler.get("mip_lod_bias", 0) != 0
        or sampler.get("min_lod", 0) > node["lod"]
        or sampler.get("max_lod", 1000) < node["lod"]
    ):
        _fail("Sampler bias or LOD clamp changes the requested explicit mip")
    return filtering, wrap


def _view_swizzle(value, descriptor):
    swizzle = descriptor.get("view_swizzle", ["Red", "Green", "Blue", "Alpha"])
    mapping = dict(zip(("Red", "Green", "Blue", "Alpha"), value))
    mapping.update(Zero=0, One=1)
    if (
        not isinstance(swizzle, list)
        or len(swizzle) != 4
        or any(v not in mapping for v in swizzle)
    ):
        _fail("Unsupported captured texture-view swizzle")
    return tuple(mapping[v] for v in swizzle)


def _broadcast(values):
    width = max(map(len, values))
    if any(len(v) not in (1, width) for v in values):
        _fail("Incompatible scalar/vector widths")
    return [v * width if len(v) == 1 else v for v in values]


def _apply(op, values, scalar):
    """Shared typed lowering used by both real nodes and numerical tests."""
    if op == "vector":
        result = sum(values, ())
        if not 1 <= len(result) <= 4:
            _fail("Composed vector exceeds four components")
        return result
    if op in ("dot", "cross", "reflect"):
        if len(values[0]) != len(values[1]):
            _fail("{} requires equal vector widths".format(op))
        a, b = values
        if op == "cross":
            if len(a) != 3:
                _fail("Cross product requires vec3")
            return tuple(
                scalar(
                    "sub", scalar("mul", a[i], b[j]), scalar("mul", a[j], b[i])
                )
                for i, j in ((1, 2), (2, 0), (0, 1))
            )
        dot = 0
        for x, y in zip(a, b):
            dot = scalar("add", dot, scalar("mul", x, y))
        if op == "dot":
            return (dot,)
        return tuple(
            scalar("sub", x, scalar("mul", scalar("mul", 2, dot), y))
            for x, y in zip(a, b)
        )
    if op in ("length", "normalize"):
        square = 0
        for x in values[0]:
            square = scalar("add", square, scalar("mul", x, x))
        length = scalar("sqrt", square)
        return (
            (length,)
            if op == "length"
            else tuple(scalar("div", x, length) for x in values[0])
        )
    return tuple(scalar(op, *parts) for parts in zip(*_broadcast(values)))


def _numeric(op, *v):
    a = v[0]
    b = v[1] if len(v) > 1 else 0
    if op == "add":
        return a + b
    if op == "sub":
        return a - b
    if op == "mul":
        return a * b
    if op == "div":
        return a / b if b else 0
    if op == "pow":
        return a**b
    if op == "min":
        return min(a, b)
    if op == "max":
        return max(a, b)
    if op == "lt":
        return float(a < b)
    if op == "gt":
        return float(a > b)
    if op == "eq":
        return float(a == b)
    if op == "step":
        return float(b >= a)
    if op == "mix":
        return a + (b - a) * v[2]
    if op == "select":
        return b if a != 0 else v[2]
    if op == "clamp":
        return min(max(a, b), v[2])
    if op == "smoothstep":
        t = min(max((v[2] - a) / (b - a), 0), 1) if a != b else 0
        return t * t * (3 - 2 * t)
    if op == "saturate":
        return min(max(a, 0), 1)
    return {
        "abs": abs,
        "floor": math.floor,
        "ceil": math.ceil,
        "fract": lambda x: x - math.floor(x),
        "sqrt": lambda x: math.sqrt(max(x, 0)),
        "sin": math.sin,
        "cos": math.cos,
        "exp": math.exp,
        "log": math.log,
        "negate": lambda x: -x,
    }[op](a)


def evaluate_recipe(
    recipe, event=None, attribute_values=None, texture_sampler=None
):
    """Evaluate the same lowering for exact math tests without Blender."""
    order = validate_recipe(recipe)
    values = {}
    for key in order:
        node = recipe["nodes"][key]
        op = node["op"]
        if op == "const":
            result = _value(node["value"])
        elif op == "uniform":
            result = _uniform(event or {}, node)
        elif op == "attribute":
            result = _value(
                (attribute_values or {})[
                    node.get("name", node.get("location"))
                ]
            )
        elif op == "texture":
            if texture_sampler is None:
                _fail("Numerical texture sampler missing")
            result = _value(texture_sampler(node, values[node["uv"]]))
        elif op == "swizzle":
            result = _swizzle(values[node["inputs"][0]], node["channels"])
        else:
            result = _apply(op, [values[r] for r in node["inputs"]], _numeric)
        values[key] = result
    return _outputs(recipe, values)


def _swizzle(value, channels):
    indices = [
        {"r": 0, "g": 1, "b": 2, "a": 3}.get(c, "xyzw".find(c))
        for c in channels
    ]
    if any(i >= len(value) for i in indices):
        _fail("Swizzle reads outside source vector")
    return tuple(value[i] for i in indices)


def _outputs(recipe, values):
    color = values[recipe["outputs"]["color"]]
    if len(color) not in (3, 4):
        _fail("Fragment color output must be vec3 or vec4")
    alpha = (
        values[recipe["outputs"]["alpha"]]
        if "alpha" in recipe["outputs"]
        else (color[3] if len(color) == 4 else 1,)
    )
    if len(alpha) != 1:
        _fail("Fragment alpha output must be scalar")
    return color[:3], alpha[0]


class _Nodes:
    def __init__(self, bpy, tree):
        self.bpy, self.tree = bpy, tree
        self.current = ""

    def node(self, kind, label):
        n = self.tree.nodes.new(kind)
        n.label = label
        n.name = self.current + " / " + label
        return n

    def connect(self, socket, value):
        if hasattr(value, "is_output"):
            self.tree.links.new(value, socket)
        else:
            socket.default_value = value

    def scalar(self, op, *v):
        if all(_number(x) for x in v):
            try:
                return _numeric(op, *v)
            except (ValueError, OverflowError, ZeroDivisionError):
                _fail("Invalid constant expression: " + op)
        if op == "mix":
            return self.scalar(
                "add",
                v[0],
                self.scalar("mul", self.scalar("sub", v[1], v[0]), v[2]),
            )
        if op == "select":
            cond = self.scalar("gt", self.scalar("abs", v[0]), 0)
            return self.scalar("mix", v[2], v[1], cond)
        if op == "clamp":
            return self.scalar("min", self.scalar("max", v[0], v[1]), v[2])
        if op == "saturate":
            return self.scalar("clamp", v[0], 0, 1)
        if op == "smoothstep":
            t = self.scalar(
                "saturate",
                self.scalar(
                    "div",
                    self.scalar("sub", v[2], v[0]),
                    self.scalar("sub", v[1], v[0]),
                ),
            )
            return self.scalar(
                "mul",
                self.scalar("mul", t, t),
                self.scalar("sub", 3, self.scalar("mul", 2, t)),
            )
        if op == "step":
            return self.scalar("sub", 1, self.scalar("lt", v[1], v[0]))
        if op == "negate":
            return self.scalar("mul", v[0], -1)
        if op == "exp":
            return self.scalar("pow", math.e, v[0])
        if op == "log":
            v = (*v, math.e)
        operations = {
            "add": "ADD",
            "sub": "SUBTRACT",
            "mul": "MULTIPLY",
            "div": "DIVIDE",
            "pow": "POWER",
            "min": "MINIMUM",
            "max": "MAXIMUM",
            "lt": "LESS_THAN",
            "gt": "GREATER_THAN",
            "eq": "COMPARE",
            "abs": "ABSOLUTE",
            "floor": "FLOOR",
            "ceil": "CEIL",
            "fract": "FRACT",
            "sqrt": "SQRT",
            "sin": "SINE",
            "cos": "COSINE",
            "log": "LOGARITHM",
        }
        node = self.node("ShaderNodeMath", op)
        node.operation = operations[op]
        for socket, value in zip(node.inputs, v):
            self.connect(socket, value)
        if op == "eq":
            node.inputs[2].default_value = 0
        return node.outputs[0]

    def vector(self, value):
        n = self.node("ShaderNodeCombineXYZ", "assemble vector")
        for sock, val in zip(n.inputs, value):
            self.connect(sock, val)
        return n.outputs[0]

    def split(self, value):
        n = self.node("ShaderNodeSeparateXYZ", "vector components")
        self.connect(n.inputs[0], value)
        return tuple(n.outputs)

    def texture(self, image, uv, filtering, wrap):
        if len(uv) not in (2, 3):
            _fail("2D texture UV must be vec2 or vec3")
        width, height = image.size

        # Source API UV has its origin at the top of the exported image.
        def tap(x, y):
            n = self.node("ShaderNodeTexImage", "original captured mip")
            n.image = image
            n.interpolation = "Closest"
            n.extension = "REPEAT" if wrap == "repeat" else "EXTEND"
            self.connect(
                n.inputs[0], self.vector((x, self.scalar("sub", 1, y), 0))
            )
            split = self.node("ShaderNodeSeparateColor", "sample RGB")
            split.mode = "RGB"
            self.connect(split.inputs[0], n.outputs["Color"])
            return (*split.outputs[:3], n.outputs["Alpha"])

        if filtering == "nearest":
            return tap(*uv[:2])
        coords, fractions = [], []
        for x, size in zip(uv[:2], (width, height)):
            if wrap == "clamp":
                x = self.scalar("saturate", x)
            p = self.scalar("sub", self.scalar("mul", x, size), 0.5)
            floor = self.scalar("floor", p)
            coords.append(floor)
            fractions.append(self.scalar("sub", p, floor))
        rows = []
        for dy in (0, 1):
            samples = []
            for dx in (0, 1):
                xy = []
                for p, offset, size in zip(coords, (dx, dy), (width, height)):
                    q = self.scalar("add", p, offset)
                    if wrap == "clamp":
                        q = self.scalar("clamp", q, 0, size - 1)
                    xy.append(
                        self.scalar("div", self.scalar("add", q, 0.5), size)
                    )
                samples.append(tap(*xy))
            rows.append(
                _apply(
                    "mix",
                    [samples[0], samples[1], (fractions[0],)],
                    self.scalar,
                )
            )
        return _apply("mix", [rows[0], rows[1], (fractions[1],)], self.scalar)


def build_material(bpy, recipe, event, evidence_dir, attributes):
    """Build an emission graph; reject unsupported inputs explicitly."""
    order = validate_recipe(recipe)
    if (
        event.get("shader_sha256", recipe["shader_sha256"])
        != recipe["shader_sha256"]
    ):
        _fail("Recipe shader hash differs from current draw shader")
    if event.get("shader_entry_point", "main") != recipe.get(
        "entry_point", "main"
    ):
        _fail("Recipe entry point differs from captured shader entry point")
    if event.get("bindings", {}).get("Pixel", {}).get("specialization"):
        _fail("Specialization constants require a specialized recipe backend")
    name = recipe.get("name") or "Fragment " + recipe["shader_sha256"][:12]
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    material.node_tree.nodes.clear()
    tree = bpy.data.node_groups.new(name + " equations", "ShaderNodeTree")
    g = _Nodes(bpy, tree)
    values = {}
    loaded = []
    try:
        for key in order:
            node = recipe["nodes"][key]
            op = node["op"]
            g.current = key
            if op == "const":
                result = _value(node["value"])
            elif op == "uniform":
                result = []
                for index, value in enumerate(_uniform(event, node)):
                    v = g.node(
                        "ShaderNodeValue",
                        "uniform {}:{} {} [{}]".format(
                            node["set"],
                            node["binding"],
                            ".".join(map(str, node["path"])),
                            index,
                        ),
                    )
                    v.outputs[0].default_value = value
                    result.append(v.outputs[0])
                result = tuple(result)
            elif op == "attribute":
                attribute, count = _attribute(attributes, node)
                n = g.node(
                    "ShaderNodeAttribute",
                    "captured varying " + attribute["name"],
                )
                n.attribute_name = attribute.get(
                    "blender_name", attribute["name"]
                )
                result = (
                    (n.outputs["Fac"],)
                    if count == 1
                    else g.split(n.outputs["Vector"])[: min(count, 3)]
                )
                if count == 4:
                    if not attribute.get("blender_w_name"):
                        _fail("vec4 varying needs a captured W attribute")
                    w = g.node("ShaderNodeAttribute", "captured varying W")
                    w.attribute_name = attribute["blender_w_name"]
                    result += (w.outputs["Fac"],)
            elif op == "texture":
                descriptor, path = _texture(event, node, evidence_dir)
                filtering, wrap = _sampling(event, node)
                fmt = descriptor.get(
                    "view_format", descriptor.get("format", "")
                )
                image_name = (
                    "Capture:"
                    + hashlib.sha256(
                        (str(path) + str(fmt)).encode()
                    ).hexdigest()[:24]
                )
                image = bpy.data.images.get(image_name)
                if image is None:
                    image = bpy.data.images.load(
                        str(path), check_existing=False
                    )
                    image.name = image_name
                    image.colorspace_settings.name = (
                        "sRGB" if "SRGB" in str(fmt).upper() else "Non-Color"
                    )
                    image.alpha_mode = "CHANNEL_PACKED"
                    image.pack()
                    loaded.append(image)
                result = _view_swizzle(
                    g.texture(image, values[node["uv"]], filtering, wrap),
                    descriptor,
                )
            elif op == "swizzle":
                result = _swizzle(values[node["inputs"][0]], node["channels"])
            else:
                result = _apply(
                    op, [values[r] for r in node["inputs"]], g.scalar
                )
            values[key] = result
        rgb, alpha = _outputs(recipe, values)
        tree.interface.new_socket(
            name="Color", in_out="OUTPUT", socket_type="NodeSocketColor"
        )
        tree.interface.new_socket(
            name="Alpha", in_out="OUTPUT", socket_type="NodeSocketFloat"
        )
        output = g.node("NodeGroupOutput", "fragment output")
        g.connect(output.inputs["Color"], g.vector(rgb))
        g.connect(output.inputs["Alpha"], alpha)
        outer = _Nodes(bpy, material.node_tree)
        core = outer.node("ShaderNodeGroup", "Original fragment equations")
        core.node_tree = tree
        emission = outer.node("ShaderNodeEmission", "Fragment RGB")
        outer.connect(emission.inputs["Color"], core.outputs["Color"])
        out = outer.node("ShaderNodeOutputMaterial", "Material output")
        outer.connect(out.inputs["Surface"], emission.outputs[0])
        # Alpha remains an explicit output for the pipeline compositor. Do not
        # silently substitute alpha blending for additive/depth/stencil states.
        from .shader_cleanup import prune_dead, tidy_layout

        prune_dead(tree)
        tidy_layout(tree)
        tidy_layout(material.node_tree)
        coverage = {
            "shader_sha256": recipe["shader_sha256"],
            "event_id": event.get("event_id"),
            "status": "recipe_translated",
            "material": material.name,
            "expression_count": len(order),
            "node_count": len(tree.nodes) + len(material.node_tree.nodes),
            "uses_capture_color_image": False,
            "explicit_sampler_overrides": [
                k
                for k, n in recipe["nodes"].items()
                if n["op"] == "texture" and "filter" in n
            ],
            "limitations": [
                "Pipeline blending, depth/stencil and multi-pass framebuffer "
                "composition are not emulated by this surface graph.",
                "Explicit original 2D mip levels only; derivatives, "
                "anisotropy, cube/volume sampling and dynamic LOD "
                "are unsupported.",
            ],
        }
        material["fragment_recipe"] = json.dumps(recipe)
        material["shader_translation_coverage"] = json.dumps(coverage)
        material["source_event_id"] = event.get("event_id", -1)
        return material, coverage
    except Exception:
        bpy.data.materials.remove(material)
        bpy.data.node_groups.remove(tree)
        for image in loaded:
            if not image.users:
                bpy.data.images.remove(image)
        raise
