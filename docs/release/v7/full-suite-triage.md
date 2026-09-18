# v7 legacy full-suite triage

Bounded diagnostic run on 2026-09-17 from `D:/Daem0n-MCP-v7-completion`,
Python 3.12 (`.tmp/venv312/Scripts/python.exe`), with the repository's normal
environment. Command:

```text
python -m pytest tests/test_linked_projects.py tests/test_context.py tests/test_context_triggers.py tests/test_communities.py tests/test_graphrag.py tests/test_contradiction.py tests/test_verification.py tests/test_execute_python.py tests/test_vectors.py --timeout=120 -q
```

The combined run collected 170 tests and exited 1: 57 failed, 108 passed,
5 skipped (30.93s). The failures are clustered as follows; no test or
production code was changed.

| Cluster | Normal run | Focused evidence | Diagnosis and minimal recommendation |
| --- | --- | --- | --- |
| `test_linked_projects.py` | 13 failed, 4 passed (exit 1; 13.74s) | `WorkspaceAccessError: UNAUTHORIZED_WORKSPACE` for direct link-manager/tool calls; `ArgumentNormalizationError: path argument must remain inside the authorized workspace`; Windows `Path.relative_to` shows `D:\repos\client` outside `.test_tmp`; one briefing assertion finds neither `localStorage` nor `HttpOnly` in the returned payload | The tests use synthetic/unregistered roots (`/repos/*`) and cross-root paths while v7 now fails closed on the registered workspace. Update the legacy fixture/calls to install/register both roots and use paths below that root. Revisit the single auth-token briefing assertion against the v7 resource contract; do not relax workspace containment. |
| `test_context.py` | 4 failed, 2 passed (exit 1; 2.09s) | All failures are `UNAUTHORIZED_WORKSPACE: workspace selector is not registered` from `get_project_context` | Stale legacy fixture assumption: `tempfile.mkdtemp()` roots are not registered. Register/install the temporary root for each call (including the Windows path-resolution case); retain the authorization check. |
| `test_context_triggers.py` | 1 failed, 24 passed (exit 1; 13.76s) | `AttributeError: daem0nmcp.server has no attribute get_triggered_context_resource` at `tests/test_context_triggers.py:604`; implementation remains in `daem0nmcp.tools.resources:267` | v6 Python compatibility export is missing from the v7 lazy-export map. Add the legacy `server` re-export/adapter for `get_triggered_context_resource`, preserving the existing guarded implementation. |
| `test_communities.py` | 5 failed, 4 passed (exit 1; 8.81s) | Every failure is `CapabilityUnavailableError: Capability 'graph' is disabled`; the focused run with `DAEM0NMCP_GRAPH_ENABLED=true` passed 30/30 across communities + GraphRAG (exit 0; 19.27s) | Configuration-only optional profile failure. Release/CI legacy graph coverage must enable the graph capability for this cluster; do not globally enable all profiles or remove the capability gate. |
| `test_graphrag.py` | 17 failed, 4 passed (exit 1; 17.24s) | Same disabled `graph` capability; graph-enabled focused run passes all 21 GraphRAG tests | Configuration-only optional profile failure. Run with graph enabled in the profile-specific job. |
| `test_contradiction.py` | 7 failed, 26 passed (exit 1; 5.52s) | Normal run: disabled `models-local`. Models-enabled rerun: 6 failures with `KeyError: 'last_hidden_state'` in installed `optimum.onnxruntime.modeling.py:729`, plus `V7_MEMORY_STREAM_MISSING` in `daem0nmcp/graph/temporal.py:211` | First layer is optional capability configuration. After enabling it, the local ONNX model/output contract is incompatible with Optimum's token-output adapter. Add/fix the pooled-output adapter/model artifact in the model integration owner; separately provide the v7 memory-stream fixture/context for invalidation. Do not make contradiction checks silently pass when the model is unavailable. |
| `test_verification.py` | 5 failed, 14 passed (exit 1; 1.30s) | Normal run: disabled `models-local`. Models-enabled rerun: all five remain `KeyError: 'last_hidden_state'` from the same Optimum adapter path | Optional capability plus model adapter/artifact mismatch. Repair the ONNX output handling or pin the compatible artifact in the model owner; preserve verification assertions. |
| `test_execute_python.py` | 3 failed, 17 passed, 5 skipped (exit 1; 5.99s) | `server._capability_manager` and `server._sandbox_executor` raise `AttributeError`; implementations are `daem0nmcp.tools.agency_tools:49` and `:50` | Stale v6 private-module expectations. Add narrowly scoped lazy compatibility exports (or migrate tests/callers to the agency-tools owner) while keeping sandbox/capability behavior unchanged. The five skips are expected unavailable-sandbox integration tests. |
| `test_vectors.py` | 2 failed, 13 passed (exit 1; 0.25s) | Normal run: `vectors.encode` raises disabled `models-local`; `cosine_similarity([1,2,3],[1,2,3])` returns `1.0` although the old test expects `0.0`. Models-enabled rerun joins the same ONNX `last_hidden_state` failure cluster | The encode failure is optional configuration; the cosine expectation is stale (NumPy is available, so the function correctly computes 1.0). Update the legacy test expectation/branch and repair the shared model adapter; do not force a false zero. |

Focused commands used the same 120-second per-test timeout. The graph rerun
was limited to the graph cluster; the model rerun was limited to
contradiction/verification/vectors. Existing v7 schema-version failures and
active-worker-owned API process tests were not rerun or classified here.
