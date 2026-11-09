"""Command-line entry point for extension installation and MCP stdio."""

import argparse
import json
import math
import shutil
import sysconfig
from pathlib import Path

from renderdoc_extension.utils.protocol import default_ipc_dir

from .config import default_extensions_dir
from .installer import install


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="omni-renderdoc-mcp")
    commands = parser.add_subparsers(dest="command")
    serve = commands.add_parser(
        "serve", help="Run the MCP server over stdio (default)"
    )
    setup = commands.add_parser(
        "install", help="Install or update the GUI extension"
    )
    for command in (serve, setup):
        command.add_argument(
            "--spirv-cross",
            type=Path,
            help="SPIRV-Cross CLI for Unity shader export (default: PATH)",
        )
        command.add_argument(
            "--dxbc-decompiler",
            type=Path,
            help="3DMigoto cmd_Decompiler for DXBC (default: PATH)",
        )
        command.add_argument(
            "--vkd3d-compiler",
            type=Path,
            help="vkd3d-compiler for DXBC compute shaders (default: PATH)",
        )
    serve.add_argument(
        "--timeout",
        type=float,
        default=600,
        help="Seconds to wait for each GUI operation (default: 600)",
    )
    setup.add_argument(
        "--extensions-dir",
        type=Path,
        default=default_extensions_dir(),
        help="RenderDoc extensions directory (default: qrenderdoc/extensions)",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = make_parser()
    args = parser.parse_args(argv)
    if args.command == "install":
        executable = shutil.which(
            "omni-renderdoc-mcp", path=sysconfig.get_path("scripts")
        ) or shutil.which("omni-renderdoc-mcp")
        try:
            destination = install(args.extensions_dir)
        except (OSError, ValueError) as error:
            parser.exit(1, f"Installation failed: {error}\n")
        server_args = ["serve"]
        for flag in (
            "spirv-cross",
            "dxbc-decompiler",
            "vkd3d-compiler",
        ):
            path = getattr(args, flag.replace("-", "_"))
            if path:
                server_args.extend(
                    ["--" + flag, str(path.expanduser().resolve())]
                )
        print(
            f"Installed {destination}. Enable 'Omni RenderDoc MCP' in the UI."
        )
        if not executable:
            print("Install the MCP command to print the client configuration.")
            return
        command = str(Path(executable).expanduser().resolve())
        print(
            json.dumps(
                {
                    "mcpServers": {
                        "omni-renderdoc-mcp": {
                            "command": command,
                            "args": server_args,
                        }
                    }
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return

    from .server import run

    if args.command is None:
        args = parser.parse_args(["serve"])
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be positive and finite")
    run(
        default_ipc_dir(),
        args.timeout,
        args.spirv_cross,
        args.dxbc_decompiler,
        args.vkd3d_compiler,
    )
