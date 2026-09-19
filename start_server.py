#!/usr/bin/env python
"""Launch the reviewed stateful MCP v7 Streamable HTTP transport."""

import argparse
import os
import sys
from pathlib import Path

# Add the package to path if running from this directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    parser = argparse.ArgumentParser(description="Start Daem0nMCP HTTP server")
    parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=9876,
        help="Port to listen on (default: 9876)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to")
    parser.add_argument(
        "--project",
        help=(
            "Project directory to serve (default: DAEM0NMCP_PROJECT_ROOT if set, "
            "else the current directory)"
        ),
    )
    args = parser.parse_args()

    if args.project:
        os.environ["DAEM0NMCP_PROJECT_ROOT"] = str(Path(args.project).resolve())
    elif not os.environ.get("DAEM0NMCP_PROJECT_ROOT"):
        os.environ["DAEM0NMCP_PROJECT_ROOT"] = str(Path.cwd().resolve())

    # Import only after the workspace environment is fixed.  Both public
    # launchers use the same v7 composition and transport-security boundary.
    from daem0nmcp.api.v7.launcher import ServerOptions, run_server
    from daem0nmcp.server import create_server

    server = create_server("streamable-http", host=args.host)
    run_server(
        server,
        ServerOptions("streamable-http", args.host, args.port),
    )


if __name__ == "__main__":
    main()
