"""Server-managed grants independent of briefing, links, and edit receipts."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

from .covenant import InvocationScope
from .protected_files import verify_owner_only_directory, verify_owner_only_file
from .transport_security import _strict_json_value

_WORKSPACE_ID = re.compile(r"ws_[0-9a-f]{24}")
_MAX_POLICY_BYTES = 65_536


class WorkspaceAccessPolicy:
    """Local authority owns configured roots; remote principals need grants.

    The small policy file is reread at each authorization point so revocation
    cannot be hidden by a stale cache or a previously recorded briefing.
    """

    def __init__(
        self,
        *,
        workspaces: Mapping[str, str],
        local_principal: str,
        path: Path | None = None,
    ) -> None:
        self._workspaces = {
            os.path.normcase(str(Path(root).resolve())): identifier
            for root, identifier in workspaces.items()
        }
        self._local_principal = local_principal
        self._path = None if path is None else path.absolute()

    def __call__(self, scope: InvocationScope) -> bool:
        identifier = self._workspaces.get(scope.canonical_workspace)
        if identifier is None:
            return False
        if scope.principal_id == self._local_principal:
            return True
        if self._path is None:
            return False
        try:
            grants = self._read_grants()
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
            return False
        return identifier in grants.get(scope.principal_id, ())

    def _read_grants(self) -> dict[str, list[str]]:
        assert self._path is not None
        verify_owner_only_directory(self._path.parent)
        verify_owner_only_file(self._path, max_bytes=_MAX_POLICY_BYTES)
        metadata = self._path.stat()
        with self._path.open("rb") as stream:
            actual = os.fstat(stream.fileno())
            if (metadata.st_dev, metadata.st_ino) != (actual.st_dev, actual.st_ino):
                raise ValueError("access policy changed while opening")
            body = stream.read(_MAX_POLICY_BYTES + 1)
        payload = _strict_json_value(body, max_bytes=_MAX_POLICY_BYTES)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema_version", "grants"}
            or type(payload["schema_version"]) is not int
            or payload["schema_version"] != 1
        ):
            raise ValueError("invalid access policy schema")
        grants = payload["grants"]
        if not isinstance(grants, dict) or len(grants) > 128:
            raise ValueError("invalid access grants")
        for principal, identifiers in grants.items():
            if (
                not isinstance(principal, str)
                or not principal.startswith("oauth-sub:")
                or not 10 < len(principal) <= 512
                or any(
                    ord(character) < 32 or ord(character) == 127
                    for character in principal
                )
            ):
                raise ValueError("invalid access principal")
            if (
                not isinstance(identifiers, list)
                or len(identifiers) > 128
                or any(
                    not isinstance(item, str) or _WORKSPACE_ID.fullmatch(item) is None
                    for item in identifiers
                )
            ):
                raise ValueError("invalid workspace grants")
            if len(set(identifiers)) != len(identifiers):
                raise ValueError("duplicate workspace grant")
        return grants
