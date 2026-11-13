"""Run the established V1 gate probes through an isolated native POSIX client."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import signal
from pathlib import Path
from types import SimpleNamespace

import native_automation_acceptance as gate
import native_posix_acceptance as posix


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--stage", required=True, choices=["edit", "missing"])
    parser.add_argument("--attempt", required=True)
    args = parser.parse_args()
    if not os.environ.get("MINIMAX_API_KEY"):
        raise RuntimeError(
            "Existing authorized MiniMax provider key required in process environment"
        )
    root = args.root.resolve()
    gate.PYTHON = root / "venv-05/bin/python"
    gate.BASE = (
        Path("/private/tmp" if posix.platform.system() == "Darwin" else "/var/tmp")
        / f"d0n-v1-gate-{os.getuid()}"
    )
    gate.binary = lambda host: str(root / "tools/bin/opencode")
    gate.original_hashes = posix.original_hashes
    gate.api_key = lambda *_args: os.environ["MINIMAX_API_KEY"]

    def environment(lane, host):
        result = posix.environment(root, host, args.attempt)
        home = Path(result["HOME"])
        result["APPDATA"] = str(home / "compat-roaming")
        result["LOCALAPPDATA"] = str(home / "compat-local")
        return result

    gate.environment = environment

    def timeout(_number, _frame):
        raise TimeoutError("Bounded native V1 automation deadline exceeded")

    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(180)
    try:
        gate.run(
            SimpleNamespace(host="opencode", stage=args.stage, attempt=args.attempt)
        )
        lane = gate.BASE / "opencode" / args.attempt
        executable = root / "tools/bin/opencode"
        (lane / "platform-provenance.json").write_text(
            json.dumps(
                {
                    "platform": platform.platform(),
                    "machine": platform.machine(),
                    "backend_python": str(gate.PYTHON),
                    "native_binary": str(executable),
                    "native_binary_sha256": hashlib.sha256(
                        executable.read_bytes()
                    ).hexdigest(),
                    "harness_sha256": hashlib.sha256(
                        Path(__file__).read_bytes()
                    ).hexdigest(),
                    "wheel_sha256": "9b6ff0e18753e2075cc14eab2eeaa4dd2435ca8be06daace00906ce183618963",
                },
                indent=2,
            )
        )
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    main()
