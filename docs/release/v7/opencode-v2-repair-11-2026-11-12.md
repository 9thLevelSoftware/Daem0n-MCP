# OpenCode V2 repair 11 acceptance

Both latest restart attempts **failed**. The selected matrix contains **16
retained historical passes and 2 latest failures**. All four native turns in
this round remain failed overall; the first Mac turn's 16 backend checks passed,
but its host qualification failed. Component successes are not combined into a
whole-workflow pass. [Structured matrix](../../../.test_tmp/native-followup-20261112/repair-11/v2/final-status-matrix.json)

The client remained OpenCode **2.2.7**, using the existing
`minimax-coding-plan/MiniMax-M3` account and full tool profile on native macOS and
Ubuntu WSL. Production remained freeze 11, wheel SHA-256
`0d8d9087888b09860297619d25c74cb2a47f0d8c7fa8f5cc92d872393f42c687`.
Only the two failed direct/Legion restart workflows were exercised. Passed
source, indexing, gate and resource cases were not repeated.

| Platform and attempt | Backend checks | Host / overall | Observed result |
| --- | --- | --- | --- |
| macOS, first standalone | Passed, all 16 | Failed | Scoped metadata discovery succeeded; two real unknown-tool calls remained. |
| Ubuntu WSL, first standalone | Failed | Failed | No Daem0n calls; repeated resource fallback and unknown routes. |
| macOS, second ready service | Failed, zero receipts | Failed | Intended project observer had setup only; session selected stale home `PWD`. |
| Ubuntu WSL, second ready service | Failed, zero receipts | Failed | Intended project observer had setup only; stale repository `PWD` selected a different location. |

The 16 historical passes retain their original backend versions, profiles,
evidence and host-audit limits. They were not reaudited with the new companion
validator. Both selected restart rows use the entire latest failed attempt.

## First native attempts

Retained denied requests were replayed against the pinned contract: `{}` lists
every configured server, including the disabled sentinel. The existing
Daem0n-only grant correctly denies that request; `{"server":"daem0nmcp"}`
matches the supported scoped form. Actual prior context lacked that guidance.
[Contract replay and effective instructions](../../../.test_tmp/native-followup-20261112/repair-11/v2/resource-scope-red-replay.json)

Freeze 11 refreshed both the installed covenant and actual owned V2 reminder.
The Mac first turn observed both updates, successfully listed six resource
entries and four templates with the server filter, and made no content read.
It then attempted direct MCP tools inside Code Mode twice and received actual
unknown-tool errors. A namespace inventory and supported built-in metadata
search also produced no registry inner hook; these legitimate operations are
distinguished in the diagnosis, without erasing the two real failures.

The WSL first turn attempted 40 resource reads: 15 were denied and 25 returned
`Resource unavailable`. None returned content. Guessed URIs included foreign
workspace IDs. It made no Daem0n tool call. These failures remain visible.
[Actual routing and read outcomes](../../../.test_tmp/native-followup-20261112/repair-11/v2/routing-diagnosis.json)

Both first context hooks contained only four native tools. The first
model-selected call occurred before a later context contained the 15 direct
tools. Pinned source feeds that context's tools into host model-request
construction and bounds MCP startup waiting to two seconds. This establishes
the first-turn exposure gap at the host request boundary; a provider wire body
was not captured.

## Ready-service repair and remaining location failure

An authenticated, owned loopback native service was started without a model
prompt. The public `/api/mcp` status route was polled for the exact project,
Daem0n connected and sentinel disabled. Both authenticated zero-model checks
passed with setup-only observation logs, unchanged fixtures and authority
roots. Earlier unauthenticated 401 checks remain preserved. The ephemeral
native service password stayed in memory/environment; logs mask its plaintext
and Basic-encoded form. [Zero-model evidence](../../../.test_tmp/native-followup-20261112/repair-11/v2/authenticated-zero-model-summary.json)

The second native turns used that service URL and exact owned observation
path. Both nevertheless failed: the intended observer recorded setup only,
while native stdout proved real model operations elsewhere. The wrapper's
missing-context `IndexError`, failed zero-receipt backend validation and failed
host qualification remain retained. They are not treated as successful
acceptance or as proof that no native calls occurred.

The pinned run handler selects `process.env.PWD ?? process.cwd()` as its
session directory. The harness changed subprocess working directory while its
environment builder preserved inherited `PWD`. A read-only reproduction
confirmed:

| Host | Actual subprocess working directory | Inherited `PWD` selected by the native run handler |
| --- | --- | --- |
| Ubuntu WSL | Disposable Legion project | `/mnt/d/Daem0n-MCP` |
| macOS | Disposable Legion project | `/Users/christopherwilloughby` |

This is a demonstrated harness location-binding defect. The service URL and
authentication were supported; warming one project did not bind the later
session to it. A canonical project `PWD` binding and native acceptance remain
for a new repair round. No third model attempt was made under the two-repair
limit.
[Pinned handler source](../../../.test_tmp/native-followup-20261112/repair-11/v2/run-server-routing-source.json),
[WSL reproduction](../../../.test_tmp/native-followup-20261112/repair-11/v2/linux/readonly-pwd-location-reproduction/stdout.log),
[Mac reproduction](../../../.test_tmp/native-followup-20261112/repair-11/v2/macos/readonly-pwd-location-reproduction/stdout.log)

The WSL second turn loaded a skill from the repository outside the fixture and
successfully changed native session metadata with `session_move` and
`session_rename`. Those are unplanned host writes. Its question call failed.
macOS made 17 outer `execute` calls, including unknown-tool and JavaScript
errors. No source-edit tool or successful Daem0n memory write was observed in
these stdout inventories; that statement is not a global filesystem audit or
a shell-sandbox claim.
[Actual second-turn inventory](../../../.test_tmp/native-followup-20261112/repair-11/v2/ready-attempt-failure-analysis.json)

## Preservation and verification

Explicit enforce installation initially refused stale managed-plugin ownership
hashes left by earlier controlled interpreter rebinds. Both plugin preimages
matched the immutable freeze-10 preparation and freeze-10 renderer. Approved
fixture maintenance backed up configuration and a consistent database,
retargeted only the launcher interpreter, used the supported installer to
refresh the covenant, then published the freeze-11 owned plugin and its hash.
This is guarded disposable-fixture maintenance, not a passed ordinary
explicit-enforce upgrade check. All sidecar environment, credential, runtime
override, permissions and sentinel data were preserved.

After the second turns, separate read-only checks verified both sets of 269
source files, prepared configuration, credential hashes, authority roots and
absence of owned service/client groups. The wrapper's unconditional process
audit passed on both hosts despite its parsing failure. Mac cleanup
`PermissionError` results remain qualified by later exact PID/PGID absence;
their cause is unexplained. No broad cleanup signal was sent.
[WSL final guards](../../../.test_tmp/native-followup-20261112/repair-11/v2/linux/post-ready-source-authority-process-audit/stdout.log),
[Mac final guards](../../../.test_tmp/native-followup-20261112/repair-11/v2/macos/post-ready-source-authority-process-audit/stdout.log)

The new companion checks observed native calls separately from the unchanged
16 backend predicates. Targeted red/green checks cover catalog scope and
shape, pairing, exact COUNSEL provenance, lifecycle, authentication, deadlines,
redirects, location binding and full event collection. Scoped Ruff lint and
format checks passed. Full-suite verification is reported by the coordinator.
The reviewed startup/auth/collector exceptions preserve all prompts,
validators, grants and production bytes.
[Final contract proof](../../../.test_tmp/native-followup-20261112/repair-11/final-freeze-contract-verification-03.json),
[Post-native freeze proof](../../../.test_tmp/native-followup-20261112/repair-11/post-native-freeze-contract-verification.json)

No commits, pushes, purchases or provider/account changes were made. Prior
matrices and all failed attempts remain identifiable. Public archives include
redacted native events and stdout; original service event logs remain on their
hosts and are identified by hashes. [Public evidence hashes](../../../.test_tmp/native-followup-20261112/repair-11/v2/public-evidence-sha256.json)
