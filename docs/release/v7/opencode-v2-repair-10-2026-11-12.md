# OpenCode V2 repair 10 acceptance

The Mac and Ubuntu WSL **full typed-tool profiles passed all 16 frozen backend
checks**. Both still **failed native-host qualification** after an inventory
request was denied. These results do not establish a clean full workflow or a
pass for the default core gateway profile.

The client remained OpenCode V2 `2.2.7`, using the same authorized MiniMax
coding-plan account and `MiniMax-M3` model. The backend wheel SHA-256 was
`5de43b4a7f3eb3ceff40fe254b10a0d5200fed63d78fd855aa0436731e5e08e7`.

| Actual attempt | Tool surface | Frozen backend checks | Native-host qualification | Overall |
| --- | --- | --- | --- | --- |
| Ubuntu WSL core control | `core` | Failed: gateway search sent `limit:"100"` | Failed: resource inventory denied | Failed |
| Mac supported profile | `full` | Passed, 16/16 | Failed: resource inventory denied | Failed |
| Ubuntu WSL supported profile | `full` | Passed, 16/16 | Failed: resource inventory denied | Failed |

The [final matrix](../../../.test_tmp/native-followup-20261112/repair-10/v2/final-status-matrix.json)
retains the failed core control separately. Across the latest 18 selected cases,
backend checks now have 18 passes. Overall results remain 16 retained historical
passes and two current host-qualification failures. Historical host behavior was
not reaudited in this phase. For the three new attempts, backend results are two
passes and one failure; overall results are three failures.

## What the full-profile runs established

Each successful backend turn observed automatic briefing, exact raw counsel
retry, the original memory record/event on replay, persisted source search and
impact, absence of removed symbols, and health. Neither performed `code_index`.
Actual source calls used the direct typed `daem0nmcp_code_search` and
`daem0nmcp_code_impact_analyze` tools. Explicit numeric arguments were JSON
integers. Two WSL stale-symbol queries omitted the optional limit; no value is
invented for those arguments.

The profile was selected through the supported installer option
`--tool-surface full`. Only the two corresponding native source-read action
grants were added. Existing memory permissions, the deny-all rule, bridge
credentials and endpoints remained intact. No wildcard permission was added.
The WSL core attempt demonstrated that the compact counsel remedy fixed exact
retry there before the later numeric gateway failure.

Restart prompts were byte-identical to the prior failed attempts. The acceptance
prompts and validators were unchanged. V2 executed its frozen staged driver;
the separately reviewed repository driver change for Windows startup readiness
was not used by these V2 runs. See the
[contract audit](../../../.test_tmp/native-followup-20261112/repair-10/v2/baseline-preservation-after.json)
and [prompt/configuration comparison](../../../.test_tmp/native-followup-20261112/repair-10/v2/prompt-config-contracts.json).

## Why host qualification failed

All three turns invoked `opencode_list_mcp_resources` with `{}`. The preserved
permission policy allows that action only for resource `daem0nmcp`, after a
general deny rule. The pinned SDK supports an optional `server` argument;
omitting it requests every configured server. Its inventory includes the
preserved disabled `acceptance_sentinel`, as well as the sole enabled project
server, `daem0nmcp`. Permission evaluation rejects the entire request when any
requested server is denied.

Thus `{}` is valid SDK input that exceeds this fixture's daemon-only grant.
The grant is matchable: `{server:"daem0nmcp"}` is the supported bounded form.
The frozen prompt permits native MCP discovery generally, so this is not
described as malformed input or an established prompt violation. It remains an
observed client request-scope failure and a failed host qualification.

Installed Mac and WSL binary source confirms that permission checking occurs
before the resource catalog call. No resource-read helper ran, no resource helper
completed successfully, and no resource document was returned. The expected
`COUNSEL_REQUIRED` response is recorded separately from the denied inventory.
Exact call IDs, inputs and errors are in the
[denial evidence](../../../.test_tmp/native-followup-20261112/repair-10/v2/native-helper-denial-details.json);
the [pinned contract and policy audit](../../../.test_tmp/native-followup-20261112/repair-10/v2/host-resource-contract/corroboration-summary.json)
links the installed binary evidence and actual configuration.

The backend pass has not been converted into a clean workflow pass. No sentinel
was removed and the Daem0n-only resource grant was not broadened. No argument
was rewritten or further provider attempt made. Scope-aware discovery guidance
or a native adapter would require separate validation.

An initial audit field labelled `observed_native_errors:[]` was too broad: it
covered unexpected v7 backend errors only. That artifact remains retained. The
[qualified final execution audit](../../../.test_tmp/native-followup-20261112/repair-10/v2/typed-execution-audit-final.json)
records backend results and native-host denials separately.

## Evidence preservation

Consistent database backups remain private. Final audits verified all 269 frozen
source files, unchanged prepared configuration and credentials, no active probe
entities, and zero observed owned V2 processes on each host. Public snapshots
exclude credentials, private profiles and database backups:

- [Mac full-profile snapshot](../../../.test_tmp/native-followup-20261112/repair-10/v2/macos/evidence10-01/receipt.json)
- [WSL core control snapshot](../../../.test_tmp/native-followup-20261112/repair-10/v2/linux/evidence10-01/receipt.json)
- [WSL full-profile snapshot](../../../.test_tmp/native-followup-20261112/repair-10/v2/linux/evidence10-full-01/receipt.json)

The [final delivery verification](../../../.test_tmp/native-followup-20261112/repair-10/v2/delivery-verification.json)
records evidence hashes and local links. Earlier matrices, raw failed attempts
and [the report through freeze 09](opencode-v2-2026-11-12.md) remain identifiable.
No production or acceptance script was changed by this worker in this phase.
No commits, pushes or purchases were made.
