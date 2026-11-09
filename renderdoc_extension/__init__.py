"""Register and unload the Omni RenderDoc GUI extension."""

from .socket_server import Listener
from .utils.protocol import default_ipc_dir

_listener = None


def register(version, ctx):
    import qrenderdoc

    global _listener
    directory = default_ipc_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _listener = Listener(ctx, directory)
    ctx.DelayedCallback(50, _listener.poll)

    def status(context, data):
        context.Extensions().MessageDialog(
            str(directory), "Omni MCP IPC directory"
        )

    ctx.Extensions().RegisterWindowMenu(
        qrenderdoc.WindowMenu.Tools, ["Omni RenderDoc MCP", "Status"], status
    )


def unregister():
    global _listener
    if _listener is not None:
        _listener.active = False
        _listener = None
