"""Shared immutable contracts for specialized retrieval projections."""

from __future__ import annotations

import re
from collections.abc import Mapping

from ..event_store import sha256_json

SPECIALIZED_BUILDER_VERSION = "retrieval-specialized-1"
SPECIALIZED_PROJECTIONS = frozenset({"graph", "outcome", "procedure", "temporal"})
_WORKSPACE_ID = re.compile(r"^ws_[0-9a-f]{24}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_PROCEDURE_FTS_CONFIG = {
    "columns": ["step_text"],
    "schema_version": 1,
    "tokenizer": "unicode61 remove_diacritics 2",
}
_OUTCOME_CONFIG = {
    "schema_version": 1,
    "source": "memory_events.latest_outcome_assertion",
}
_TYPED_CONFIGS = {
    "graph": {
        "schema_version": 1,
        "source": [
            "memory_fact_versions.record_ref",
            "memory_relationship_versions",
        ],
    },
    "temporal": {
        "schema_version": 1,
        "source": "memory_fact_versions",
    },
}
PROCEDURE_FTS_BUILD_CONFIG_HASH = sha256_json(_PROCEDURE_FTS_CONFIG)
SPECIALIZED_BUILD_CONFIG_HASHES = {
    "graph": sha256_json(_TYPED_CONFIGS["graph"]),
    "outcome": sha256_json(_OUTCOME_CONFIG),
    "procedure": PROCEDURE_FTS_BUILD_CONFIG_HASH,
    "temporal": sha256_json(_TYPED_CONFIGS["temporal"]),
}


def procedure_fts_table_name(workspace_id: str, generation: int) -> str:
    """Return the exact safe procedure FTS partition identifier."""

    if (
        not isinstance(workspace_id, str)
        or _WORKSPACE_ID.fullmatch(workspace_id) is None
    ):
        raise ValueError("workspace_id must be an opaque v7 identifier")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
    ):
        raise ValueError("generation must be a positive integer")
    return f"retrieval_procedure_fts_{workspace_id[3:]}_g{generation}"


def specialized_builder_contract_hash(
    projection_name: str,
    build_config_hash: str,
    *,
    builder_version: str = SPECIALIZED_BUILDER_VERSION,
) -> str:
    if projection_name not in SPECIALIZED_PROJECTIONS:
        raise ValueError("specialized projection name is invalid")
    if (
        not isinstance(build_config_hash, str)
        or _HASH.fullmatch(build_config_hash) is None
    ):
        raise ValueError("build_config_hash must be a SHA-256 digest")
    if (
        not isinstance(builder_version, str)
        or not builder_version
        or builder_version != builder_version.strip()
    ):
        raise ValueError("builder_version must be non-empty")
    return sha256_json(
        {
            "build_config_hash": build_config_hash,
            "builder_version": builder_version,
            "projection": projection_name,
        }
    )


def specialized_projection_contract(
    workspace_id: str,
    projection_name: str,
    generation: int,
    content_digest: str,
    *,
    builder_version: str = SPECIALIZED_BUILDER_VERSION,
) -> tuple[str, str, dict[str, object]]:
    """Return the exact builder/runtime contract for one generation."""

    if projection_name not in SPECIALIZED_PROJECTIONS:
        raise ValueError("specialized projection name is invalid")
    if not isinstance(content_digest, str) or _HASH.fullmatch(content_digest) is None:
        raise ValueError("content_digest must be a SHA-256 digest")
    if projection_name == "procedure":
        storage_target = procedure_fts_table_name(workspace_id, generation)
        details: dict[str, object] = {
            "build_config_hash": PROCEDURE_FTS_BUILD_CONFIG_HASH,
            "content_digest": content_digest,
            "fts_table": storage_target,
            "projection": "procedure",
            "schema_version": 1,
        }
    elif projection_name == "outcome":
        storage_target = "record_outcome_view"
        details = {
            **_OUTCOME_CONFIG,
            "build_config_hash": SPECIALIZED_BUILD_CONFIG_HASHES["outcome"],
            "content_digest": content_digest,
            "projection": "outcome",
        }
    else:
        configuration = _TYPED_CONFIGS[projection_name]
        storage_target = (
            "memory_relationship_versions+memory_fact_versions.record_ref"
            if projection_name == "graph"
            else "memory_fact_versions"
        )
        details = {
            **configuration,
            "build_config_hash": SPECIALIZED_BUILD_CONFIG_HASHES[projection_name],
            "content_digest": content_digest,
            "projection": projection_name,
        }
    build_config_hash = SPECIALIZED_BUILD_CONFIG_HASHES[projection_name]
    details["builder_contract_hash"] = specialized_builder_contract_hash(
        projection_name,
        build_config_hash,
        builder_version=builder_version,
    )
    return storage_target, build_config_hash, details


def specialized_manifest_matches_contract(
    details: object,
    workspace_id: str,
    projection_name: str,
    generation: int,
) -> bool:
    """Fail closed unless manifest details exactly match the runtime contract."""

    if not isinstance(details, Mapping):
        return False
    content_digest = details.get("content_digest")
    if not isinstance(content_digest, str) or _HASH.fullmatch(content_digest) is None:
        return False
    try:
        expected = specialized_projection_contract(
            workspace_id,
            projection_name,
            generation,
            content_digest,
        )[2]
    except ValueError:
        return False
    return dict(details) == expected


__all__ = [
    "PROCEDURE_FTS_BUILD_CONFIG_HASH",
    "SPECIALIZED_BUILDER_VERSION",
    "SPECIALIZED_BUILD_CONFIG_HASHES",
    "SPECIALIZED_PROJECTIONS",
    "procedure_fts_table_name",
    "specialized_builder_contract_hash",
    "specialized_manifest_matches_contract",
    "specialized_projection_contract",
]
