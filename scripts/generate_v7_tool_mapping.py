#!/usr/bin/env python3
"""Generate or verify the canonical v6-to-v7 tool mapping document."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from daem0nmcp.api.v7.mapping import (  # noqa: E402
    MappingCoverageError,
    render_mapping_json,
    validate_mapping,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate the canonical Daem0nMCP v6-to-v7 tool mapping."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_ROOT / "docs" / "v6-to-v7-tools.json",
        help="mapping output path",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail instead of writing when the output is missing or stale",
    )
    return parser


def _write_if_changed(path: Path, expected: bytes) -> None:
    if path.is_file() and path.read_bytes() == expected:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(expected)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        validate_mapping()
        expected = render_mapping_json().encode("utf-8")
    except MappingCoverageError as exc:
        print(f"mapping validation failed: {exc}", file=sys.stderr)
        return 2

    output = args.output.resolve()
    if args.check:
        if not output.is_file() or output.read_bytes() != expected:
            print(f"generated mapping is out of date: {output}", file=sys.stderr)
            return 1
        return 0

    _write_if_changed(output, expected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
