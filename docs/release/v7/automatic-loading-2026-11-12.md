# agy automatic covenant loading

The installed Windows Antigravity CLI 1.4.7 automatically loaded the managed
project covenant in two fresh native account-mode sessions. This closes the
earlier evidence gap caused by an explicit instruction-file read.

`scripts/native_agy_loading_acceptance.py` creates two independent Git-root
projects and private profiles. Both receive the staged installer covenant; only
the positive project receives a random 128-bit acceptance marker in `AGENTS.md`.
The identical model prompt does not name the instruction file or include the
marker. It asks for already-loaded project policy, with model-driven file,
command, network, and MCP operations denied in the private permission policy.

| Observation | Positive | Negative control |
| --- | --- | --- |
| Native model | `gemini-3.8-flash-low` | Same |
| Native result | `SUCCESS` | `SUCCESS` |
| Tool events | 0 | 0 |
| Project marker | Exact random marker | `null` |
| Auto-brief triggers | All four correct | All four correct |
| Counsel handling | Read `error.counsel`, retry exact `error.remedy` | Same |

Evidence is retained at
`.test_tmp/native-followup-20261112/agy-loading-01/`: request records, native
stream events, individual summaries, and `summary.json` with ten passing
checks. The native conversation IDs are
`2265ba5d-a720-4ac0-af40-e37e988bdb31` and
`4bb94c2d-b12b-4647-b6ba-d3c90aaf278a`. All 18 monitored original file hashes
matched before and after these runs. No authentication material was extracted
or copied; the normal shared OS keyring supplied the existing account login.
No private credit-overage setting was enabled.

The positive marker proves project instruction preloading rather than recall
from model training or MCP server instructions. The negative control still has
the same covenant, so its correct covenant explanation alone is not evidence
of the marker or project-file source. Neither run exercises a memory call,
restart persistence, indexing, or host hooks; those have separate acceptance
receipts. The backend/installer is the earlier frozen staging environment named
in each request, not a claim that every later implementation change was tested.

## Native macOS and Ubuntu WSL follow-up

Antigravity CLI **1.5.7 passed** the same positive/negative test on the Mac mini
(macOS 27.0.1 arm64) and Ubuntu WSL (Linux 6.18.33.2, x86_64). These are separate
native runs, not results inferred from Windows or CI. The fresh projects used
the installed frozen-05 covenant, wheel SHA-256
`9b6ff0e18753e2075cc14eab2eeaa4dd2435ca8be06daace00906ce183618963`.

`scripts/native_posix_agy_loading.py` uses each previously authenticated,
isolated account without copying its credentials. It temporarily denies the
declared native file, directory/search, command, network and MCP tools in that
owned account's permission settings, then restores the exact settings bytes.
The projects and conversations are fresh; the identical prompts contain
neither an instruction filename nor the random marker.

| Native lane | Positive marker | Negative marker | Tool calls | Covenant explanation | Preservation |
| --- | --- | --- | --- | --- | --- |
| macOS, agy 1.5.7 | Exact random marker | `null` | 0 in each | Four triggers and exact counsel/remedy handling | Original profiles unchanged; owned settings restored |
| Ubuntu WSL, agy 1.5.7 | Exact random marker | `null` | 0 in each | Four triggers and exact counsel/remedy handling | Original profiles unchanged; owned settings restored |

The [Mac native response and checks](../../../.test_tmp/native-followup-20261112/platforms/macos/agy-strict-native-loading-01/stdout.log)
and [Ubuntu WSL native response and checks](../../../.test_tmp/native-followup-20261112/platforms/linux/agy-strict-native-loading-01/stdout.log)
retain the actual first responses, conversation IDs and all twelve passing
checks per platform. Each native process group was observed closed. Complete
requests and raw streams remain in each test root's
`agy-loading/strict-preload-01/`; the earlier Windows evidence remains unchanged.

The final allowlisted local export also retains the raw probe artifacts:

| Native lane | Actual streams | Requests and instruction hashes |
| --- | --- | --- |
| macOS | [positive](../../../.test_tmp/native-followup-20261112/platforms/macos/retained-snapshot-02/agy-loading/strict-preload-01/positive-events.jsonl), [negative](../../../.test_tmp/native-followup-20261112/platforms/macos/retained-snapshot-02/agy-loading/strict-preload-01/negative-events.jsonl) | [positive request](../../../.test_tmp/native-followup-20261112/platforms/macos/retained-snapshot-02/agy-loading/strict-preload-01/positive-request.json), [negative request](../../../.test_tmp/native-followup-20261112/platforms/macos/retained-snapshot-02/agy-loading/strict-preload-01/negative-request.json), [instruction artifacts](../../../.test_tmp/native-followup-20261112/platforms/macos/retained-snapshot-02/agy-loading/strict-preload-01/instruction-artifacts.json) |
| Ubuntu WSL | [positive](../../../.test_tmp/native-followup-20261112/platforms/linux/retained-snapshot-02/agy-loading/strict-preload-01/positive-events.jsonl), [negative](../../../.test_tmp/native-followup-20261112/platforms/linux/retained-snapshot-02/agy-loading/strict-preload-01/negative-events.jsonl) | [positive request](../../../.test_tmp/native-followup-20261112/platforms/linux/retained-snapshot-02/agy-loading/strict-preload-01/positive-request.json), [negative request](../../../.test_tmp/native-followup-20261112/platforms/linux/retained-snapshot-02/agy-loading/strict-preload-01/negative-request.json), [instruction artifacts](../../../.test_tmp/native-followup-20261112/platforms/linux/retained-snapshot-02/agy-loading/strict-preload-01/instruction-artifacts.json) |

Instruction-file copies/hashes were captured after the zero-tool native runs
and are labeled accordingly. They do not replace the original request/stream
records. Every exported file hash was verified; account profiles and credential
files were excluded.

Normal covenant-guided memory calls and restart persistence were exercised
separately through agy on both real source snapshots. Those results belong to
the [platform acceptance report](platform-acceptance-2026-11-12.md), not this
zero-tool preloading probe.
