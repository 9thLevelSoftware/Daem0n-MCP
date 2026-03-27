"""Unified error handling for workflow tools."""



class WorkflowError(Exception):
    """Base class for workflow errors."""

    def __init__(self, message: str, recovery_hint: str | None = None):
        super().__init__(message)
        self.recovery_hint = (
            recovery_hint or "Check the action parameter and try again."
        )


class InvalidActionError(WorkflowError):
    """Raised when an invalid action is requested."""

    def __init__(self, action: str, valid_actions: list[str]):
        self.action = action
        self.valid_actions = valid_actions
        message = (
            f"Invalid action '{action}'. Valid actions: {', '.join(valid_actions)}"
        )
        hint = f"Use one of: {', '.join(valid_actions)}"
        super().__init__(message, recovery_hint=hint)


class MissingParamError(WorkflowError):
    """Raised when a required parameter is missing for an action."""

    def __init__(self, param: str, action: str):
        self.param = param
        self.action = action
        message = f"Missing required parameter '{param}' for action '{action}'"
        hint = f"Provide the '{param}' parameter when using action='{action}'"
        super().__init__(message, recovery_hint=hint)
