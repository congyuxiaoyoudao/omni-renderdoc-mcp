"""One private request directory per outstanding MCP call."""

import asyncio
import tempfile
import time
from pathlib import Path

from renderdoc_extension.utils.protocol import publish, read_json


class BridgeError(RuntimeError):
    """The extension rejected a request or did not reply in time."""


class BridgeClient:
    def __init__(self, directory: Path, timeout: float):
        self.directory = directory
        self.timeout = timeout

    async def call(self, method: str, params: dict) -> dict:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="call-", dir=self.directory
        ) as request_dir:
            working = Path(request_dir)
            deadline = time.monotonic() + self.timeout
            publish(
                working / "request.json", {"method": method, "params": params}
            )
            response = working / "response.json"
            while not response.is_file():
                if time.monotonic() >= deadline:
                    raise BridgeError(
                        f"{method} timed out after {self.timeout:g}s. "
                        f"Check the extension and IPC path: {self.directory}. "
                        "An in-progress export can continue in RenderDoc."
                    )
                await asyncio.sleep(0.05)
            message = read_json(response)
            if "error" in message:
                raise BridgeError(message["error"])
            return message["result"]
