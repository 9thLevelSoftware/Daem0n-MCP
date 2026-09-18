# P10 Linux packaging development evidence

Run date: 2026-09-17. Docker Desktop Linux engine was available (Docker
29.7.2, engine linux/amd64). Existing images were inspected without removal or
prune. The tested wheel is the earlier development artifact from
`.tmp/packaging-run-20260917/` with SHA256
`D9673A5650A738D40C53EFA5F2BD0711AEBD12332F85D2B4AF6A3B2CF1658D6C`.

Images pulled successfully (exit 0):

| Python | Image digest |
| --- | --- |
| 3.10 | `python@sha256:fd76ade0c607f27677bc04be3c60749f400eedc941d9e72967e19a4cedff80c2` |
| 3.11 | `python@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534` |
| 3.12 | `python@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea` |

Each container mounted the wheel read only and installed it into a fresh
container-local virtual environment. Unique container names were used:
`daem0n-v7-linux-310-r5`, `daem0n-v7-linux-311-r5`, and
`daem0n-v7-linux-312-r5`. All final container commands exited 0.

The self-contained `.tmp/linux-certification/transport_smoke.py` harness ran
outside the repository source as the server's module source. It initialized a
fresh v7 storage database per transport, then exercised denied pre-brief
recall, briefing, recall, exact preflight, memory store, outcome recording,
and recall within the same server session over stdio and streamable HTTP.
The original harness did not restart the process; no Linux restart evidence
is claimed by these runs. Results:

| Container | Install/import | Protocol ritual |
| --- | ---: | --- |
| Python 3.10 | 0 | `PASS stdio`, `PASS streamable-http` |
| Python 3.11 | 0 | `PASS stdio`, `PASS streamable-http` |
| Python 3.12 | 0 | `PASS stdio`, `PASS streamable-http` |

The installed module printed from `/opt/venv/lib/python3.x/site-packages/daem0nmcp/__init__.py`.
The container harness imported only installed package modules; no source server
module was imported from the checkout. Core profile was used and no optional
model/service profile was run.

The first r2 attempt failed because the harness mounted the wheel under the
invalid filename `pkg.whl` (pip rejected it), and the corrected r2 harness
then reached the installed server but lacked database initialization and reused
one workspace across transports. The workspace reuse was a harness error.
The missing initialization exposed the known fresh-start production defect in
this pre-bootstrap wheel; manually initializing the database limits this check
to an established-storage workflow. After initializing storage and isolating
transport workspaces, the r5 runs passed on all three Python versions. Logs and result records are under
`.tmp/linux-certification/`; `linux-results-r5.json` records the completed
matrix.

This is development feasibility evidence for the current wheel snapshot, not
a release certification. A new wheel must include the bootstrap and subsequent
repairs and pass without harness-created storage. Optional profiles, external
services, ONNX model behavior, and the broader full suite remain separate gates.
