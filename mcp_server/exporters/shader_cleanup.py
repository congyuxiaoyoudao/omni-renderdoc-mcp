"""Conservative constant evaluation and dead-code removal for shader nodes.

Only captured uniform arithmetic is evaluated. Texture reads, attributes,
geometry and lighting remain native nodes, with original uniforms in metadata.
"""

import json
import math


def _value(socket):
    value = socket.default_value
    return tuple(value) if hasattr(value, "__len__") else float(value)


def _assign(socket, value):
    if not hasattr(socket, "default_value"):
        return False
    try:
        current = socket.default_value
        if hasattr(current, "__len__"):
            if not isinstance(value, tuple):
                value = (value,) * 3
            if len(current) == 4 and len(value) == 3:
                value += (1.0,)
            socket.default_value = value[: len(current)]
        elif isinstance(value, tuple):
            # Avoid implicit color/vector-to-float conversions.
            return False
        else:
            socket.default_value = value
        return True
    except (TypeError, ValueError, OverflowError):
        return False


def _math(op, a, b, c):
    simple = {
        "ADD": lambda: a + b,
        "SUBTRACT": lambda: a - b,
        "MULTIPLY": lambda: a * b,
        "DIVIDE": lambda: a / b if b else 0.0,
        "MULTIPLY_ADD": lambda: a * b + c,
        "MINIMUM": lambda: min(a, b),
        "MAXIMUM": lambda: max(a, b),
        "ABSOLUTE": lambda: abs(a),
        "FLOOR": lambda: math.floor(a),
        "CEIL": lambda: math.ceil(a),
        "FRACT": lambda: a - math.floor(a),
        "SQRT": lambda: math.sqrt(max(0.0, a)),
        "POWER": lambda: math.pow(a, b),
        "SINE": lambda: math.sin(a),
        "COSINE": lambda: math.cos(a),
        "LESS_THAN": lambda: float(a < b),
        "GREATER_THAN": lambda: float(a > b),
        "COMPARE": lambda: float(abs(a - b) <= max(c, 0.0)),
        "SIGN": lambda: float((a > 0) - (a < 0)),
        "EXPONENT": lambda: math.exp(a),
        "MODULO": lambda: math.fmod(a, b) if b else 0.0,
        "FLOORED_MODULO": lambda: a % b if b else 0.0,
        "ARCCOSINE": lambda: math.acos(max(-1.0, min(1.0, a))),
        "ARCSINE": lambda: math.asin(max(-1.0, min(1.0, a))),
    }
    if op not in simple:
        raise ValueError(op)
    return simple[op]()


def _evaluate(node):
    kind = node.bl_idname
    if kind in ("ShaderNodeValue", "ShaderNodeRGB"):
        return {0: _value(node.outputs[0])}
    if any(s.is_linked for s in node.inputs if s.enabled):
        return None
    inputs = [_value(s) for s in node.inputs if hasattr(s, "default_value")]
    if kind == "ShaderNodeCombineXYZ":
        return {0: tuple(inputs[:3])}
    if kind == "ShaderNodeSeparateXYZ":
        return dict(enumerate(inputs[0]))
    if kind == "ShaderNodeMath":
        result = _math(node.operation, *(inputs + [0.0] * 3)[:3])
        if node.use_clamp:
            result = max(0.0, min(1.0, result))
        return {0: result}
    if kind == "ShaderNodeVectorMath":
        a, b, _, scale = inputs
        op = node.operation
        if op in ("ADD", "SUBTRACT", "MULTIPLY", "DIVIDE"):
            return {0: tuple(_math(op, x, y, 0) for x, y in zip(a, b))}
        if op == "SCALE":
            return {0: tuple(x * scale for x in a)}
        if op == "DOT_PRODUCT":
            return {1: sum(x * y for x, y in zip(a, b))}
        length = math.sqrt(sum(x * x for x in a))
        if op == "LENGTH":
            return {1: length}
        if op == "NORMALIZE":
            return {0: tuple(x / length if length else 0.0 for x in a)}
        if op == "CROSS_PRODUCT":
            return {
                0: (
                    a[1] * b[2] - a[2] * b[1],
                    a[2] * b[0] - a[0] * b[2],
                    a[0] * b[1] - a[1] * b[0],
                )
            }
    return None


def _roots(tree):
    return [
        n
        for n in tree.nodes
        if n.bl_idname
        in (
            "ShaderNodeOutputMaterial",
            "ShaderNodeOutputWorld",
            "NodeGroupOutput",
        )
        and getattr(n, "is_active_output", True)
    ]


def _identity(node):
    """Prune inactive uniform branches before traversing their inputs."""

    def is_value(socket, wanted):
        if socket.is_linked:
            return False
        value = _value(socket)
        return (
            all(v == wanted for v in value)
            if isinstance(value, tuple)
            else value == wanted
        )

    if node.bl_idname == "ShaderNodeMath" and not node.use_clamp:
        a, b = node.inputs[:2]
        if node.operation == "MULTIPLY":
            if is_value(a, 0) or is_value(b, 0):
                return 0, 0.0
            if is_value(a, 1):
                return 0, b
            if is_value(b, 1):
                return 0, a
        if node.operation == "ADD":
            if is_value(a, 0):
                return 0, b
            if is_value(b, 0):
                return 0, a
        if node.operation in ("SUBTRACT", "DIVIDE") and is_value(
            b, 0 if node.operation == "SUBTRACT" else 1
        ):
            return 0, a
    if node.bl_idname == "ShaderNodeVectorMath":
        a, b = node.inputs[:2]
        if node.operation == "SCALE":
            if is_value(node.inputs[3], 0):
                return 0, (0.0, 0.0, 0.0)
            if is_value(node.inputs[3], 1):
                return 0, a
        if node.operation == "MULTIPLY":
            if is_value(a, 0) or is_value(b, 0):
                return 0, (0.0, 0.0, 0.0)
            if is_value(a, 1):
                return 0, b
            if is_value(b, 1):
                return 0, a
        if node.operation == "ADD":
            if is_value(a, 0):
                return 0, b
            if is_value(b, 0):
                return 0, a
    if (
        node.bl_idname == "ShaderNodeMixRGB"
        and node.blend_type == "MIX"
        and not node.use_clamp
    ):
        if is_value(node.inputs[0], 0):
            return 0, node.inputs[1]
        if is_value(node.inputs[0], 1):
            return 0, node.inputs[2]
    return None


def prune_dead(tree):
    reachable = set()
    pending = _roots(tree)
    dependencies = {}
    for link in tree.links:
        dependencies.setdefault(link.to_node, []).append(link.from_node)
    while pending:
        node = pending.pop()
        if node in reachable:
            continue
        reachable.add(node)
        pending.extend(dependencies.get(node, ()))
    removed = 0
    for node in list(tree.nodes):
        if node not in reachable and node.type != "FRAME":
            tree.nodes.remove(node)
            removed += 1
    for node in list(tree.nodes):
        if node.type == "FRAME" and not any(
            n.parent == node for n in tree.nodes
        ):
            tree.nodes.remove(node)
    return removed


def simplify_tree(tree):
    """Fold captured constants and remove inputs with no output dependency."""
    before = len(tree.nodes)
    uniforms = {}
    for node in tree.nodes:
        if node.bl_idname in ("ShaderNodeValue", "ShaderNodeRGB"):
            uniforms[node.label or node.name] = _value(node.outputs[0])
    folded = 0
    while True:
        changed = False
        outgoing = {}
        for link in tree.links:
            outgoing.setdefault(link.from_socket, []).append(link)
        for node in list(tree.nodes):
            if not any(s in outgoing for s in node.outputs):
                continue
            identity = _identity(node)
            if identity is not None:
                index, source = identity
                source_socket = (
                    source.links[0].from_socket
                    if hasattr(source, "is_linked") and source.is_linked
                    else None
                )
                value = (
                    _value(source)
                    if hasattr(source, "is_linked") and not source.is_linked
                    else source
                )
                if source_socket is not None and (
                    source_socket.type != source.type
                    or source_socket.type != node.outputs[index].type
                ):
                    # Keep Blender's implicit input/output conversion.
                    continue
                for link in outgoing.get(node.outputs[index], ()):
                    destination = link.to_socket
                    if source_socket is not None:
                        tree.links.remove(link)
                        tree.links.new(source_socket, destination)
                    elif _assign(destination, value):
                        tree.links.remove(link)
                    else:
                        continue
                    changed = True
                    folded += 1
                continue
            try:
                values = _evaluate(node)
            except (ValueError, TypeError, OverflowError, ZeroDivisionError):
                continue
            if values is None:
                continue
            for index, value in values.items():
                if not all(
                    math.isfinite(x)
                    for x in (value if isinstance(value, tuple) else (value,))
                ):
                    continue
                for link in outgoing.get(node.outputs[index], ()):
                    if _assign(link.to_socket, value):
                        tree.links.remove(link)
                        changed = True
                        folded += 1
        if not changed:
            break
    removed = prune_dead(tree)
    tree["captured_uniforms_before_cleanup"] = json.dumps(uniforms)
    return {
        "before": before,
        "after": len(tree.nodes),
        "constant_links_evaluated": folded,
        "dead_nodes_removed": removed,
    }


def tidy_layout(tree):
    """Compact the actual remaining operations in dependency order."""
    nodes = [n for n in tree.nodes if n.type != "FRAME"]
    rank = {}
    dependencies = {}
    for link in tree.links:
        dependencies.setdefault(link.to_node, []).append(link.from_node)

    def depth(n, active=()):
        if n in rank:
            return rank[n]
        if n in active:
            return 0
        rank[n] = 1 + max(
            (
                depth(source, active + (n,))
                for source in dependencies.get(n, ())
            ),
            default=-1,
        )
        return rank[n]

    rows = {}
    for node in nodes:
        column = depth(node)
        row = rows.get(column, 0)
        rows[column] = row + 1
        node.parent = None
        node.location = (column * 230, -row * 160)
        node.width = 190
        node.hide = node.bl_idname in (
            "ShaderNodeMath",
            "ShaderNodeVectorMath",
        )
    for node in list(tree.nodes):
        if node.type == "FRAME":
            tree.nodes.remove(node)


def group_stages(bpy, tree):
    """Make inspectable fragment functions from existing semantic frames."""
    if tree.is_embedded_data:
        raise ValueError("Stage grouping requires a standalone node group")
    frames = [n for n in tree.nodes if n.type == "FRAME"]
    created = []
    for frame in frames:
        members = {
            n
            for n in tree.nodes
            if n.parent == frame
            and n.type
            not in (
                "GROUP_INPUT",
                "GROUP_OUTPUT",
                "FRAME",
                "OUTPUT_MATERIAL",
                "OUTPUT_WORLD",
            )
        }
        if len(members) < 4:
            continue
        inbound, outbound = {}, {}
        for link in tree.links:
            key = (
                link.from_node.name,
                list(link.from_node.outputs).index(link.from_socket),
            )
            if link.to_node in members and link.from_node not in members:
                inbound.setdefault(key, []).append(
                    (
                        link.to_node.name,
                        list(link.to_node.inputs).index(link.to_socket),
                    )
                )
            if link.from_node in members and link.to_node not in members:
                outbound.setdefault(key, []).append(link.to_socket)
        if not outbound:
            continue
        successors = {}
        for link in tree.links:
            successors.setdefault(link.from_node, []).append(link.to_node)
        pending = [
            node
            for member in members
            for node in successors.get(member, ())
            if node not in members
        ]
        visited, feedback = set(), False
        while pending:
            node = pending.pop()
            if node in members:
                feedback = True
                break
            if node in visited:
                continue
            visited.add(node)
            pending.extend(successors.get(node, ()))
        if feedback:
            tree["grouping_skipped_" + frame.name] = (
                "Interleaved stage: grouping would introduce a feedback loop"
            )
            continue
        name = frame.name
        function = tree.copy()
        function.name = tree.name + " / " + name
        member_names = {n.name for n in members}
        for node in list(function.nodes):
            if node.name not in member_names:
                function.nodes.remove(node)
            else:
                node.parent = None
        function.interface.clear()
        input_node = function.nodes.new("NodeGroupInput")
        output_node = function.nodes.new("NodeGroupOutput")
        type_map = {
            "VALUE": "NodeSocketFloat",
            "VECTOR": "NodeSocketVector",
            "RGBA": "NodeSocketColor",
            "SHADER": "NodeSocketShader",
        }
        for key, destinations in inbound.items():
            original = tree.nodes[key[0]].outputs[key[1]]
            label = (tree.nodes[key[0]].label or key[0]) + ": " + original.name
            socket = function.interface.new_socket(
                name=label, in_out="INPUT", socket_type=type_map[original.type]
            )
            for dst_name, dst_index in destinations:
                function.links.new(
                    input_node.outputs[socket.identifier],
                    function.nodes[dst_name].inputs[dst_index],
                )
        for key in outbound:
            original = tree.nodes[key[0]].outputs[key[1]]
            label = (tree.nodes[key[0]].label or key[0]) + ": " + original.name
            socket = function.interface.new_socket(
                name=label,
                in_out="OUTPUT",
                socket_type=type_map[original.type],
            )
            function.links.new(
                function.nodes[key[0]].outputs[key[1]],
                output_node.inputs[socket.identifier],
            )
        instance = tree.nodes.new("ShaderNodeGroup")
        instance.node_tree = function
        instance.label = name
        instance.name = name + " function"
        for index, key in enumerate(inbound):
            tree.links.new(
                tree.nodes[key[0]].outputs[key[1]], instance.inputs[index]
            )
        for index, destinations in enumerate(outbound.values()):
            for socket in destinations:
                tree.links.new(instance.outputs[index], socket)
        for node in members:
            tree.nodes.remove(node)
        tree.nodes.remove(frame)
        tidy_layout(function)
        created.append(function.name)
    tidy_layout(tree)
    return created


def cleanup_material(material):
    seen, reports = set(), []

    def visit(tree):
        if tree in seen or tree.get("fragment_cleanup_applied", False):
            return
        seen.add(tree)
        for node in list(tree.nodes):
            if node.type == "GROUP" and node.node_tree:
                visit(node.node_tree)
        result = simplify_tree(tree)
        result["tree"] = tree.name
        reports.append(result)
        tree["fragment_cleanup_applied"] = True
        # Preserve semantic stage frames in shader functions.
        if tree == material.node_tree:
            tidy_layout(tree)

    visit(material.node_tree)
    return reports
