"""Route IPC methods to capture queries or mesh export."""


def execute(ctx, method, params):
    from .renderdoc_facade import dispatch, replay

    if method not in (
        "export_frame_meshes",
        "stage_pass_shaders",
    ):
        return dispatch(ctx, method, params)
    if not ctx.IsCaptureLoaded():
        raise ValueError("Open a capture before exporting assets.")

    from .services.mesh import export_frame_meshes

    source_capture = ctx.GetCaptureFilename()

    def export(controller):
        if method == "stage_pass_shaders":
            from .services.shader_export import stage_pass_shaders

            return stage_pass_shaders(
                controller, source_capture=source_capture, **params
            )
        options = dict(params, source_capture=source_capture)
        pass_id = options.pop("pass_event_id", None)
        if pass_id is not None:
            import renderdoc as rd

            from .services.shader_export import select_pass

            if options.get("event_ids") is not None:
                raise ValueError("Choose either pass_event_id or event_ids")
            scope, actions = select_pass(controller, pass_id)
            options["scope"] = scope
            draw = rd.ActionFlags.Drawcall | rd.ActionFlags.MeshDispatch
            options["event_ids"] = [
                a.eventId for a in actions if a.flags & draw
            ]
        return export_frame_meshes(controller, **options)

    return replay(ctx, export, restore_event=True)
