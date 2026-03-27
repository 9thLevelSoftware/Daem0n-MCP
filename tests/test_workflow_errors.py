"""Tests for workflow error handling."""


class TestWorkflowErrors:
    """Verify workflow error types are importable and well-formed."""

    def test_workflow_error_exists(self):
        """WorkflowError base class should be importable."""
        from daem0nmcp.workflows.errors import WorkflowError

        assert issubclass(WorkflowError, Exception)

    def test_invalid_action_error(self):
        """InvalidActionError should provide action and valid_actions."""
        from daem0nmcp.workflows.errors import InvalidActionError

        err = InvalidActionError("foo", ["bar", "baz"])
        assert "foo" in str(err)
        assert "bar" in str(err)
        assert err.action == "foo"
        assert err.valid_actions == ["bar", "baz"]

    def test_missing_param_error(self):
        """MissingParamError should provide param name and action."""
        from daem0nmcp.workflows.errors import MissingParamError

        err = MissingParamError("topic", "recall")
        assert "topic" in str(err)
        assert "recall" in str(err)
        assert err.param == "topic"
        assert err.action == "recall"

    def test_workflow_error_has_recovery_hint(self):
        """Errors should provide recovery_hint for user guidance."""
        from daem0nmcp.workflows.errors import InvalidActionError

        err = InvalidActionError("foo", ["bar"])
        assert hasattr(err, "recovery_hint")
        assert err.recovery_hint is not None
