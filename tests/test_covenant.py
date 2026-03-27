"""Tests for Sacred Covenant enforcement."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from daem0nmcp.covenant import (
    COMMUNION_REQUIRED_TOOLS,
    COUNSEL_REQUIRED_TOOLS,
    COVENANT_EXEMPT_TOOLS,
    CovenantEnforcer,
    CovenantViolation,
    PreflightToken,
)


class TestCovenantViolation:
    """Test the CovenantViolation response structure."""

    def test_communion_required_response(self):
        """COMMUNION_REQUIRED should include remedy."""
        response = CovenantViolation.communion_required("test_project")
        assert response["status"] == "blocked"
        assert response["violation"] == "COMMUNION_REQUIRED"
        assert "remedy" in response
        assert response["remedy"]["tool"] == "get_briefing"

    def test_counsel_required_response(self):
        """COUNSEL_REQUIRED should include action description."""
        response = CovenantViolation.counsel_required("remember", "test_project")
        assert response["status"] == "blocked"
        assert response["violation"] == "COUNSEL_REQUIRED"
        assert "remember" in response["message"]
        assert response["remedy"]["tool"] == "context_check"


class TestPreflightToken:
    """Test preflight token generation and validation."""

    def test_token_generation(self):
        """Token should be generated with signature."""
        token = PreflightToken.issue(
            action="editing src/auth.py",
            session_id="abc123-2025010112",
            project_path="/test/project",
        )
        assert token.action == "editing src/auth.py"
        assert token.session_id == "abc123-2025010112"
        assert token.signature is not None
        assert len(token.signature) == 64  # SHA256 hex

    def test_token_expiry(self):
        """Token should expire after TTL."""
        token = PreflightToken.issue(
            action="test",
            session_id="abc123",
            project_path="/test",
            ttl_seconds=1,
        )
        assert not token.is_expired()

        # Manually expire
        token.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        assert token.is_expired()

    def test_token_verification(self):
        """Valid token should verify successfully."""
        token = PreflightToken.issue(
            action="editing src/auth.py",
            session_id="abc123",
            project_path="/test",
        )
        serialized = token.serialize()

        verified = PreflightToken.verify(serialized, project_path="/test")
        assert verified is not None
        assert verified.action == "editing src/auth.py"

    def test_token_tamper_detection(self):
        """Tampered token should fail verification."""
        token = PreflightToken.issue(
            action="editing src/auth.py",
            session_id="abc123",
            project_path="/test",
        )
        serialized = token.serialize()

        # Tamper with the token
        import json

        data = json.loads(serialized)
        data["action"] = "malicious action"
        tampered = json.dumps(data)

        verified = PreflightToken.verify(tampered, project_path="/test")
        assert verified is None


class TestCovenantEnforcer:
    """Test the enforcement decorators."""

    @pytest.fixture
    def mock_session_state(self):
        """Create mock session state."""
        return {
            "session_id": "abc123-2025010112",
            "project_path": "/test/project",
            "briefed": False,
            "context_checks": [],
            "pending_decisions": [],
        }

    @pytest.fixture
    def briefed_session_state(self, mock_session_state):
        """Session that has been briefed."""
        mock_session_state["briefed"] = True
        return mock_session_state

    @pytest.fixture
    def counseled_session_state(self, briefed_session_state):
        """Session with context check performed."""
        briefed_session_state["context_checks"] = [
            {"topic": "remember", "timestamp": datetime.now(timezone.utc).isoformat()}
        ]
        return briefed_session_state

    @pytest.mark.asyncio
    async def test_requires_communion_blocks_unbriefed(self, mock_session_state):
        """Tool should be blocked if not briefed."""
        enforcer = CovenantEnforcer()

        with patch.object(
            enforcer, "_get_session_state", return_value=mock_session_state
        ):
            result = await enforcer.check_communion("/test/project")

        assert result is not None
        assert result["violation"] == "COMMUNION_REQUIRED"

    @pytest.mark.asyncio
    async def test_requires_communion_allows_briefed(self, briefed_session_state):
        """Tool should be allowed if briefed."""
        enforcer = CovenantEnforcer()

        with patch.object(
            enforcer, "_get_session_state", return_value=briefed_session_state
        ):
            result = await enforcer.check_communion("/test/project")

        assert result is None  # No violation

    @pytest.mark.asyncio
    async def test_requires_counsel_blocks_without_check(self, briefed_session_state):
        """Tool should be blocked if no recent context_check."""
        enforcer = CovenantEnforcer()

        with patch.object(
            enforcer, "_get_session_state", return_value=briefed_session_state
        ):
            result = await enforcer.check_counsel("remember", "/test/project")

        assert result is not None
        assert result["violation"] == "COUNSEL_REQUIRED"

    @pytest.mark.asyncio
    async def test_requires_counsel_allows_with_check(self, counseled_session_state):
        """Tool should be allowed with recent context_check."""
        enforcer = CovenantEnforcer()

        with patch.object(
            enforcer, "_get_session_state", return_value=counseled_session_state
        ):
            result = await enforcer.check_counsel("remember", "/test/project")

        assert result is None  # No violation

    def test_exempt_tools_list(self):
        """get_briefing and health should be exempt."""
        assert "get_briefing" in COVENANT_EXEMPT_TOOLS
        assert "health" in COVENANT_EXEMPT_TOOLS

    def test_communion_required_tools_list(self):
        """Mutating tools should require communion."""
        assert "remember" in COMMUNION_REQUIRED_TOOLS
        assert "remember_batch" in COMMUNION_REQUIRED_TOOLS
        assert "add_rule" in COMMUNION_REQUIRED_TOOLS
        assert "update_rule" in COMMUNION_REQUIRED_TOOLS
        assert "record_outcome" in COMMUNION_REQUIRED_TOOLS
        assert "prune_memories" in COMMUNION_REQUIRED_TOOLS

    def test_counsel_required_tools_list(self):
        """Mutating tools should require counsel."""
        assert "remember" in COUNSEL_REQUIRED_TOOLS
        assert "remember_batch" in COUNSEL_REQUIRED_TOOLS
        assert "add_rule" in COUNSEL_REQUIRED_TOOLS
        assert "prune_memories" in COUNSEL_REQUIRED_TOOLS


class TestPreflightTokenIntegration:
    """Test preflight token in context_check response."""

    @pytest.fixture
    def db_manager(self, tmp_path):
        from daem0nmcp.database import DatabaseManager

        return DatabaseManager(str(tmp_path / "storage"))

    @pytest.mark.asyncio
    async def test_context_check_returns_preflight_token(self, db_manager):
        """context_check should include a preflight_token."""
        await db_manager.init_db()

        from daem0nmcp import server

        server._project_contexts.clear()

        project_path = str(db_manager.storage_path.parent.parent)

        # Briefing first
        await server.get_briefing(project_path=project_path)

        # Mock recall and check_rules to avoid vector dimension issues
        ctx = await server.get_project_context(project_path)
        with (
            patch.object(ctx.memory_manager, "recall", return_value={}),
            patch.object(ctx.rules_engine, "check_rules", return_value={}),
        ):
            # context_check should return token
            result = await server.context_check(
                description="About to edit auth.py",
                project_path=project_path,
            )

        assert "preflight_token" in result

        # Verify token is valid
        from daem0nmcp.covenant import PreflightToken

        token = PreflightToken.verify(result["preflight_token"], project_path)
        assert token is not None
        assert token.action == "About to edit auth.py"
