"""Execute capture queries and restore replay state after event inspection."""

from functools import partial

from .services import action_service as actions
from .services import capture_manager
from .services import pipeline_service as pipeline
from .services import resource_service as resources
from .services import search_service as search


def replay(ctx, callback, restore_event=False):
    """Run on the replay thread and propagate SWIG callback errors."""
    if not ctx.IsCaptureLoaded():
        raise ValueError("No capture loaded")
    saved_event = ctx.CurEvent() if restore_event else None
    result = []
    errors = []

    def invoke(controller):
        try:
            try:
                result.append(callback(controller))
            finally:
                if saved_event is not None:
                    controller.SetFrameEvent(saved_event, True)
        except Exception as error:
            errors.append(error)

    ctx.Replay().BlockInvoke(invoke)
    if errors:
        raise errors[0]
    if not result:
        raise RuntimeError("The replay controller is unavailable")
    return result[0]


REPLAY_TOOLS = {
    f.__name__: f
    for f in (
        actions.get_draw_calls,
        actions.get_frame_summary,
        actions.get_draw_call_details,
        actions.get_action_timings,
        resources.get_buffer_contents,
        resources.get_texture_info,
        resources.get_texture_data,
        pipeline.get_shader_info,
        pipeline.get_pipeline_state,
        search.find_draws_by_resource,
        search.find_draws_by_shader,
        search.find_draws_by_texture,
    )
}

NAMED_RESOURCES = {
    "find_draws_by_shader",
    "find_draws_by_texture",
    "get_pipeline_state",
}

SELECT_EVENT = NAMED_RESOURCES | {"find_draws_by_resource", "get_shader_info"}


def dispatch(ctx, method, params):
    """Execute a tool on the UI thread, delegating GPU access to replay()."""
    if method == "get_capture_status":
        return capture_manager.get_capture_status(ctx, replay)
    if method == "list_captures":
        return capture_manager.list_captures(**params)
    if method == "open_capture":
        return capture_manager.open_capture(ctx, replay=replay, **params)

    handler = REPLAY_TOOLS[method]
    if not ctx.IsCaptureLoaded():
        raise ValueError("No capture loaded")
    if method in NAMED_RESOURCES:
        names = {
            str(r.resourceId): ctx.GetResourceName(r.resourceId)
            for r in ctx.GetResources()
        }
        handler = partial(handler, names=names)
    return replay(
        ctx, partial(handler, **params), restore_event=method in SELECT_EVENT
    )
