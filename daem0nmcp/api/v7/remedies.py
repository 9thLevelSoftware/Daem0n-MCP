"""Compact exact counsel payloads without changing canonical admission inputs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from pydantic import BaseModel

_BOUND_FIELDS = frozenset(
    {"workspace_id", "preflight_token", "challenge_token", "idempotency_key"}
)


def _json(value: Any) -> str:
    # Python equality treats True == 1 == 1.0; capability arguments must keep
    # those JSON types distinct. No fallback serializer or coercion is allowed.
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def compact_remedy_arguments(
    model: type[BaseModel], arguments: Mapping[str, Any]
) -> dict[str, Any]:
    """Omit only provably redundant defaults from a JSON-ready exact remedy.

    Capability issuance and validation still use the full canonical request.
    Unknown/data-dependent factories prevent even the baseline validation
    from being attempted, including when aliases could hide supplied values.
    Any failure to prove equivalence retains the original payload.
    """
    original = dict(arguments)
    try:
        fields = model.model_fields
        if any(
            field.default_factory is not None
            and field.default_factory is not list
            and field.default_factory is not dict
            for field in fields.values()
        ):
            return original
        canonical = _json(
            model.model_validate(deepcopy(original)).model_dump(mode="json")
        )
        result = dict(original)
        for name, field in fields.items():
            if name in _BOUND_FIELDS or name not in result or field.is_required():
                continue
            if field.default_factory is list:
                default: Any = []
            elif field.default_factory is dict:
                default = {}
            elif field.default_factory is not None:
                continue
            else:
                default = field.default
            if _json(result[name]) != _json(default):
                continue
            candidate = {key: value for key, value in result.items() if key != name}
            normalized = model.model_validate(deepcopy(candidate)).model_dump(
                mode="json"
            )
            if _json(normalized) == canonical:
                result = candidate
        return result
    except Exception:
        # This is a presentation optimization, never a new admission path.
        return original


__all__ = ["compact_remedy_arguments"]
