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

    project_root = Path(
        args.project or os.environ.get("DAEM0NMCP_PROJECT_ROOT") or Path.cwd()
    )
    if not project_root.is_dir():
        # Otherwise the server silently creates and serves an empty workspace
        # wherever a typo points.
        parser.error(f"project directory does not exist: {project_root}")
    os.environ["DAEM0NMCP_PROJECT_ROOT"] = str(project_root.resolve())
    print(f"Serving project: {project_root.resolve()}", file=sys.stderr)

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
