# P10 packaging development evidence

Run date: 2026-09-17. Checkout: `D:/Daem0n-MCP-v7-completion`, branch
`v7/completion`. All artifacts, environments, scripts, and logs are under
`.tmp/packaging-run-20260917/`.

## Artifacts

Command:

```text
.tmp/venv312/Scripts/python.exe -m build --outdir .tmp/packaging-run-20260917
```

Exit code: 0. Setuptools emitted its existing deprecation warning for the
table form of `project.license`; this did not prevent the build.

| Artifact | Size | SHA256 |
| --- | ---: | --- |
| `daem0nmcp-7.0.0.dev0-py3-none-any.whl` | 1,040,029 bytes | `D9673A5650A738D40C53EFA5F2BD0711AEBD12332F85D2B4AF6A3B2CF1658D6C` |
| `daem0nmcp-7.0.0.dev0.tar.gz` | 1,359,785 bytes | `530BEF84002703E0881DEA5C3EE0D610ECBFEC26893952A5000E25F434E6A2B8` |

The wheel contains 236 entries, 218 Python files, and 17 UI package asset
files. The package-data check found the expected UI static/template files.

## Clean core installs

Fresh environments were created with `uv venv` and installed without editable
mode using `uv pip install`:

| Artifact/interpreter | Environment | Install exit |
| --- | --- | ---: |
| wheel / Python 3.12.14 | `.tmp/packaging-run-20260917/venv-wheel312` | 0 |
| wheel / Python 3.10.21 | `.tmp/packaging-run-20260917/venv-wheel310` | 0 |
| sdist / Python 3.12.14 | `.tmp/packaging-run-20260917/venv-sdist312` | 0 |

The three `daem0nmcp --help` checks each exited 0. Installed discovery checks
also exited 0 in all three environments: 75 tools, 6 concrete resources, 4
resource templates (10 resource/template entries), and 5 UI static files.
`daem0nmcp.__file__` resolved inside each environment's `site-packages`, and
the source checkout root was absent from `sys.path`.

## Real transport workflow

`.tmp/packaging-run-20260917/transport_smoke.py` uses the existing
`tests/api_v7/process_client.py` harness with an explicit installed Python
executable. The parent script was launched from `D:/`; server subprocesses
were started with the harness's scrubbed environment and with core profile
only. Each run performed communion/briefing, denied pre-brief recall, recall,
exact preflight, memory store, outcome recording, restart, and post-restart
recall over both transports.

| Environment | stdio + streamable HTTP | Exit |
| --- | --- | ---: |
| wheel / Python 3.12 | `PASS stdio`, `PASS streamable-http` | 0 |
| wheel / Python 3.10 | `PASS stdio`, `PASS streamable-http` | 0 |
| sdist / Python 3.12 | `PASS stdio`, `PASS streamable-http` | 0 |

An earlier sdist attempt reused the wheel run's workspace and failed the
expected empty initial recall assertion. After removing only those owned
temporary workspace directories, the isolated sdist run passed both
transports; this was harness data contamination, not an install failure.

## Optional profile resolution

Each advertised extra was checked independently with the clean wheel 3.12
environment using `uv pip install --dry-run`; no optional dependencies were
globally enabled or downloaded for service execution. Every resolution exited
0:

```text
tasks local graph apps models-local models-hosted agency-e2b observability tracing
```

Resolved package details are in `.tmp/packaging-run-20260917/optional-resolution.json`
and the per-extra `resolve-*.log` files. These are dependency-resolution
results only. No external Qdrant, hosted model, E2B, OpenTelemetry collector,
or other service certification is claimed. The known Python 3.10 ONNX
`>=1.24.1` wheel support concern was not changed or bypassed.

## Remaining gates

Packaging and core protocol smoke checks pass for wheel and sdist on Python
3.10/3.12. Release certification still requires the coordinator's broader
full-suite result, optional profile runtime/service checks, and the existing
ONNX/Python 3.10 decision. No production or configuration files were changed
for this evidence collection.
