"""Exercise native Codex same-session changed-content resource reads."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from native_acceptance_drivers import run_codex, write_json
from native_windows_acceptance import (
    environment,
    existing_access_credentials,
    original_hashes,
    runtime_provenance,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lane", type=Path, required=True)
    parser.add_argument("--attempt", default="resource-change-1")
    args = parser.parse_args()
    metadata = json.loads((args.lane / "fixture.json").read_text(encoding="utf-8"))
    if metadata["host"] != "codex":
        raise ValueError("This probe requires an independently prepared Codex fixture")
    evidence = args.lane / "evidence" / args.attempt
    env = environment(args.lane, "codex")
    before = original_hashes()
    runtime_provenance(metadata["backend_python"], args.lane, env, args.attempt)
    warning = {
        "workspace_id": metadata["workspace_id"],
        "record_type": "warning",
        "content": "Synthetic native Codex resource refresh sentinel 20261112; acceptance data only",
        "rationale": "Prove exact warning record appears on same native URI after a real MCP write",
        "idempotency_key": "native-resource-change-codex-20261112-warning-01",
    }
    credentials = existing_access_credentials("codex")
    result = run_codex(
        argv=[metadata["binary"], "app-server"],
        cwd=Path(metadata["project"]),
        env=env,
        workspace_id=metadata["workspace_id"],
        manifest=metadata["manifest"],
        evidence=evidence,
        stage="resource-change",
        access_token=credentials["access_token"],
        account_id=credentials["account_id"],
        resource_delta={"store": warning},
        timeout=300,
    )
    after = original_hashes()
    write_json(
        evidence / "preservation.json",
        {"before": before, "after": after, "original_files_unchanged": before == after},
    )
    print(json.dumps(result["validation"], indent=2))
    return 0 if result["validation"]["status"] == "passed" and before == after else 1


if __name__ == "__main__":
    raise SystemExit(main())
