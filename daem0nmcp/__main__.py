"""
Entry point for python -m daem0nmcp

Forces unbuffered stdout/stderr on Windows to prevent MCP stdio hanging.
"""

import os
import sys

# Force unbuffered output BEFORE importing anything else
# This is critical for MCP stdio transport on Windows
if sys.platform == "win32":
    # Disable buffering on stdout and stderr
    import io

    sys.stdout = io.TextIOWrapper(
        open(sys.stdout.fileno(), "wb", buffering=0), write_through=True  # noqa: SIM115
    )
    sys.stderr = io.TextIOWrapper(
        open(sys.stderr.fileno(), "wb", buffering=0), write_through=True  # noqa: SIM115
    )

    # Also set environment variable for any subprocesses
    os.environ["PYTHONUNBUFFERED"] = "1"

# Now import and run the server
from daem0nmcp.server import main

if __name__ == "__main__":
    main()
