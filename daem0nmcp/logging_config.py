"""Structured logging configuration for Daem0nMCP."""

import inspect
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from functools import wraps

# Context variable for request tracking
request_id_var: ContextVar[str] = ContextVar("request_id", default="")
_release_callback: Callable[[], Awaitable[None]] | None = None


class StructuredFormatter(logging.Formatter):
    """JSON-structured log formatter."""

    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(""),
        }

        # Add extra fields
        if hasattr(record, "duration_ms"):
            log_data["duration_ms"] = record.duration_ms
        if hasattr(record, "tool_name"):
            log_data["tool_name"] = record.tool_name

        return json.dumps(log_data)


def with_request_id(func: Callable) -> Callable:
    """Decorator to add request ID to tool calls."""

    @wraps(func)
    async def wrapper(*args, **kwargs):
        request_id = str(uuid.uuid4())[:8]
        token = request_id_var.set(request_id)
        start = time.perf_counter()

        try:
            result = await func(*args, **kwargs)
            return result
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            logger = logging.getLogger(func.__module__)
            logger.info(
                "Tool completed",
                extra={
                    "duration_ms": round(duration_ms, 2),
                    "tool_name": func.__name__,
                },
            )
            if _release_callback:
                maybe_coro = _release_callback()
                if inspect.isawaitable(maybe_coro):
                    await maybe_coro
            request_id_var.reset(token)

    return wrapper


def set_release_callback(callback: Callable[[], Awaitable[None]]) -> None:
    """Register a callback to release per-request resources."""
    global _release_callback
    _release_callback = callback
