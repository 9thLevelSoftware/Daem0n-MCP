"""Observe native POSIX reminder callbacks using the reviewed host harness."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import sys
from pathlib import Path

import native_posix_acceptance as posix
import native_reminder_acceptance as native
from native_acceptance_drivers import write_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--host", choices=("codex", "agy", "omp", "pi"), required=True)
    parser.add_argument("--attempt", required=True)
    args = parser.parse_args()
    if not args.attempt.replace("-", "").isalnum():
        parser.error("Use a simple bounded attempt identifier")
    parent = Path("/private/tmp" if sys.platform == "darwin" else "/var/tmp")
    native.BASE = parent / "daem0nmcp-native-acceptance-20261112/reminders05"
    native.PYTHON = Path(sys.executable)
    native.original_hashes = posix.original_hashes
    native.binary = lambda host: str(args.root / "tools/bin" / host)
    native.api_key = lambda _host, _provider: os.environ["MINIMAX_API_KEY"]
    native_account = args.host == "codex" and platform.system() != "Darwin"
    account_config = args.root / "private/codex-account/.codex/config.toml"
    agy_settings = (
        args.root / "private/agy-account/.gemini/antigravity-cli/settings.json"
    )
    restore = (
        account_config
        if native_account
        else agy_settings
        if args.host == "agy"
        else None
    )
    before = restore.read_bytes() if restore else None

    def environment(lane, host):
        env = posix.environment(args.root, host, f"reminder-{args.attempt}")
        home = Path(env["HOME"])
        env["APPDATA"] = str(home / "AppData/Roaming")
        env["LOCALAPPDATA"] = str(home / "AppData/Local")
        if native_account:
            env["CODEX_HOME"] = str(account_config.parent)
        return env

    native.environment = environment
    if args.host == "codex" and not native_account:

        def access_only(_host):
            account = json.loads((Path.home() / ".codex/auth.json").read_text())[
                "tokens"
            ]
            return {
                "access_token": account["access_token"],
                "account_id": account["account_id"],
            }

        native.existing_access_credentials = access_only
    if args.host == "pi":
        template = args.root / "reminder-templates" / args.attempt
        project = template / "projects/pi"
        lane = template / "evidence/pi"
        project.mkdir(parents=True, exist_ok=False)
        lane.mkdir(parents=True)
        posix._pi_trust(lane, project)
        shutil.copyfile(
            lane / "pi-project-trust.mjs", template / "pi-project-trust.mjs"
        )
        native.LEGACY = template
    original_prepare = native.prepare

    def prepare(host, attempt, *arguments):
        result = original_prepare(host, attempt, *arguments)
        if native_account:
            text = account_config.read_text()
            if text.count('cli_auth_credentials_store = "ephemeral"') != 1:
                raise RuntimeError(
                    "Owned account configuration differs from the reviewed harness"
                )
            account_config.write_text(
                text.replace(
                    'cli_auth_credentials_store = "ephemeral"',
                    'cli_auth_credentials_store = "file"',
                )
            )
        if host == "agy":
            native.PROMPT = native.PROMPT.replace(
                "write_to_file", "write_file"
            ).replace("replace_file_content", "edit_file")
        return result

    native.prepare = prepare
    original_turn = native.native_turn
    if native_account:
        native.native_turn = lambda *values, **kwargs: original_turn(
            *values, **kwargs, native_account=True
        )
    sys.argv = [
        sys.argv[0],
        "--host",
        args.host,
        "--attempt",
        args.attempt,
        "--backend-python",
        sys.executable,
    ]
    if args.host == "agy":
        sys.argv.append("--agy-write-edit")
    elif args.host == "codex":
        sys.argv.append("--edit-matcher")
    try:
        native.main()
    finally:
        if restore is not None:
            restore.write_bytes(before)
    lane = native.BASE / args.host / args.attempt
    summary_path = lane / "summary.json"
    summary = json.loads(summary_path.read_text())
    restoration = {
        "owned_account_config_restored": restore is None
        or restore.read_bytes() == before,
        "restored_path": str(restore) if restore else None,
        "before_sha256": hashlib.sha256(before).hexdigest() if before else None,
        "after_sha256": posix.digest(restore) if restore else None,
        "native_summary_sha256": posix.digest(summary_path),
        "backend_python": sys.executable,
        "backend_wheel_sha256": "9b6ff0e18753e2075cc14eab2eeaa4dd2435ca8be06daace00906ce183618963",
        "native_account_refresh_material_extracted": False,
    }
    write_json(lane / "posix-restoration.json", restoration)
    status = (
        "passed"
        if summary["status"] == "passed"
        and restoration["owned_account_config_restored"]
        else "failed"
    )
    print(
        json.dumps(
            {
                "host": args.host,
                "status": status,
                "evidence": str(lane),
                "restoration": restoration,
            }
        )
    )
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
