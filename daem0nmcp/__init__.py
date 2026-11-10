"""
Daem0nMCP Core Package
"""

import os as _os

# FastMCP loads `FASTMCP_*` settings from a `.env` in the working directory
# when it is first imported. The server is normally launched inside a project,
# so that file is repository-controlled input. Point FastMCP at the null device
# before any module of this package can import it, unless the operator named a
# file.
_os.environ.setdefault("FASTMCP_ENV_FILE", _os.devnull)

try:
    from importlib.metadata import version as _get_version

    __version__ = _get_version("daem0nmcp")
except Exception:
    # Fallback for development or if package not installed
    __version__ = "7.0.0.dev0"
