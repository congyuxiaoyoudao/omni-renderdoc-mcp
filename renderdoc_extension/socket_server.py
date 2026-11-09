"""Poll requests from the MCP server on the RenderDoc UI thread."""

from .request_handler import execute
from .utils.protocol import publish, read_json


class Listener:
    def __init__(self, ctx, directory):
        self.ctx = ctx
        self.directory = directory
        self.active = True

    def poll(self):
        if not self.active:
            return
        try:
            for path in sorted(self.directory.glob("call-*/request.json")):
                self.process(path)
                break
        finally:
            if self.active:
                self.ctx.DelayedCallback(50, self.poll)

    def process(self, path):
        claimed = path.with_name("running.json")
        try:
            path.rename(claimed)
        except FileNotFoundError:
            return
        try:
            request = read_json(claimed)
            result = execute(self.ctx, request["method"], request["params"])
            publish(path.with_name("response.json"), {"result": result})
        except FileNotFoundError as error:
            if not path.parent.is_dir():
                return
            self.reply_error(path, error)
        except Exception as error:
            # Carry failures across the GUI/stdio process boundary.
            self.reply_error(path, error)

    @staticmethod
    def reply_error(path, error):
        try:
            publish(
                path.with_name("response.json"),
                {"error": "{}: {}".format(type(error).__name__, error)},
            )
        except FileNotFoundError:
            pass  # The client cancelled or timed out while replay was running.
