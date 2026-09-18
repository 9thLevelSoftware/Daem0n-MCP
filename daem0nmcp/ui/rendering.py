"""Single secure HTML, CSP, asset, and compatibility-URI boundary."""

from __future__ import annotations

import base64
import hashlib
import json
import math
from dataclasses import dataclass
from functools import cache
from importlib import resources
from string import Template
from typing import Any, Final
from urllib.parse import quote

from .payloads import InvalidAppPayload, normalize_app_payload

MCP_APPS_MIME: Final = "text/html;profile=mcp-app"
MAX_COMPAT_JSON_BYTES: Final = 65_536
MAX_COMPAT_URI_CHARS: Final = 262_144
_INVALID_PAYLOAD_MESSAGE: Final = "invalid UI resource payload"


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise UIResourcePayloadError(_INVALID_PAYLOAD_MESSAGE)
    return parsed


@dataclass(frozen=True)
class AppSpec:
    app_id: str
    title: str
    resource_uri: str
    resource_name: str
    description: str
    scripts: tuple[str, ...]
    styles: tuple[str, ...] = ("daemon.css",)


APP_SPECS: Final[dict[str, AppSpec]] = {
    "test": AppSpec(
        "test",
        "Daem0n UI",
        "ui://daem0n/test",
        "Test UI",
        "Test UI to validate MCP Apps infrastructure",
        ("runtime.js", "renderers/test.js"),
    ),
    "search": AppSpec(
        "search",
        "Daem0n Search Results",
        "ui://daem0n/search",
        "Search Results",
        "Visual search results with filtering and score insights",
        ("messenger.js", "runtime.js", "renderers/search.js"),
    ),
    "briefing": AppSpec(
        "briefing",
        "Daem0n Session Briefing",
        "ui://daem0n/briefing",
        "Session Briefing",
        "Briefing dashboard with session context",
        ("messenger.js", "runtime.js", "renderers/briefing.js"),
    ),
    "covenant": AppSpec(
        "covenant",
        "Daem0n Covenant Status",
        "ui://daem0n/covenant",
        "Covenant Status",
        "Sacred Covenant state dashboard",
        ("messenger.js", "runtime.js", "renderers/covenant.js"),
    ),
    "community": AppSpec(
        "community",
        "Daem0n Community Map",
        "ui://daem0n/community",
        "Community Cluster Map",
        "Interactive community treemap",
        ("d3.bundle.js", "messenger.js", "runtime.js", "renderers/community.js"),
    ),
    "graph": AppSpec(
        "graph",
        "Daem0n Memory Graph",
        "ui://daem0n/graph",
        "Memory Graph",
        "Interactive memory graph viewer",
        ("d3.bundle.js", "messenger.js", "runtime.js", "renderers/graph.js"),
    ),
}


class UIResourcePayloadError(ValueError):
    """Stable, input-redacting compatibility-resource error."""


def serialize_app_data(data: dict[str, Any]) -> str:
    """Serialize compact JSON with raw-text-element delimiters escaped."""
    raw = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    return raw.translate(
        str.maketrans(
            {
                "<": "\\u003c",
                ">": "\\u003e",
                "&": "\\u0026",
                "\u2028": "\\u2028",
                "\u2029": "\\u2029",
            }
        )
    )


@cache
def _load_package_text(kind: str, name: str) -> str:
    parts = name.split("/")
    asset = resources.files("daem0nmcp.ui").joinpath(kind, *parts)
    try:
        text = asset.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, OSError) as error:
        raise RuntimeError("required UI asset is unavailable") from error
    if not text:
        raise RuntimeError("required UI asset is unavailable")
    closing = "</script" if kind == "static" and name.endswith(".js") else "</style"
    if (
        kind == "static"
        and (name.endswith(".js") or name.endswith(".css"))
        and closing in text.lower()
    ):
        raise RuntimeError("required UI asset is unsafe")
    return text


def _digest(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return "'sha256-" + base64.b64encode(digest).decode("ascii") + "'"


def _csp(scripts: list[str], styles: list[str]) -> str:
    directives = [
        "default-src 'none'",
        "base-uri 'none'",
        "object-src 'none'",
        "connect-src 'none'",
        "form-action 'none'",
        "frame-src 'none'",
        "worker-src 'none'",
        "font-src 'none'",
        "media-src 'none'",
        "manifest-src 'none'",
        "img-src data: blob:",
        "script-src " + " ".join(_digest(source) for source in scripts),
        "script-src-attr 'none'",
        "style-src " + " ".join(_digest(source) for source in styles),
        "style-src-attr 'none'",
    ]
    return ";".join(directives)


def render_app_document(app_id: str, data: dict[str, Any]) -> str:
    """Render the sole owned shell for a fixed app and normalized payload."""
    try:
        spec = APP_SPECS[app_id]
    except KeyError as error:
        raise InvalidAppPayload("invalid app payload") from error
    normalized = normalize_app_payload(app_id, data)
    serialized = serialize_app_data(normalized)
    script_sources = [_load_package_text("static", name) for name in spec.scripts]
    style_sources = [_load_package_text("static", name) for name in spec.styles]
    styles = "".join(
        f'<style data-asset="{name}">{source}</style>'
        for name, source in zip(spec.styles, style_sources, strict=True)
    )
    scripts = "".join(
        f'<script data-asset="{name}">{source}</script>'
        for name, source in zip(spec.scripts, script_sources, strict=True)
    )
    shell = _load_package_text("templates", "app.html")
    return Template(shell).substitute(
        APP_ID=spec.app_id,
        CSP=_csp(script_sources, style_sources),
        TITLE=spec.title,
        STYLES=styles,
        APP_DATA=serialized,
        SCRIPTS=scripts,
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise UIResourcePayloadError(_INVALID_PAYLOAD_MESSAGE)
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise UIResourcePayloadError(_INVALID_PAYLOAD_MESSAGE)


def _validate_shape(root: dict[str, Any]) -> None:
    members = 0
    stack: list[tuple[Any, int]] = [(root, 1)]
    while stack:
        value, depth = stack.pop()
        if isinstance(value, dict):
            if depth > 16:
                raise UIResourcePayloadError(_INVALID_PAYLOAD_MESSAGE)
            members += len(value)
            for key, child in value.items():
                key.encode("utf-8")
                if isinstance(child, str):
                    child.encode("utf-8")
                elif isinstance(child, (dict, list)):
                    stack.append((child, depth + 1))
        elif isinstance(value, list):
            if depth > 16:
                raise UIResourcePayloadError(_INVALID_PAYLOAD_MESSAGE)
            members += len(value)
            for child in value:
                if isinstance(child, str):
                    child.encode("utf-8")
                elif isinstance(child, (dict, list)):
                    stack.append((child, depth + 1))
        if members > 10_000:
            raise UIResourcePayloadError(_INVALID_PAYLOAD_MESSAGE)


def parse_compat_payload(data: str) -> dict[str, Any]:
    """Validate one framework-decoded JSON object without URL-decoding again."""
    try:
        if (
            not isinstance(data, str)
            or not data
            or len(data.encode("utf-8")) > MAX_COMPAT_JSON_BYTES
        ):
            raise UIResourcePayloadError(_INVALID_PAYLOAD_MESSAGE)
        parsed = json.loads(
            data,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
            parse_float=_parse_finite_float,
        )
        if type(parsed) is not dict:
            raise UIResourcePayloadError(_INVALID_PAYLOAD_MESSAGE)
        _validate_shape(parsed)
        return parsed
    except UIResourcePayloadError:
        raise
    except (
        json.JSONDecodeError,
        UnicodeError,
        RecursionError,
        ValueError,
        TypeError,
    ) as error:
        raise UIResourcePayloadError(_INVALID_PAYLOAD_MESSAGE) from error


def build_compat_ui_uri(app_id: str, data: dict[str, Any]) -> str | None:
    """Return a bounded single-segment compatibility URI, or a base fallback."""
    try:
        spec = APP_SPECS[app_id]
        normalized = normalize_app_payload(app_id, data)
        raw = json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        if len(raw.encode("utf-8")) > MAX_COMPAT_JSON_BYTES:
            return None
        uri = f"{spec.resource_uri}/{quote(raw, safe='')}"
        return uri if len(uri) <= MAX_COMPAT_URI_CHARS else None
    except (InvalidAppPayload, KeyError, TypeError, ValueError, UnicodeError):
        return None


__all__ = [
    "APP_SPECS",
    "MCP_APPS_MIME",
    "MAX_COMPAT_JSON_BYTES",
    "MAX_COMPAT_URI_CHARS",
    "AppSpec",
    "UIResourcePayloadError",
    "build_compat_ui_uri",
    "parse_compat_payload",
    "render_app_document",
    "serialize_app_data",
]
