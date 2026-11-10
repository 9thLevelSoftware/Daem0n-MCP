#!/usr/bin/env python3
"""Deprecated manual Claude Code hook; kept so old settings entries exit 0."""

import sys

print(
    "Daem0n: this manual hook is deprecated; "
    "run python -m daem0nmcp.cli install-claude-hooks",
    file=sys.stderr,
)
sys.exit(0)
