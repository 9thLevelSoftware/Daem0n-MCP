"""
Shared FastMCP instance for Daem0nMCP.

This module provides the singleton FastMCP instance that all tool modules
register their @mcp.tool decorators against. It sits at the bottom of the
import hierarchy to break circular imports:

    mcp_instance  (this module -- zero business-logic imports)
        <- context_manager  (ProjectContext lifecycle)
            <- tools/*  (tool definitions import mcp + context_manager)
                <- server.py  (composition root wires everything together)

By isolating the FastMCP instance here, tool modules can be extracted from
server.py without creating circular dependencies.
"""

import logging
import os
import sys

try:
    from fastmcp import FastMCP
except ImportError:
    print(
        "ERROR: fastmcp not installed. Run: pip install fastmcp>=3.0.0b1",
        file=sys.stderr,
    )
    sys.exit(1)

try:
    from .config import settings
    from .logging_config import StructuredFormatter
except ImportError:
    from daem0nmcp.config import settings
    from daem0nmcp.logging_config import StructuredFormatter

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Configure structured logging (optional - only if env var set)
if os.getenv("DAEM0NMCP_STRUCTURED_LOGS"):
    handler = logging.StreamHandler()
    handler.setFormatter(StructuredFormatter())
    daem0n_logger = logging.getLogger("daem0nmcp")
    daem0n_logger.addHandler(handler)
    daem0n_logger.setLevel(logging.INFO)

# Initialize FastMCP server
mcp = FastMCP("Daem0nMCP")
