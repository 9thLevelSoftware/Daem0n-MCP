"""Numerical validation for Qdrant's float32 cosine representation."""

from __future__ import annotations

import hashlib
import json
import math
import struct
from collections.abc import Sequence

VECTOR_ATTESTATION_FORMAT = "qdrant-cosine-f32-le-v1"


def cosine_vectors_match(actual: object, expected: object) -> bool:
    """Compare cosine direction, allowing only float32 rounding error.

    Qdrant normalizes cosine vectors on upload and stores float32 values:
    https://qdrant.tech/documentation/manage-data/collections/
    Magnitude is therefore not part of this representation's contract. Payload,
    identity, dimension and checksums are validated separately by the caller.
    """
    if any(
        not isinstance(value, Sequence) or isinstance(value, (str, bytes))
        for value in (actual, expected)
    ):
        return False
    assert isinstance(actual, Sequence) and isinstance(expected, Sequence)
    if not actual or len(actual) != len(expected):
        return False
    normalized = []
    for vector in (actual, expected):
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for value in vector
        ):
            return False
        try:
            values = [float(value) for value in vector]
            norm = math.hypot(*values)
        except (ValueError, OverflowError):
            return False
        if not math.isfinite(norm) or norm == 0 or not all(map(math.isfinite, values)):
            return False
        normalized.append([value / norm for value in values])
    return all(
        math.isclose(left, right, rel_tol=1e-6, abs_tol=1e-7)
        for left, right in zip(*normalized, strict=True)
    )


def provider_vector_sha256(
    vector: object,
    *,
    workspace_id: str,
    provider_key: str,
    vector_space_hash: str,
    record_id: str,
    content_hash: str,
    source_event_id: str,
    dimension: int,
) -> str:
    """Hash the exact finite float32 provider representation and provenance."""

    if (
        not isinstance(vector, Sequence)
        or isinstance(vector, (str, bytes))
        or len(vector) != dimension
        or dimension < 1
    ):
        raise ValueError("provider vector is invalid")
    packed = bytearray()
    squared = 0.0
    for raw in vector:
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError("provider vector is invalid")
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError("provider vector is invalid")
        if value == 0.0:
            value = 0.0
        try:
            encoded = struct.pack("<f", value)
            rounded = struct.unpack("<f", encoded)[0]
        except (OverflowError, struct.error) as exc:
            raise ValueError("provider vector is invalid") from exc
        if not math.isfinite(rounded):
            raise ValueError("provider vector is invalid")
        squared += rounded * rounded
        packed.extend(encoded)
    if not math.isfinite(squared) or squared == 0.0:
        raise ValueError("provider vector is invalid")
    envelope = {
        "content_hash": content_hash,
        "dimension": dimension,
        "format": VECTOR_ATTESTATION_FORMAT,
        "provider_key": provider_key,
        "record_id": record_id,
        "source_event_id": source_event_id,
        "vector_space_hash": vector_space_hash,
        "workspace_id": workspace_id,
    }
    metadata = json.dumps(
        envelope, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    digest = hashlib.sha256(b"daem0nmcp-dense-vector-attestation-v1\0")
    digest.update(len(metadata).to_bytes(8, "big"))
    digest.update(metadata)
    digest.update(packed)
    return digest.hexdigest()


__all__ = [
    "VECTOR_ATTESTATION_FORMAT",
    "cosine_vectors_match",
    "provider_vector_sha256",
]
