#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
export PYTHONHASHSEED=0
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export HF_DATASETS_OFFLINE=1
if [[ -x .venv/Scripts/python.exe ]]; then
    python=.venv/Scripts/python.exe
elif [[ -x .venv/bin/python ]]; then
    python=.venv/bin/python
else
    printf '%s\n' 'A project virtual environment is required.' >&2
    exit 1
fi
exec "$python" -m benchmarks.autoresearch_benchmark
