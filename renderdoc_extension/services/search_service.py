"""Find draw and dispatch actions by their bound GPU resources."""

import renderdoc as rd

from .action_service import walk
from .pipeline_service import active_shaders, shader_stage
from .resource_service import resource_key


def scan(controller, match):
    events = controller.GetStructuredFile()
    matches = []
    count = 0
    draw = (
        rd.ActionFlags.Drawcall
        | rd.ActionFlags.Dispatch
        | rd.ActionFlags.MeshDispatch
    )
    for action in walk(controller.GetRootActions()):
        if not action.flags & draw:
            continue
        count += 1
        controller.SetFrameEvent(action.eventId, False)
        reason = match(controller.GetPipelineState())
        if reason is not None:
            matches.append(
                {
                    "event_id": action.eventId,
                    "name": action.GetName(events),
                    "match_reason": reason,
                }
            )
    return {
        "matches": matches,
        "scanned_draws": count,
        "total_matches": len(matches),
    }


def find_draws_by_shader(controller, names, shader_name, stage=None):
    needle = shader_name.casefold()
    wanted = shader_stage(stage) if stage else None

    def match(pipe):
        for shader_stage_id, shader in active_shaders(pipe):
            if wanted is not None and wanted != shader_stage_id:
                continue
            entry = pipe.GetShaderEntryPoint(shader_stage_id)
            name = names.get(str(shader), "")
            for kind, value in (("entry_point", entry), ("name", name)):
                if needle in value.casefold():
                    return "{} {}: '{}'".format(shader_stage_id, kind, value)
        return None

    return scan(controller, match)


def bindings(pipe):
    for stage, shader in active_shaders(pipe):
        yield shader, "{} shader".format(stage)
        for label, read in (
            ("SRV", pipe.GetReadOnlyResources),
            ("UAV", pipe.GetReadWriteResources),
            ("CB", pipe.GetConstantBlocks),
        ):
            for used in read(stage):
                yield (
                    used.descriptor.resource,
                    "{} {} slot {}".format(
                        stage,
                        label,
                        used.access.index,
                    ),
                )
    for i, view in enumerate(pipe.GetOutputTargets()):
        yield view.resource, "RenderTarget[{}]".format(i)
    yield pipe.GetDepthTarget().resource, "DepthTarget"
    for i, buf in enumerate(pipe.GetVBuffers()):
        yield buf.resourceId, "VertexBuffer[{}]".format(i)
    yield pipe.GetIBuffer().resourceId, "IndexBuffer"


def find_draws_by_texture(controller, names, texture_name):
    needle = texture_name.casefold()
    targets = {
        str(t.resourceId)
        for t in controller.GetTextures()
        if needle in names.get(str(t.resourceId), "").casefold()
    }

    def match(pipe):
        for rid, reason in bindings(pipe):
            key = str(rid)
            if key in targets:
                return "{}: '{}'".format(reason, names[key])
        return None

    return scan(controller, match)


def find_draws_by_resource(controller, resource_id):
    key = resource_key(resource_id)
    if key == str(rd.ResourceId.Null()):
        raise ValueError("Cannot search for a null resource ID")

    def match(pipe):
        return next(
            (role for rid, role in bindings(pipe) if str(rid) == key), None
        )

    return scan(controller, match)
