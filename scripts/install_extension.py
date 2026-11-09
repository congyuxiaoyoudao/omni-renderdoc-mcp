"""Install the extension from this checkout using the shared installer."""

import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from mcp_server.cli import main

    main(["install", *sys.argv[1:]])
