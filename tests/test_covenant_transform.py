"""Test CovenantTransform implementation for FastMCP 3.0.

This tests the middleware-style Sacred Covenant enforcement that intercepts
tool calls to ensure proper communion (get_briefing) and counsel (context_check)
before allowing operations.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_covenant_transform_exists():
    """Verify CovenantTransform can be imported."""
    from daem0nmcp.transforms.covenant import CovenantTransform

    assert CovenantTransform is not None


@pytest.mark.asyncio
async def test_covenant_transform_blocks_without_briefing():
    """Tool calls should be blocked if get_briefing not called."""
    from daem0nmcp.transforms.covenant import CovenantTransform

    transform = CovenantTransform()

    # Should return blocked response when not briefed
    result = transform.check_tool_access(
        tool_name="remember",
        project_path="/test/project",
        get_state=lambda p: {"briefed": False, "context_checks": []},
    )

    assert result is not None
    assert result["status"] == "blocked"
    assert result["violation"] == "COMMUNION_REQUIRED"


@pytest.mark.asyncio
async def test_covenant_transform_allows_exempt_tools():
    """Exempt tools should always be allowed."""
    from daem0nmcp.transforms.covenant import CovenantTransform

    transform = CovenantTransform()

    # get_briefing is exempt - should be allowed even without briefing
    result = transform.check_tool_access(
        tool_name="get_briefing",
        project_path="/test/project",
        get_state=lambda p: {"briefed": False, "context_checks": []},
    )

    assert result is None  # None means allowed


@pytest.mark.asyncio
async def test_covenant_transform_blocks_counsel_required():
    """Counsel-required tools need context_check."""
    from daem0nmcp.transforms.covenant import CovenantTransform

    transform = CovenantTransform()

    # Briefed but no context_check
    result = transform.check_tool_access(
        tool_name="remember",
        project_path="/test/project",
        get_state=lambda p: {"briefed": True, "context_checks": []},
    )

    assert result is not None
    assert result["status"] == "blocked"
    assert result["violation"] == "COUNSEL_REQUIRED"


@pytest.mark.asyncio
async def test_covenant_transform_allows_with_fresh_counsel():
    """Tools should be allowed when counsel is fresh."""
    from daem0nmcp.transforms.covenant import CovenantTransform

    transform = CovenantTransform()

    # Briefed with fresh context_check
    result = transform.check_tool_access(
        tool_name="remember",
        project_path="/test/project",
        get_state=lambda p: {
            "briefed": True,
            "context_checks": [
                {
                    "topic": "remember",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ],
        },
    )

    assert result is None  # None means allowed


@pytest.mark.asyncio
async def test_covenant_transform_blocks_stale_counsel():
    """Tools should be blocked when counsel is stale (expired)."""
    from datetime import timedelta

    from daem0nmcp.transforms.covenant import CovenantTransform

    transform = CovenantTransform()

    # Briefed with stale context_check (10 minutes old, TTL is 5 minutes)
    stale_time = datetime.now(timezone.utc) - timedelta(minutes=10)
    result = transform.check_tool_access(
        tool_name="remember",
        project_path="/test/project",
        get_state=lambda p: {
            "briefed": True,
            "context_checks": [
                {"topic": "remember", "timestamp": stale_time.isoformat()}
            ],
        },
    )

    assert result is not None
    assert result["status"] == "blocked"
    assert result["violation"] == "COUNSEL_EXPIRED"


@pytest.mark.asyncio
async def test_covenant_transform_communion_only_tools():
    """Some tools require communion but not counsel."""
    from daem0nmcp.transforms.covenant import CovenantTransform

    transform = CovenantTransform()

    # record_outcome requires communion but not counsel
    result = transform.check_tool_access(
        tool_name="record_outcome",
        project_path="/test/project",
        get_state=lambda p: {"briefed": True, "context_checks": []},
    )

    # Should be allowed because record_outcome only needs communion, not counsel
    assert result is None


@pytest.mark.asyncio
async def test_covenant_transform_read_only_tools_exempt():
    """Read-only tools should be exempt."""
    from daem0nmcp.transforms.covenant import CovenantTransform

    transform = CovenantTransform()

    # recall is a read-only tool - should be allowed without briefing
    result = transform.check_tool_access(
        tool_name="recall",
        project_path="/test/project",
        get_state=lambda p: {"briefed": False, "context_checks": []},
    )

    assert result is None  # None means allowed


@pytest.mark.asyncio
async def test_covenant_transform_health_exempt():
    """Health tool should always be exempt."""
    from daem0nmcp.transforms.covenant import CovenantTransform

    transform = CovenantTransform()

    # health is always allowed
    result = transform.check_tool_access(
        tool_name="health",
        project_path="/test/project",
        get_state=lambda p: {"briefed": False, "context_checks": []},
    )

    assert result is None  # None means allowed


@pytest.mark.asyncio
async def test_covenant_transform_context_check_exempt():
    """context_check itself should be exempt."""
    from daem0nmcp.transforms.covenant import CovenantTransform

    transform = CovenantTransform()

    # context_check is part of the covenant flow - should be allowed
    result = transform.check_tool_access(
        tool_name="context_check",
        project_path="/test/project",
        get_state=lambda p: {"briefed": True, "context_checks": []},
    )

    assert result is None  # None means allowed


@pytest.mark.asyncio
async def test_covenant_transform_tool_classifications():
    """Verify tool classification sets are properly defined."""
    from daem0nmcp.transforms.covenant import (
        COMMUNION_REQUIRED_TOOLS,
        COUNSEL_REQUIRED_TOOLS,
        COVENANT_EXEMPT_TOOLS,
    )

    # Entry points and diagnostics should be exempt
    assert "get_briefing" in COVENANT_EXEMPT_TOOLS
    assert "health" in COVENANT_EXEMPT_TOOLS
    assert "context_check" in COVENANT_EXEMPT_TOOLS

    # Read-only query tools should be exempt
    assert "recall" in COVENANT_EXEMPT_TOOLS
    assert "recall_for_file" in COVENANT_EXEMPT_TOOLS
    assert "search_memories" in COVENANT_EXEMPT_TOOLS

    # Mutating tools require communion
    assert "remember" in COMMUNION_REQUIRED_TOOLS
    assert "remember_batch" in COMMUNION_REQUIRED_TOOLS
    assert "add_rule" in COMMUNION_REQUIRED_TOOLS

    # Highly destructive operations require both communion and counsel
    assert "remember" in COUNSEL_REQUIRED_TOOLS
    assert "prune_memories" in COUNSEL_REQUIRED_TOOLS


@pytest.mark.asyncio
async def test_covenant_violation_response_structure():
    """Test the CovenantViolation response structure."""
    from daem0nmcp.transforms.covenant import CovenantViolation

    # Test communion_required response
    response = CovenantViolation.communion_required("/test/project")
    assert response["status"] == "blocked"
    assert response["violation"] == "COMMUNION_REQUIRED"
    assert "remedy" in response
    assert response["remedy"]["tool"] == "get_briefing"
    assert response["project_path"] == "/test/project"

    # Test counsel_required response
    response = CovenantViolation.counsel_required("remember", "/test/project")
    assert response["status"] == "blocked"
    assert response["violation"] == "COUNSEL_REQUIRED"
    assert "remedy" in response
    assert response["remedy"]["tool"] == "context_check"
    assert response["tool_blocked"] == "remember"

    # Test counsel_expired response
    response = CovenantViolation.counsel_expired("remember", "/test/project", 400)
    assert response["status"] == "blocked"
    assert response["violation"] == "COUNSEL_EXPIRED"
    assert "400" in response["message"]  # Age should be in message


@pytest.mark.asyncio
async def test_covenant_transform_no_project_path():
    """Tools should work when project_path is None (for some tools)."""
    from daem0nmcp.transforms.covenant import CovenantTransform

    transform = CovenantTransform()

    # health tool doesn't need project_path
    result = transform.check_tool_access(
        tool_name="health", project_path=None, get_state=lambda p: None
    )

    assert result is None  # None means allowed


@pytest.mark.asyncio
async def test_covenant_transform_missing_state():
    """Handle case when state is unavailable."""
    from daem0nmcp.transforms.covenant import CovenantTransform

    transform = CovenantTransform()

    # State callback returns None
    result = transform.check_tool_access(
        tool_name="remember", project_path="/test/project", get_state=lambda p: None
    )

    assert result is not None
    assert result["status"] == "blocked"
    assert result["violation"] == "COMMUNION_REQUIRED"


# ============================================================================
# COVENANT MIDDLEWARE TESTS
# ============================================================================


@pytest.mark.asyncio
async def test_covenant_middleware_exists():
    """Verify CovenantMiddleware can be imported."""
    from daem0nmcp.transforms.covenant import CovenantMiddleware

    assert CovenantMiddleware is not None


@pytest.mark.asyncio
async def test_covenant_middleware_inherits_from_fastmcp_middleware():
    """Verify CovenantMiddleware inherits from FastMCP Middleware when available."""
    from daem0nmcp.transforms.covenant import (
        _FASTMCP_MIDDLEWARE_AVAILABLE,
        CovenantMiddleware,
    )

    if _FASTMCP_MIDDLEWARE_AVAILABLE:
        from fastmcp.server.middleware import Middleware

        assert issubclass(CovenantMiddleware, Middleware)
    else:
        # When FastMCP middleware is not available, it falls back to object
        assert True  # Just verify it can be imported


@pytest.mark.asyncio
async def test_covenant_middleware_has_on_call_tool():
    """Verify CovenantMiddleware has on_call_tool method."""
    from daem0nmcp.transforms.covenant import CovenantMiddleware

    middleware = CovenantMiddleware(get_state=lambda p: None)
    assert hasattr(middleware, "on_call_tool")
    assert callable(middleware.on_call_tool)


@pytest.mark.asyncio
async def test_covenant_middleware_blocks_via_transform():
    """Verify CovenantMiddleware uses CovenantTransform for enforcement."""
    import json

    from daem0nmcp.transforms.covenant import (
        _FASTMCP_MIDDLEWARE_AVAILABLE,
        CovenantMiddleware,
    )

    if not _FASTMCP_MIDDLEWARE_AVAILABLE:
        pytest.skip("FastMCP 3.0 middleware not available")

    # Create middleware with unbriefed state
    middleware = CovenantMiddleware(
        get_state=lambda p: {"briefed": False, "context_checks": []}
    )

    # Create mock context and call_next
    from mcp import types as mt

    mock_message = mt.CallToolRequestParams(
        name="remember", arguments={"project_path": "/test/project", "content": "test"}
    )

    class MockContext:
        message = mock_message

    mock_call_next = AsyncMock()

    # Call on_call_tool
    result = await middleware.on_call_tool(MockContext(), mock_call_next)

    # Should be blocked - FastMCP 3.0 returns ToolResult, not raw list
    from fastmcp.tools import ToolResult

    assert result is not None
    assert isinstance(result, ToolResult)
    assert len(result.content) == 1
    assert result.content[0].type == "text"

    # Parse the JSON text to verify violation
    violation = json.loads(result.content[0].text)
    assert violation["status"] == "blocked"
    assert violation["violation"] == "COMMUNION_REQUIRED"

    # call_next should NOT have been called
    mock_call_next.assert_not_called()


@pytest.mark.asyncio
async def test_covenant_middleware_allows_valid_requests():
    """Verify CovenantMiddleware allows requests that satisfy covenant."""
    from daem0nmcp.transforms.covenant import (
        _FASTMCP_MIDDLEWARE_AVAILABLE,
        CovenantMiddleware,
    )

    if not _FASTMCP_MIDDLEWARE_AVAILABLE:
        pytest.skip("FastMCP 3.0 middleware not available")

    # Create middleware with briefed state and fresh counsel
    middleware = CovenantMiddleware(
        get_state=lambda p: {
            "briefed": True,
            "context_checks": [
                {
                    "topic": "remember",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ],
        }
    )

    # Create mock context and call_next
    from mcp import types as mt

    mock_message = mt.CallToolRequestParams(
        name="remember", arguments={"project_path": "/test/project", "content": "test"}
    )

    class MockContext:
        message = mock_message

    expected_result = [{"type": "text", "text": "Success!"}]
    mock_call_next = AsyncMock(return_value=expected_result)
    mock_context = MockContext()

    # Call on_call_tool
    result = await middleware.on_call_tool(mock_context, mock_call_next)

    # Should be allowed
    assert result == expected_result

    # call_next SHOULD have been called with the full context
    mock_call_next.assert_called_once_with(mock_context)


@pytest.mark.asyncio
async def test_covenant_middleware_allows_exempt_tools():
    """Verify CovenantMiddleware allows exempt tools without checking state."""
    from daem0nmcp.transforms.covenant import (
        _FASTMCP_MIDDLEWARE_AVAILABLE,
        CovenantMiddleware,
    )

    if not _FASTMCP_MIDDLEWARE_AVAILABLE:
        pytest.skip("FastMCP 3.0 middleware not available")

    # Create middleware - state callback returns unbriefed
    middleware = CovenantMiddleware(
        get_state=lambda p: {"briefed": False, "context_checks": []}
    )

    # Create mock context for get_briefing (exempt tool)
    from mcp import types as mt

    mock_message = mt.CallToolRequestParams(
        name="get_briefing", arguments={"project_path": "/test/project"}
    )

    class MockContext:
        message = mock_message

    expected_result = [{"type": "text", "text": "Briefing data..."}]
    mock_call_next = AsyncMock(return_value=expected_result)
    mock_context = MockContext()

    # Call on_call_tool
    result = await middleware.on_call_tool(mock_context, mock_call_next)

    # Should be allowed even though not briefed
    assert result == expected_result
    mock_call_next.assert_called_once_with(mock_context)


# ============================================================================
# SERVER INTEGRATION TESTS
# ============================================================================


def test_server_has_covenant_middleware():
    """Verify server has CovenantMiddleware registered."""
    from daem0nmcp.transforms.covenant import _FASTMCP_MIDDLEWARE_AVAILABLE

    if not _FASTMCP_MIDDLEWARE_AVAILABLE:
        pytest.skip("FastMCP 3.0 middleware not available")

    from daem0nmcp.server import _covenant_middleware

    # Verify middleware exists
    assert _covenant_middleware is not None

    # Verify it's a CovenantMiddleware instance
    from daem0nmcp.transforms.covenant import CovenantMiddleware

    assert isinstance(_covenant_middleware, CovenantMiddleware)


def test_server_middleware_callback_available():
    """Verify server provides state callback for middleware."""
    from daem0nmcp.server import _get_context_state_for_middleware

    # Callback should exist
    assert _get_context_state_for_middleware is not None
    assert callable(_get_context_state_for_middleware)

    # Should return None for non-existent project
    result = _get_context_state_for_middleware("/non/existent/path")
    assert result is None

    # Should return None for None project_path
    result = _get_context_state_for_middleware(None)
    assert result is None


def test_fastmcp_middleware_flag_available():
    """Verify the _FASTMCP_MIDDLEWARE_AVAILABLE flag is exported."""
    from daem0nmcp.transforms.covenant import _FASTMCP_MIDDLEWARE_AVAILABLE

    # Should be a boolean
    assert isinstance(_FASTMCP_MIDDLEWARE_AVAILABLE, bool)

    # With fastmcp>=3.0.0b1 installed, should be True
    assert _FASTMCP_MIDDLEWARE_AVAILABLE is True


@pytest.mark.asyncio
async def test_server_integration_briefing_enables_tools(tmp_path):
    """Full integration test: get_briefing should enable tools via middleware."""
    from daem0nmcp import server

    # Clear any existing contexts
    server._project_contexts.clear()

    project_path = str(tmp_path)

    # Before briefing, state should be None or unbriefed
    state = server._get_context_state_for_middleware(project_path)
    # State is None because project context doesn't exist yet
    assert state is None

    # Call get_briefing to initialize and brief the project
    result = await server.get_briefing(project_path=project_path)
    assert result is not None

    # After briefing, state should show briefed=True
    state = server._get_context_state_for_middleware(project_path)
    assert state is not None
    assert state["briefed"] is True


@pytest.mark.asyncio
async def test_server_integration_context_check_enables_counsel(tmp_path):
    """Full integration test: context_check should add counsel."""
    from daem0nmcp import server

    # Clear any existing contexts
    server._project_contexts.clear()

    project_path = str(tmp_path)

    # First, call get_briefing
    await server.get_briefing(project_path=project_path)

    # Before context_check, context_checks should be empty
    state = server._get_context_state_for_middleware(project_path)
    assert state["context_checks"] == []

    # Call context_check
    result = await server.context_check(
        description="About to remember something", project_path=project_path
    )
    assert result is not None

    # After context_check, context_checks should have an entry
    state = server._get_context_state_for_middleware(project_path)
    assert len(state["context_checks"]) > 0
    assert "timestamp" in state["context_checks"][0]
