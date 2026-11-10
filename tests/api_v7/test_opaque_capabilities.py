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

    def test_consumed_handle_frees_capacity_and_keeps_its_replay_code(self) -> None:
        from daem0nmcp.api.v7.opaque_capabilities import (
            OpaqueCapabilityAuthority,
        )
        from daem0nmcp.covenant import TokenValidationError

        authority = OpaqueCapabilityAuthority(
            self.delegate, max_handles=1, clock=lambda: self.now
        )
        spent = []
        for index in range(5):
            issued = authority.issue(self.scope, "memory_store", f"{index:064x}")
            authority.verify(issued.token)
            authority.release_consumed(issued.token)
            spent.append(issued.token)
        # No signed claims are retained for a spent capability.
        self.assertEqual({}, dict(authority._handles))
        for token in spent:
            with self.assertRaises(TokenValidationError) as raised:
                authority.verify(token)
            self.assertEqual("TOKEN_REPLAYED", raised.exception.code)
        authority.release_consumed(spent[0])
        authority.release_consumed("cap_unknown_handle_0000")

        self.now += self.delegate.ttl_seconds
        authority.issue(self.scope, "memory_store", "a" * 64)
        self.assertEqual({}, authority._consumed)
        self.assertEqual({}, authority._session_consumed)
        with self.assertRaises(TokenValidationError) as raised:
            authority.verify(spent[0])
        self.assertEqual("TOKEN_TAMPERED", raised.exception.code)

    def test_replay_markers_are_bounded_per_session(self) -> None:
        from daem0nmcp.api.v7.opaque_capabilities import (
            OpaqueCapabilityAuthority,
        )
        from daem0nmcp.covenant import TokenValidationError

        authority = OpaqueCapabilityAuthority(
            self.delegate, max_handles_per_session=2, clock=lambda: self.now
        )
        other = InvocationScope("oauth-sub:other", "mcp-session:other", "tests/w")
        kept = authority.issue(other, "memory_store", "c" * 64)
        authority.release_consumed(kept.token)
        spent = []
        for index in range(4):
            issued = authority.issue(self.scope, "memory_store", f"{index:064x}")
            authority.release_consumed(issued.token)
            spent.append(issued.token)

        self.assertEqual(3, len(authority._consumed))
        codes = []
        for token in [*spent, kept.token]:
            with self.assertRaises(TokenValidationError) as raised:
                authority.verify(token)
            codes.append(raised.exception.code)
        self.assertEqual(
            [
                "TOKEN_TAMPERED",
                "TOKEN_TAMPERED",
                "TOKEN_REPLAYED",
                "TOKEN_REPLAYED",
                "TOKEN_REPLAYED",
            ],
            codes,
        )

    def test_quotas_refuse_the_caller_without_evicting_anyone(self) -> None:
        from daem0nmcp.api.v7.opaque_capabilities import (
            OpaqueCapabilityAuthority,
            OpaqueCapabilityCapacityError,
        )

        authority = OpaqueCapabilityAuthority(
            self.delegate,
            max_handles=4,
            max_handles_per_principal=3,
            max_handles_per_session=2,
            clock=lambda: self.now,
        )

        def scope(principal: str, session: str) -> InvocationScope:
            return InvocationScope(principal, session, "tests/private-workspace")

        live = [
            authority.issue(scope("p1", "s1"), "memory_store", "1" * 64),
            authority.issue(scope("p1", "s1"), "memory_store", "2" * 64),
        ]
        with self.assertRaisesRegex(OpaqueCapabilityCapacityError, "session"):
            authority.issue(scope("p1", "s1"), "memory_store", "3" * 64)
        live.append(authority.issue(scope("p1", "s2"), "memory_store", "4" * 64))
        with self.assertRaisesRegex(OpaqueCapabilityCapacityError, "principal"):
            authority.issue(scope("p1", "s3"), "memory_store", "5" * 64)
        # The same session name under another principal is a separate share.
        live.append(authority.issue(scope("p2", "s1"), "memory_store", "6" * 64))
        with self.assertRaises(OpaqueCapabilityCapacityError):
            authority.issue(scope("p3", "s1"), "memory_store", "7" * 64)
        for issued in live:
            self.assertEqual(
                issued.args_sha256, authority.verify(issued.token)["args_sha256"]
            )

        authority.discard(live[0].token)
        authority.release_consumed(live[3].token)
        authority.issue(scope("p1", "s1"), "memory_store", "8" * 64)
        authority.issue(scope("p3", "s1"), "memory_store", "9" * 64)
        for name in (
            "max_handles",
            "max_handles_per_principal",
            "max_handles_per_session",
        ):
            for invalid in (0, True, 1_000_001, "4"):
                with self.assertRaises(ValueError):
                    OpaqueCapabilityAuthority(self.delegate, **{name: invalid})

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
