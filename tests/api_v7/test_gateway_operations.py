from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastmcp import Client

from daem0nmcp.api.v7.gateway_operations import (
    CORE_LISTED_TOOLS,
    GATEWAY_TOOL_NAMES,
    bind_gateway_handlers,
)
from daem0nmcp.api.v7.models import ErrorCode, FieldError
from daem0nmcp.api.v7.policy import V7_TOOL_LEVELS
from daem0nmcp.api.v7.production import build_production_surface, create_v7_server
from daem0nmcp.api.v7.responses import ResponseFactory
from daem0nmcp.config import Settings
from tests.api_v7.process_client import initialize_workspaces


class GatewayOperationsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        environment = patch.dict(os.environ, {"DAEM0NMCP_PROJECT_ROOT": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        [self.workspace] = await initialize_workspaces((self.root,))
        self.settings = Settings(
            project_root=str(self.root),
            workspace_roots=[str(self.root)],
            tool_surface="core",
            covenant_mode="guided",
            dream_enabled=False,
        )
        self.surface = build_production_surface(
            "stdio", settings=self.settings, environ={}
        )
        self.server = self.surface.build_server()
        self.scope = {"workspace_id": self.workspace.workspace_id}

    async def test_core_lists_nine_and_full_lists_all_registered_tools(self) -> None:
        self.assertEqual(9, len(CORE_LISTED_TOOLS))
        async with Client(self.server) as client:
            self.assertEqual(
                CORE_LISTED_TOOLS, {tool.name for tool in await client.list_tools()}
            )
        full = build_production_surface(
            "stdio",
            settings=self.settings.model_copy(update={"tool_surface": "full"}),
            environ={},
        ).build_server()
        async with Client(full) as client:
            names = {tool.name for tool in await client.list_tools()}
            self.assertEqual(77, len(names))
            self.assertEqual(set(V7_TOOL_LEVELS), names)

    async def test_search_describes_hidden_pruning_tools(self) -> None:
        async with Client(self.server) as client:
            result = await client.call_tool(
                "daem0n_tools_search", {"query": "prune old memories"}
            )
            envelope = result.structured_content
            self.assertTrue(envelope["ok"], envelope)
            descriptors = {tool["name"]: tool for tool in envelope["data"]["tools"]}
            self.assertTrue(GATEWAY_TOOL_NAMES.isdisjoint(descriptors))
            for name, covenant in (
                ("memory_prune_preview", "communion"),
                ("memory_prune", "destructive"),
            ):
                with self.subTest(tool=name):
                    descriptor = descriptors[name]
                    self.assertEqual(covenant, descriptor["covenant"])
                    self.assertFalse(descriptor["listed"])
                    self.assertEqual(
                        name == "memory_prune_preview", descriptor["read_only"]
                    )
                    self.assertIn(
                        "workspace_id", descriptor["input_schema"]["required"]
                    )
            empty = await client.call_tool(
                "daem0n_tools_search", {"query": "zzzzunmatchedcapability"}
            )
            self.assertEqual([], empty.structured_content["data"]["tools"])

    async def test_gateway_search_text_and_direct_hidden_call(self) -> None:
        async with Client(self.server) as client:
            result = await client.call_tool(
                "daem0n_tool_call",
                {
                    **self.scope,
                    "tool": "memory_search_text",
                    "arguments": {"query": "x"},
                },
            )
            envelope = result.structured_content
            self.assertTrue(envelope["ok"], envelope)
            self.assertEqual("memory_search_text", envelope["data"]["tool"])
            self.assertIsNotNone(envelope["meta"]["covenant"]["auto_brief"])
            direct = await client.call_tool(
                "memory_search_text", {**self.scope, "query": "x"}
            )
            self.assertTrue(direct.structured_content["ok"], direct.structured_content)
            self.assertEqual(
                envelope["data"]["data"], direct.structured_content["data"]
            )

    async def test_gateway_preserves_stored_user_text_with_host_path(self) -> None:
        content = r"hostpathroundtrip documents C:\proj\x.py as user-authored text."
        server = create_v7_server("stdio", settings=self.settings, environ={})
        async with Client(server) as client:
            stored = await client.call_tool(
                "memory_store",
                {
                    **self.scope,
                    "record_type": "decision",
                    "content": content,
                    "idempotency_key": "gateway-user-path-roundtrip",
                },
            )
            stored_envelope = stored.structured_content
            self.assertTrue(stored_envelope["ok"], stored_envelope)
            arguments = {"query": "hostpathroundtrip"}
            direct = await client.call_tool(
                "memory_search_text", {**self.scope, **arguments}
            )
            direct_envelope = direct.structured_content
            self.assertTrue(direct_envelope["ok"], direct_envelope)
            gateway = await client.call_tool(
                "daem0n_tool_call",
                {
                    **self.scope,
                    "tool": "memory_search_text",
                    "arguments": arguments,
                },
            )
            envelope = gateway.structured_content
            self.assertTrue(envelope["ok"], envelope)
            self.assertEqual(direct_envelope["data"], envelope["data"]["data"])
            hits = envelope["data"]["data"]["items"]
            self.assertEqual(1, len(hits))
            self.assertEqual(content, hits[0]["bounded_excerpt"])
            self.assertEqual(
                stored_envelope["data"]["record"]["record_id"],
                hits[0]["record"]["record_id"],
            )

    async def test_unknown_recursion_and_nested_workspace_are_rejected(self) -> None:
        async with Client(self.server) as client:
            cases = [
                ("not_a_registered_tool", {}, "tool", "UNKNOWN_TOOL"),
                ("daem0n_tool_call", {}, "tool", "UNKNOWN_TOOL"),
                ("daem0n_tools_search", {}, "tool", "UNKNOWN_TOOL"),
                (
                    "memory_search_text",
                    {**self.scope, "query": "x"},
                    "arguments.workspace_id",
                    "WORKSPACE_ID_TOP_LEVEL",
                ),
                ("memory_search_text", {}, "arguments.query", "INVALID_VALUE"),
                (
                    "memory_search_text",
                    {"query": "x", "limit": 0},
                    "arguments.limit",
                    "INVALID_VALUE",
                ),
            ]
            for tool, arguments, field, code in cases:
                with self.subTest(tool=tool, arguments=arguments):
                    result = await client.call_tool(
                        "daem0n_tool_call",
                        {**self.scope, "tool": tool, "arguments": arguments},
                        raise_on_error=False,
                    )
                    envelope = result.structured_content
                    self.assertFalse(envelope["ok"], envelope)
                    self.assertEqual("INVALID_ARGUMENT", envelope["error"]["code"])
                    self.assertEqual(
                        field, envelope["error"]["field_errors"][0]["field"]
                    )
                    self.assertEqual(code, envelope["error"]["field_errors"][0]["code"])

    async def test_unsafe_extra_argument_keys_return_sanitized_field_errors(
        self,
    ) -> None:
        async with Client(self.server) as client:
            for key in (
                "plain neutral words",
                r"C:\private\host-location.py",
                str(self.root / "private-location.py"),
                "/health",
                "a" * 256,
            ):
                with self.subTest(key=key):
                    result = await client.call_tool(
                        "daem0n_tool_call",
                        {
                            **self.scope,
                            "tool": "memory_search_text",
                            "arguments": {"query": "x", key: "unexpected"},
                        },
                        raise_on_error=False,
                    )
                    envelope = result.structured_content
                    self.assertFalse(envelope["ok"], envelope)
                    self.assertEqual("INVALID_ARGUMENT", envelope["error"]["code"])
                    self.assertTrue(envelope["error"]["field_errors"])
                    for error in envelope["error"]["field_errors"]:
                        diagnostic = FieldError.model_validate(error)
                        self.assertEqual("arguments", diagnostic.field)
                        self.assertEqual("INVALID_VALUE", diagnostic.code)
                    self.assertNotIn(json.dumps(key)[1:-1], json.dumps(envelope))

    async def test_destructive_challenge_retries_exact_surface_remedy(self) -> None:
        full_server = build_production_surface(
            "stdio",
            settings=self.settings.model_copy(update={"tool_surface": "full"}),
            environ={},
        ).build_server()
        selection = {
            "older_than_days": 30,
            "categories": ["decision"],
            "min_recall_count": 2,
            "protect_successful": False,
        }
        for surface, server in (("core", self.server), ("full", full_server)):
            with self.subTest(surface=surface):
                async with Client(server) as client:
                    preview = await client.call_tool(
                        "daem0n_tool_call",
                        {
                            **self.scope,
                            "tool": "memory_prune_preview",
                            "arguments": selection,
                        },
                    )
                    preview_envelope = preview.structured_content
                    self.assertTrue(preview_envelope["ok"], preview_envelope)
                    arguments = {
                        **selection,
                        "selection_token": preview_envelope["data"]["data"][
                            "selection_token"
                        ],
                    }
                    self.assertNotIn("preflight_token", arguments)
                    challenged = await client.call_tool(
                        "daem0n_tool_call",
                        {**self.scope, "tool": "memory_prune", "arguments": arguments},
                        raise_on_error=False,
                    )
                    envelope = challenged.structured_content
                    self.assertFalse(envelope["ok"], envelope)
                    self.assertEqual("COUNSEL_REQUIRED", envelope["error"]["code"])
                    counsel = envelope["error"]["counsel"]
                    self.assertTrue(counsel["preflight_token"])
                    self.assertIn("DESTRUCTIVE_OPERATION", counsel["reasons"])
                    self.assertEqual(
                        envelope["meta"]["request_id"],
                        envelope["error"]["correlation_id"],
                    )
                    remedy = envelope["error"]["remedy"]
                    target_arguments = {
                        **arguments,
                        "preflight_token": counsel["preflight_token"],
                    }
                    if surface == "core":
                        self.assertEqual(
                            {
                                "tool": "daem0n_tool_call",
                                "arguments": {
                                    **self.scope,
                                    "tool": "memory_prune",
                                    "arguments": target_arguments,
                                },
                            },
                            remedy,
                        )
                    else:
                        self.assertEqual(
                            {
                                "tool": "memory_prune",
                                "arguments": {**self.scope, **target_arguments},
                            },
                            remedy,
                        )
                    retried = await client.call_tool(
                        remedy["tool"], remedy["arguments"]
                    )
                    self.assertTrue(
                        retried.structured_content["ok"], retried.structured_content
                    )
                    if surface == "core":
                        self.assertEqual(
                            "memory_prune", retried.structured_content["data"]["tool"]
                        )

    async def test_binder_sanitizes_adapter_exceptions(self) -> None:
        adapter = AsyncMock()
        handlers = bind_gateway_handlers(
            manifest=self.surface.manifest,
            adapters={"memory_search_text": adapter},
            listed_tools=CORE_LISTED_TOOLS,
            response_factory=ResponseFactory(),
        )
        for exception, expected in (
            (TypeError("private argument detail"), ErrorCode.INVALID_ARGUMENT),
            (RuntimeError("private runtime detail"), ErrorCode.INTERNAL_ERROR),
            (ValueError("private runtime detail"), ErrorCode.INTERNAL_ERROR),
        ):
            with self.subTest(exception=type(exception).__name__):
                adapter.side_effect = exception
                with patch("daem0nmcp.api.v7.responses._LOGGER"):
                    result = await handlers["daem0n_tool_call"](
                        **self.scope,
                        tool="memory_search_text",
                        arguments={"query": "x"},
                    )
                self.assertFalse(result.ok)
                self.assertEqual(expected, result.error.code)
                self.assertFalse(result.error.retryable)
                self.assertIsNone(result.error.remedy)
                self.assertEqual(result.meta.request_id, result.error.correlation_id)
                if expected == ErrorCode.INVALID_ARGUMENT:
                    self.assertEqual(
                        [
                            FieldError(
                                field="arguments",
                                code="INVALID_VALUE",
                                message="Invalid value.",
                            )
                        ],
                        result.error.field_errors,
                    )
                else:
                    self.assertEqual([], result.error.field_errors)
                self.assertNotIn("private", result.model_dump_json())
