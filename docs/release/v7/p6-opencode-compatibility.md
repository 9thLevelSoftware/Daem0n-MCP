# P6 OpenCode compatibility

OpenCode `1.18.21` was installed locally. Its exact upstream tag is
[`v1.18.21` at commit `826d9ad`](https://github.com/anomalyco/opencode/tree/826d9ad46a22bef0294998e08daa3c4904fea28f).
The V1 package contract declares `tool.execute.before` and
`tool.execute.after`. In this exact release, the
[MCP wrapper](https://github.com/anomalyco/opencode/blob/826d9ad46a22bef0294998e08daa3c4904fea28f/packages/opencode/src/session/tools.ts#L390-L428)
passes the raw MCP `CallToolResult` to `tool.execute.after` before it renders
model-visible text. The adapter forwards only that host-observed object to the
trusted staging hook and reads `structuredContent`; it never parses the output
string as approval evidence.

The published `@opencode-ai/plugin@1.18.21` package does export `v2/effect` and
`v2/promise`. Their exact type declarations expose agent, model, catalog,
command, integration, reference, and skill configuration domains, but no
native or MCP tool-execution lifecycle hook. The live installer selection
therefore rejects an explicit V2 request with that capability reason and uses
V1 for native edit enforcement. It does not infer availability from the older
implementation-plan document.

The canonical V1 TypeScript plugin is shipped at
`daem0nmcp.opencode_assets/daem0n.ts`; the installer loads it with
`importlib.resources`. The installer provisions the same host credential used
by the MCP process and writes only its path into `opencode.json`. A wheel built
with the all-profile environment contained that resource.

Local acceptance used the installed OpenCode `1.18.21` binary with
`openai/gpt-5.6-sol`, whose exact native tool was `apply_patch`. The host denied
the first raw patch, surfaced the opaque remedy, observed the actual
`edit_preflight` MCP result, accepted only the identical patch retry, and then
captured the successful edit. The reviewed candidate was promoted through an
exact `memory_preflight` and was returned by ordinary `memory_recall`. The live
logs are `.tmp/p6-live-gpt-fab4c478/live-gpt-fixed.log` and
`.tmp/p6-live-gpt-fab4c478/live-gpt-promote.log`. Remote-client certification
remains open because no external authenticated HTTPS host was available.
