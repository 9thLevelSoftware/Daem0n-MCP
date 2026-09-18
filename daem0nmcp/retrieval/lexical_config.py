"""Shared immutable configuration for lexical projection partitions."""

from __future__ import annotations

import hashlib
import json
import re

LEXICAL_BM25_WEIGHTS = (1.0, 0.7, 1.5)
LEXICAL_TOKENIZER = "unicode61 remove_diacritics 2"
LEXICAL_BUILD_CONFIG = {
    "columns": {
        "content": LEXICAL_BM25_WEIGHTS[0],
        "rationale": LEXICAL_BM25_WEIGHTS[1],
        "tags_text": LEXICAL_BM25_WEIGHTS[2],
    },
    "projection_version": 1,
    "tokenizer": LEXICAL_TOKENIZER,
}

_WORKSPACE_ID = re.compile(r"^ws_[0-9a-f]{24}$")
_FTS_PARTITION = re.compile(r"^retrieval_fts_[0-9a-f]{24}_g[1-9][0-9]*$")


def lexical_build_config_hash() -> str:
    encoded = json.dumps(
        LEXICAL_BUILD_CONFIG,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def lexical_fts_table_name(workspace_id: str, generation: int) -> str:
    if (
        not isinstance(workspace_id, str)
        or _WORKSPACE_ID.fullmatch(workspace_id) is None
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
    ):
        raise ValueError("invalid lexical FTS partition coordinates")
    table_name = f"retrieval_fts_{workspace_id[3:]}_g{generation}"
    if _FTS_PARTITION.fullmatch(table_name) is None:
        raise ValueError("invalid lexical FTS partition identifier")
    return table_name


__all__ = [
    "LEXICAL_BM25_WEIGHTS",
    "LEXICAL_BUILD_CONFIG",
    "LEXICAL_TOKENIZER",
    "lexical_build_config_hash",
    "lexical_fts_table_name",
]
