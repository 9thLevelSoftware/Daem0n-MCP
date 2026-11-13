"""Bind the reviewed native resource probe to isolated POSIX fixtures."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

import native_posix_acceptance as posix
import native_resource_acceptance as resources


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--host", choices=("pi", "omp"), required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--wheel-sha256", required=True)
    args = parser.parse_args()
    if not args.attempt.replace("-", "").isalnum():
        parser.error("Use a simple bounded attempt identifier")
    if re.fullmatch(r"[0-9a-f]{64}", args.wheel_sha256) is None:
        parser.error("Supply the exact reviewed installed wheel SHA256")
    key = os.environ.get("MINIMAX_API_KEY")
    if not key:
        parser.error("Supply the existing authorized provider key in process memory")
    matrix = f"resources-{args.host}-{args.attempt}"
    lane = posix.lane_path(args.root, args.host, "daem0n", matrix)

    def prepare(host, attempt):
        if (host, attempt) != (args.host, args.attempt):
            raise RuntimeError("Resource preparation scope differs from its request")
        before = posix.original_hashes()
        preparation = argparse.Namespace(
            root=args.root,
            host=host,
            source="daem0n",
            matrix=matrix,
            fixtures=args.root / "final-fixtures",
            projects_root=Path(
                "/private/tmp" if sys.platform == "darwin" else "/var/tmp"
            )
            / "daem0nmcp-native-acceptance-20261112",
            automation="remind",
        )
        posix.prepare(preparation)
        metadata = json.loads((lane / "fixture.json").read_text())
        project = Path(metadata["project"])
        env = posix.environment(args.root, host, "daem0n")
        if host == "pi":
            shutil.copyfile(lane / "pi-project-trust.mjs", lane / "project-trust.mjs")
        return lane, project, env, metadata["binary"], before

    resources.PYTHON = Path(sys.executable)
    resources.SHA = args.wheel_sha256
    resources.BASE = args.root / "resources"
    resources.original_hashes = posix.original_hashes
    resources.native.prepare = prepare
    resources.native.original_hashes = posix.original_hashes
    resources.native.api_key = lambda _host, _provider: key
    sys.argv = [sys.argv[0], "--host", args.host, "--attempt", args.attempt]
    resources.main()
    assessment = json.loads((lane / "verified-assessment.json").read_text())
    return 0 if assessment["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
