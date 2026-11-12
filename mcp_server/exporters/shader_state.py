"""Translate representable raster, depth and blend state into ShaderLab."""

COMPARE = {
    "Never": "Never",
    "AlwaysTrue": "Always",
    "Less": "Less",
    "LessEqual": "LEqual",
    "Greater": "Greater",
    "GreaterEqual": "GEqual",
    "Equal": "Equal",
    "NotEqual": "NotEqual",
}
FACTORS = {
    "Zero": "Zero",
    "One": "One",
    "SrcCol": "SrcColor",
    "InvSrcCol": "OneMinusSrcColor",
    "DstCol": "DstColor",
    "InvDstCol": "OneMinusDstColor",
    "SrcAlpha": "SrcAlpha",
    "InvSrcAlpha": "OneMinusSrcAlpha",
    "DstAlpha": "DstAlpha",
    "InvDstAlpha": "OneMinusDstAlpha",
    "SrcAlphaSat": "SrcAlphaSaturate",
}
OPERATIONS = {
    "Add": "Add",
    "Subtract": "Sub",
    "ReversedSubtract": "RevSub",
    "Minimum": "Min",
    "Maximum": "Max",
}
STENCIL = {
    "Keep": "Keep",
    "Zero": "Zero",
    "Replace": "Replace",
    "IncSat": "IncrSat",
    "DecSat": "DecrSat",
    "IncWrap": "IncrWrap",
    "DecWrap": "DecrWrap",
    "Invert": "Invert",
}


def _blend(blend: dict, target: int) -> tuple[list[str], list[str]]:
    mask = "".join(
        name for i, name in enumerate("RGBA") if blend["write_mask"] & (1 << i)
    )
    lines = [f"ColorMask {mask or '0'} {target}"]
    notes = []
    if blend["logic_enabled"]:
        notes.append(f"Target {target} requires logic op {blend['logic_op']}")
    elif blend["enabled"]:
        factors, ops = [], []
        for channel in (blend["color"], blend["alpha"]):
            src = FACTORS.get(channel["source"])
            dst = FACTORS.get(channel["destination"])
            op = OPERATIONS.get(channel["operation"])
            if not (src and dst and op):
                notes.append(f"Target {target} requires a custom blend state")
                break
            factors.append(f"{src} {dst}")
            ops.append(op)
        else:
            lines.append(f"Blend {target} " + ", ".join(factors))
            lines.append(f"BlendOp {target} " + ", ".join(ops))
    return lines, notes


def shaderlab_state(pipeline: dict) -> tuple[list[str], list[str]]:
    lines, notes = [], []
    bias = pipeline.get("depth_bias", {})
    if bias.get("enabled") and any(
        bias.get(k, 0) != 0 for k in ("constant", "slope")
    ):
        notes.append(
            "Native depth bias requires a verified ShaderLab offset adapter"
        )
    cull = {"NoCull": "Off", "Back": "Back", "Front": "Front"}.get(
        pipeline.get("cull_mode")
    )
    if cull:
        lines.append(f"Cull {cull}")
        if cull != "Off" and "front_ccw" in pipeline:
            winding = "CCW" if pipeline["front_ccw"] else "CW"
            notes.append(
                f"Match imported front-face winding to captured {winding}"
            )
    elif pipeline.get("cull_mode"):
        notes.append("Captured cull mode requires a custom raster state")
    if pipeline.get("fill_mode", "Solid") != "Solid":
        notes.append("Non-solid fill requires runtime setup")
    if "depth_test" in pipeline:
        test = pipeline["depth_test"]
        func = COMPARE.get(pipeline["depth_func"]) if test else "Always"
        if func:
            lines.append(f"ZTest {func}")
        lines.append(
            "ZWrite " + ("On" if test and pipeline["depth_write"] else "Off")
        )
    if pipeline.get("depth_bounds"):
        notes.append("Captured depth bounds require runtime setup")
    if pipeline.get("stencil_test"):
        faces = pipeline["stencil_faces"]
        common = ("reference", "compare_mask", "write_mask")
        if len(faces) == 2 and all(faces[0][k] == faces[1][k] for k in common):
            front = faces[0]
            lines.extend(
                [
                    "Stencil {",
                    f"Ref {front['reference']}",
                    f"ReadMask {front['compare_mask']}",
                    f"WriteMask {front['write_mask']}",
                ]
            )
            for side, face in zip(("Front", "Back"), faces):
                lines.extend(
                    [
                        f"Comp{side} {COMPARE[face['func']]}",
                        f"Pass{side} {STENCIL[face['pass']]}",
                        f"Fail{side} {STENCIL[face['fail']]}",
                        f"ZFail{side} {STENCIL[face['depth_fail']]}",
                    ]
                )
            lines.append("}")
        else:
            notes.append(
                "Per-face stencil reference or masks require runtime setup"
            )
    blends = pipeline.get("blends", [])
    for attachment in pipeline.get("render_targets", []):
        target = attachment["index"]
        index = target if pipeline["independent_blend"] else 0
        if index < len(blends):
            state, limitations = _blend(blends[index], target)
            lines.extend(state)
            notes.extend(limitations)
    return lines, notes
