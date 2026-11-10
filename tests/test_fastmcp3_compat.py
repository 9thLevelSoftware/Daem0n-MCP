# tests/test_fastmcp3_compat.py
"""Test FastMCP 3.4.7 compatibility."""


def test_fastmcp_import():
    """Verify the pinned FastMCP import works."""
    from fastmcp import FastMCP

    assert FastMCP is not None


def test_fastmcp_version():
    """Verify the exact framework version reviewed by the v7 adapter."""
    import fastmcp

    version = getattr(fastmcp, "__version__", "0.0.0")
    assert version == "3.4.7", f"Expected FastMCP 3.4.7, got {version}"


def test_server_import():
    """Verify server module imports correctly with pinned FastMCP."""
    from daem0nmcp import server

    assert server.mcp is not None


async def test_tools_have_version_metadata():
    """Verify all MCP tools have version metadata for migration safety."""
    from daem0nmcp import __version__, server

    mcp = server.mcp

    # Get all registered tools via the reviewed FastMCP async API.
    tools = await mcp.list_tools()

    assert tools, "No tools registered - server may not have initialized correctly"

    tools_without_version = []
    for tool in tools:
        # FunctionTool stores version directly
        version = getattr(tool, "version", None)
        if version is None:
            tools_without_version.append(tool.name)

    assert not tools_without_version, (
        f"Tools missing version metadata: {tools_without_version}. "
        f"All tools should have version='{__version__}'"
    )


async def test_tools_version_matches_package():
    """Verify tool versions match the package version."""
    from daem0nmcp import __version__, server

    mcp = server.mcp

    # Get all registered tools via the reviewed FastMCP async API.
    tools = await mcp.list_tools()

    mismatched_versions = []
    for tool in tools:
        version = getattr(tool, "version", None)
        if version is not None and str(version) != __version__:
            mismatched_versions.append((tool.name, version, __version__))

    assert not mismatched_versions, (
        f"Tools with mismatched versions: {mismatched_versions}. "
        f"Expected all tools to have version='{__version__}'"
    )
