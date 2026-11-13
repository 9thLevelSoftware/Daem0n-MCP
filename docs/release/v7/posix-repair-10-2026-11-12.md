# POSIX native repair 10

This is a targeted follow-up, not a rerun of the full native matrix. Historical
attempts and qualifications remain unchanged. The [structured results](../../../.test_tmp/native-followup-20261112/repair-10/posix/results.json)
contain the exact selected summaries, hashes, native operations, checks,
configuration profiles, backend provenance and cleanup observations.

| Platform | Client / source | Check | Strict result |
|---|---|---|---|
| macOS | OMP / Python | Restart | Passed |
| macOS | OMP / TypeScript | Restart | Passed |
| Ubuntu WSL | OMP / Python | Restart | Passed |
| Ubuntu WSL | OMP / TypeScript | Restart | Passed |
| macOS | OpenCode V1 / TypeScript | Necessary index setup | **Failed: extra scoped health call** |
| macOS | OpenCode V1 / TypeScript | Removal | Passed |
| macOS | OpenCode V1 / TypeScript | Restart | Passed |

All four OMP restarts actually called `memory_recall` first and received
`meta.covenant.auto_brief`. Original record/event replay, persisted source
symbols/callers, stale-result absence and the no-index restart requirement
passed. These native calls used the original driver and unchanged acceptance
prompt/validator; they do not depend on the separately diagnosed Windows OMP
startup-readiness correction.

Mac V1's setup made three actual calls: discovery, scoped `system_health`, then
the direct typed `code_index`. Its index component successfully indexed the
owned added source: 270 files at generation 6. The extra health call violated
the setup's bounded operation list, so its strict failure remains an additional
required qualification. The coordinator continued the already-authorized
removal against that proven native index component without repeating setup or
relabelling it. Removal indexed the restored 269-file source at generation 7;
all strict removal checks passed. The following restart passed without an index
call. **The overall Mac V1 workflow remains conservatively failed because the
required setup qualification failed**, while removal/restart are resolved.

Both hosts used fresh isolated Python environments and frozen wheel SHA256
`5de43b4a7f3eb3ceff40fe254b10a0d5200fed63d78fd855aa0436731e5e08e7`.
All 246 installed package files matched the wheel, with no unexpected package
files. Native versions/models were unchanged: OMP 18.16.0 / MiniMax-M2.7 and
OpenCode V1 1.18.21 / MiniMax-M3, using the existing provider accounts.

OMP remained `core/remind`. Its owned Covenant and native JavaScript reminder
were refreshed through the supported installer. Mac V1 explicitly used
`full/enforce`; direct typed tools were actually exercised. Its earlier bridge
credential, runtime path, sidecar and enforcement plugin remained unchanged.
No new edit-enforcement probe was run in this follow-up. Consistent SQLite
backups and source/configuration/storage-authority guards preceded retargeting;
logical database contents remained unchanged during installation. Final guards
verified restored frozen sources, absent added probes, unchanged reviewed
configuration, workspace IDs and storage authority. All seven exact primary
native groups were empty in the final read-only audit. Neither host retains an
active primary model call, and both provider leases were released to V2.

Public raw events, receipts, approval decisions, effective instructions,
configuration snapshots and summaries are retained separately:

- [macOS: 134 files](../../../.test_tmp/native-followup-20261112/repair-10/posix/macos/retained-public-01/EXPORT-MANIFEST.json), archive SHA256 `f64972038278456693f0770ad6a8a45fa74b3155e00767c5983c3ed34e4264c4`.
- [Ubuntu WSL: 93 files](../../../.test_tmp/native-followup-20261112/repair-10/posix/linux/retained-public-01/EXPORT-MANIFEST.json), archive SHA256 `562b5c016df931757c0efb351084099386c15ce915215d591560d005cfc86b5c`.

Every exported file hash was verified and selected credential-literal scans
found zero matches. Databases, authentication profiles and bridge credential
files were excluded. The narrow prior-09 interpreter allowlist addition was
checked separately: exact owned-root acceptance and foreign-root refusal both
passed; [real output](../../../.test_tmp/native-followup-20261112/repair-10/posix/prior09-allowlist.log)
and [JUnit](../../../.test_tmp/native-followup-20261112/repair-10/posix/prior09-allowlist.xml)
are retained. External access/quota blockers were not retried.
