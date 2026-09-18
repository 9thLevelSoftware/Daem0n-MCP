from __future__ import annotations

import base64
import unittest

from daem0nmcp.covenant import CapabilityAuthority, InvocationScope


class OpaqueCapabilityAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 1_000
        self.delegate = CapabilityAuthority(
            secret=b"o" * 32,
            kid="opaque-test",
            clock=lambda: self.now,
        )
        self.scope = InvocationScope(
            "oauth-sub:private-user",
            "mcp-session:private-session",
            "tests/private-workspace",
        )

    def test_handle_contains_no_reversible_scope_claims_and_verifies(self) -> None:
        from daem0nmcp.api.v7.opaque_capabilities import (
            OpaqueCapabilityAuthority,
        )

        authority = OpaqueCapabilityAuthority(
            self.delegate,
            token_factory=lambda: "cap_public_handle_0001",
            clock=lambda: self.now,
        )
        issued = authority.issue(self.scope, "memory_store", "a" * 64)

        self.assertEqual("cap_public_handle_0001", issued.token)
        self.assertNotIn(".", issued.token)
        encoded = (
            base64.urlsafe_b64encode(self.scope.canonical_workspace.encode("utf-8"))
            .rstrip(b"=")
            .decode("ascii")
        )
        self.assertNotIn(encoded, issued.token)
        self.assertNotIn("private", issued.token)
        claims = authority.verify(issued.token)
        self.assertEqual(self.scope.canonical_workspace, claims["workspace"])
        self.assertEqual("memory_store", claims["operation"])
        self.assertEqual("a" * 64, claims["args_sha256"])

    def test_live_handles_are_never_evicted_under_capacity_pressure(self) -> None:
        from daem0nmcp.api.v7.opaque_capabilities import (
            OpaqueCapabilityAuthority,
            OpaqueCapabilityCapacityError,
        )
        from daem0nmcp.covenant import TokenValidationError

        handles = iter(("cap_public_handle_0001", "cap_public_handle_0002"))
        authority = OpaqueCapabilityAuthority(
            self.delegate,
            token_factory=lambda: next(handles),
            max_handles=1,
            clock=lambda: self.now,
        )
        first = authority.issue(self.scope, "memory_store", "a" * 64)

        with self.assertRaises(OpaqueCapabilityCapacityError):
            authority.issue(self.scope, "memory_store", "b" * 64)
        self.assertEqual("a" * 64, authority.verify(first.token)["args_sha256"])

        self.now += self.delegate.ttl_seconds
        second = authority.issue(self.scope, "memory_store", "b" * 64)
        self.assertEqual("b" * 64, authority.verify(second.token)["args_sha256"])
        with self.assertRaisesRegex(TokenValidationError, "TOKEN_TAMPERED"):
            authority.verify("cap_unknown_handle_0000")

        self.now += self.delegate.ttl_seconds
        with self.assertRaisesRegex(TokenValidationError, "TOKEN_EXPIRED"):
            authority.verify(second.token)

    def test_handle_factory_is_strict_and_collision_never_rebinds(self) -> None:
        from daem0nmcp.api.v7.opaque_capabilities import (
            OpaqueCapabilityAuthority,
        )

        authority = OpaqueCapabilityAuthority(
            self.delegate,
            token_factory=lambda: "not valid whitespace",
            clock=lambda: self.now,
        )
        with self.assertRaises(ValueError):
            authority.issue(self.scope, "memory_store", "a" * 64)

        authority = OpaqueCapabilityAuthority(
            self.delegate,
            token_factory=lambda: "cap_public_handle_0001",
            clock=lambda: self.now,
        )
        authority.issue(self.scope, "memory_store", "a" * 64)
        with self.assertRaises(RuntimeError):
            authority.issue(self.scope, "memory_store", "b" * 64)

    def test_legacy_json_capability_keeps_stable_unsupported_error(self) -> None:
        from daem0nmcp.api.v7.opaque_capabilities import (
            OpaqueCapabilityAuthority,
        )
        from daem0nmcp.covenant import TokenValidationError

        authority = OpaqueCapabilityAuthority(self.delegate)

        with self.assertRaises(TokenValidationError) as raised:
            authority.verify('{"operation":"memory_store","token":"legacy"}')

        self.assertEqual("TOKEN_LEGACY_UNSUPPORTED", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
