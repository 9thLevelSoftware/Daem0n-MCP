# Optional interface acceptance — 2026-11-12

These lanes are independent of core memory and code-indexing acceptance. No
account was created, service purchased, or provider credential substituted.

| Lane | Result | Observed boundary |
| --- | --- | --- |
| MCP Apps in MCPJam Inspector 3.21.0 | **Blocked** | Local host starts and accepts its private browser-session token, then redirects to MCPJam account login. The user confirmed they have no existing account. |
| Hosted embeddings | **Unsupported** | There is no hosted embedding provider adapter in this implementation. |
| Advertised hosted-model token counting | **Passed** | Installed `tiktoken` 0.14.0 encodes and decodes the fixed acceptance text with `cl100k_base` (9 tokens). This is not an embedding-service call. |
| Live E2B execution | **Blocked** | No existing `E2B_API_KEY` in the inspected Windows Process/User/Machine or native Mac/WSL process environments. No sandbox was created. |
| E2B disabled-capability response | **Passed** | After following the actual covenant remedy, the server returns `CAPABILITY_DISABLED` with `E2B_API_KEY_MISSING`. |

## MCP Apps

The pinned official npm host was installed privately under
`.test_tmp/native-followup-20261112/mcpjam`. An independent disposable workspace
was populated with two linked memory records and a rebuilt community. Tool
responses and resource discovery were retained before opening the inspector.
Local computer and browser-control features were disabled for this test host;
its server was bound to loopback. Both the ordinary Chrome session and a fresh
in-app browser reached the same account sign-in boundary. Neither populated
dashboard rendering nor dashboard actions were reached, so **no shell has a
rendering pass** from this run. The owned inspector processes were stopped after
recording the blocker.

Private evidence in `.test_tmp/native-followup-20261112/apps/`:

- `inspector-launch.json`, `inspector.log`: pinned native host launch.
- `mcpjam-login-blocked.png`, `rendering-result.json`: observed UI boundary and
  user-confirmed absence of an existing account.
- `scenario.json`, `seed-tool-responses.jsonl`, `seed-resource-inventory.json`:
  prepared data and protocol evidence, not UI rendering evidence.

This preliminary protocol setup used the workspace Python runtime before the
final implementation freeze. It does not qualify the final native matrix.

## Embeddings and E2B

`optional-capabilities.json` records the token-counting result and explicit
unsupported/blocked states. `e2b-optional-initial.json` retains the initial
covenant challenge; `e2b-optional-disabled.json` is the actual final disabled
response after the exact remedy retry. An earlier preliminary response named
`e2b-disabled-response.json` contains a covenant challenge and must not be used
as evidence of capability gating.

Live E2B isolation, network policy, timeout, output limits, cleanup and server
responsiveness remain unexecuted because access is absent. A disabled-capability
response does not establish those behaviors. Native MCP resource browsing is
reported separately by the clients that expose it.
